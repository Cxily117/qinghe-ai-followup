# models/ —— 数据模型（含问答助手归档）

- `chat.py` —— **问答助手归档**：会话（sessions）/ 消息（messages）全量落库，含 citations 引用元数据
- `audit.py` —— 审计留痕表（入库前递归脱敏住院号/姓名）
- `kb.py` —— **知识库**：制度文档 + 分块（向量 1024 维）
- `user.py` —— 用户表，`role` 字段定义 admin / staff 双角色
- 其余：任务、随访、患者目录等共 12 张表

归档脚本：`../../scripts/archive_audit.py`（gzip + sha256 + manifest，先校验再删）。
