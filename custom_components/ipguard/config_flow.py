"""Explicit phone/email verification, installation consent, and reauthentication."""

from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    AuthenticationError,
    AuthIdentifier,
    ExistingInstallationError,
    InvalidCodeError,
    InvalidInputError,
    IpguardClient,
    IpguardError,
    RateLimitError,
    Session,
    SessionStatus,
    VerificationExpiredError,
    validate_identifier,
    validate_installation_label,
    validate_verification_code,
)
from .const import (
    CONF_IDENTIFIER,
    CONF_INSTALLATION_NAME,
    CONF_REPLACE_EXISTING,
    CONF_REQUEST_NEW_CODE,
    CONF_SESSION,
    CONF_VERIFICATION_CODE,
    DOMAIN,
)

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_IDENTIFIER): TextSelector(),
        probatio.Required(CONF_INSTALLATION_NAME): TextSelector(),
    }
)
VERIFICATION_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_VERIFICATION_CODE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        probatio.Optional(CONF_REQUEST_NEW_CODE, default=False): BooleanSelector(),
    }
)
REPLACEMENT_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_REPLACE_EXISTING, default=False): BooleanSelector(),
    }
)


def account_key(identifier: AuthIdentifier) -> str:
    """Match exact account identifiers; phone/email aliases remain unverified."""
    value = (
        identifier.value.lower()
        if identifier.kind.value == "email"
        else identifier.value
    )
    return f"{identifier.kind.value}:{value}"


def flow_error(error: IpguardError) -> str:
    """Translate classified failures without exposing arbitrary error contents."""
    if isinstance(error, InvalidCodeError):
        return "invalid_code"
    if isinstance(error, VerificationExpiredError):
        return "code_expired"
    if isinstance(error, RateLimitError):
        return "rate_limited"
    if isinstance(error, AuthenticationError):
        return "invalid_auth"
    if isinstance(error, InvalidInputError):
        return "invalid_input"
    return "cannot_connect"


class IpguardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Maintain one explicit enrollment challenge until completion or cancellation."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._client: IpguardClient | None = None
        self._saved_session: Session | None = None
        self._restoring_entry: ConfigEntry | None = None
        self._identifier = ""
        self._label = ""

    async def _async_close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.async_close()

    @callback
    def async_remove(self) -> None:
        """HA calls this hook on cancellation and every terminal flow result."""
        client, self._client = self._client, None
        if client is not None:
            self.hass.async_create_task(client.async_close(), "IPGUARD flow cleanup")

    async def _async_request_code(self) -> None:
        """Call only from an explicit account/resend submission."""
        if self._client is None:
            self._client = IpguardClient(session=self._saved_session)
        await self._client.async_request_verification(self._identifier, self._label)

    def _user_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA,
                {
                    CONF_IDENTIFIER: self._identifier,
                    CONF_INSTALLATION_NAME: self._label,
                },
            ),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            return self._user_form(errors)
        identifier: AuthIdentifier | None = None
        try:
            identifier = validate_identifier(user_input[CONF_IDENTIFIER])
        except InvalidInputError:
            errors[CONF_IDENTIFIER] = "invalid_identifier"
        try:
            self._label = validate_installation_label(
                user_input[CONF_INSTALLATION_NAME]
            )
        except InvalidInputError:
            errors[CONF_INSTALLATION_NAME] = "invalid_name"
        if errors or identifier is None:
            return self._user_form(errors)

        await self.async_set_unique_id(account_key(identifier))
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
        else:
            self._abort_if_unique_id_configured()
        self._identifier = user_input[CONF_IDENTIFIER].strip()
        try:
            await self._async_request_code()
        except IpguardError as error:
            errors["base"] = flow_error(error)
        except OSError, TimeoutError:
            errors["base"] = "cannot_connect"
        else:
            return await self.async_step_verification()
        return self._user_form(errors)

    async def async_step_verification(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            return self.async_show_form(
                step_id="verification", data_schema=VERIFICATION_SCHEMA, errors=errors
            )
        if user_input.get(CONF_REQUEST_NEW_CODE) is True:
            try:
                await self._async_request_code()
            except IpguardError as error:
                errors["base"] = flow_error(error)
            except OSError, TimeoutError:
                errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="verification", data_schema=VERIFICATION_SCHEMA, errors=errors
            )

        code = user_input.get(CONF_VERIFICATION_CODE, "")
        try:
            validate_verification_code(code)
        except InvalidInputError:
            errors[CONF_VERIFICATION_CODE] = "invalid_code"
        if not errors:
            if self._client is None:
                return self.async_abort(reason="challenge_missing")
            try:
                await self._client.async_verify_code(code)
            except IpguardError as error:
                errors["base"] = flow_error(error)
            except OSError, TimeoutError:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish_registration(replace_existing=False)
        return self.async_show_form(
            step_id="verification", data_schema=VERIFICATION_SCHEMA, errors=errors
        )

    async def _async_finish_registration(
        self, *, replace_existing: bool
    ) -> ConfigFlowResult:
        if self._client is None:
            return self.async_abort(reason="challenge_missing")
        try:
            session = await self._client.async_complete_registration(
                replace_existing=replace_existing
            )
            await self._client.async_get_relays()
        except ExistingInstallationError:
            return await self.async_step_replace()
        except IpguardError as error:
            return self.async_show_form(
                step_id="verification",
                data_schema=VERIFICATION_SCHEMA,
                errors={"base": flow_error(error)},
            )
        except OSError, TimeoutError:
            return self.async_show_form(
                step_id="verification",
                data_schema=VERIFICATION_SCHEMA,
                errors={"base": "cannot_connect"},
            )
        if session.status is not SessionStatus.ACTIVATED:
            return self.async_show_form(
                step_id="verification",
                data_schema=VERIFICATION_SCHEMA,
                errors={"base": "invalid_auth"},
            )
        await self._async_close_client()
        data = {CONF_SESSION: session.to_dict()}
        if self._restoring_entry is not None:
            return self.async_update_reload_and_abort(
                self._restoring_entry, data_updates=data, title=session.label
            )
        return self.async_create_entry(title=session.label, data=data)

    async def async_step_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Decline by default; only true sends the replacement protocol event."""
        if user_input is not None:
            if user_input.get(CONF_REPLACE_EXISTING) is not True:
                await self._async_close_client()
                return self.async_abort(reason="replacement_declined")
            return await self._async_finish_registration(replace_existing=True)
        return self.async_show_form(step_id="replace", data_schema=REPLACEMENT_SCHEMA)

    def _restore(self, entry: ConfigEntry) -> bool:
        payload = entry.data.get(CONF_SESSION)
        if not isinstance(payload, Mapping):
            return False
        try:
            self._saved_session = Session.from_dict(payload)
        except InvalidInputError, TypeError, ValueError:
            return False
        self._identifier = self._saved_session.identifier.value
        try:
            self._label = validate_installation_label(self._saved_session.label)
        except InvalidInputError:
            self._label = ""
        self._restoring_entry = entry
        return True

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        if not self._restore(self._get_reauth_entry()):
            return self.async_abort(reason="invalid_session")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=probatio.Schema({})
        )
