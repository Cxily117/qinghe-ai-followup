"""知识库服务：切片 → 向量化 → 入库 → 发布（★ 阶段 1 核心模块 ① 的迁移目标）。

两态设计（对应需求清单的合规要求）：
  上传即入库，但状态是 draft —— 检索只认 published。
  也就是「医生还没签核的制度，AI 不会拿去回答护士的问题」，这条线必须在代码里。

版本策略：
  同名文档再次上传 → version 自动 +1；发布新版时把旧版置为 archived。
  旧版切片继续留在库里（审计可追溯），只是不再参与检索。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.gateway.embedding import get_embedder
from app.core.config import get_settings
from app.core.errors import AppError
from app.models.kb import KbChunk, KbDocument

logger = logging.getLogger(__name__)


# ======================================================================
# 文本切分（与阶段 1 原型同一套逻辑，参数改为从 settings 读）
#   - 先按空行切自然段，短段合并到 ~chunk_size
#   - 单段超长则滑窗硬切，步长 = size - overlap，避免关键句被切断
# ======================================================================
def chunk_markdown(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """把制度正文切成 <= size 字的语义切片。"""
    s = get_settings()
    size = size or s.chunk_size
    overlap = overlap or s.chunk_overlap

    # 去掉代码块、图片、HTML 注释——制度文档里偶尔有排版残留，会干扰长度统计
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            step = max(size - overlap, 1)
            for i in range(0, len(para), step):
                chunks.append(para[i : i + size])
            continue

        if len(buffer) + len(para) + 1 <= size:
            buffer = f"{buffer}\n{para}" if buffer else para
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c.strip()]


async def _next_version(db: AsyncSession, title: str) -> int:
    """同名文档的版本号 +1；新标题从 1 开始。"""
    current = (
        await db.execute(select(func.max(KbDocument.version)).where(KbDocument.title == title))
    ).scalar()
    return int(current or 0) + 1


async def create_document(
    db: AsyncSession,
    *,
    title: str,
    doc_type: str,
    content: str,
    file_path: str,
    uploaded_by: int,
) -> tuple[KbDocument, int]:
    """上传并入库一份制度文档：切片 + 向量化 + 落库（状态 draft）。

    返回 (文档, 切片数)。任何一步失败都整体回滚，不留下「有文档没切片」的脏数据。
    """
    chunks = chunk_markdown(content)
    if not chunks:
        raise ValueError("文档内容为空或无法切分，请检查文件")

    doc = KbDocument(
        title=title,
        doc_type=doc_type,
        version=await _next_version(db, title),
        file_path=file_path,
        status="draft",
        uploaded_by=uploaded_by,
    )
    db.add(doc)
    await db.flush()  # 拿到自增 id，用于切片外键

    # 向量化：客户端内部已按 32 条一批切分，避免超过上游批量上限
    vectors = await get_embedder().embed(chunks)

    db.add_all(
        [
            KbChunk(
                document_id=doc.id,
                chunk_index=idx,
                content=chunk,
                embedding=vectors[idx].tolist(),
                token_count=len(chunk),
            )
            for idx, chunk in enumerate(chunks)
        ]
    )
    await db.commit()
    await db.refresh(doc)

    logger.info("文档入库完成：id=%s title=%s version=%s chunks=%s", doc.id, title, doc.version, len(chunks))
    return doc, len(chunks)


async def publish_document(db: AsyncSession, doc: KbDocument) -> KbDocument:
    """把文档置为 published；同名旧版自动 archived（同一时点只有一版生效）。"""
    stale = (
        await db.execute(
            select(KbDocument).where(
                KbDocument.title == doc.title,
                KbDocument.id != doc.id,
                KbDocument.status == "published",
            )
        )
    ).scalars().all()
    for old in stale:
        old.status = "archived"
        logger.info("旧版本归档：id=%s version=%s", old.id, old.version)

    doc.status = "published"
    await db.commit()
    await db.refresh(doc)
    return doc


async def list_chunks(db: AsyncSession, document_id: int, limit: int = 200) -> list[KbChunk]:
    return (
        await db.execute(
            select(KbChunk)
            .where(KbChunk.document_id == document_id)
            .order_by(KbChunk.chunk_index.asc())
            .limit(limit)
        )
    ).scalars().all()


async def count_published_chunks(db: AsyncSession) -> int:
    """当前可检索切片数——健康检查与运维看板用，也是「知识库是否为空」的判据。"""
    return int(
        (
            await db.execute(
                select(func.count(KbChunk.id))
                .join(KbDocument, KbDocument.id == KbChunk.document_id)
                .where(KbDocument.status == "published")
            )
        ).scalar()
        or 0
    )


async def get_document(db: AsyncSession, document_id: int) -> KbDocument:
    doc = await db.get(KbDocument, document_id)
    if doc is None:
        raise AppError("DOC_NOT_FOUND")
    return doc


async def list_documents_page(
    db: AsyncSession,
    *,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """文档分页列表（带切片数）。API 与后台页面共用，避免两套筛选口径。"""
    stmt = select(KbDocument)
    if status:
        stmt = stmt.where(KbDocument.status == status)
    if q:
        stmt = stmt.where(KbDocument.title.contains(q.strip()))

    total = int(
        (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    )
    rows = list(
        (
            await db.execute(
                stmt.order_by(KbDocument.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )

    counts = dict(
        (
            await db.execute(
                select(KbChunk.document_id, func.count()).group_by(KbChunk.document_id)
            )
        ).all()
    )
    for row in rows:
        # 挂在 ORM 实例上的临时属性，模板直接读 doc.chunk_count
        setattr(row, "chunk_count", int(counts.get(row.id, 0)))

    return {
        "items": rows,
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


async def archive_document(db: AsyncSession, doc: KbDocument) -> KbDocument:
    """下架：退出检索但保留切片与原文。

    和「删除」分开是有意的。审计要能回答"三个月前那条回答依据的是哪份文件"，
    文件真删了，引用就变成 404，整条证据链断掉。所以对已发布文档，
    正确操作是归档而不是删除。
    """
    if doc.status == "archived":
        return doc
    doc.status = "archived"
    await db.commit()
    await db.refresh(doc)
    logger.info("文档归档：id=%s title=%s", doc.id, doc.title)
    return doc


async def delete_document(db: AsyncSession, doc: KbDocument) -> None:
    """物理删除（仅限 draft / archived）。

    已发布文档必须先归档：否则正在被检索的制度会突然消失，
    而依赖它的历史回答还在库里，两边的依据对不上。
    """
    if doc.status == "published":
        raise AppError("DOC_HAS_BEEN_PUBLISHED")

    # SQLite 默认不开外键级联，这里显式删切片，两种方言行为一致
    await db.execute(delete(KbChunk).where(KbChunk.document_id == doc.id))
    file_path = doc.file_path
    await db.delete(doc)
    await db.commit()

    # 落盘原文一并清掉；失败只记日志——数据库记录已删，文件残留不影响业务
    if file_path:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("删除原文文件失败：%s (%s)", file_path, exc)

    logger.info("文档已删除：id=%s title=%s", doc.id, doc.title)
