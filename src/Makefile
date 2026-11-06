# 常用命令的唯一入口：新人不用记一堆参数，CI 与本地跑的是同一条命令。
# 用法：make help
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python.exe
else
PY := .venv/bin/python
endif

.PHONY: help install lint fmt test cov check migrate run smoke archive

help:  ## 列出所有可用命令
	@grep -E "^[a-z-]+:.*##" $(MAKEFILE_LIST) | sed "s/:.*##/\t/" | sort

install:  ## 安装运行与开发依赖
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

lint:  ## 静态检查（不修改文件）
	$(PY) -m ruff check .

fmt:  ## 自动修可修复的问题并格式化
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

test:  ## 单元测试（不依赖模型与网络）
	$(PY) -m pytest

cov:  ## 单元测试 + 覆盖率
	$(PY) -m pytest --cov --cov-report=term-missing

check: lint test  ## CI 等价：lint + 单测
	$(PY) -m ruff format --check .

migrate:  ## 建表/升级到最新版本
	$(PY) -m alembic upgrade head

run:  ## 启动服务（读 .env）
	$(PY) run_server.py

smoke:  ## 端到端冒烟（需先 make run；会真调模型）
	$(PY) scripts/smoke_test.py --base-url http://127.0.0.1:8000

archive:  ## 审计数据冷归档预演（加 --apply 才真删）
	$(PY) scripts/archive_audit.py --days 180
