"""Constants for the FreePBX Assist integration."""

DOMAIN = "freepbx_assist"

CONF_SIP_PORT = "sip_port"
CONF_PBX_HOST = "pbx_host"
CONF_ALLOWED_EXTENSIONS = "allowed_extensions"
CONF_PIPELINE_MAP = "pipeline_map"
CONF_PIPELINE_TIMEOUT = "pipeline_timeout"

DEFAULT_SIP_PORT = 5060
DEFAULT_PIPELINE_TIMEOUT = 30

# Known extensions -> room (area). Devices for these are pre-created at setup
# with the area already assigned; any other caller still gets a device on
# first call, just without an area until one is assigned in the UI.
EXTENSION_AREAS = {
    "1000": "Office",
    "2000": "Lounge",
    "3000": "Bedroom",
    "4000": "Attic",
}

# Sample format used on the RTP leg (voip-utils encodes/decodes OPUS at this rate)
RATE = 16000
WIDTH = 2
CHANNELS = 1

# Hang up if no RTP audio arrives from the caller for this long
AUDIO_TIMEOUT = 8.0
# Hang up after this many consecutive pipeline errors
MAX_ERRORS = 2
# Absolute cap on call length
MAX_CALL_SECONDS = 300
