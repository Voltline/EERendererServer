import base64
import math
import os
import threading
import time
import uuid

import cv2
import numpy as np
import requests as http_requests

from .config import (
    SCAN_FRAME_COUNT,
    WLT_API_KEY,
    WLT_BASE_URL,
    WLT_MOCK,
    WLT_MOCK_SPZ,
    MOCK_GENERATION_TIME,
)
from .frame_buffer import get_current_frame_bgr
from .gimbal_service import controller, init_gimbal_sync, set_gimbal_sync
from . import state

_3dgs_jobs = {}
_3dgs_jobs_lock = threading.Lock()

_mock_operations = {}
_mock_operations_lock = threading.Lock()


def capture_multiframe_sequence(n_frames: int = SCAN_FRAME_COUNT):
    """执行多帧水平扫描，用于 multi-image 3DGS 生成
    在 -90°~90° 范围内均匀采集 n_frames 帧 (不传 azimuth，由模型自动推断)。
    返回: [{"img": ndarray, "yaw_deg": float}, ...]
    """
    if controller is None:
        print("[3DGS] 云台未连接，无法执行扫描")
        return []

    captured = []
    yaw_positions = [
        -90.0 + i * 180.0 / (n_frames - 1) for i in range(n_frames)
    ]

    print(f"[3DGS] 开始 {n_frames} 帧水平扫描 (yaw: {yaw_positions[0]:.0f}°~{yaw_positions[-1]:.0f}°)...")
    init_gimbal_sync()
    time.sleep(1.0)

    for i, yaw_deg in enumerate(yaw_positions):
        yaw_rad = math.radians(yaw_deg)
        set_gimbal_sync(yaw_rad, 0.0)
        time.sleep(0.6)

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
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "SCANNING"
        print(f"[3DGS-MOCK] 模拟扫描中... (job={job_id[:8]})")
        time.sleep(3)

        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "UPLOADING"
        print("[3DGS-MOCK] 模拟上传中...")
        time.sleep(2)

        operation_id = f"mock-op-{uuid.uuid4()}"
        world_id = f"mock-world-{uuid.uuid4()}"
        now = time.time()

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
        state.scan_lock.release()


def _run_3dgs_job_real(job_id: str, display_name: str, text_prompt: str, model: str):
    """后台线程 (真实模式)：扫描 → 编码 → 提交 World Labs 生成"""
    try:
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "SCANNING"

        frames = capture_multiframe_sequence()
        if not frames:
            with _3dgs_jobs_lock:
                _3dgs_jobs[job_id]["status"] = "FAILED"
                _3dgs_jobs[job_id]["error"] = "扫描失败: 无法采集帧"
            return

        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id]["status"] = "UPLOADING"

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
        state.scan_lock.release()


def _run_3dgs_job(job_id: str, display_name: str, text_prompt: str, model: str):
    """后台线程入口：根据 WLT_MOCK 分发到真实/Mock 实现"""
    if WLT_MOCK:
        _run_3dgs_job_mock(job_id, display_name, text_prompt, model)
    else:
        _run_3dgs_job_real(job_id, display_name, text_prompt, model)


def register_3dgs_routes(app):
    from flask import request, Response, jsonify

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

        if not state.scan_lock.acquire(blocking=False):
            return jsonify({"error": "扫描正在进行中"}), 503

        data = request.get_json(force=True) if request.data else {}
        display_name = data.get("display_name", "")
        text_prompt = data.get("text_prompt", "")
        model = data.get("model", "marble-1.0")

        job_id = str(uuid.uuid4())
        with _3dgs_jobs_lock:
            _3dgs_jobs[job_id] = {
                "status": "SCANNING",
                "operation_id": None,
                "world_id": None,
                "error": None,
                "created_at": time.time(),
            }

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
        if WLT_MOCK:
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
            img = np.zeros((64, 128, 3), dtype=np.uint8)
            img[:] = (128, 160, 200)
            cv2.putText(img, "MOCK PANO", (8, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            _, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return Response(
                buf.tobytes(),
                mimetype="image/jpeg",
                headers={"Content-Disposition": f'attachment; filename="mock_pano_{world_id[:8]}.jpg"'},
            )

        if quality != "mesh" and os.path.isfile(WLT_MOCK_SPZ):
            print(f"[3DGS-MOCK] 返回真实 SPZ 文件: {WLT_MOCK_SPZ}")
            return Response(
                open(WLT_MOCK_SPZ, "rb").read(),
                mimetype="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="mock_{quality}_{world_id[:8]}.spz"'},
            )

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
