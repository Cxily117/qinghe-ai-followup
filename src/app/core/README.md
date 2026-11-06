# core/ —— 工程横切与权限

- `deps.py` —— **权限闸门**：`require_admin` 依赖注入，管理接口强制 admin 角色，staff 越权返回 403
- `security.py` —— JWT 签发与校验
- `config.py` —— 配置与启动自检（生产环境密钥检查）
- `ratelimit.py` —— 接口限流（滑动窗口）

角色定义在 `../models/user.py` 的 `role` 字段：`admin`（管理员）/ `staff`（普通员工）。
权限实测证据：冒烟测试权限场景 49 条断言（见仓库顶层 `test/`）。
