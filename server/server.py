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

@app.route("/init", methods=["GET"])
def init():
    with state_lock:
        controller.set_yaw_pitch(0, 0)
    return jsonify({})

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

if __name__ == "__main__":
    controller.set_yaw_pitch(0, 0)
    app.run(host="0.0.0.0",
            port=30000,
            debug=True,
            use_reloader=False)
