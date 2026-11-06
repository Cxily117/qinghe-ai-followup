# 变更记录

版本号遵循语义化版本：`主.次.修`。每次发布前把 `[Unreleased]` 的内容归入新版本，
并同步 `app/main.py` 里的 `FastAPI(version=...)`。

## [Unreleased]

### 测试补强（覆盖率 25% → 56%）
- 新增 `tests/conftest.py`：真库夹具（临时 SQLite + `Base.metadata.create_all`），
  此前 5 个测试文件全是纯函数，service 层的 SQL 逻辑（去重窗口、版本号、软撤回）无人覆盖
- `tests/test_task_service.py`（14 例）：任务状态机 draft→confirmed/cancelled、
  确认时改节点按同一出院日重算、已确认不可取消、模型编造住院号被拒、24h 去重、越权不可见
- `tests/test_audit_service.py`（10 例）：脱敏**回读数据库**验证（入参/问题原文/工具结果落库后
  查不到住院号-手机号-身份证）、业务正文不脱敏的对照项、标记去重与软撤回、审计列表筛选与分页
- `tests/test_knowledge_service.py`（10 例）：切片合并/滑窗、版本号 +1、发布后旧版自动归档、
  draft 不可检索 → published 可检索 → archived 又退出检索（用 `rag.retrieve` 真跑）
- `tests/test_api_flows.py`（9 例）：鉴权 401/403 区分、客户端塞 `status=confirmed` 被忽略、
  医护越权访问他人草稿 404、知识库发布限管理员、审计标记与质控报表、mock 全链路问答落库
- 覆盖率闸门：`pyproject.toml` 的 `fail_under = 55` + CI `--cov-fail-under=55`

### 修复
- **任务去重标记写库丢失**：`generate_draft` 复用草稿时用 `meta.setdefault("ai", {})["reused"] = True`
  原地改嵌套字典，SQLAlchemy 比对 JSON 新旧值相等 → 不发 UPDATE，`reused` 从未落库
  （由 `test_generate_draft_dedupes_within_24h` 捕获，改为新建 ai 字典）

### 工程规范化
- 引入 ruff 统一 lint 与格式化，规则集写入 `pyproject.toml`（启用 `E/W/F/I/UP/B/BLE/ISC/C4/RUF`，
  豁免中文全角标点、行宽、FastAPI `Depends` 默认参数三条）
- 全仓格式化一次（67 个文件，纯排版，无逻辑变更），此后 `ruff format --check` 进 CI
- 新增 54 个单元测试：规则引擎、输出护栏、PHI 脱敏、令牌校验、限流窗口、生产配置自检
- 新增 `Makefile`（`make check` = lint + 单测 + 格式校验）、`.pre-commit-config.yaml`、
  `.editorconfig`、`.github/workflows/ci.yml`
- 依赖拆分为 `requirements.txt`（运行）与 `requirements-dev.txt`（开发）

### 修复
- 审计表 `result_digest` 只掩码了住院号，**嵌套结构里的患者姓名仍是明文**
  （`{"patient": {"name": "张伟"}}`），改为递归脱敏
- 消除 13 处 `except` 内 `raise` 未声明来源（`raise ... from`），异常链不再断裂
- 修正 2 处隐式字符串拼接、2 处分号多语句、2 个未使用变量

## [0.7.0] - 2026-11-05

### 新增
- 患者目录落库（`patients` 表 + 迁移 `0005`），`resolve_patient` 改为查库，
  后台新增患者目录页（在册数/来源分布/搜索/录入/停用）
- 自助改密码与管理员重置他人密码（`POST /auth/password`、`POST /admin/users/{id}/password`）
- 应用层限流（`app/core/ratelimit.py`）：登录 20/min·IP、问答 30/min·用户、智能体 15/min·用户
- 管理后台入口在顶栏按角色显示
- PWA 支持（manifest + 图标 + favicon）

### 安全
- JWT 密钥轮换为 64 位随机值，令牌有效期 720 → 120 分钟
- 问答端与后台 Cookie 均带 `Secure`；`APP_ENV=prod` 下默认密钥/超长有效期拒绝启动
- 运行记录的问题原文入库前掩码（住院号/手机号/身份证）
- 审计日志冷归档脚本（`scripts/archive_audit.py`）
- 清理 91 个测试账号（含 23 个公网可达的管理员）

### 修复
- 托管平台网关覆盖 `Authorization` 头导致全站 401 → 令牌按
  `Authorization → Cookie → X-Auth-Token` 三路尝试，登录额外下发 HttpOnly Cookie
- 回答永远无引用：提示词只要求"附来源名称"而引用识别只认 `[1][2]`
- 模型自写「引用文档来源：- 《某规范》」列表被清理，改为挂真实命中条文
- 一句话短答案（<60 字）命中资料却没有来源卡片
