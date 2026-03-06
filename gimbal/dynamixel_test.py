from dynamixel_sdk import *  # 官方 Dynamixel SDK

# ===== 基本参数（与 OpenTeleVision 完全一致）=====
DEVICENAME = "COM3"
BAUDRATE = 57600
PROTOCOL_VERSION = 2.0

DXL_IDS = [10, 11]

# ===== Control Table =====
ADDR_TORQUE_ENABLE    = 64
ADDR_GOAL_POSITION    = 116
ADDR_PRESENT_POSITION = 132

TORQUE_ENABLE  = 1
TORQUE_DISABLE = 0

# ===== 初始化通信 =====
portHandler = PortHandler(DEVICENAME)
packetHandler = PacketHandler(PROTOCOL_VERSION)

if not portHandler.openPort():
    raise RuntimeError("无法打开串口")

if not portHandler.setBaudRate(BAUDRATE):
    raise RuntimeError("无法设置波特率")

# ===== 使能电机 =====
for dxl_id in DXL_IDS:
    dxl_comm_result, dxl_error = packetHandler.write1ByteTxRx(
        portHandler, dxl_id, ADDR_TORQUE_ENABLE, TORQUE_ENABLE
    )
    if dxl_comm_result != COMM_SUCCESS:
        print(f"ID {dxl_id} 通信失败")
    elif dxl_error != 0:
        print(f"ID {dxl_id} 电机错误")
    else:
        print(f"ID {dxl_id} 已使能")

# ===== 写一个测试位置 =====
# 4096 = 一圈
goal_positions = {
    10: 2048,
    11: 3072,
}

for dxl_id, pos in goal_positions.items():
    packetHandler.write4ByteTxRx(
        portHandler, dxl_id, ADDR_GOAL_POSITION, pos
    )

input("电机已运动，按回车退出...")

# ===== 关闭电机 =====
for dxl_id in DXL_IDS:
    packetHandler.write1ByteTxRx(
        portHandler, dxl_id, ADDR_TORQUE_ENABLE, TORQUE_DISABLE
    )

portHandler.closePort()
