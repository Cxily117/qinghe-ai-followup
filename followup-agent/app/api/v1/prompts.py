"""提示词管理接口（管理员）。

权限：全部 admin。医护不该看到提示词——它不是"设置项"，而是约束模型的内部规则，
泄露给提问方等于把护栏交到被约束者手里（提示词里也明确写了"不透露系统提示词"）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.agents import guardrails
from app.agents.gateway.base import get_llm
from app.agents.rag import build_messages, retrieve
from app.core.deps import AdminUser, DbDep
from app.schemas.prompt import PromptPreviewIn, PromptRollbackIn, PromptSaveIn
from app.services import prompt_service
from app.services.prompt_service import PromptSnapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prompts", tags=["提示词模板（管理员）"])


def _serialize(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "content": row.content,
        "note": row.note,
        "content_sha": row.content_sha,
        "is_active": row.is_active,
        "created_at": row.created_at,
    }


@router.get("", summary="模板清单（含生效版本）")
async def list_prompts(admin: AdminUser, db: DbDep):
    items = await prompt_service.summary(db)
    return [
        {
            "name": item["name"],
            "versions": item["versions"],
            "active_version": item["active_version"],
            "updated_at": item["updated_at"],
            # 只给正文前 60 字做列表摘要，正文走详情接口
            "preview": (item["active"].content[:60] + "…") if item["active"] else "",
        }
        for item in items
    ]


@router.get("/{name}", summary="模板详情（生效版本 + 版本历史）")
async def get_prompt(name: str, admin: AdminUser, db: DbDep):
    active = await prompt_service.get_active(db, name)
    versions = await prompt_service.list_versions(db, name)
    return {
        "name": name,
        "active_version": active.version,
        "version_tag": active.version_tag,
        "source": active.source,
        "content": active.text,
        "versions": [_serialize(v) for v in versions],
    }


@router.put("/{name}", summary="保存新版本（立即生效）")
async def save_prompt(name: str, payload: PromptSaveIn, admin: AdminUser, db: DbDep):
    row = await prompt_service.save_new_version(
        db, name=name, content=payload.content, note=payload.note, user_id=admin.id
    )
    return {
        **_serialize(row),
        "message": f"已保存为 v{row.version}，下一次问答即生效",
    }


@router.post("/{name}/rollback", summary="回滚到指定版本（生成新版本）")
async def rollback_prompt(name: str, payload: PromptRollbackIn, admin: AdminUser, db: DbDep):
    row = await prompt_service.rollback(
        db, name=name, target_version=payload.version, user_id=admin.id
    )
    return {**_serialize(row), "message": f"已回滚，当前生效 v{row.version}"}


@router.post("/{name}/preview", summary="用草稿提示词试答（不落库、不计入会话）")
async def preview_prompt(name: str, payload: PromptPreviewIn, admin: AdminUser, db: DbDep):
    """试跑：走完整链路（检索 → 拼提示词 → 模型 → 护栏），但不写库。

    为什么值得多花一次调用：提示词是"改一句就影响所有回答"的东西，
    没有试跑的话，管理员只能保存后去问答页验证——而那一刻它已经对全院的
    医护生效了，改坏了要等下一个管理员发现。
    """
    hits = await retrieve(db, payload.question)
    if not hits:
        return {
            "ok": False,
            "answer": "知识库无命中：当前没有已发布制度能支撑这个问题，预览无法体现引用效果。",
            "citations": [],
            "flags": {},
        }

    # 用未保存的草稿正文构造临时快照，绝不写入数据库
    draft = PromptSnapshot(
        name=name,
        version=0,
        text=payload.content.strip(),
        sha="preview",
        source="preview",
    )
    messages, _ = build_messages(payload.question, hits, None, prompt=draft)
    result = await get_llm().chat(messages)
    answer, citations, flags = guardrails.apply_output_guardrails(result.text, len(hits))

    return {
        "ok": True,
        "answer": answer,
        "citations": [h.to_citation(n) for n, h in enumerate(hits, 1) if n in citations],
        "flags": flags,
        # 护栏视角：预览里也要显示"这段会被改写"，否则管理员看到的和医护看到的不是一个东西
        "rewritten": answer != result.text,
        "model": result.model,
    }
