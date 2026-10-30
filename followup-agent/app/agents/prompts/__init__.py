"""提示词模板（★ 阶段 1 核心模块 ⑥ 的迁移目标）。

为什么不把提示词写在 Python 字符串里：
  制度问答的提示词是要被「审核」的资产——医务处/质控要看它到底约束了模型什么。
  存成带版本号的 markdown：① 变更可 diff、可评审；② 回答的 meta 里记 version，
  事后能证明「当时用的是哪一版护栏」。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    text: str


@lru_cache(maxsize=16)
def load_prompt(name: str = "policy_qa") -> PromptTemplate:
    """读取 prompts/<name>.md，解析文件头部的 `---` 元数据块。

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
