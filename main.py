"""
统一服务端 - 整合云台控制 + 视频流 + 极速全景拼接 + 3DGS 生成
端口: 30000 (HTTP API), 40000 (GStreamer TCP), 7880 (LiveKit)
"""
import asyncio
import subprocess
import socket
import threading
import time
import math
import uuid
import os
import base64
import cv2
import numpy as np
import requests as http_requests
from flask import Flask, request, Response, jsonify
from flasgger import Swagger
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

# ============ World Labs 配置 ============
WLT_API_KEY = os.environ.get("WLT_API_KEY", "")
WLT_BASE_URL = "https://api.worldlabs.ai/marble/v1"
WLT_MOCK = os.environ.get("WLT_MOCK", "0").strip().lower() in ("1", "true", "yes")
# Mock 模式下的 SPZ 文件路径: 环境变量 > 默认 ./mock.spz
WLT_MOCK_SPZ = os.environ.get("WLT_MOCK_SPZ", os.path.join(os.path.dirname(__file__), "mock.spz"))

# ============ 全局状态 ============
current_yaw = 0.0
current_pitch = 0.0
state_lock = threading.Lock()

latest_frame_lock = threading.Lock()
latest_frame = None

is_scanning = False
scan_lock = threading.Lock()

# ============ 3DGS 任务状态管理 ============
# job 状态: SCANNING -> UPLOADING -> SUBMITTED -> COMPLETED / FAILED
_3dgs_jobs = {}  # job_id -> { status, operation_id, world_id, error, created_at }
_3dgs_jobs_lock = threading.Lock()

# Mock 模式的 operation 记录: operation_id -> { created_at, world_id }
_mock_operations = {}
_mock_operations_lock = threading.Lock()
# Mock 模拟生成时间 (秒)
MOCK_GENERATION_TIME = int(os.environ.get("WLT_MOCK_TIME", "15"))

# ============ Flask 应用 ============ ,ml;
app = Flask(__name__)
swagger_config = {
    "headers": [],
    "specs": [{
        "endpoint": "apispec",
        "route": "/apispec.json",
        "rule_filter": lambda rule: True,
        "model_filter": lambda tag: True,
    }],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/",
}
swagger_template = {
    "info": {
        "title": "HumanoidRendererServer API",
        "description": "人形机器人渲染服务器 - 云台控制 + 视频流 + 全景拼接 + 3DGS 生成",
        "version": "1.0.0",
    },
    "tags": [
        {"name": "3DGS", "description": "3D Gaussian Splat 生成流程 (World Labs)"},
        {"name": "云台", "description": "云台控制"},
        {"name": "扫描", "description": "全景扫描拼接"},
    ],
}
swagger = Swagger(app, config=swagger_config, template=swagger_template)


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
    """复位云台
    ---
    tags:
      - 云台
    responses:
      200:
        description: 云台已复位
        schema:
          type: object
          properties:
            yaw:
              type: number
            pitch:
              type: number
    """
    init_gimbal_sync()
    return jsonify({"yaw": current_yaw, "pitch": current_pitch})


@app.route("/gimbal/set", methods=["POST"])
def gimbal_set():
    """设置云台角度
    ---
    tags:
      - 云台
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            yaw:
              type: number
              description: 偏航角 (弧度)
              example: 0.5
            pitch:
              type: number
              description: 俯仰角 (弧度)
              example: 0.0
    responses:
      200:
        description: 当前云台角度
        schema:
          type: object
          properties:
            yaw:
              type: number
            pitch:
              type: number
    """
    global current_yaw, current_pitch
    data = request.get_json(force=True)
    target_yaw = float(data.get("yaw", current_yaw))
    target_pitch = float(data.get("pitch", current_pitch))

    with state_lock:
        current_yaw = target_yaw
        current_pitch = target_pitch
        controller.set_yaw_pitch(current_yaw, current_pitch)

    return jsonify({"yaw": current_yaw, "pitch": current_pitch})


# ============ 3DGS 多图采集 ============

SCAN_FRAME_COUNT = 8  # 扫描帧数 (均匀分布在 -90°~90° 范围)


def capture_multiframe_sequence(n_frames: int = SCAN_FRAME_COUNT):
    """执行多帧水平扫描，用于 multi-image 3DGS 生成
    在 -90°~90° 范围内均匀采集 n_frames 帧 (不传 azimuth，由模型自动推断)。
    返回: [{"img": ndarray, "yaw_deg": float}, ...]
    """
    if controller is None:
        print("[3DGS] 云台未连接，无法执行扫描")
        return []

    captured = []
    # 在 [-90, 90] 范围内均匀分布 n_frames 个角度
    yaw_positions = [
        -90.0 + i * 180.0 / (n_frames - 1) for i in range(n_frames)
    ]

    print(f"[3DGS] 开始 {n_frames} 帧水平扫描 (yaw: {yaw_positions[0]:.0f}°~{yaw_positions[-1]:.0f}°)...")
    init_gimbal_sync()
    time.sleep(1.0)

    for i, yaw_deg in enumerate(yaw_positions):
        yaw_rad = math.radians(yaw_deg)
        set_gimbal_sync(yaw_rad, 0.0)  # pitch 固定 0
        time.sleep(0.6)  # 等待机械稳定

        frame = None
        for _ in range(10):
            frame = get_current_frame_bgr()
            if frame is not None:
                break
            time.sleep(0.02)

        if frame is not None:
            captured.append({
                "img": frame.copy(),
                "yaw_deg": yaw_deg,
            })
            print(f"  -> [{i+1}/{n_frames}] OK (yaw={yaw_deg:.1f}°)")
        else:
            print(f"  -> [{i+1}/{n_frames}] 丢帧 (yaw={yaw_deg:.1f}°)")

    init_gimbal_sync()
    print(f"[3DGS] 扫描完成，采集到 {len(captured)}/{n_frames} 帧")
    return captured


def _encode_frame_to_base64_jpg(frame: np.ndarray, quality: int = 95) -> str:
    """将 BGR ndarray 编码为 base64 JPEG 字符串"""
    success, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not success:
        raise RuntimeError("JPEG 编码失败")
    return base64.b64encode(buffer.tobytes()).decode('utf-8')


def _wlt_headers():
    """World Labs API 请求头"""
    return {
        "WLT-Api-Key": WLT_API_KEY,
        "Content-Type": "application/json",
    }


def _run_3dgs_job_mock(job_id: str, display_name: str, text_prompt: str, model: str):
    """后台线程 (Mock 模式)：模拟扫描 + 提交流程"""
    try:
        # 模拟扫描阶段
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "SCANNING"
        print(f"[3DGS-MOCK] 模拟扫描中... (job={job_id[:8]})")
        time.sleep(3)  # 模拟 3 秒扫描

        # 模拟上传阶段
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "UPLOADING"
        print(f"[3DGS-MOCK] 模拟上传中...")
        time.sleep(2)  # 模拟 2 秒上传

        # 生成模拟 ID
        operation_id = f"mock-op-{uuid.uuid4()}"
        world_id = f"mock-world-{uuid.uuid4()}"
        now = time.time()

        # 记录 mock operation (用于轮询时计算进度)
        with _mock_operations_lock:
            _mock_operations[operation_id] = {
                "created_at": now,
                "world_id": world_id,
                "display_name": display_name or "Mock Scene",
                "model": model,
            }

        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "SUBMITTED"
            _3dgs_jobs[job_id]["operation_id"] = operation_id
            _3dgs_jobs[job_id]["world_id"] = world_id

        print(f"[3DGS-MOCK] 提交成功! op={operation_id[:16]}..., world={world_id[:16]}...")
        print(f"[3DGS-MOCK] 模拟生成将在 {MOCK_GENERATION_TIME} 秒后完成")

    except Exception as e:
        import traceback
        traceback.print_exc()
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "FAILED"
            _3dgs_jobs[job_id]["error"] = str(e)
    finally:
        scan_lock.release()


def _run_3dgs_job_real(job_id: str, display_name: str, text_prompt: str, model: str):
    """后台线程 (真实模式)：扫描 → 编码 → 提交 World Labs 生成"""
    try:
        # --- 阶段 1: 扫描 ---
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "SCANNING"

        frames = capture_multiframe_sequence()
        if not frames:
            with _3dgs_jobs_lock:
                _3dgs_jobs[job_id]["status"] = "FAILED"
                _3dgs_jobs[job_id]["error"] = "扫描失败: 无法采集帧"
            return

        # --- 阶段 2: 编码 + 提交 ---
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "UPLOADING"

        # 构建 multi-image prompt (新版 API 使用嵌套 content + discriminator)
        # azimuth = yaw_deg % 360 (World Labs: 0°=正前, 顺时针)
        multi_image_items = []
        for f in frames:
            b64 = _encode_frame_to_base64_jpg(f["img"])
            multi_image_items.append({
                "azimuth": f["yaw_deg"] % 360,
                "content": {
                    "source": "data_base64",
                    "data_base64": b64,
                    "extension": "jpg",
                },
            })

        body = {
            "world_prompt": {
                "type": "multi-image",
                "multi_image_prompt": multi_image_items,
                "reconstruct_images": True,
            },
            "model": model,
        }
        if display_name:
            body["display_name"] = display_name
        if text_prompt:
            body["world_prompt"]["text_prompt"] = text_prompt

        print(f"[3DGS] 向 World Labs 提交生成请求 (model={model})...")
        resp = http_requests.post(
            f"{WLT_BASE_URL}/worlds:generate",
            headers=_wlt_headers(),
            json=body,
            timeout=30,
        )

        if resp.status_code != 200:
            with _3dgs_jobs_lock:
                _3dgs_jobs[job_id]["status"] = "FAILED"
                _3dgs_jobs[job_id]["error"] = f"World Labs API 错误 {resp.status_code}: {resp.text[:500]}"
            print(f"[3DGS] 提交失败: {resp.status_code} {resp.text[:200]}")
            return

        result = resp.json()
        print(f"[3DGS] API 返回: {str(result)[:500]}")
        operation_id = result.get("operation_id", "")
        metadata = result.get("metadata") or {}
        world_id = metadata.get("world_id", "")

        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "SUBMITTED"
            _3dgs_jobs[job_id]["operation_id"] = operation_id
            _3dgs_jobs[job_id]["world_id"] = world_id

        print(f"[3DGS] 提交成功! operation_id={operation_id}, world_id={world_id}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "FAILED"
            _3dgs_jobs[job_id]["error"] = str(e)
    finally:
        scan_lock.release()


def _run_3dgs_job(job_id: str, display_name: str, text_prompt: str, model: str):
    """后台线程入口：根据 WLT_MOCK 分发到真实/Mock 实现"""
    if WLT_MOCK:
        _run_3dgs_job_mock(job_id, display_name, text_prompt, model)
    else:
        _run_3dgs_job_real(job_id, display_name, text_prompt, model)


# ============ 3DGS API 端点 ============

@app.route("/scan/3dgs", methods=["POST"])
def scan_3dgs():
    """触发 3DGS 生成 (完全异步)
    ---
    tags:
      - 3DGS
    description: |
      触发 8 帧水平扫描 (-90°~90° 均匀分布) → base64 编码 → 提交 World Labs multi-image 生成。
      完全异步执行，立即返回 job_id。不传 azimuth，由模型自动推断视角。

      **完整流程:**
      1. `POST /scan/3dgs` → 获取 job_id
      2. 轮询 `GET /3dgs/job/{job_id}` → 等待状态变为 SUBMITTED，获取 operation_id
      3. 轮询 `GET /3dgs/operation/{operation_id}` → 等待 done=true
      4. `GET /3dgs/world/{world_id}` → 获取资产链接
      5. `GET /3dgs/asset?url=xxx` → 下载 SPZ 文件
    parameters:
      - in: body
        name: body
        required: false
        schema:
          type: object
          properties:
            display_name:
              type: string
              description: World 显示名称
              example: "Scene 001"
            text_prompt:
              type: string
              description: 辅助文本描述 (可留空)
              example: ""
            model:
              type: string
              description: 生成模型
              enum: ["Marble 0.1-plus", "Marble 0.1-mini"]
              default: "Marble 0.1-plus"
    responses:
      200:
        description: 任务已提交
        schema:
          type: object
          properties:
            job_id:
              type: string
              description: 本地任务 ID
      500:
        description: WLT_API_KEY 未配置
      503:
        description: 扫描正在进行中
    """
    if not WLT_API_KEY and not WLT_MOCK:
        return jsonify({"error": "WLT_API_KEY 未配置 (或设置 WLT_MOCK=1 启用 Mock 模式)"}), 500

    if not scan_lock.acquire(blocking=False):
        return jsonify({"error": "扫描正在进行中"}), 503

    data = request.get_json(force=True) if request.data else {}
    display_name = data.get("display_name", "")
    text_prompt = data.get("text_prompt", "")
    model = data.get("model", "Marble 0.1-plus")

    job_id = str(uuid.uuid4())
    with _3dgs_jobs_lock:
        _3dgs_jobs[job_id] = {
            "status": "SCANNING",
            "operation_id": None,
            "world_id": None,
            "error": None,
            "created_at": time.time(),
        }

    # 后台线程执行扫描 + 上传 + 提交
    t = threading.Thread(
        target=_run_3dgs_job,
        args=(job_id, display_name, text_prompt, model),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/3dgs/job/<job_id>", methods=["GET"])
def get_3dgs_job(job_id):
    """查询本地 3DGS 任务状态
    ---
    tags:
      - 3DGS
    description: |
      查询扫描+上传任务的本地进度。
      状态流转: SCANNING → UPLOADING → SUBMITTED / FAILED
    parameters:
      - in: path
        name: job_id
        type: string
        required: true
        description: 从 POST /scan/3dgs 返回的 job_id
    responses:
      200:
        description: 任务状态
        schema:
          type: object
          properties:
            job_id:
              type: string
            status:
              type: string
              enum: [SCANNING, UPLOADING, SUBMITTED, FAILED]
            operation_id:
              type: string
              description: World Labs operation ID (SUBMITTED 后可用)
            world_id:
              type: string
              description: World Labs world ID (SUBMITTED 后可用)
            error:
              type: string
              description: 错误信息 (FAILED 时)
      404:
        description: 任务不存在
    """
    with _3dgs_jobs_lock:
        job = _3dgs_jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "job_id": job_id,
        "status": job["status"],
        "operation_id": job["operation_id"],
        "world_id": job["world_id"],
        "error": job["error"],
    })


@app.route("/3dgs/operation/<operation_id>", methods=["GET"])
def get_3dgs_operation(operation_id):
    """代理 World Labs Operation 轮询
    ---
    tags:
      - 3DGS
    description: |
      透传 World Labs 的 `GET /marble/v1/operations/{operation_id}` 接口。
      当 `done=true` 时生成完成，`response` 字段包含完整 World 数据。
      建议每 5-10 秒轮询一次，标准模式约需 5 分钟。
    parameters:
      - in: path
        name: operation_id
        type: string
        required: true
        description: 从 /3dgs/job 返回的 operation_id
    responses:
      200:
        description: World Labs operation 状态
        schema:
          type: object
          properties:
            operation_id:
              type: string
            done:
              type: boolean
            metadata:
              type: object
              properties:
                progress:
                  type: object
                  properties:
                    status:
                      type: string
                    description:
                      type: string
                world_id:
                  type: string
            response:
              type: object
              description: 生成完成时包含 World 数据
            error:
              type: object
              description: 失败时包含错误详情
    """
    # --- Mock 模式 ---
    if WLT_MOCK:
        with _mock_operations_lock:
            mock_op = _mock_operations.get(operation_id)
        if not mock_op:
            return jsonify({"error": "Operation not found (mock)"}), 404

        elapsed = time.time() - mock_op["created_at"]
        done = elapsed >= MOCK_GENERATION_TIME
        progress_pct = min(100, int(elapsed / MOCK_GENERATION_TIME * 100))

        response_data = {
            "operation_id": operation_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mock_op["created_at"])),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mock_op["created_at"] + 3600)),
            "done": done,
            "error": None,
            "metadata": {
                "progress": {
                    "status": "SUCCEEDED" if done else "IN_PROGRESS",
                    "description": f"Mock: {progress_pct}% complete" if not done else "Mock: World generation completed successfully",
                },
                "world_id": mock_op["world_id"],
            },
            "response": None,
        }
        if done:
            base_url = request.host_url.rstrip("/")
            response_data["response"] = {
                "id": mock_op["world_id"],
                "display_name": mock_op["display_name"],
                "world_marble_url": f"https://marble.worldlabs.ai/world/{mock_op['world_id']}",
                "assets": {
                    "caption": "Mock generated scene for testing purposes.",
                    "thumbnail_url": f"{base_url}/3dgs/mock/thumbnail/{mock_op['world_id']}",
                    "splats": {
                        "spz_urls": {
                            "full_res": f"{base_url}/3dgs/mock/asset/{mock_op['world_id']}?quality=full_res",
                            "500k": f"{base_url}/3dgs/mock/asset/{mock_op['world_id']}?quality=500k",
                            "100k": f"{base_url}/3dgs/mock/asset/{mock_op['world_id']}?quality=100k",
                        }
                    },
                    "mesh": {
                        "collider_mesh_url": f"{base_url}/3dgs/mock/asset/{mock_op['world_id']}?quality=mesh",
                    },
                    "imagery": {
                        "pano_url": f"{base_url}/3dgs/mock/asset/{mock_op['world_id']}?quality=pano",
                    },
                },
                "model": mock_op["model"],
            }
        return jsonify(response_data)

    # --- 真实模式 ---
    if not WLT_API_KEY:
        return jsonify({"error": "WLT_API_KEY 未配置"}), 500

    resp = http_requests.get(
        f"{WLT_BASE_URL}/operations/{operation_id}",
        headers=_wlt_headers(),
        timeout=15,
    )
    return Response(
        resp.content,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "application/json"),
    )


@app.route("/3dgs/world/<world_id>", methods=["GET"])
def get_3dgs_world(world_id):
    """获取 World 详情 (含资产链接)
    ---
    tags:
      - 3DGS
    description: |
      透传 World Labs 的 `GET /marble/v1/worlds/{world_id}` 接口。
      返回包含 SPZ 下载链接、全景图、碰撞网格等资产信息。
    parameters:
      - in: path
        name: world_id
        type: string
        required: true
        description: World ID
    responses:
      200:
        description: World 详情
        schema:
          type: object
          properties:
            world_id:
              type: string
            assets:
              type: object
              properties:
                splats:
                  type: object
                  properties:
                    spz_urls:
                      type: object
                      properties:
                        full_res:
                          type: string
                        500k:
                          type: string
                        100k:
                          type: string
                imagery:
                  type: object
                  properties:
                    pano_url:
                      type: string
                mesh:
                  type: object
                  properties:
                    collider_mesh_url:
                      type: string
    """
    # --- Mock 模式 ---
    if WLT_MOCK:
        # 查找 mock operation 中匹配的 world_id
        mock_info = None
        with _mock_operations_lock:
            for op in _mock_operations.values():
                if op["world_id"] == world_id:
                    mock_info = op
                    break

        base_url = request.host_url.rstrip("/")
        return jsonify({
            "world_id": world_id,
            "display_name": mock_info["display_name"] if mock_info else "Mock World",
            "tags": ["mock", "test"],
            "world_marble_url": f"https://marble.worldlabs.ai/world/{world_id}",
            "assets": {
                "caption": "Mock generated scene for testing purposes.",
                "thumbnail_url": f"{base_url}/3dgs/mock/thumbnail/{world_id}",
                "splats": {
                    "spz_urls": {
                        "full_res": f"{base_url}/3dgs/mock/asset/{world_id}?quality=full_res",
                        "500k": f"{base_url}/3dgs/mock/asset/{world_id}?quality=500k",
                        "100k": f"{base_url}/3dgs/mock/asset/{world_id}?quality=100k",
                    }
                },
                "mesh": {
                    "collider_mesh_url": f"{base_url}/3dgs/mock/asset/{world_id}?quality=mesh",
                },
                "imagery": {
                    "pano_url": f"{base_url}/3dgs/mock/asset/{world_id}?quality=pano",
                },
            },
            "model": mock_info["model"] if mock_info else "Marble 0.1-plus",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mock_info["created_at"])) if mock_info else None,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "permission": {"public": False},
        })

    # --- 真实模式 ---
    if not WLT_API_KEY:
        return jsonify({"error": "WLT_API_KEY 未配置"}), 500

    resp = http_requests.get(
        f"{WLT_BASE_URL}/worlds/{world_id}",
        headers=_wlt_headers(),
        timeout=15,
    )
    return Response(
        resp.content,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "application/json"),
    )


@app.route("/3dgs/asset", methods=["GET"])
def get_3dgs_asset():
    """代理下载资产文件 (SPZ 等)
    ---
    tags:
      - 3DGS
    description: |
      通过服务器代理下载 World Labs CDN 上的资产文件。
      适用于客户端无法直接访问外网的场景。
    parameters:
      - in: query
        name: url
        type: string
        required: true
        description: 资产文件的完整 URL (从 /3dgs/world 返回的 spz_urls 中获取)
    responses:
      200:
        description: 文件流
      400:
        description: 缺少 url 参数
      502:
        description: 下载失败
    """
    asset_url = request.args.get("url")
    if not asset_url:
        return jsonify({"error": "缺少 url 参数"}), 400

    try:
        resp = http_requests.get(asset_url, stream=True, timeout=120)
        return Response(
            resp.iter_content(chunk_size=8192),
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/octet-stream"),
            headers={
                "Content-Disposition": resp.headers.get(
                    "Content-Disposition",
                    'attachment; filename="asset.spz"'
                )
            },
        )
    except Exception as e:
        return jsonify({"error": f"下载失败: {str(e)}"}), 502


# ============ Mock 资产端点 ============

@app.route("/3dgs/mock/asset/<world_id>", methods=["GET"])
def mock_asset_download(world_id):
    """Mock 资产文件下载
    ---
    tags:
      - 3DGS
    description: |
      Mock 模式下的资产文件下载端点。返回一个小型随机二进制文件模拟 SPZ/mesh 等。
      仅在 WLT_MOCK=1 时有效。
    parameters:
      - in: path
        name: world_id
        type: string
        required: true
      - in: query
        name: quality
        type: string
        required: false
        description: "资产质量: full_res, 500k, 100k, mesh, pano"
    responses:
      200:
        description: Mock 文件
      404:
        description: Mock 模式未启用
    """
    if not WLT_MOCK:
        return jsonify({"error": "Mock 模式未启用"}), 404

    quality = request.args.get("quality", "full_res")

    if quality == "pano":
        # 返回一个最小 JPEG (1x1 像素灰图)
        # FF D8 FF E0 ... minimal JPEG
        img = np.zeros((64, 128, 3), dtype=np.uint8)
        img[:] = (128, 160, 200)  # 浅蓝色背景
        cv2.putText(img, "MOCK PANO", (8, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        _, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return Response(
            buf.tobytes(),
            mimetype="image/jpeg",
            headers={"Content-Disposition": f'attachment; filename="mock_pano_{world_id[:8]}.jpg"'},
        )

    # SPZ: 优先从磁盘读取真实 .spz 文件
    if quality != "mesh" and os.path.isfile(WLT_MOCK_SPZ):
        print(f"[3DGS-MOCK] 返回真实 SPZ 文件: {WLT_MOCK_SPZ}")
        return Response(
            open(WLT_MOCK_SPZ, "rb").read(),
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="mock_{quality}_{world_id[:8]}.spz"'},
        )

    # 无真实文件时回退到随机字节
    sizes = {"full_res": 4096, "500k": 2048, "100k": 1024, "mesh": 512}
    data_size = sizes.get(quality, 1024)
    mock_data = os.urandom(data_size)

    ext = "spz" if quality != "mesh" else "glb"
    return Response(
        mock_data,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="mock_{quality}_{world_id[:8]}.{ext}"'},
    )


@app.route("/3dgs/mock/thumbnail/<world_id>", methods=["GET"])
def mock_thumbnail(world_id):
    """Mock 缩略图
    ---
    tags:
      - 3DGS
    description: Mock 模式下返回一个带文字标识的缩略图。
    parameters:
      - in: path
        name: world_id
        type: string
        required: true
    responses:
      200:
        description: Mock JPEG 缩略图
    """
    if not WLT_MOCK:
        return jsonify({"error": "Mock 模式未启用"}), 404

    img = np.zeros((120, 160, 3), dtype=np.uint8)
    img[:] = (60, 60, 60)
    cv2.putText(img, "MOCK", (30, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
    cv2.putText(img, world_id[:8], (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
    _, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return Response(buf.tobytes(), mimetype="image/jpeg")


@app.route("/3dgs/mock/status", methods=["GET"])
def mock_status():
    """查看 Mock 模式状态
    ---
    tags:
      - 3DGS
    description: 返回当前 Mock 模式的配置和活跃 mock operation 信息。
    responses:
      200:
        description: Mock 状态信息
    """
    with _mock_operations_lock:
        ops = []
        for op_id, op in _mock_operations.items():
            elapsed = time.time() - op["created_at"]
            ops.append({
                "operation_id": op_id,
                "world_id": op["world_id"],
                "elapsed_seconds": round(elapsed, 1),
                "done": elapsed >= MOCK_GENERATION_TIME,
            })

    return jsonify({
        "mock_enabled": WLT_MOCK,
        "mock_generation_time_seconds": MOCK_GENERATION_TIME,
        "active_operations": ops,
    })


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
    """全景扫描拼接
    ---
    tags:
      - 扫描
    description: 5×3 网格扫描 → equirectangular 全景拼接 → 返回 JPEG。同步阻塞约 15-20 秒。
    responses:
      200:
        description: JPEG 全景图
        content:
          image/jpeg:
            schema:
              type: string
              format: binary
      500:
        description: 扫描或拼接失败
      503:
        description: 扫描正在进行中
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
    """查询扫描状态
    ---
    tags:
      - 扫描
    responses:
      200:
        description: 扫描状态
        schema:
          type: object
          properties:
            scanning:
              type: boolean
    """
    return jsonify({"scanning": is_scanning})

@app.route("/gimbal/delta", methods=["POST"])
def gimbal_delta():
    """增量调整云台角度
    ---
    tags:
      - 云台
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            delta_yaw:
              type: number
              description: 偏航增量 (弧度)
              example: 0.1
            delta_pitch:
              type: number
              description: 俯仰增量 (弧度)
              example: 0.0
    responses:
      200:
        description: 当前云台角度
        schema:
          type: object
          properties:
            yaw:
              type: number
            pitch:
              type: number
    """
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
    print(f"[Main] Swagger UI: http://localhost:30000/apidocs/")
    if WLT_MOCK:
        print(f"[Main] *** MOCK 模式已启用 *** (模拟生成时间: {MOCK_GENERATION_TIME}s)")
    elif WLT_API_KEY:
        print(f"[Main] World Labs API 已配置 (key: ...{WLT_API_KEY[-4:]})")
    else:
        print("[Main] [Warn] WLT_API_KEY 未设置，3DGS 功能不可用 (设置 WLT_MOCK=1 可启用 Mock)")

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