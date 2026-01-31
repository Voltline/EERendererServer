from flask import Flask, request, jsonify
import threading
import math

from gimbal.dynamixel_driver import DynamixelDriver
from gimbal.gimbal_controller import GimbalController

app = Flask(__name__)

# 当前云台状态（弧度）
current_yaw = 0.0
current_pitch = 0.0

state_lock = threading.Lock()

# 初始化你的控制器
device = {
    "device_name": "COM3",
    "baud_rate": 57600,
    "protocol_version": 2,
    "horizontal_id": 11,
    "vertical_id": 10
}

controller = GimbalController(
    DynamixelDriver(device)
)

# 初始化扫描参数设定
YAW_RANGE = math.radians(180)       # 180度
YAW_STEPS = 4                       # 横向4帧
YAW_STEP_ANGLE = math.radians(45)   # 每帧45度
START_YAW = -math.radians(90)       # 从-90度开始扫描（保留10度的余量）

# 纵向三个条带的Pitch参数
# 上(-25°), 下(+25°)
PITCH_LEVELS = {
    "upper": math.radians(-25),
    "lower": math.radians(25)
}

@app.route("/init", methods=["GET"])
def init():
    global current_yaw, current_pitch
    with state_lock:
        current_yaw = 0
        current_pitch = 0
        controller.set_yaw_pitch(0, 0)
    return jsonify({"yaw": current_yaw, "pitch": current_pitch})

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

@app.route("/gimbal/set", methods=["POST"])
def gimbal_set():
    """绝对定位接口：直接跳转到目标角度，并更新全局状态"""
    global current_yaw, current_pitch

    data = request.get_json(force=True)

    # 如果没传，就保持当前值
    target_yaw = float(data.get("yaw", current_yaw))
    target_pitch = float(data.get("pitch", current_pitch))

    with state_lock:
        current_yaw = target_yaw
        current_pitch = target_pitch
        controller.set_yaw_pitch(current_yaw, current_pitch)

    print(f"Set to Absolute: Yaw={current_yaw}, Pitch={current_pitch}")

    return jsonify({
        "yaw": current_yaw,
        "pitch": current_pitch
    })

@app.route("/scan/init/<band>", methods=["GET"])
def init_band(band):
    """将云台移动到条带的起始位置(最左侧)
    :param band: 'upper', 'middle', 'lower'
    """
    global current_yaw, current_pitch
    if band not in PITCH_LEVELS:
        return jsonify({"error": "Invalid band"}), 400

    with state_lock:
        current_yaw = START_YAW
        current_pitch = PITCH_LEVELS[band]
        controller.set_yaw_pitch(current_yaw, current_pitch)

    return jsonify({
        "band": band,
        "start_yaw": current_yaw,
        "pitch": current_pitch,
        "steps": YAW_STEPS,
        "step_angle": YAW_STEP_ANGLE
    })

if __name__ == "__main__":
    controller.set_yaw_pitch(0, 0)
    app.run(host="0.0.0.0",
            port=30000,
            debug=True,
            use_reloader=False)
