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

    # ZED stream-type=2 是上下堆叠：上半部分是左眼，下半部分是右眼
    # 总尺寸 1920x2160，每只眼 1920x1080
    left_frame = bgr[:HEIGHT // 2, :]  # 取上半部分（左眼）
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
    import os

    # 创建调试目录
    debug_dir = "panorama_debug"
    os.makedirs(debug_dir, exist_ok=True)

    # 扫描参数：上中下三层，每层9张
    yaw_positions = [
        math.radians(-80),
        math.radians(-40),
        math.radians(0),
        math.radians(40),
        math.radians(80),
    ]
    pitch_positions = [
        math.radians(-30),  # 上（低头）
        math.radians(0),  # 中
        math.radians(30),  # 下（抬头）
    ]

    images = []

    # 复位
    init_gimbal()
    time.sleep(0.8)

    # 扫描
    for pitch in pitch_positions:
        for yaw in yaw_positions:
            set_gimbal(yaw, pitch)
            time.sleep(1.0)  # 等待稳定

            # 抓取帧（多次以确保最新）
            frame = None
            for _ in range(10):
                frame = get_current_frame_bgr()
                time.sleep(0.1)

            if frame is not None:
                images.append(frame.copy())
                idx = len(images)
                # 保存调试图像
                debug_path = f"{debug_dir}/frame_{idx:02d}_yaw{math.degrees(yaw):.0f}_pitch{math.degrees(pitch):.0f}.jpg"
                cv2.imwrite(debug_path, frame)
                print(f"[Panorama] 捕获图像 {idx}/27, Yaw={math.degrees(yaw):.1f}°, Pitch={math.degrees(pitch):.1f}°")
            else:
                print(f"[Panorama] 警告: 无法获取帧")

    # 复位
    init_gimbal()

    if len(images) < 2:
        raise RuntimeError(f"图像数量不足: {len(images)}")

    # 检查图像是否都相同
    if len(images) >= 2:
        diff = cv2.absdiff(images[0], images[1])
        if np.mean(diff) < 1.0:
            print("[Panorama] 警告: 前两张图像几乎相同，可能帧没有更新！")

    # 缩小图像加速拼接
    scale = 0.4  # 缩小到40%
    images_small = [cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) for img in images]
    print(f"[Panorama] 缩放图像: {images_small[0].shape}")

    num_yaw = len(yaw_positions)
    num_pitch = len(pitch_positions)

    # 检查是否有CUDA支持
    try:
        cuda_available = cv2.cuda.getCudaEnabledDeviceCount() > 0
    except:
        cuda_available = False
    print(f"[Panorama] CUDA加速: {'可用' if cuda_available else '不可用'}")

    # 创建快速 Stitcher
    if cuda_available:
        stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
        # 尝试使用GPU加速的特征检测
        try:
            stitcher.setFeaturesFinder(cv2.cuda_ORB.create(500))
        except:
            pass
    else:
        stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)

    # 使用ORB特征检测器（比SIFT/SURF快10倍）
    try:
        orb = cv2.ORB_create(nfeatures=500)  # 减少特征点数量
        stitcher.setFeaturesFinder(cv2.detail_ORB(nfeatures=500))
    except Exception as e:
        print(f"[Panorama] 使用默认特征检测器: {e}")

    stitcher.setPanoConfidenceThresh(0.5)

    # 分行拼接，每行单独处理
    print(f"[Panorama] 开始分行拼接...")
    row_panoramas = []

    for row_idx in range(num_pitch):
        row_images = images_small[row_idx * num_yaw: (row_idx + 1) * num_yaw]
        print(f"[Panorama] 拼接第 {row_idx + 1}/{num_pitch} 行 ({len(row_images)} 张)...")

        start_time = time.time()

        # 尝试不同的参数组合
        success = False
        for attempt, (mode, conf_thresh, nfeatures) in enumerate([
            (cv2.Stitcher_PANORAMA, 0.5, 500),
            (cv2.Stitcher_PANORAMA, 0.3, 1000),
            (cv2.Stitcher_SCANS, 0.2, 1500),
        ]):
            try:
                stitcher = cv2.Stitcher_create(mode)
                stitcher.setPanoConfidenceThresh(conf_thresh)
                try:
                    stitcher.setFeaturesFinder(cv2.detail_ORB(nfeatures=nfeatures))
                except:
                    pass

                status, row_pano = stitcher.stitch(row_images)

                if status == cv2.Stitcher_OK:
                    success = True
                    elapsed = time.time() - start_time
                    row_panoramas.append(row_pano)
                    cv2.imwrite(f"{debug_dir}/row_{row_idx + 1}.jpg", row_pano)
                    print(
                        f"[Panorama] 第 {row_idx + 1} 行完成 (尝试{attempt + 1})，耗时 {elapsed:.1f}s，尺寸: {row_pano.shape}")
                    break
            except Exception as e:
                print(f"[Panorama] 第 {row_idx + 1} 行尝试 {attempt + 1} 异常: {e}")

        if not success:
            print(f"[Panorama] 第 {row_idx + 1} 行所有尝试失败，使用简单拼接")
            row_pano = cv2.hconcat(row_images)
            row_panoramas.append(row_pano)

    # 用 Stitcher 合并各行
    print(f"[Panorama] 开始合并 {len(row_panoramas)} 行...")

    if len(row_panoramas) == 1:
        panorama = row_panoramas[0]
    else:
        # 尝试用 Stitcher 合并各行
        merge_success = False
        for attempt, (mode, conf_thresh, nfeatures) in enumerate([
            (cv2.Stitcher_PANORAMA, 0.3, 1000),
            (cv2.Stitcher_PANORAMA, 0.2, 1500),
            (cv2.Stitcher_SCANS, 0.1, 2000),
        ]):
            try:
                stitcher = cv2.Stitcher_create(mode)
                stitcher.setPanoConfidenceThresh(conf_thresh)
                try:
                    stitcher.setFeaturesFinder(cv2.detail_ORB(nfeatures=nfeatures))
                except:
                    pass

                status, panorama = stitcher.stitch(row_panoramas)

                if status == cv2.Stitcher_OK:
                    merge_success = True
                    print(f"[Panorama] 行合并成功 (尝试{attempt + 1})，尺寸: {panorama.shape}")
                    break
                else:
                    print(f"[Panorama] 行合并尝试 {attempt + 1} 失败，状态码: {status}")
            except Exception as e:
                print(f"[Panorama] 行合并尝试 {attempt + 1} 异常: {e}")

        if not merge_success:
            print(f"[Panorama] 行合并所有尝试失败，使用简单垂直拼接")
            # 调整各行宽度一致后垂直合并
            max_width = max(p.shape[1] for p in row_panoramas)
            row_panoramas_resized = []
            for p in row_panoramas:
                if p.shape[1] < max_width:
                    diff_w = max_width - p.shape[1]
                    left = diff_w // 2
                    right = diff_w - left
                    p = cv2.copyMakeBorder(p, 0, 0, left, right, cv2.BORDER_CONSTANT, value=(0, 0, 0))
                row_panoramas_resized.append(p)
            panorama = cv2.vconcat(row_panoramas_resized)

    print(f"[Panorama] 合并完成! 尺寸: {panorama.shape}")

    # 裁剪到目标视野范围（约180°水平 x 90°垂直）
    h, w = panorama.shape[:2]

    # 计算裁剪区域（去掉边缘多余部分）
    estimated_fov = 260
    target_fov = 180

    crop_ratio = target_fov / estimated_fov
    crop_width = int(w * crop_ratio)
    start_x = (w - crop_width) // 2

    estimated_v_fov = 117
    target_v_fov = 90
    crop_v_ratio = target_v_fov / estimated_v_fov
    crop_height = int(h * crop_v_ratio)
    start_y = (h - crop_height) // 2

    # 裁剪
    panorama_cropped = panorama[start_y:start_y + crop_height, start_x:start_x + crop_width]
    print(f"[Panorama] 裁剪后尺寸: {panorama_cropped.shape}")

    cv2.imwrite(f"{debug_dir}/panorama_result.jpg", panorama_cropped)
    return panorama_cropped


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
