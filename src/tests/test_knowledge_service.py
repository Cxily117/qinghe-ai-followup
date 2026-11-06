"""知识库「发布 → 检索」链路：切片质量、版本治理、以及"没发布的绝不能答"。

为什么这条链路值得单独测：
  错误回答带来的医疗风险，源头往往不是模型，而是**检索到了不该被检索的内容**——
  草稿版制度、已下架的旧版、被切碎到失去上下文的片段。所以断言的重点不是
  「能不能答」，而是「谁在什么状态下可以被检索到」：

    draft     → 不可检索（还在改的制度不能进提示词）
    published → 可检索（且回答带得出引用）
    archived  → 又不可检索（同名新版发布时旧版自动归档）

以及一个容易被忽略的治理规则：同一标题同一时点只能有一版生效。
"""

from __future__ import annotations

import pytest

from app.agents import rag
from app.core.errors import AppError
from app.models.kb import KbChunk
from app.services import knowledge_service

POLICY_V1 = """# 骨科术后随访制度

## 一、适用范围
本制度适用于本院骨科关节置换、脊柱内固定术后出院患者的随访管理，
由责任护士按方案节点执行，随访记录须当日录入系统。

## 二、第 7 天
电话随访，询问切口愈合情况、疼痛评分与体温；体温超过 38.5℃ 视为红旗症状，
须当日升级主管医生，并在系统中登记升级时间与接收人。

## 三、第 14 天
电话随访，确认伤口敷料情况、下肢肿胀程度与活动量；出现渗液、红肿加重、
剧痛或意识改变的，按红旗症状处理，不得仅做口头安抚。

## 四、第 30 天
门诊复查，评估关节活动度与负重情况，拍摄 X 线片并与术后即刻片对比，
由专科医生判断是否进入下一阶段康复计划。

## 五、失访处理
连续两次联系未果记失访，须在 3 个工作日内改约或上门核实，
失访原因纳入月度质控统计，不得直接关闭任务。

## 六、记录要求
每次随访须记录联系人、联系方式、主诉、体温、切口情况、处置建议与下次随访时间；
记录缺项视为未完成随访，质控抽查按缺项数扣分。

## 七、升级路径
红旗症状 → 主管医生 → 值班二线 → 科主任，逐级升级并登记时间戳；
升级过程中的每一次交接都要有接收人姓名，不得出现"已通知"这类无主记录。
"""

POLICY_V2 = POLICY_V1.replace("38.5℃", "38.0℃").replace(
    "评估关节活动度与负重情况", "评估关节活动度、负重情况与假体位置"
)


# ---------------------------------------------------------------- 切片
def test_chunk_merges_paragraphs_and_splits_long_ones() -> None:
    text = "短段一。\n\n短段二。\n\n" + ("长" * 1200)
    chunks = knowledge_service.chunk_markdown(text, size=500, overlap=50)
    assert len(chunks) >= 3
    assert all(len(c) <= 500 for c in chunks)
    # 前两段被合并进同一个切片（都短，没必要各占一个）
    assert chunks[0].startswith("短段一。") and "短段二。" in chunks[0]
    # 超长段落滑窗切开后要有重叠，避免关键句被切断
    long_chunks = [c for c in chunks if set(c) == {"长"}]
    assert len(long_chunks[0]) == 500


def test_chunk_strips_code_blocks_and_images() -> None:
    text = "正文一。\n\n```sql\nselect 1\n```\n\n![图](a.png)\n\n正文二。"
    chunks = knowledge_service.chunk_markdown(text, size=500, overlap=50)
    joined = "".join(chunks)
    assert "select 1" not in joined
    assert "a.png" not in joined
    assert "正文一。" in joined and "正文二。" in joined


# ---------------------------------------------------------------- 入库
@pytest.mark.asyncio
async def test_create_document_chunks_and_versions(db, staff) -> None:
    doc, n = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    assert doc.version == 1
    assert doc.status == "draft"  # 上传即发布是事故来源，必须停在草稿
    assert n >= 2 and n == len(await knowledge_service.list_chunks(db, doc.id))

    # 切片带向量、带长度，检索才有得算
    first = (await knowledge_service.list_chunks(db, doc.id))[0]
    assert first.chunk_index == 0
    assert first.embedding and len(first.embedding) == 1024
    assert first.token_count == len(first.content)

    # 同名再传一版：版本号 +1，旧版仍是 draft（没发布，不涉及归档）
    v2, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V2,
        file_path="",
        uploaded_by=staff.id,
    )
    assert v2.version == 2


@pytest.mark.asyncio
async def test_create_document_rejects_empty_content(db, staff) -> None:
    with pytest.raises(ValueError, match="内容为空"):
        await knowledge_service.create_document(
            db,
            title="空文档",
            doc_type="policy",
            content="   \n\n  ",
            file_path="",
            uploaded_by=staff.id,
        )


# ---------------------------------------------------------------- 发布 → 检索
@pytest.mark.asyncio
async def test_draft_is_invisible_to_retrieval_until_published(db, staff) -> None:
    doc, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )

    # 草稿态：知识库对检索表现为空，问句也不该命中
    assert await knowledge_service.count_published_chunks(db) == 0
    assert await rag.retrieve(db, "第 7 天要做几次随访", top_k=5) == []

    await knowledge_service.publish_document(db, doc)
    assert doc.status == "published"
    assert await knowledge_service.count_published_chunks(db) > 0

    hits = await rag.retrieve(db, "体温超过多少度算红旗症状", top_k=5)
    assert hits, "发布后必须能被检索到"
    assert all(h.doc_title == "骨科术后随访制度" for h in hits)
    # 38.5℃ 那条必须在召回集里（mock 向量无语义，只验"可召回"，不验排序）
    assert any("38.5" in h.content for h in hits)


@pytest.mark.asyncio
async def test_publishing_new_version_archives_old_one(db, staff) -> None:
    v1, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    await knowledge_service.publish_document(db, v1)

    v2, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V2,
        file_path="",
        uploaded_by=staff.id,
    )
    await knowledge_service.publish_document(db, v2)

    await db.refresh(v1)
    assert v1.status == "archived"  # 同一时点只有一版生效
    assert v2.status == "published"

    # 检索命中新版口径（38.0℃），旧版（38.5℃）已退出
    hits = await rag.retrieve(db, "体温超过多少度算红旗症状", top_k=10)
    titles = {h.content for h in hits}
    assert any("38.0" in c for c in titles)
    assert not any("38.5" in c for c in titles)


@pytest.mark.asyncio
async def test_archived_document_leaves_retrieval_but_keeps_chunks(db, staff) -> None:
    doc, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    await knowledge_service.publish_document(db, doc)
    await knowledge_service.archive_document(db, doc)

    assert doc.status == "archived"
    assert await rag.retrieve(db, "第 7 天电话随访", top_k=5) == []
    # 切片保留：历史回答的引用要能回查，"依据哪份文件"不能断链
    assert len(await knowledge_service.list_chunks(db, doc.id)) > 0
    assert await knowledge_service.count_published_chunks(db) == 0

    # 幂等：重复下架不报错
    assert (await knowledge_service.archive_document(db, doc)).status == "archived"


# ---------------------------------------------------------------- 删除与列表
@pytest.mark.asyncio
async def test_published_document_cannot_be_deleted(db, staff) -> None:
    doc, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    await knowledge_service.publish_document(db, doc)

    with pytest.raises(AppError) as exc:
        await knowledge_service.delete_document(db, doc)
    assert exc.value.code == "DOC_HAS_BEEN_PUBLISHED"

    # 先归档再删：切片一并清掉，不留孤儿行
    await knowledge_service.archive_document(db, doc)
    await knowledge_service.delete_document(db, doc)
    assert await knowledge_service.list_chunks(db, doc.id) == []


@pytest.mark.asyncio
async def test_document_list_filters_and_chunk_counts(db, staff) -> None:
    a, _ = await knowledge_service.create_document(
        db,
        title="骨科随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    b, _ = await knowledge_service.create_document(
        db,
        title="心内科随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    await knowledge_service.publish_document(db, b)

    page = await knowledge_service.list_documents_page(db)
    assert page["total"] == 2
    assert all(item.chunk_count > 0 for item in page["items"])

    drafted = await knowledge_service.list_documents_page(db, status="draft")
    assert [d.id for d in drafted["items"]] == [a.id]

    searched = await knowledge_service.list_documents_page(db, q="心内")
    assert [d.id for d in searched["items"]] == [b.id]

    paged = await knowledge_service.list_documents_page(db, page=1, page_size=1)
    assert len(paged["items"]) == 1 and paged["pages"] == 2

    with pytest.raises(AppError) as exc:
        await knowledge_service.get_document(db, 999_999)
    assert exc.value.code == "DOC_NOT_FOUND"


@pytest.mark.asyncio
async def test_retrieval_ignores_chunks_without_embedding(db, staff) -> None:
    """脏数据兜底：切片向量列被清空时不能把整次检索打断（上线后最可能的人工误操作）。"""
    doc, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content=POLICY_V1,
        file_path="",
        uploaded_by=staff.id,
    )
    await knowledge_service.publish_document(db, doc)
    for chunk in (await db.execute(__import__("sqlalchemy").select(KbChunk))).scalars().all():
        chunk.embedding = None
    await db.commit()

    hits = await rag.retrieve(db, "第 7 天电话随访", top_k=5)
    assert isinstance(hits, list)  # 不抛异常即可
