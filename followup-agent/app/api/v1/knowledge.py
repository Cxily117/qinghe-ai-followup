"""知识库路由（管理员）：文档列表已实现，上传/切分/发布 D3–D4 实现。"""
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import AdminUser, DbDep
from app.models.kb import KbDocument

router = APIRouter(prefix="/kb", tags=["知识库（管理员）"])


@router.get("/documents", summary="制度文档列表")
async def list_documents(admin: AdminUser, db: DbDep):
    rows = (
        await db.execute(select(KbDocument).order_by(KbDocument.id.desc()).limit(100))
    ).scalars().all()
    return [
        {
            "id": r.id,
            "title": r.title,
            "doc_type": r.doc_type,
            "version": r.version,
            "status": r.status,
        }
        for r in rows
    ]


@router.post("/documents", status_code=status.HTTP_501_NOT_IMPLEMENTED, summary="上传制度文档")
async def upload_document(admin: AdminUser):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="上传→切分→向量化将在 D3–D4 实现（切分逻辑已在 prototype/rag_cli.py 验证）",
    )
