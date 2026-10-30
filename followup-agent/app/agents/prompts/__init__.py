"""提示词模板的「文件兜底」与「出厂默认值」。

阶段 3 起提示词的主存储是数据库（prompt_templates 表），管理员可在后台在线编辑。
本模块只保留两件事：
  1. 出厂默认正文（首次启动写入 v1 用）；
  2. 读文件兜底 —— 数据库不可用、或某个模板还没种子数据时，问答不能因此断掉。
    「宁可回退到上一版固定文本，也不要拿空提示词去问模型」是这里的底线。

为什么仍然保留带版本号的 markdown 文件：
  代码评审（diff）看 md 比看数据库行直观，且新环境冷启动要有初始正文。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent

# 首次初始化时写入数据库的出厂正文（需求给定的默认模板）
DEFAULT_POLICY_QA = """你是院内随访规范助手，只能依据检索到的随访制度条文作答；制度无对应内容时必须明确回复「随访规范中未查询到相关内容」；回答末尾自动附上引用文档来源，严禁编造随访标准与诊疗建议。"""

# 目前受管的模板：key 与 prompts/<key>.md 同名
MANAGED_PROMPTS = ("policy_qa", "task_extract")

# 各模板的「关键约束」——保存时缺少这些语义就直接拒绝，见 prompt_service.validate
REQUIRED_KEYWORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    # policy_qa：必须约束「引用」与「拒答」，这两条是合规的根
    "policy_qa": (("引用",), ("未查询到", "未涉及", "超出", "未收录", "无法回答", "暂无")),
    "task_extract": (("草稿",), ("offset_days", "天数")),
}


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    text: str


@lru_cache(maxsize=16)
def load_prompt(name: str = "policy_qa") -> PromptTemplate:
    """读取 prompts/<name>.md，解析文件头部的 `---` 元数据块（仅作兜底）。

    文件格式约定：
        ---
        version: v1
        updated: 2026-10-30
        ---
        <系统提示词正文>
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"提示词模板不存在：{path}")

    raw = path.read_text(encoding="utf-8")
    version = "v0"

    if raw.startswith("---"):
        _, _, rest = raw.partition("---")
        meta_block, _, body = rest.partition("---")
        for line in meta_block.strip().splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "version":
                version = value.strip()
        raw = body.strip()

    return PromptTemplate(name=name, version=version, text=raw.strip())


def default_text(name: str) -> str:
    """取该模板的出厂正文：policy_qa 用需求给定的默认模板，其余读 md。"""
    if name == "policy_qa":
        return DEFAULT_POLICY_QA
    try:
        return load_prompt(name).text
    except FileNotFoundError:
        return ""
