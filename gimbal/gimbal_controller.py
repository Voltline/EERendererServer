import math
import time
from gimbal.dynamixel_driver import DynamixelDriver


class GimbalController:
    def __init__(
        self,
        driver,
        yaw_center_pos: int = 3000,
        pitch_center_pos: int = 2048,
        yaw_range_rad: float = math.pi,
        pitch_range_rad: float = math.pi / 2,
        yaw_range_ticks: int = 2000,
        pitch_range_ticks: int = 1100,
    ):
        """
        :param driver: DynamixelDriver 实例
        :param yaw_center_pos: yaw 中位
        :param pitch_center_pos: pitch 中位
        :param yaw_range_rad: yaw 最大角度范围（弧度，±）
        :param pitch_range_rad: pitch 最大角度范围（弧度，±）
        :param yaw_range_ticks: yaw 对应的 Dynamixel 刻度范围（±）
        :param pitch_range_ticks: pitch 对应的 Dynamixel 刻度范围（±）
        """
        self.driver = driver

        self.yaw_center_pos = yaw_center_pos
        self.pitch_center_pos = pitch_center_pos

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
            self.yaw_center_pos
            + yaw_rad / self.yaw_range_rad * self.yaw_range_ticks
        )

        pitch_pos = int(
            self.pitch_center_pos
            + pitch_rad / self.pitch_range_rad * self.pitch_range_ticks
        )

        yaw_pos = self._clamp(yaw_pos, 2000, 4000)
        pitch_pos = self._clamp(pitch_pos, 1800, 2900)

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
    time.sleep(2)

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