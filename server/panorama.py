import math
import time

import cv2
import numpy as np

from .config import (
    ZED_MINI_HD1080_FOV_X,
    ZED_MINI_HD1080_FOV_Y,
    PANORAMA_BLEND_EXPONENT,
    PANORAMA_YAW_DEG_LIST,
    PANORAMA_PITCH_DEG_LIST,
)
from .frame_buffer import get_current_frame_bgr
from .gimbal_service import controller, init_gimbal_sync, set_gimbal_sync
from . import state


def capture_grid_sequence():
    """执行 5x3 的网格扫描"""
    if controller is None:
        print("云台未连接，无法执行物理扫描")
        return []

    captured_frames = []
    yaw_deg_list = PANORAMA_YAW_DEG_LIST
    pitch_deg_list = PANORAMA_PITCH_DEG_LIST

    yaw_rads = [math.radians(d) for d in yaw_deg_list]
    pitch_rads = [math.radians(d) for d in pitch_deg_list]

    print(f"[Capture] 开始采集 {len(pitch_deg_list)}行 x {len(yaw_deg_list)}列...")

    init_gimbal_sync()
    time.sleep(1.0)

    total_count = 0
    for pitch in pitch_rads:
        for yaw in yaw_rads:
            set_gimbal_sync(yaw, pitch)

            time.sleep(0.6)

            frame = None
            for _ in range(10):
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
                print(f"  -> [{total_count+1}] OK (P:{math.degrees(pitch):.1f}, Y:{math.degrees(yaw):.0f})")
            else:
                print(f"  -> [{total_count+1}] 丢帧 (P:{math.degrees(pitch):.1f}, Y:{math.degrees(yaw):.0f})")

            total_count += 1

    init_gimbal_sync()
    return captured_frames


def generate_equirectangular(
        frames,
        pano_w=4096,
        pano_h=2048,
        fov_x=ZED_MINI_HD1080_FOV_X,
        fov_y=ZED_MINI_HD1080_FOV_Y,
):
    """
    极速拼接：Input -> NumPy Vectorization -> cv2.remap -> Output Array
    """
    if not frames:
        return None

    print(
        "[Stitch] 开始生成全景图 "
        f"(FOV={math.degrees(fov_x):.1f}°x{math.degrees(fov_y):.1f}°, "
        f"blend_exp={PANORAMA_BLEND_EXPONENT:.1f})..."
    )
    st = time.time()

    lon = np.linspace(-np.pi, np.pi, pano_w, dtype=np.float32)
    lat = np.linspace(np.pi / 2, -np.pi / 2, pano_h, dtype=np.float32)
    lon_grid, lat_grid = np.meshgrid(lon, lat)

    cos_lat = np.cos(lat_grid)
    sin_lat = np.sin(lat_grid)
    x_world = cos_lat * np.sin(lon_grid)
    y_world = sin_lat
    z_world = cos_lat * np.cos(lon_grid)

    points_world = np.stack((x_world, y_world, z_world), axis=-1).reshape(-1, 3)

    pano_acc = np.zeros((pano_h, pano_w, 3), dtype=np.float32)
    weight_acc = np.zeros((pano_h, pano_w), dtype=np.float32)

    tan_half_fov_x = math.tan(fov_x / 2)
    tan_half_fov_y = math.tan(fov_y / 2)
    for f in frames:
        img = f["img"]
        h_img, w_img = img.shape[:2]
        cam_yaw = f["yaw"]
        cam_pitch = f["pitch"]

        cy, sy = math.cos(-cam_yaw), math.sin(-cam_yaw)
        cp, sp = math.cos(-cam_pitch), math.sin(-cam_pitch)

        Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
        Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], dtype=np.float32)
        R = Rx @ Ry

        points_cam = points_world @ R.T

        pc_x = points_cam[:, 0]
        pc_y = points_cam[:, 1]
        pc_z = points_cam[:, 2]

        pc_z_safe = np.where(pc_z <= 0, 1e-5, pc_z)
        u = - (pc_x / pc_z_safe) / tan_half_fov_x
        v = (pc_y / pc_z_safe) / tan_half_fov_y

        u_grid = u.reshape(pano_h, pano_w)
        v_grid = v.reshape(pano_h, pano_w)

        map_x = ((u_grid * 0.5 + 0.5) * w_img).astype(np.float32)
        map_y = ((0.5 - v_grid * 0.5) * h_img).astype(np.float32)
        pc_z = pc_z.reshape(pano_h, pano_w)

        mask_valid = (pc_z > 0) & (map_x >= 0) & (map_x < w_img) & (map_y >= 0) & (map_y < h_img)

        w_x = np.maximum(0, 1.0 - np.abs(u_grid))
        w_y = np.maximum(0, 1.0 - np.abs(v_grid))
        w_map = np.power(w_x * w_y, PANORAMA_BLEND_EXPONENT)

        final_mask = mask_valid & (w_map > 0)
        w_map = w_map * final_mask.astype(np.float32)

        if np.sum(final_mask) == 0:
            continue

        warped_img = cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

        mask_3ch = np.dstack([w_map] * 3)
        pano_acc += warped_img * mask_3ch
        weight_acc += w_map

    weight_acc[weight_acc == 0] = 1.0
    weight_acc_3ch = np.dstack([weight_acc] * 3)
    pano_final = pano_acc / weight_acc_3ch
    pano_final = np.clip(pano_final, 0, 255).astype(np.uint8)

    cv2.flip(pano_final, 1, pano_final)

    cv2.imwrite("pano_final.jpg", pano_final)

    print(f"[Stitch] 完成! 耗时: {time.time() - st:.3f}s")
    return pano_final


def register_panorama_routes(app):
    from flask import Response, jsonify

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
        if not state.scan_lock.acquire(blocking=False):
            return jsonify({"error": "Scanning in progress"}), 503

        try:
            state.is_scanning = True

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
            state.is_scanning = False
            state.scan_lock.release()

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
        return jsonify({"scanning": state.is_scanning})
