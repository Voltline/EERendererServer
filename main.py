"""
统一服务端 - 整合云台控制 + 视频流 + 极速全景拼接
端口: 30000 (HTTP API), 40000 (GStreamer TCP), 7880 (LiveKit)
"""
import asyncio
import subprocess
import socket
import threading
import time
import math
import cv2
import numpy as np
from flask import Flask, request, Response, jsonify
from livekit import api, rtc

# ============ LiveKit 配置 ============
LIVEKIT_URL = "ws://localhost:7880"
API_KEY = "devkey"
API_SECRET = "secret"
ROOM_NAME = "my-room"

# ============ ZED 相机配置 ============
WIDTH = 1920
HEIGHT = 2160
FRAME_SIZE = WIDTH * HEIGHT * 3 // 2  # I420 格式

gst_exe = r"C:/Program Files/gstreamer/1.0/msvc_x86_64/bin/gst-launch-1.0"
GST_CMD = [
    gst_exe, "-q",
    "zedsrc", "camera-resolution=1", "camera-fps=30", "stream-type=2", "!",
    "videoconvert", "!",
    f"video/x-raw,format=I420,width={WIDTH},height={HEIGHT},framerate=30/1", "!",
    "tcpserversink", "host=127.0.0.1", "port=40000", "sync=false"
]

# ============ 云台配置 ============
try:
    from gimbal.dynamixel_driver import DynamixelDriver
    from gimbal.gimbal_controller import GimbalController
except ImportError:
    print("[Warn] 未找到 gimbal 模块，将在无云台模式下运行")
    GimbalController = None

device_config = {
    "device_name": "COM3",
    "baud_rate": 57600,
    "protocol_version": 2,
    "horizontal_id": 11,
    "vertical_id": 10
}

# 尝试初始化云台
controller = None
if GimbalController:
    try:
        controller = GimbalController(DynamixelDriver(device_config))
        print("[Init] 云台连接成功")
    except Exception as e:
        print(f"[Init] 云台连接失败: {e}")

# ============ 全局状态 ============
current_yaw = 0.0
current_pitch = 0.0
state_lock = threading.Lock()

latest_frame_lock = threading.Lock()
latest_frame = None

is_scanning = False
scan_lock = threading.Lock()

# ============ Flask 应用 ============
app = Flask(__name__)


# ============ 工具函数 ============
def i420_to_bgr(i420_data: bytes, width: int, height: int) -> np.ndarray:
    """将I420格式转换为BGR格式"""
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
    """获取当前帧的BGR格式（只取左眼）"""
    global latest_frame

    with latest_frame_lock:
        if latest_frame is None:
            return None
        frame_data = bytes(latest_frame)

    bgr = i420_to_bgr(frame_data, WIDTH, HEIGHT)
    # ZED stream-type=2 上下堆叠：上半部分是左眼 (1920x1080)
    left_frame = bgr[:HEIGHT // 2, :]
    return left_frame


def set_gimbal_sync(yaw: float, pitch: float):
    """同步设置云台角度"""
    global current_yaw, current_pitch
    if controller is None: return

    with state_lock:
        current_yaw = yaw
        current_pitch = pitch
        controller.set_yaw_pitch(yaw, pitch)


def init_gimbal_sync():
    """复位云台"""
    global current_yaw, current_pitch
    if controller is None: return

    with state_lock:
        current_yaw = 0
        current_pitch = 0
        controller.set_yaw_pitch(0, 0)


# ============ 云台控制 API ============
@app.route("/init", methods=["GET"])
def init():
    init_gimbal_sync()
    return jsonify({"yaw": current_yaw, "pitch": current_pitch})


@app.route("/gimbal/set", methods=["POST"])
def gimbal_set():
    global current_yaw, current_pitch
    data = request.get_json(force=True)
    target_yaw = float(data.get("yaw", current_yaw))
    target_pitch = float(data.get("pitch", current_pitch))

    with state_lock:
        current_yaw = target_yaw
        current_pitch = target_pitch
        controller.set_yaw_pitch(current_yaw, current_pitch)

    return jsonify({"yaw": current_yaw, "pitch": current_pitch})


# ============ 核心逻辑：全景扫描 & 拼接 ============

def capture_grid_sequence():
    """执行 5x3 的网格扫描"""
    if controller is None:
        print("云台未连接，无法执行物理扫描")
        return []

    captured_frames = []
    # 5列: -90 到 90
    yaw_deg_list = [-90, -45, 0, 45, 90]
    # 3行: 仰视 -> 平视 -> 俯视
    pitch_deg_list = [-45, 0, 45]

    yaw_rads = [math.radians(d) for d in yaw_deg_list]
    pitch_rads = [math.radians(d) for d in pitch_deg_list]

    print(f"[Capture] 开始采集 {len(pitch_deg_list)}行 x {len(yaw_deg_list)}列...")

    # 复位
    init_gimbal_sync()
    time.sleep(1.0) # 给一点时间回中

    total_count = 0
    for pitch in pitch_rads:
        for yaw in yaw_rads:
            set_gimbal_sync(yaw, pitch)

            # 机械稳定延迟 (越稳越不容易重影)
            time.sleep(0.6)

            # 读图重试
            frame = None
            for _ in range(10): # 增加重试次数
                frame = get_current_frame_bgr()
                if frame is not None:
                    break
                time.sleep(0.02)

            if frame is not None:
                captured_frames.append({
                    "img": frame.copy(),
                    "yaw": yaw,
                    "pitch": pitch
                })
                print(f"  -> [{total_count+1}] OK (P:{math.degrees(pitch):.0f}, Y:{math.degrees(yaw):.0f})")
            else:
                print(f"  -> [{total_count+1}] 丢帧 (P:{math.degrees(pitch):.0f}, Y:{math.degrees(yaw):.0f})")

            total_count += 1

    init_gimbal_sync()
    return captured_frames


def generate_equirectangular(
        frames,
        pano_w=4096,
        pano_h=2048,
        fov_x=math.radians(102),
        fov_y=math.radians(57),
):
    """
    极速拼接：Input -> NumPy Vectorization -> cv2.remap -> Output Array
    """
    if not frames:
        return None

    print("[Stitch] 开始生成全景图 (GPU/Vectorized)...")
    st = time.time()

    # 建立画布 (Spherical Grid)
    lon = np.linspace(-np.pi, np.pi, pano_w, dtype=np.float32)
    lat = np.linspace(np.pi / 2, -np.pi / 2, pano_h, dtype=np.float32)
    lon_grid, lat_grid = np.meshgrid(lon, lat)

    # 世界坐标向量 (World Vectors)
    cos_lat = np.cos(lat_grid)
    sin_lat = np.sin(lat_grid)
    x_world = cos_lat * np.sin(lon_grid)  # x
    y_world = sin_lat                     # y (up)
    z_world = cos_lat * np.cos(lon_grid)  # z (forward)

    # 展平为 (N, 3) 矩阵
    points_world = np.stack((x_world, y_world, z_world), axis=-1).reshape(-1, 3)

    # 初始化累加器
    pano_acc = np.zeros((pano_h, pano_w, 3), dtype=np.float32)
    weight_acc = np.zeros((pano_h, pano_w), dtype=np.float32)

    tan_half_fov_x = math.tan(fov_x / 2)
    tan_half_fov_y = math.tan(fov_y / 2)
    yaw_blend_range = math.radians(45) * 1.2

    # 循环处理每张图 (无像素级循环)
    for i, f in enumerate(frames):
        img = f["img"]
        h_img, w_img = img.shape[:2]
        cam_yaw = f["yaw"]
        cam_pitch = f["pitch"]

        # 构建旋转矩阵 R (Yaw * Pitch)
        cy, sy = math.cos(-cam_yaw), math.sin(-cam_yaw)
        cp, sp = math.cos(-cam_pitch), math.sin(-cam_pitch)

        Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
        Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], dtype=np.float32)
        R = Rx @ Ry

        # 批量旋转: World -> Camera
        points_cam = points_world @ R.T

        pc_x = points_cam[:, 0]
        pc_y = points_cam[:, 1]
        pc_z = points_cam[:, 2]

        # 透视投影
        pc_z_safe = np.where(pc_z <= 0, 1e-5, pc_z) # 避免除零
        u = - (pc_x / pc_z_safe) / tan_half_fov_x
        v =   (pc_y / pc_z_safe) / tan_half_fov_y

        # 映射到 UV 坐标
        map_x = ((u * 0.5 + 0.5) * w_img).reshape(pano_h, pano_w).astype(np.float32)
        map_y = ((0.5 - v * 0.5) * h_img).reshape(pano_h, pano_w).astype(np.float32)
        pc_z = pc_z.reshape(pano_h, pano_w)

        # 有效性 Mask
        mask_valid = (pc_z > 0) & (map_x >= 0) & (map_x < w_img) & (map_y >= 0) & (map_y < h_img)

        # 权重计算 (基于 Yaw 距离)
        yaw_diff = np.abs(lon_grid - cam_yaw)
        yaw_diff = np.minimum(yaw_diff, 2 * np.pi - yaw_diff) # wrap-around
        w_map = np.maximum(0, 1.0 - (yaw_diff / yaw_blend_range))

        # 综合 Mask
        final_mask = mask_valid & (w_map > 0)
        w_map = w_map * final_mask.astype(np.float32)

        if np.sum(final_mask) == 0:
            continue

        # 极速重映射
        warped_img = cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

        # 累加
        mask_3ch = np.dstack([w_map] * 3)
        pano_acc += warped_img * mask_3ch
        weight_acc += w_map

    # 归一化
    weight_acc[weight_acc == 0] = 1.0
    weight_acc_3ch = np.dstack([weight_acc] * 3)
    pano_final = pano_acc / weight_acc_3ch
    pano_final = np.clip(pano_final, 0, 255).astype(np.uint8)

    # 水平翻转 (Standard Equirectangular)
    cv2.flip(pano_final, 1, pano_final)

    cv2.imwrite("pano_final.jpg", pano_final)

    print(f"[Stitch] 完成! 耗时: {time.time() - st:.3f}s")
    return pano_final


@app.route("/scan/panorama", methods=["GET"])
def get_panorama():
    """
    触发扫描 -> 拼接 -> 返回 JPEG 图片
    """
    global is_scanning

    # 简单的非阻塞锁，防止重复点击
    if not scan_lock.acquire(blocking=False):
        return jsonify({"error": "Scanning in progress"}), 503

    try:
        is_scanning = True

        frames = capture_grid_sequence()

        if not frames:
            return jsonify({"error": "No frames captured (Gimbal offline?)"}), 500

        pano_img = generate_equirectangular(frames)

        if pano_img is None:
             return jsonify({"error": "Stitching failed"}), 500

        success, buffer = cv2.imencode('.jpg', pano_img, [int(cv2.IMWRITE_JPEG_QUALITY), 100])

        if not success:
            return jsonify({"error": "Image encoding failed"}), 500

        return Response(buffer.tobytes(), mimetype='image/jpeg')

    except Exception as e:
        print(f"[Error] Scan failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        is_scanning = False
        scan_lock.release()


@app.route("/scan/status", methods=["GET"])
def scan_status():
    return jsonify({"scanning": is_scanning})

@app.route("/gimbal/delta", methods=["POST"])
def gimbal_delta():
    global current_yaw, current_pitch

    data = request.get_json(force=True)

    delta_yaw = float(data.get("delta_yaw", 0.0))
    delta_pitch = float(data.get("delta_pitch", 0.0))
    print(delta_yaw, delta_pitch)

    with state_lock:
        current_yaw += delta_yaw
        current_pitch += delta_pitch

        controller.set_yaw_pitch(current_yaw, current_pitch)

    return jsonify({
        "yaw": current_yaw,
        "pitch": current_pitch
    })

# ============ Flask 启动器 ============
def run_flask():
    app.run(host="0.0.0.0", port=30000, debug=False, use_reloader=False, threaded=True)


# ============ 主流程 ============
async def main():
    global latest_frame

    # 1. 启动 Flask (后台线程)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("[Main] Flask API ready at :30000")

    # 2. 启动 GStreamer
    print("[Main] Launching GStreamer...")
    gst_process = subprocess.Popen(GST_CMD)
    await asyncio.sleep(3)  # 等待 GStreamer 预热

    if gst_process.poll() is not None:
        print("[Main] GStreamer failed to start.")
        return

    # 3. 建立 TCP 数据连接
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", 40000))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print("[Main] GStreamer TCP Connected.")
    except Exception as e:
        print(f"[Main] Connection failed: {e}")
        gst_process.kill()
        return

    # 4. 连接 LiveKit
    print("[Main] Connecting to LiveKit...")
    room = rtc.Room()
    grants = api.VideoGrants(
        room_join=True, room=ROOM_NAME,
        can_publish=True, can_subscribe=True,
    )
    token = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity("zed_publisher")
        .with_name("ZED Camera")
        .with_grants(grants)
        .to_jwt()
    )

    try:
        await room.connect(LIVEKIT_URL, token)
        print(f"[Main] LiveKit Connected: {ROOM_NAME}")
    except Exception as e:
        print(f"[Main] LiveKit connection skipped: {e}")

    # 发布视频轨道
    source = rtc.VideoSource(WIDTH, HEIGHT)
    track = rtc.LocalVideoTrack.create_video_track("zed_source", source)

    # 显式配置推流参数
    options = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        video_codec=rtc.VideoCodec.H264,  # 强制 H.264
        video_encoding=rtc.VideoEncoding(
            max_framerate=30,
            max_bitrate=6_000_000  # 6 Mbps
        ),
    )

    if room.isconnected:
        print("[Main] Publishing track with options...")
        await room.local_participant.publish_track(track, options)
        print("[Main] Track published!")

    # 主循环：读取流 -> 更新全局帧 -> 推流
    buffer = bytearray()
    print("[Main] Service Running. Waiting for requests...")

    try:
        while True:
            # 读取刚好一帧的大小
            while len(buffer) < FRAME_SIZE:
                # 注意：sock.recv 是阻塞调用，在极高负载下可能会稍微卡顿 asyncio
                # 但在 localhost 环境下通常够快。如果还卡，需要改写为 non-blocking。
                chunk = sock.recv(FRAME_SIZE - len(buffer))
                if not chunk:
                    raise RuntimeError("Stream closed")
                buffer.extend(chunk)

            # 提取一帧
            frame_data = buffer[:FRAME_SIZE]
            del buffer[:FRAME_SIZE]

            # 更新全局帧 (供 HTTP 全景扫描使用)
            with latest_frame_lock:
                latest_frame = bytes(frame_data)

            # 发送给 LiveKit
            if room.isconnected:
                frame = rtc.VideoFrame(
                    width=WIDTH, height=HEIGHT,
                    type=rtc.VideoBufferType.I420,
                    data=frame_data,
                )
                source.capture_frame(frame)

            # 让出控制权给 Flask 线程
            await asyncio.sleep(0)

    except Exception as e:
        print(f"[Main] Loop Error: {e}")
    finally:
        sock.close()
        gst_process.kill()
        await room.disconnect()

if __name__ == "__main__":
    asyncio.run(main())