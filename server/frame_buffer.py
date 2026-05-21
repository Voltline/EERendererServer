import threading

import cv2
import numpy as np

from .config import WIDTH, HEIGHT

_latest_frame_lock = threading.Lock()
_latest_frame = None


def set_latest_frame(frame_data: bytes):
    global _latest_frame
    with _latest_frame_lock:
        _latest_frame = bytes(frame_data)


def i420_to_bgr(i420_data: bytes, width: int, height: int) -> np.ndarray:
    """Convert I420 frame to BGR."""
    y_size = width * height
    uv_size = y_size // 4

    y = np.frombuffer(i420_data[:y_size], dtype=np.uint8).reshape((height, width))
    u = np.frombuffer(i420_data[y_size:y_size + uv_size], dtype=np.uint8).reshape((height // 2, width // 2))
    v = np.frombuffer(i420_data[y_size + uv_size:], dtype=np.uint8).reshape((height // 2, width // 2))

    u_up = cv2.resize(u, (width, height), interpolation=cv2.INTER_LINEAR)
    v_up = cv2.resize(v, (width, height), interpolation=cv2.INTER_LINEAR)

    yuv = cv2.merge([y, u_up, v_up])
    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
    return bgr


def get_current_frame_bgr() -> np.ndarray:
    """Return latest left-eye frame as BGR, or None if unavailable."""
    with _latest_frame_lock:
        if _latest_frame is None:
            return None
        frame_data = bytes(_latest_frame)

    bgr = i420_to_bgr(frame_data, WIDTH, HEIGHT)
    left_frame = bgr[:HEIGHT // 2, :]
    return left_frame
