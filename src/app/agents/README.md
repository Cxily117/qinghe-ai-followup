# agents/ —— Agent 框架

交付清单「Agent 框架」项的代码本体：

- `runtime.py` —— 执行循环：逐步调用工具、逐轮决策留痕
- `rag.py` —— 检索增强问答：强制带 [1][2] 引用编号，无据不作答
- `tools.py` + `gateway/` —— Function Calling 工具注册与大模型供应商适配（可替换供应商）
- `guardrails.py` —— 护栏：拒答、来源清理、改写不叠加、递归脱敏
- `rules.py` —— 规则引擎；`prompts/` —— 提示词版本管理

写操作止步草稿，人工确认后才生效（确认点见 `../api/v1/tasks.py` 与 `../api/v1/agent.py`）。
