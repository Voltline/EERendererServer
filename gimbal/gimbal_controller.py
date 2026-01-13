import math
import time
from gimbal.dynamixel_driver import DynamixelDriver


class GimbalController:
    def __init__(
        self,
        driver,
        center_pos: int = 2048,
        yaw_range_rad: float = math.pi / 2,
        pitch_range_rad: float = math.pi / 4,
        yaw_range_ticks: int = 1024,
        pitch_range_ticks: int = 512,
    ):
        """
        :param driver: DynamixelDriver 实例
        :param center_pos: 云台中位（通常是 2048）
        :param yaw_range_rad: yaw 最大角度范围（弧度，±）
        :param pitch_range_rad: pitch 最大角度范围（弧度，±）
        :param yaw_range_ticks: yaw 对应的 Dynamixel 刻度范围（±）
        :param pitch_range_ticks: pitch 对应的 Dynamixel 刻度范围（±）
        """
        self.driver = driver

        self.center_pos = center_pos

        self.yaw_range_rad = yaw_range_rad
        self.pitch_range_rad = pitch_range_rad

        self.yaw_range_ticks = yaw_range_ticks
        self.pitch_range_ticks = pitch_range_ticks

    def __del__(self):
        self.driver.close()

    def _clamp(self, value, min_value, max_value):
        return max(min_value, min(max_value, value))

    def set_yaw_pitch(self, yaw_rad: float, pitch_rad: float):
        """
        根据 yaw / pitch（弧度）驱动云台
        """

        yaw_rad = self._clamp(
            yaw_rad, -self.yaw_range_rad, self.yaw_range_rad
        )
        pitch_rad = self._clamp(
            pitch_rad, -self.pitch_range_rad, self.pitch_range_rad
        )

        yaw_pos = int(
            self.center_pos
            + yaw_rad / self.yaw_range_rad * self.yaw_range_ticks
        )

        pitch_pos = int(
            self.center_pos
            + pitch_rad / self.pitch_range_rad * self.pitch_range_ticks
        )

        yaw_pos = self._clamp(yaw_pos, 0, 4095)
        pitch_pos = self._clamp(pitch_pos, 0, 4095)

        self.driver.set_position(
            horizontal_pos=yaw_pos,
            vertical_pos=pitch_pos
        )

if __name__ == "__main__":
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
    controller.set_yaw_pitch(0, 0)

    amplitude = math.pi  # 左右最大 ±45°
    period = 4.0  # 4 秒一个完整来回
    dt = 0.02  # 50 Hz 更新

    t = 0.0
    try:
        while True:
            yaw = amplitude * math.sin(2 * math.pi * t / period)
            controller.set_yaw_pitch(yaw, 0)

            t += dt
    except KeyboardInterrupt:
        print("停止运动")