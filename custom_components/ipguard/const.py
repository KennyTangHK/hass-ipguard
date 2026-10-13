"""Local integration constants; these are not server-facing metadata."""

from homeassistant.const import Platform

DOMAIN = "ipguard"
PLATFORMS = [Platform.BUTTON, Platform.EVENT, Platform.CAMERA]
CONF_IDENTIFIER = "identifier"
CONF_INSTALLATION_NAME = "installation_name"
CONF_VERIFICATION_CODE = "verification_code"
CONF_SESSION = "session"
CONF_REQUEST_NEW_CODE = "request_new_code"
CONF_REPLACE_EXISTING = "replace_existing"
CONF_VIDEO_ENABLED = "video_enabled"
CONF_SCRYPTED_URL = "scrypted_url"
CONF_HA_URL = "ha_url"
CONF_COMPANION_CREDENTIAL = "companion_credential"
CONF_RTSP_URL = "rtsp_url"
CONF_VERIFY_SSL = "verify_ssl"
SCRYPTED_PLUGIN = "scrypted-ipguard"
