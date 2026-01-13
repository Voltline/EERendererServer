import dynamixel_sdk as dxsdk

# 常量定义
ADDR_TORQUE_ENABLE = 64
ADDR_GOAL_POSITION = 116

TORQUE_ENABLE = 1
TORQUE_DISABLE = 0

# DynamixelDriver类
# 用于与底层设备和驱动
class DynamixelDriver:
    def __init__(self, dxl_device: dict):
        """Dynamixel驱动初始化
        :param dxl_device: 设备基本信息字典
        """
        self.dxl_device = dxl_device
        # 设备名称(串口)
        self.device_name = self.dxl_device["device_name"]
        # 设备波特率
        self.baud_rate = self.dxl_device["baud_rate"]
        # Dynamixel协议版本号
        self.protocol_version = self.dxl_device["protocol_version"]
        # 水平方向电机 ID
        self.horizontal_id = self.dxl_device["horizontal_id"]
        # 垂直方向电机 ID
        self.vertical_id = self.dxl_device["vertical_id"]
        # DynamixelSDK所需的PortHandler
        self.port_handler = dxsdk.PortHandler(self.device_name)
        # DynamixelSDK所需的PacketHandler
        self.packet_handler = dxsdk.PacketHandler(self.protocol_version)

        # 初始化时尝试连接设备
        self._connect()
        self._enable_torque()

    def _connect(self):
        if not self.port_handler.openPort():
            raise RuntimeError(f"无法打开设备{self.device_name}的串口")

        if not self.port_handler.setBaudRate(self.baud_rate):
            raise RuntimeError(f"无法设置设备{self.device_name}的波特率为{self.baud_rate}")

    def _enable_torque(self):
        for dxl_id in (self.horizontal_id, self.vertical_id):
            if dxl_id is None:
                continue

            dxl_comm_result, dxl_error = self.packet_handler.write1ByteTxRx(
                self.port_handler,
                dxl_id,
                ADDR_TORQUE_ENABLE,
                TORQUE_ENABLE
            )

            if dxl_comm_result != dxsdk.COMM_SUCCESS:
                raise RuntimeError(f"ID {dxl_id} 通信失败")
            if dxl_error != 0:
                raise RuntimeError(f"ID {dxl_id} 电机返回错误")

    def set_position(self, horizontal_pos: int = None, vertical_pos: int = None):
        if horizontal_pos is not None:
            self.packet_handler.write4ByteTxRx(
                self.port_handler,
                self.horizontal_id,
                ADDR_GOAL_POSITION,
                int(horizontal_pos)
            )

        if vertical_pos is not None:
            self.packet_handler.write4ByteTxRx(
                self.port_handler,
                self.vertical_id,
                ADDR_GOAL_POSITION,
                int(vertical_pos)
            )

    def close(self):
        for dxl_id in (self.horizontal_id, self.vertical_id):
            if dxl_id is None:
                continue

            self.packet_handler.write1ByteTxRx(
                self.port_handler,
                dxl_id,
                ADDR_TORQUE_ENABLE,
                TORQUE_DISABLE
            )

        self.port_handler.closePort()

if __name__ == "__main__":
    device = {
        "device_name": "COM3",
        "baud_rate": 57600,
        "protocol_version": 2,
        "horizontal_id": 11,
        "vertical_id": 10
    }
    driver = DynamixelDriver(device)