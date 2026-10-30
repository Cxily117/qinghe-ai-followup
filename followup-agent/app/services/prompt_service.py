"""提示词服务：读生效版本、保存新版本、回滚、出厂种子。

三条硬规则（比"能编辑"重要得多）：

1）**每次问答都从库里读**，不做进程内长缓存。
   多 worker 部署时，任何缓存都会出现"A 进程已改、B 进程还在用旧版"，
   而需求是"修改后立即生效"。这里是一次 (name, is_active) 索引查询，
   相对一次模型调用（几百毫秒起）成本可忽略，换来的是行为可预测。

2）**保存即新增版本，永不原地改**。
   历史正文是追责材料。回滚也走"复制旧正文成新版本"，
   否则版本号会倒退，事后无法解释"为什么 v3 又生效了"。

3）**库不可用时回退到文件默认版**，并把 source 标成 file_fallback。
   宁可让管理员在页面上看到"正在使用兜底模板"的黄色提示，
   也不能把空字符串当系统提示词发出去——那等于把模型的护栏全撤了。
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import (
    MANAGED_PROMPTS,
    REQUIRED_KEYWORDS,
    default_text,
    load_prompt,
)
from app.core.errors import AppError
from app.models.prompt import PromptTemplate

logger = logging.getLogger(__name__)

PROMPT_MIN_LEN = 80  # 短于此长度基本等于没写规则，拒绝保存
PROMPT_MAX_LEN = 8000  # 超过这个长度会挤掉【资料】块，模型反而容易忽略制度原文


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class PromptSnapshot:
    """问答时实际使用的提示词快照（不可变，便于直接塞进 meta）。"""

    __slots__ = ("name", "version", "text", "sha", "source", "updated_at")

    def __init__(self, name: str, version: int, text: str, sha: str, source: str, updated_at=None):
        self.name = name
        self.version = version
        self.text = text
        self.sha = sha
        self.source = source
        self.updated_at = updated_at

    @property
    def version_tag(self) -> str:
        """写进 messages.meta 的版本标识，形如 db:v3#a1b2c3d4e5f6。

        带上正文指纹而不是只写 v3：万一有人直接改库把 v3 的正文换了，
        版本号一样但哈希不同，能立刻看出"这条回答依据的正文和现在库里的不一致"。
        """
        prefix = "db" if self.source == "db" else "file"
        return f"{prefix}:v{self.version}#{self.sha}"

    def __repr__(self) -> str:  # 日志里好读
        return f"<PromptSnapshot {self.version_tag} len={len(self.text)}>"


async def get_active(db: AsyncSession, name: str = "policy_qa") -> PromptSnapshot:
    """取当前生效版本；库里没有就回退文件默认版（并记 warning）。"""
    row = (
        await db.execute(
            select(PromptTemplate)
            .where(PromptTemplate.name == name, PromptTemplate.is_active.is_(True))
            .order_by(PromptTemplate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is not None and len(row.content.strip()) >= 20:
        return PromptSnapshot(
            name=name,
            version=row.version,
            text=row.content.strip(),
            sha=row.content_sha or _sha(row.content),
            source="db",
            updated_at=row.created_at,
        )

    # 兜底：库里没有 / 正文异常短 → 用文件版，绝不拿空提示词去调模型
    try:
        fallback = load_prompt(name)
    except FileNotFoundError:
        text = default_text(name)
        if not text:
            raise AppError("PROMPT_NOT_FOUND")
        logger.warning("提示词 %s 无库内版本且无 md 文件，使用内置默认正文", name)
        return PromptSnapshot(name, 1, text, _sha(text), "file_fallback")

    logger.warning("提示词 %s 无可用库内版本，回退文件版 %s", name, fallback.version)
    digits = "".join(ch for ch in fallback.version if ch.isdigit()) or "0"
    return PromptSnapshot(name, int(digits), fallback.text, _sha(fallback.text), "file_fallback")


def validate(name: str, content: str) -> str:
    """保存前校验。返回清洗后的正文；不合规直接抛 AppError（页面会原样展示原因）。"""
    if name not in MANAGED_PROMPTS:
        raise AppError("PROMPT_NAME_INVALID", f"不支持的模板名：{name}")

    text = (content or "").strip()
    if len(text) < PROMPT_MIN_LEN:
        raise AppError("PROMPT_TOO_SHORT")
    if len(text) > PROMPT_MAX_LEN:
        raise AppError("PROMPT_TOO_LONG")

    # 关键约束缺失就拒绝保存：管理员可能只想删掉一句"多余"的话，
    # 但删掉的恰好是"必须拒答"或"必须标引用"，线上问答会立刻开始编造。
    missing: list[str] = []
    for group in REQUIRED_KEYWORDS.get(name, ()):
        if not any(kw in text for kw in group):
            missing.append(" / ".join(group))
    if missing:
        raise AppError(
            "PROMPT_MISSING_RULE",
            f"提示词缺少关键约束：{'；'.join(missing)}。这两条是合规底线，不能删除。",
        )
    return text


async def _next_version(db: AsyncSession, name: str) -> int:
    current = (
        await db.execute(select(func.max(PromptTemplate.version)).where(PromptTemplate.name == name))
    ).scalar()
    return int(current or 0) + 1


async def save_new_version(
    db: AsyncSession,
    *,
    name: str,
    content: str,
    note: str = "",
    user_id: int | None = None,
) -> PromptTemplate:
    """保存为新版本并立即生效（旧版本原地保留，只把 is_active 置 false）。"""
    text = validate(name, content)
    version = await _next_version(db, name)

    # 先停用旧版再插入新版，顺序不能反：uq_prompt_active_per_name 是唯一索引，
    # 同时存在两条 is_active 会直接约束冲突（这正是我们要的保护）
    await db.execute(
        update(PromptTemplate)
        .where(PromptTemplate.name == name, PromptTemplate.is_active.is_(True))
        .values(is_active=False)
    )
    row = PromptTemplate(
        name=name,
        version=version,
        content=text,
        note=(note or "").strip()[:200],
        content_sha=_sha(text),
        is_active=True,
        created_by=user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info("提示词更新：%s v%s by user=%s len=%s", name, version, user_id, len(text))
    return row


async def rollback(
    db: AsyncSession, *, name: str, target_version: int, user_id: int | None = None
) -> PromptTemplate:
    """回滚 = 把旧版正文复制成一个新版本。版本号单调递增，历史不会出现"倒带"。"""
    old = (
        await db.execute(
            select(PromptTemplate).where(
                PromptTemplate.name == name, PromptTemplate.version == target_version
            )
        )
    ).scalar_one_or_none()
    if old is None:
        raise AppError("PROMPT_NOT_FOUND", f"模板 {name} 没有 v{target_version}")
    return await save_new_version(
        db,
        name=name,
        content=old.content,
        note=f"回滚自 v{target_version}",
        user_id=user_id,
    )


async def list_versions(db: AsyncSession, name: str, limit: int = 50) -> list[PromptTemplate]:
    return list(
        (
            await db.execute(
                select(PromptTemplate)
                .where(PromptTemplate.name == name)
                .order_by(PromptTemplate.version.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def summary(db: AsyncSession) -> list[dict]:
    """模板概览：给管理页左侧列表用。"""
    rows = (
        await db.execute(select(PromptTemplate).order_by(PromptTemplate.name, PromptTemplate.version))
    ).scalars().all()

    grouped: dict[str, dict] = {}
    for row in rows:
        item = grouped.setdefault(
            row.name,
            {"name": row.name, "versions": 0, "active_version": None, "updated_at": None, "active": None},
        )
        item["versions"] += 1
        if row.is_active:
            item["active_version"] = row.version
            item["updated_at"] = row.created_at
            item["active"] = row
    return [grouped[n] for n in MANAGED_PROMPTS if n in grouped] + [
        v for k, v in grouped.items() if k not in MANAGED_PROMPTS
    ]


async def ensure_seed(db: AsyncSession) -> None:
    """首次启动播种：v0=迁移前的文件版（存档），v1=出厂正文（生效）。

    两个版本都留着是有意的：管理员打开历史时能看到"上线前系统用的是什么"，
    而不是从 v1 开始凭空出现。
    """
    for name in MANAGED_PROMPTS:
        exists = (
            await db.execute(select(func.count(PromptTemplate.id)).where(PromptTemplate.name == name))
        ).scalar()
        if exists:
            continue

        try:
            legacy = load_prompt(name)
        except FileNotFoundError:
            legacy = None

        if legacy is not None:
            db.add(
                PromptTemplate(
                    name=name,
                    version=0,
                    content=legacy.text,
                    note="迁移前的文件版（存档）",
                    content_sha=_sha(legacy.text),
                    is_active=False,
                )
            )

        text = default_text(name)
        if not text:
            continue
        db.add(
            PromptTemplate(
                name=name,
                version=1,
                content=text,
                note="系统初始化：出厂默认模板",
                content_sha=_sha(text),
                is_active=True,
            )
        )
        logger.info("提示词种子已写入：%s v1（%s 字）", name, len(text))
    await db.commit()
