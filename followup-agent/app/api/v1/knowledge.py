"""知识库路由（管理员）：上传 → 切片 → 向量化 → 发布。

权限：全部要求 admin —— 制度维护是管理员职责，医护只读（通过问答间接使用）。
合规：上传后状态为 draft，必须显式 publish 才进入检索；发布新版会自动归档旧版。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.deps import AdminUser, DbDep
from app.models.kb import KbChunk, KbDocument
from app.schemas.kb import ChunkOut, KbStats, DocumentOut
from app.services import knowledge_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["知识库（管理员）"])

# 允许的文档类型：与需求清单里「随访制度文档」的四个类别对应
ALLOWED_DOC_TYPES = {"随访方案", "病种规范", "知情告知", "质控要求"}
ALLOWED_EXT = {".md", ".markdown", ".txt"}
MAX_BYTES = 2 * 1024 * 1024  # 2MB：制度文档正常几百 KB，超了多半放错了文件


@router.get("/documents", summary="制度文档列表")
async def list_documents(admin: AdminUser, db: DbDep):
    rows = (
        await db.execute(select(KbDocument).order_by(KbDocument.id.desc()).limit(100))
    ).scalars().all()

    # 顺带带上切片数：管理员一眼能看出「上传了但没切出内容」的异常文档
    counts = dict(
        (
            await db.execute(
                select(KbChunk.document_id, func.count()).group_by(KbChunk.document_id)
            )
        ).all()
    )

    return [
        DocumentOut(
            id=r.id,
            title=r.title,
            doc_type=r.doc_type,
            version=r.version,
            status=r.status,
            chunks=int(counts.get(r.id, 0)),
        ).model_dump()
        for r in rows
    ]


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    summary="上传制度文档（自动切片+向量化，状态 draft）",
)
async def upload_document(
    admin: AdminUser,
    db: DbDep,
    file: UploadFile = File(..., description="Markdown / 纯文本制度文件"),
    title: str = Form(..., description="文档标题，同名文档自动升版本"),
    doc_type: str = Form("随访方案", description="随访方案/病种规范/知情告知/质控要求"),
):
    settings = get_settings()

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"仅支持 {sorted(ALLOWED_EXT)} 格式；PDF/Word 解析在下一阶段接入",
        )
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"doc_type 必须是 {sorted(ALLOWED_DOC_TYPES)} 之一",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 2MB 上限")

    # 中文制度文件常见 UTF-8 / GBK 两种编码，都试一下，避免管理员因为编码卡住
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="无法识别文件编码（请另存为 UTF-8）")

    # 原始文件留档：审计要能回答「当时上传的原文是什么」，不能只留切片
    storage = Path(settings.kb_storage_dir)
    storage.mkdir(parents=True, exist_ok=True)
    saved = storage / f"{uuid.uuid4().hex}{ext}"
    saved.write_bytes(raw)

    try:
        doc, n_chunks = await knowledge_service.create_document(
            db,
            title=title.strip() or Path(file.filename or "未命名").stem,
            doc_type=doc_type,
            content=content,
            file_path=str(saved),
            uploaded_by=admin.id,
        )
    except ValueError as exc:
        saved.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # 向量化失败（如 Key 没配）也要把落盘文件清掉
        saved.unlink(missing_ok=True)
        logger.exception("文档入库失败")
        raise HTTPException(status_code=502, detail=f"文档入库失败：{exc}")

    return {
        "id": doc.id,
        "title": doc.title,
        "version": doc.version,
        "doc_type": doc.doc_type,
        "status": doc.status,
        "chunks": n_chunks,
        "tip": "文档为 draft 状态，尚未参与检索；请调用 /kb/documents/{id}/publish 发布",
    }


@router.post("/documents/{document_id}/publish", summary="发布文档（进入检索，旧版自动归档）")
async def publish_document(document_id: int, admin: AdminUser, db: DbDep):
    doc = await db.get(KbDocument, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status == "published":
        return {"id": doc.id, "status": doc.status, "message": "该文档已是发布状态"}

    doc = await knowledge_service.publish_document(db, doc)
    return {
        "id": doc.id,
        "title": doc.title,
        "version": doc.version,
        "status": doc.status,
        "message": "已发布，本次问答起可被检索",
    }


@router.get("/documents/{document_id}/chunks", summary="查看切片（核对切片质量）")
async def list_chunks(document_id: int, admin: AdminUser, db: DbDep):
    doc = await db.get(KbDocument, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    chunks = await knowledge_service.list_chunks(db, document_id)
    return {
        "document_id": document_id,
        "title": doc.title,
        "status": doc.status,
        "total": len(chunks),
        "items": [
            ChunkOut(
                chunk_id=c.id,
                chunk_index=c.chunk_index,
                content=c.content,
                token_count=c.token_count,
            ).model_dump()
            for c in chunks
        ],
    }


@router.get("/stats", summary="知识库统计")
async def kb_stats(admin: AdminUser, db: DbDep):
    s = get_settings()
    total = int((await db.execute(select(func.count(KbDocument.id)))).scalar() or 0)
    return KbStats(
        documents_total=total,
        published_chunks=await knowledge_service.count_published_chunks(db),
        embed_provider=s.embed_provider,
        embed_dim=s.embed_dim,
    ).model_dump()
