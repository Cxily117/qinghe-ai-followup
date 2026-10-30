# -*- coding: utf-8 -*-
"""统一启动入口：读 PORT 环境变量（部署平台约定），本地默认 8000。

为什么不用 uvicorn 命令行直接写死端口：
  部署沙箱通过 PORT 环境变量注入端口，且 startCmd 不保证经 shell 展开 $PORT——
  在 Python 里读环境变量是唯一两端都可靠的方式。

用法：
    python run_server.py                       # 本地 8000
    PORT=3000 python run_server.py             # 沙箱/容器注入端口
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # 必须绑 0.0.0.0：容器/沙箱里 127.0.0.1 对外不可达
        port=int(os.environ.get("PORT", "8000")),
    )
