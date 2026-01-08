gst-launch-1.0 -e -v `
  zedsrc camera-resolution=1 camera-fps=30 stream-type=0 ! `
  videoconvert ! `
  video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1 ! `
  x264enc bitrate=5000 speed-preset=ultrafast tune=zerolatency key-int-max=30 bframes=0 sliced-threads=false ! `
  video/x-h264,profile=constrained-baseline,stream-format=byte-stream ! `
  h264parse config-interval=1 ! `
  tcpserversink host=0.0.0.0 port=16400 recover-policy=keyframe sync-method=latest-keyframe