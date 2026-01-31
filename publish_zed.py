import asyncio
import subprocess
import socket
import threading
import time
import math
import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify
from livekit import api, rtc

URL = "ws://localhost:7880"
API_KEY = "devkey"
API_SECRET = "secret"
ROOM_NAME = "my-room"

WIDTH = 1920
HEIGHT = 2160

# I420 = Y plane (W*H) + U (W*H/4) + V (W*H/4) = W*H*3/2
FRAME_SIZE = WIDTH * HEIGHT * 3 // 2

gst_exe = r"C:/Program Files/gstreamer/1.0/msvc_x86_64/bin/gst-launch-1.0"

GST_CMD = [
    gst_exe, "-q",
    "zedsrc", "camera-resolution=1", "camera-fps=30", "stream-type=2", "!",
    "videoconvert", "!",
    f"video/x-raw,format=I420,width={WIDTH},height={HEIGHT},framerate=30/1", "!",
    "tcpserversink", "host=127.0.0.1", "port=40000", "sync=false"
]

# ============ 全景扫描相关 ============
GIMBAL_SERVER = "http://localhost:30000"  # 云台服务器地址

# 全局状态
latest_frame_lock = threading.Lock()
latest_frame = None  # 最新的I420帧数据
is_scanning = False
scan_lock = threading.Lock()

# Flask应用
flask_app = Flask(__name__)


def i420_to_bgr(i420_data: bytes, width: int, height: int) -> np.ndarray:
    """将I420格式转换为BGR格式"""
    # I420: Y平面 + U平面(1/4) + V平面(1/4)
    y_size = width * height
    uv_size = y_size // 4
    
    y = np.frombuffer(i420_data[:y_size], dtype=np.uint8).reshape((height, width))
    u = np.frombuffer(i420_data[y_size:y_size + uv_size], dtype=np.uint8).reshape((height // 2, width // 2))
    v = np.frombuffer(i420_data[y_size + uv_size:], dtype=np.uint8).reshape((height // 2, width // 2))
    
    # 上采样U和V
    u_up = cv2.resize(u, (width, height), interpolation=cv2.INTER_LINEAR)
    v_up = cv2.resize(v, (width, height), interpolation=cv2.INTER_LINEAR)
    
    # YUV to BGR
    yuv = cv2.merge([y, u_up, v_up])
    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
    
    return bgr


def get_current_frame_bgr() -> np.ndarray:
    """获取当前帧的BGR格式（只取左眼）"""
    global latest_frame
    
    with latest_frame_lock:
        if latest_frame is None:
            return None
        frame_data = bytes(latest_frame)
    
    bgr = i420_to_bgr(frame_data, WIDTH, HEIGHT)
    
    # ZED是左右眼并排，取左半边
    left_frame = bgr[:, :WIDTH // 2]
    return left_frame


def set_gimbal(yaw: float, pitch: float):
    """设置云台角度（弧度）"""
    response = requests.post(
        f"{GIMBAL_SERVER}/gimbal/set",
        json={"yaw": yaw, "pitch": pitch},
        timeout=5
    )
    response.raise_for_status()


def init_gimbal():
    """复位云台"""
    response = requests.get(f"{GIMBAL_SERVER}/init", timeout=5)
    response.raise_for_status()


def scan_and_stitch() -> np.ndarray:
    """执行扫描并拼接全景图"""
    # 扫描参数
    yaw_positions = [
        math.radians(-67.5),
        math.radians(-22.5),
        math.radians(22.5),
        math.radians(67.5)
    ]
    pitch_positions = [
        math.radians(-25),  # 上
        math.radians(25)    # 下
    ]
    
    images = []
    
    # 复位
    init_gimbal()
    time.sleep(1)
    
    # 扫描
    for pitch in pitch_positions:
        for yaw in yaw_positions:
            set_gimbal(yaw, pitch)
            time.sleep(0.8)  # 等待稳定
            
            # 抓取帧（多次以确保最新）
            for _ in range(5):
                frame = get_current_frame_bgr()
                time.sleep(0.05)
            
            if frame is not None:
                images.append(frame)
                print(f"[Panorama] 捕获图像 {len(images)}, Yaw={math.degrees(yaw):.1f}°, Pitch={math.degrees(pitch):.1f}°")
            else:
                print(f"[Panorama] 警告: 无法获取帧")
    
    # 复位
    init_gimbal()
    
    if len(images) < 2:
        raise RuntimeError(f"图像数量不足: {len(images)}")
    
    # 拼接
    print(f"[Panorama] 开始拼接 {len(images)} 张图像...")
    stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
    status, panorama = stitcher.stitch(images)
    
    if status == cv2.Stitcher_OK:
        print(f"[Panorama] 拼接成功! 尺寸: {panorama.shape}")
        return panorama
    else:
        error_msgs = {
            cv2.Stitcher_ERR_NEED_MORE_IMGS: "需要更多图像",
            cv2.Stitcher_ERR_HOMOGRAPHY_EST_FAIL: "单应性估计失败",
            cv2.Stitcher_ERR_CAMERA_PARAMS_ADJUST_FAIL: "相机参数调整失败"
        }
        raise RuntimeError(f"拼接失败: {error_msgs.get(status, f'错误码 {status}')}")


@flask_app.route("/scan/panorama", methods=["GET"])
def get_panorama():
    """全景扫描API"""
    global is_scanning
    
    if not scan_lock.acquire(blocking=False):
        return jsonify({"error": "扫描正在进行中"}), 503
    
    try:
        is_scanning = True
        print("[Panorama] 开始全景扫描...")
        
        panorama = scan_and_stitch()
        
        # 编码为JPEG
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
        success, buffer = cv2.imencode('.jpg', panorama, encode_params)
        
        if not success:
            return jsonify({"error": "图像编码失败"}), 500
        
        print(f"[Panorama] 完成，图像大小: {len(buffer)} bytes")
        
        return Response(
            buffer.tobytes(),
            mimetype='image/jpeg'
        )
        
    except Exception as e:
        print(f"[Panorama] 错误: {e}")
        return jsonify({"error": str(e)}), 500
        
    finally:
        is_scanning = False
        scan_lock.release()


@flask_app.route("/scan/status", methods=["GET"])
def scan_status():
    """检查扫描状态"""
    return jsonify({
        "scanning": is_scanning,
        "available": not scan_lock.locked()
    })


def run_flask():
    """在后台线程运行Flask"""
    flask_app.run(host="0.0.0.0", port=30001, debug=False, use_reloader=False)


async def main():
    global latest_frame
    
    # 启动Flask服务（后台线程）
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("[Main] Flask服务已启动在端口30001")
    
    gst_process = subprocess.Popen(GST_CMD)
    await asyncio.sleep(2)

    if gst_process.poll() is not None:
        print("错误：GStreamer 启动失败")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", 40000))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    room = rtc.Room()

    grants = api.VideoGrants(
        room_join=True,
        room=ROOM_NAME,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_publish_sources=["camera"],
    )

    token = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity("windows_zed_publisher")
        .with_name("ZED Camera")
        .with_grants(grants)
        .to_jwt()
    )

    await room.connect(URL, token)
    print(f"joined room: {ROOM_NAME}")

    source = rtc.VideoSource(WIDTH, HEIGHT)
    track = rtc.LocalVideoTrack.create_video_track("zed_i420", source)

    options = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        simulcast=False,
        video_codec=rtc.VideoCodec.H264,
        video_encoding=rtc.VideoEncoding(
            max_framerate=30,
            max_bitrate=10000000,
        ),
    )

    publication = await room.local_participant.publish_track(track, options)

    print("publishing...")

    buffer = bytearray()

    try:
        while True:
            while len(buffer) < FRAME_SIZE:
                chunk = sock.recv(FRAME_SIZE - len(buffer))
                if not chunk:
                    raise RuntimeError("GStreamer 数据流中断")
                buffer.extend(chunk)

            frame_data = buffer[:FRAME_SIZE]
            del buffer[:FRAME_SIZE]
            
            # 保存最新帧供全景扫描使用
            with latest_frame_lock:
                latest_frame = bytes(frame_data)

            frame = rtc.VideoFrame(
                width=WIDTH,
                height=HEIGHT,
                type=rtc.VideoBufferType.I420,
                data=frame_data,
            )
            source.capture_frame(frame)

            await asyncio.sleep(0)

    finally:
        sock.close()
        gst_process.kill()
        await room.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
