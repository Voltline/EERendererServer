import asyncio
import socket
import subprocess
import threading

from livekit import api, rtc

from .api import create_app, run_flask
from .config import (
    API_KEY,
    API_SECRET,
    FRAME_SIZE,
    GST_CMD,
    HEIGHT,
    LIVEKIT_URL,
    MOCK_GENERATION_TIME,
    ROOM_NAME,
    WIDTH,
    WLT_API_KEY,
    WLT_MOCK,
)
from .frame_buffer import set_latest_frame


async def main():
    app = create_app()

    flask_thread = threading.Thread(target=run_flask, args=(app,), daemon=True)
    flask_thread.start()
    print("[Main] Flask API ready at :30000")
    print("[Main] Swagger UI: http://localhost:30000/apidocs/")
    if WLT_MOCK:
        print(f"[Main] *** MOCK 模式已启用 *** (模拟生成时间: {MOCK_GENERATION_TIME}s)")
    elif WLT_API_KEY:
        print(f"[Main] World Labs API 已配置 (key: ...{WLT_API_KEY[-4:]})")
    else:
        print("[Main] [Warn] WLT_API_KEY 未设置，3DGS 功能不可用 (设置 WLT_MOCK=1 可启用 Mock)")

    print("[Main] Launching GStreamer...")
    gst_process = subprocess.Popen(GST_CMD)
    await asyncio.sleep(3)

    if gst_process.poll() is not None:
        print("[Main] GStreamer failed to start.")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", 40000))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print("[Main] GStreamer TCP Connected.")
    except Exception as e:
        print(f"[Main] Connection failed: {e}")
        gst_process.kill()
        return

    print("[Main] Connecting to LiveKit...")
    room = rtc.Room()
    grants = api.VideoGrants(
        room_join=True, room=ROOM_NAME,
        can_publish=True, can_subscribe=True,
    )
    token = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity("zed_publisher")
        .with_name("ZED Camera")
        .with_grants(grants)
        .to_jwt()
    )

    try:
        await room.connect(LIVEKIT_URL, token)
        print(f"[Main] LiveKit Connected: {ROOM_NAME}")
    except Exception as e:
        print(f"[Main] LiveKit connection skipped: {e}")

    source = rtc.VideoSource(WIDTH, HEIGHT)
    track = rtc.LocalVideoTrack.create_video_track("zed_source", source)

    options = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        video_codec=rtc.VideoCodec.H264,
        video_encoding=rtc.VideoEncoding(
            max_framerate=30,
            max_bitrate=6_000_000
        ),
    )

    if room.isconnected:
        print("[Main] Publishing track with options...")
        await room.local_participant.publish_track(track, options)
        print("[Main] Track published!")

    buffer = bytearray()
    print("[Main] Service Running. Waiting for requests...")

    try:
        while True:
            while len(buffer) < FRAME_SIZE:
                chunk = sock.recv(FRAME_SIZE - len(buffer))
                if not chunk:
                    raise RuntimeError("Stream closed")
                buffer.extend(chunk)

            frame_data = buffer[:FRAME_SIZE]
            del buffer[:FRAME_SIZE]

            set_latest_frame(frame_data)

            if room.isconnected:
                frame = rtc.VideoFrame(
                    width=WIDTH, height=HEIGHT,
                    type=rtc.VideoBufferType.I420,
                    data=frame_data,
                )
                source.capture_frame(frame)

            await asyncio.sleep(0)

    except Exception as e:
        print(f"[Main] Loop Error: {e}")
    finally:
        sock.close()
        gst_process.kill()
        await room.disconnect()
