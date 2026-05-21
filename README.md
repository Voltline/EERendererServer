# HumanoidRendererServer

面向人形机器人渲染的一体化 Python 服务，整合云台控制、视频流转发、全景扫描拼接与 3DGS 生成。

## 功能概览

- 云台控制：初始化、设置角度、增量调整
- 采集与拼接：基于 ZED 视频流的网格扫描与 equirectangular 全景图生成
- 3DGS 生成：多视角采集并调用 World Labs 生成，支持 Mock 模式
- 视频分发：通过 LiveKit 发布视频轨道
- API 文档：内置 Swagger UI

## 目录结构

```
main.py
server/
	api.py
	config.py
	frame_buffer.py
	gimbal_service.py
	panorama.py
	runtime.py
	state.py
	three_dgs.py
gimbal/
	dynamixel_driver.py
	gimbal_controller.py
	dynamixel_test.py
	scan_horizontal.py
```

## 运行前提

- Python 3.x
- GStreamer (含 gst-launch-1.0)
- ZED 相机及其驱动
- Dynamixel SDK (用于云台)
- LiveKit 服务端 (默认 ws://localhost:7880)

## 配置说明

主要配置集中在 `server/config.py`，以下环境变量可选：

- `WLT_API_KEY`: World Labs API Key
- `WLT_MOCK`: 设为 1/true/yes 启用 Mock 模式
- `WLT_MOCK_SPZ`: Mock 模式下返回的 SPZ 文件路径
- `WLT_MOCK_TIME`: Mock 生成时间(秒)
- `PANORAMA_FOV_SCALE`: 全景拼接 FOV 缩放
- `PANORAMA_BLEND_EXPONENT`: 全景融合权重指数

### .env 配置

服务读取的是进程环境变量，`.env` 不会被自动加载。如果使用 `.env`，请在启动前将其加载到环境变量中。
建议的 `.env` 内容示例：

```
WLT_API_KEY=your_key_here
WLT_MOCK=1
WLT_MOCK_SPZ=./mock.spz
WLT_MOCK_TIME=15
```

默认端口：

- HTTP API: 30000
- GStreamer TCP: 40000
- LiveKit: 7880

## 启动方式

```
python main.py
```

启动后可访问 Swagger UI：

```
http://localhost:30000/apidocs/
```

## LiveKit 启动与 Token

LiveKit 的启动与 Token 生成示例在 `gst_test.sh` 中也有给出。

### 什么时候需要生成 Token

本服务会自行签发 Token 用于连接 LiveKit 并发布视频轨道，因此仅运行服务端时无需手动生成 Token。
当你需要让外部客户端加入房间(例如 Web/桌面查看端、调试订阅端)时，才需要手动生成 Token。
如果暂时没有查看端或订阅端需求，可以跳过 Token 生成步骤。

### 启动与生成示例

1) 启动 LiveKit Server (开发模式)：

```
livekit-server.exe --dev --bind 0.0.0.0
```

2) 生成测试 Token (用于客户端或查看端加入房间)：

```
lk token create --api-key devkey --api-secret secret --join --room my-room --identity test_viewer --valid-for 24h
```

参数说明：

- `--api-key` / `--api-secret` 必须与 `server/config.py` 中的 `API_KEY`、`API_SECRET` 一致
- `--room` 必须与 `ROOM_NAME` 一致
- `--identity` 是客户端身份标识，建议每个连接唯一
- `--valid-for` 是 Token 有效期

服务端发布视频轨道时会自行签发 Token；上面的命令主要用于测试客户端订阅或验证房间连通性。

## API 简要流程

- 云台复位：`GET /init`
- 全景扫描：`GET /scan/panorama`
- 3DGS 生成：`POST /scan/3dgs` → 轮询 `/3dgs/job/{job_id}` → `/3dgs/operation/{operation_id}` → `/3dgs/world/{world_id}` → `/3dgs/asset?url=...`

## 注意事项

- 云台未连接时，扫描与 3DGS 采集会返回空结果或失败
- GStreamer 与 LiveKit 连接失败时会降级运行
- Mock 模式用于离线验证 API 与前端流程
