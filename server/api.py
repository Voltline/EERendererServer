from flask import Flask
from flasgger import Swagger

from .gimbal_service import register_gimbal_routes
from .panorama import register_panorama_routes
from .three_dgs import register_3dgs_routes


def create_app():
    app = Flask(__name__)
    swagger_config = {
        "headers": [],
        "specs": [{
            "endpoint": "apispec",
            "route": "/apispec.json",
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/apidocs/",
    }
    swagger_template = {
        "info": {
            "title": "EERendererServer API",
            "description": "EERenderer服务器 - 云台控制 + 视频流 + 全景拼接 + 3DGS 生成",
            "version": "1.0.0",
        },
        "tags": [
            {"name": "3DGS", "description": "3D Gaussian Splat 生成流程 (World Labs)"},
            {"name": "云台", "description": "云台控制"},
            {"name": "扫描", "description": "全景扫描拼接"},
        ],
    }
    Swagger(app, config=swagger_config, template=swagger_template)

    register_gimbal_routes(app)
    register_panorama_routes(app)
    register_3dgs_routes(app)

    return app


def run_flask(app: Flask):
    app.run(host="0.0.0.0", port=30000, debug=False, use_reloader=False, threaded=True)
