import asyncio
import subprocess
import socket
from livekit import api, rtc

URL = "ws://localhost:7880"
API_KEY = "devkey"
API_SECRET = "secret"
ROOM_NAME = "my-room"

WIDTH = 1920
HEIGHT = 1080

# I420 = Y plane (W*H) + U (W*H/4) + V (W*H/4) = W*H*3/2
FRAME_SIZE = WIDTH * HEIGHT * 3 // 2

gst_exe = r"C:/Program Files/gstreamer/1.0/msvc_x86_64/bin/gst-launch-1.0"

GST_CMD = [
    gst_exe, "-q",
    "zedsrc", "camera-resolution=1", "camera-fps=30", "stream-type=0", "!",
    "videoconvert", "!",
    f"video/x-raw,format=I420,width={WIDTH},height={HEIGHT},framerate=30/1", "!",
    "tcpserversink", "host=127.0.0.1", "port=8888", "sync=false"
]

async def main():
    gst_process = subprocess.Popen(GST_CMD)
    await asyncio.sleep(2)

    if gst_process.poll() is not None:
        print("错误：GStreamer 启动失败")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", 8888))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    room = rtc.Room()

    grants = api.VideoGrants(
        room_join=True,
        room=ROOM_NAME,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_publish_sources=["camera"],  # 明确允许发布 camera
    )

    token = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity("windows_zed_publisher")
        .with_name("ZED Camera")
        .with_grants(grants)
        .to_jwt()
    )

    await room.connect(URL, token)
    print(f"joined room: {ROOM_NAME}")

    source = rtc.VideoSource(WIDTH, HEIGHT)
    track = rtc.LocalVideoTrack.create_video_track("zed_i420", source)

    options = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        simulcast=False,

        # 强制 H.264
        video_codec=rtc.VideoCodec.H264,

        video_encoding=rtc.VideoEncoding(
            max_framerate=30,
            max_bitrate=10000000,  # 10Mbps
        ),
    )

    publication = await room.local_participant.publish_track(track, options)

    print("publishing...")

    buffer = bytearray()

    try:
        while True:
            while len(buffer) < FRAME_SIZE:
                chunk = sock.recv(FRAME_SIZE - len(buffer))
                if not chunk:
                    raise RuntimeError("GStreamer 数据流中断")
                buffer.extend(chunk)

            frame_data = buffer[:FRAME_SIZE]
            del buffer[:FRAME_SIZE]

            frame = rtc.VideoFrame(
                width=WIDTH,
                height=HEIGHT,
                type=rtc.VideoBufferType.I420,
                data=frame_data,
            )
            source.capture_frame(frame)

            await asyncio.sleep(0)

    finally:
        sock.close()
        gst_process.kill()
        await room.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
