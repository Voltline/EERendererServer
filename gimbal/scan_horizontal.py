import time
import math
from dynamixel_sdk import *

DEVICENAME = "COM3"
BAUDRATE = 57600
PROTOCOL_VERSION = 2.0

DXL_ID = 11 # ID 11的电机为水平方向

ADDR_TORQUE_ENABLE = 64
ADDR_GOAL_POSITION = 116

TORQUE_ENABLE = 1
TORQUE_DISABLE = 0

CENTER = 2048
SWING_RANGE = 800        # 左右摆动幅度（刻度）
PERIOD = 4.0             # 完整来回一次的时间（秒）

portHandler = PortHandler(DEVICENAME)
packetHandler = PacketHandler(PROTOCOL_VERSION)

if not portHandler.openPort():
    raise RuntimeError("无法打开串口")

if not portHandler.setBaudRate(BAUDRATE):
    raise RuntimeError("无法设置波特率")

packetHandler.write1ByteTxRx(
    portHandler, DXL_ID, ADDR_TORQUE_ENABLE, TORQUE_ENABLE
)

print("开始左右往复运动，Ctrl+C 退出")

start_time = time.time()

try:
    while True:
        t = time.time() - start_time

        # 使用正弦函数生成平滑的左右往复运动
        offset = int(SWING_RANGE * math.sin(2 * math.pi * t / PERIOD))
        goal_position = CENTER + offset

        packetHandler.write4ByteTxRx(
            portHandler, DXL_ID, ADDR_GOAL_POSITION, goal_position
        )

        time.sleep(0.02)  # 50 Hz 更新
except KeyboardInterrupt:
    print("停止运动")

packetHandler.write1ByteTxRx(
    portHandler, DXL_ID, ADDR_TORQUE_ENABLE, TORQUE_DISABLE
)
portHandler.closePort()
