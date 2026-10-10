"""Local integration constants; these are not server-facing metadata."""

from homeassistant.const import Platform

DOMAIN = "ipguard"
PLATFORMS = [Platform.BUTTON, Platform.EVENT]
CONF_IDENTIFIER = "identifier"
CONF_INSTALLATION_NAME = "installation_name"
CONF_VERIFICATION_CODE = "verification_code"
CONF_SESSION = "session"
CONF_REQUEST_NEW_CODE = "request_new_code"
CONF_REPLACE_EXISTING = "replace_existing"
