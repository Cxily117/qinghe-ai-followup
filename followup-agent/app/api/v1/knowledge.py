"""知识库路由（管理员）：上传 → 切片 → 向量化 → 发布 / 归档 / 删除。

权限：全部要求 admin —— 制度维护是管理员职责，医护只读（通过问答间接使用）。
合规：
  - 上传后状态为 draft，必须显式 publish 才进入检索；发布新版自动归档旧版；
  - 已发布文档不允许直接删除，只能归档下架 —— 历史回答的引用要能点开看原文；
  - 原文落盘留档（file_path），切片之外还留着"当时上传的文件本体"。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.deps import AdminUser, CurrentUser, DbDep
from app.core.errors import AppError
from app.models.kb import KbChunk, KbDocument
from app.schemas.kb import ChunkOut, DocumentOut, KbStats
from app.services import knowledge_service
from app.services.doc_parser import SUPPORTED_EXT, extract_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["知识库（管理员）"])

# 允许的文档类型：与需求清单里「随访制度文档」的四个类别对应
ALLOWED_DOC_TYPES = {"随访方案", "病种规范", "知情告知", "质控要求"}
MAX_BYTES = 2 * 1024 * 1024  # 2MB：制度文档正常几百 KB，超了多半放错了文件


def _doc_out(doc: KbDocument, chunks: int | None = None) -> dict:
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        doc_type=doc.doc_type,
        version=doc.version,
        status=doc.status,
        chunks=chunks,
    ).model_dump()


@router.get("/documents", summary="制度文档列表（可按状态/关键词筛选）")
async def list_documents(
    admin: AdminUser,
    db: DbDep,
    status_filter: str | None = Query(default=None, alias="status", description="draft|published|archived"),
    q: str | None = Query(default=None, description="标题关键词"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    page_data = await knowledge_service.list_documents_page(
        db, status=status_filter, q=q, page=page, page_size=page_size
    )
    return {
        "items": [_doc_out(r, r.chunk_count) for r in page_data["items"]],
        "total": page_data["total"],
        "page": page_data["page"],
        "pages": page_data["pages"],
    }


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    summary="上传制度文档（自动切片+向量化，状态 draft）",
)
async def upload_document(
    admin: AdminUser,
    db: DbDep,
    file: UploadFile = File(..., description="Markdown / 纯文本 / PDF 制度文件"),
    title: str = Form(..., description="文档标题，同名文档自动升版本"),
    doc_type: str = Form("随访方案", description="随访方案/病种规范/知情告知/质控要求"),
):
    settings = get_settings()

    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise AppError("UNSUPPORTED_FORMAT")
    if doc_type not in ALLOWED_DOC_TYPES:
        raise AppError("VALIDATION_ERROR", f"文档类型必须是 {'/'.join(sorted(ALLOWED_DOC_TYPES))} 之一")

    raw = await file.read()
    if not raw:
        raise AppError("VALIDATION_ERROR", "文件内容为空")
    if len(raw) > MAX_BYTES:
        raise AppError("FILE_TOO_LARGE")

    # 统一走 doc_parser：pdf 走文本层抽取，md/txt 走 UTF-8 → GB18030 兜底
    content = extract_text(raw, ext)

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
        raise AppError("VALIDATION_ERROR", str(exc))
    except Exception as exc:  # 向量化失败（如 Key 没配）也要把落盘文件清掉
        saved.unlink(missing_ok=True)
        logger.exception("文档入库失败")
        raise AppError("KB_UNAVAILABLE", f"文档入库失败：{exc}")

    return {
        "id": doc.id,
        "title": doc.title,
        "version": doc.version,
        "doc_type": doc.doc_type,
        "status": doc.status,
        "chunks": n_chunks,
        "tip": "文档为 draft 状态，尚未参与检索；请调用 /kb/documents/{id}/publish 发布",
    }


@router.get("/documents/{document_id}", summary="文档详情")
async def document_detail(document_id: int, admin: AdminUser, db: DbDep):
    doc = await knowledge_service.get_document(db, document_id)
    chunk_count = int(
        (await db.execute(select(func.count(KbChunk.id)).where(KbChunk.document_id == doc.id))).scalar()
        or 0
    )
    return {
        **_doc_out(doc, chunk_count),
        "created_at": doc.created_at,
        "file_path": doc.file_path,
        "file_exists": bool(doc.file_path) and Path(doc.file_path).exists(),
    }


@router.post("/documents/{document_id}/publish", summary="发布文档（进入检索，旧版自动归档）")
async def publish_document(document_id: int, admin: AdminUser, db: DbDep):
    doc = await knowledge_service.get_document(db, document_id)
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


@router.post("/documents/{document_id}/archive", summary="归档下架（退出检索，保留切片）")
async def archive_document(document_id: int, admin: AdminUser, db: DbDep):
    doc = await knowledge_service.get_document(db, document_id)
    doc = await knowledge_service.archive_document(db, doc)
    return {
        "id": doc.id,
        "status": doc.status,
        "message": "已归档下架，本次问答起不再被检索；历史引用的原文仍可查看",
    }


@router.delete("/documents/{document_id}", summary="删除文档（仅 draft / archived）")
async def delete_document(document_id: int, admin: AdminUser, db: DbDep):
    doc = await knowledge_service.get_document(db, document_id)
    await knowledge_service.delete_document(db, doc)
    return {"id": document_id, "message": "已删除（含切片与落盘原文）"}


@router.get("/documents/{document_id}/chunks", summary="查看切片（核对切片质量）")
async def list_chunks(
    document_id: int,
    admin: AdminUser,
    db: DbDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    doc = await knowledge_service.get_document(db, document_id)
    total = int(
        (await db.execute(select(func.count(KbChunk.id)).where(KbChunk.document_id == doc.id))).scalar()
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(KbChunk)
                .where(KbChunk.document_id == doc.id)
                .order_by(KbChunk.chunk_index.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )
    return {
        "document_id": document_id,
        "title": doc.title,
        "status": doc.status,
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": [
            ChunkOut(
                chunk_id=c.id,
                chunk_index=c.chunk_index,
                content=c.content,
                token_count=c.token_count,
            ).model_dump()
            for c in rows
        ],
    }


@router.get("/citations/{chunk_id}", summary="读取引用片段原文（登录可读）")
async def citation_detail(chunk_id: int, user: CurrentUser, db: DbDep):
    """前端点击引用卡片时用：把 80 字预览展开成完整条文。

    权限刻意放宽到「登录即可读」——医护要核对依据，不能要求他们先是管理员。
    但只放 published 文档的切片：制度未签核前，条文不该被人当作依据引用。
    """
    row = (
        await db.execute(
            select(KbChunk, KbDocument)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(KbChunk.id == chunk_id, KbDocument.status == "published")
        )
    ).first()
    if row is None:
        raise AppError("CHUNK_NOT_FOUND")

    chunk, doc = row
    return {
        "chunk_id": chunk.id,
        "document_id": doc.id,
        "doc_title": doc.title,
        "doc_type": doc.doc_type,
        "version": doc.version,
        "chunk_index": chunk.chunk_index,
        # 原文不做二次截断：核对条文时缺字比字数多危险得多
        "content": chunk.content,
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
