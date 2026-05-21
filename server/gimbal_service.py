import threading

from .config import device_config

try:
    from gimbal.dynamixel_driver import DynamixelDriver
    from gimbal.gimbal_controller import GimbalController
except ImportError:
    print("[Warn] 未找到 gimbal 模块，将在无云台模式下运行")
    GimbalController = None

controller = None
if GimbalController:
    try:
        controller = GimbalController(DynamixelDriver(device_config))
        print("[Init] 云台连接成功")
    except Exception as e:
        print(f"[Init] 云台连接失败: {e}")

current_yaw = 0.0
current_pitch = 0.0
state_lock = threading.Lock()


def set_gimbal_sync(yaw: float, pitch: float):
    """同步设置云台角度"""
    global current_yaw, current_pitch
    if controller is None:
        return

    with state_lock:
        current_yaw = yaw
        current_pitch = pitch
        controller.set_yaw_pitch(yaw, pitch)


def init_gimbal_sync():
    """复位云台"""
    global current_yaw, current_pitch
    if controller is None:
        return

    with state_lock:
        current_yaw = 0
        current_pitch = 0
        controller.set_yaw_pitch(0, 0)


def set_yaw_pitch(yaw: float, pitch: float):
    global current_yaw, current_pitch
    with state_lock:
        current_yaw = yaw
        current_pitch = pitch
        controller.set_yaw_pitch(current_yaw, current_pitch)


def apply_delta(delta_yaw: float, delta_pitch: float):
    global current_yaw, current_pitch
    with state_lock:
        current_yaw += delta_yaw
        current_pitch += delta_pitch
        controller.set_yaw_pitch(current_yaw, current_pitch)


def register_gimbal_routes(app):
    from flask import request, jsonify

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
