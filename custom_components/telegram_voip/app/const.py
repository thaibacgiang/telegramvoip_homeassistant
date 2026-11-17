VERSION = "0.2.0"
SLUG = "telegram_voip"
API_PORT = 3332

DEFAULT_TTS_VOICE = "vi-VN-HoaiMyNeural"
DEFAULT_TTS_RATE = "+0%"
DEFAULT_TTS_ENGINE = "wyoming"
DEFAULT_WYOMING_HOST = "172.30.32.1"
DEFAULT_WYOMING_PORT = 10300
DEFAULT_WYOMING_VOICE = "ngoc-huyen-moi"
DEFAULT_TTS_FALLBACK_EDGE = True
DEFAULT_RING_TIMEOUT = 45
DEFAULT_RING_ATTEMPTS = 2
DEFAULT_RING_INTERVAL = 15
DEFAULT_MAX_CALL_SECONDS = 180
DEFAULT_LOG_LIMIT = 300

VOICES = (
    "vi-VN-HoaiMyNeural",
    "vi-VN-NamMinhNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
)

# Giọng của add-on Wyoming Vietnamese (engine nghitts). Chỉ dùng được các giọng
# đã khai báo trong tùy chọn tts_voice của add-on Wyoming.
WYOMING_VOICES = (
    "ngoc-huyen-moi",
    "duy-onyx-moi",
    "thanh-phuong-viettel",
    "ngoc-ngan",
    "mai-phuong",
)
