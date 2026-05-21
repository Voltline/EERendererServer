import math
import os

LIVEKIT_URL = "ws://localhost:7880"
API_KEY = "devkey"
API_SECRET = "secret"
ROOM_NAME = "my-room"

WIDTH = 1920
HEIGHT = 2160
FRAME_SIZE = WIDTH * HEIGHT * 3 // 2

ZED_MINI_HD1080_FOV_X_DEG = 66
ZED_MINI_HD1080_FOV_Y_DEG = 40
PANORAMA_FOV_SCALE = float(os.environ.get("PANORAMA_FOV_SCALE", "1.0"))
ZED_MINI_HD1080_FOV_X = math.radians(ZED_MINI_HD1080_FOV_X_DEG * PANORAMA_FOV_SCALE)
ZED_MINI_HD1080_FOV_Y = math.radians(ZED_MINI_HD1080_FOV_Y_DEG * PANORAMA_FOV_SCALE)
PANORAMA_BLEND_EXPONENT = float(os.environ.get("PANORAMA_BLEND_EXPONENT", "3.0"))
PANORAMA_YAW_DEG_LIST = [-90, -45, 0, 45, 90]
PANORAMA_PITCH_DEG_LIST = [-20, 10, 40]

gst_exe = r"C:/Program Files/gstreamer/1.0/msvc_x86_64/bin/gst-launch-1.0"
GST_CMD = [
    gst_exe,
    "-q",
    "zedsrc", "camera-resolution=1", "camera-fps=30", "stream-type=2", "!",
    "videoconvert", "!",
    f"video/x-raw,format=I420,width={WIDTH},height={HEIGHT},framerate=30/1", "!",
    "tcpserversink", "host=127.0.0.1", "port=40000", "sync=false",
]

device_config = {
    "device_name": "COM3",
    "baud_rate": 57600,
    "protocol_version": 2,
    "horizontal_id": 11,
    "vertical_id": 10,
}

WLT_API_KEY = os.environ.get("WLT_API_KEY", "")
WLT_BASE_URL = "https://api.worldlabs.ai/marble/v1"
WLT_MOCK = os.environ.get("WLT_MOCK", "0").strip().lower() in ("1", "true", "yes")

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
WLT_MOCK_SPZ = os.environ.get("WLT_MOCK_SPZ", os.path.join(_ROOT_DIR, "mock.spz"))
MOCK_GENERATION_TIME = int(os.environ.get("WLT_MOCK_TIME", "15"))

SCAN_FRAME_COUNT = 8
