"""接口层契约：路由、鉴权、错误码、以及"两个角色能看到什么"。

service 层单测证明逻辑对；这一层证明**接线对**——依赖注入没装错、权限闸门真的拦得住、
错误 envelope 是前端约定的三段式。过度依赖 service 单测而漏掉这层，典型事故是：
函数本身没问题，但某条路由忘了写 `AdminUser`，医护直接调到了管理端接口。

用 httpx.ASGITransport 直接打 app，不起真服务、不走网络；库用测试夹具的临时 SQLite，
每个用例独立，可重复跑。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.deps import get_db
from app.core.security import hash_password
from app.main import create_app
from app.models import User
from app.services import knowledge_service
from tests.conftest import add_session

PASSWORD = "Followup@2026"


@pytest_asyncio.fixture
async def client(db) -> AsyncClient:
    """把应用的 db 依赖替换成测试会话，其余照常装配。"""

    async def _override_db():
        yield db

    app = create_app()
    app.dependency_overrides[get_db] = _override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def nurse(db) -> User:
    user = User(
        username="nurse01",
        password_hash=hash_password(PASSWORD),
        full_name="王护士",
        role="staff",
        department="骨科",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def boss(db) -> User:
    user = User(
        username="admin01",
        password_hash=hash_password(PASSWORD),
        full_name="周敏",
        role="admin",
        department="护理部",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, username: str, password: str = PASSWORD) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------------------------------------------------------- 认证
@pytest.mark.asyncio
async def test_login_sets_httponly_cookie_and_me_works(client, nurse) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "nurse01", "password": PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    cookie = resp.headers["set-cookie"]
    assert "qh_token=" in cookie and "HttpOnly" in cookie  # XSS 偷不走令牌

    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["username"] == "nurse01"
    assert me.json()["role"] == "staff"
    assert "password_hash" not in me.json()  # 哈希绝不能外泄


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_and_disabled_account(client, db) -> None:
    db.add(User(username="nurse02", password_hash=hash_password(PASSWORD), role="staff"))
    db.add(
        User(
            username="gone",
            password_hash=hash_password(PASSWORD),
            role="staff",
            status="disabled",
        )
    )
    await db.commit()

    bad = await client.post("/api/v1/auth/login", json={"username": "nurse02", "password": "x"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "AUTH_REQUIRED"
    # 不泄露"用户是否存在"：查无此人与密码错用同一句提示
    assert bad.json()["message"] == "用户名或密码错误"

    disabled = await client.post(
        "/api/v1/auth/login", json={"username": "gone", "password": PASSWORD}
    )
    assert disabled.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_and_forbidden_are_distinguished(client, nurse, boss) -> None:
    anon = await client.get("/api/v1/tasks/drafts")
    assert anon.status_code == 401

    nurse_headers = await _login(client, "nurse01")
    # 医护调管理端：403 而不是 404——权限不足要说清楚，不能让前端以为是地址错了
    forbidden = await client.get("/api/v1/audit/sessions", headers=nurse_headers)
    assert forbidden.status_code == 403

    boss_headers = await _login(client, "admin01")
    ok = await client.get("/api/v1/audit/sessions", headers=boss_headers)
    assert ok.status_code == 200


# ---------------------------------------------------------------- 随访任务
@pytest.mark.asyncio
async def test_task_flow_through_api_never_auto_confirms(client, nurse) -> None:
    headers = await _login(client, "nurse01")

    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"question": "患者住院号 ZY2026001，骨科术后，第 7 天电话随访，第 30 天门诊复查"},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["confirmed_by"] is None

    # 客户端硬塞 status=confirmed：入参模型里没有这个字段，必须被忽略
    sneaky = await client.post(
        f"/api/v1/tasks/{draft['id']}/confirm",
        headers=headers,
        json={"status": "confirmed", "note": "我确认"},
    )
    assert sneaky.status_code == 200
    body = sneaky.json()
    assert body["status"] == "confirmed"
    assert body["content"]["confirm"]["by"] == "nurse01"

    listed = await client.get("/api/v1/tasks/drafts", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == draft["id"] for item in listed.json())

    detail = await client.get(f"/api/v1/tasks/{draft['id']}", headers=headers)
    assert detail.status_code == 200


@pytest.mark.asyncio
async def test_other_nurse_cannot_see_or_confirm_draft(client, nurse, db) -> None:
    headers = await _login(client, "nurse01")
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"question": "患者住院号 ZY2026002，第 3 天电话随访"},
    )
    draft_id = created.json()["id"]

    db.add(User(username="nurse09", password_hash=hash_password(PASSWORD), role="staff"))
    await db.commit()
    intruder = await _login(client, "nurse09")

    # 草稿里有患者标识：横向不可见，且返回 404 不提示"存在但无权"
    assert (await client.get(f"/api/v1/tasks/{draft_id}", headers=intruder)).status_code == 404
    assert (
        await client.post(f"/api/v1/tasks/{draft_id}/confirm", headers=intruder, json={})
    ).status_code == 404


@pytest.mark.asyncio
async def test_missing_patient_id_returns_error_envelope(client, nurse) -> None:
    headers = await _login(client, "nurse01")
    resp = await client.post(
        "/api/v1/tasks", headers=headers, json={"question": "新建一份随访，第 7 天电话随访"}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "患者标识" in body["message"]
    assert body["request_id"]  # 报障能定位到日志


# ---------------------------------------------------------------- 知识库（发布闸门）
@pytest.mark.asyncio
async def test_kb_publish_is_admin_only_and_moves_document_into_retrieval(
    client, nurse, boss, db
) -> None:
    doc, _ = await knowledge_service.create_document(
        db,
        title="骨科术后随访制度",
        doc_type="policy",
        content="# 骨科术后随访制度\n\n第 7 天电话随访，体温超过 38.5℃ 升级主管医生。",
        file_path="",
        uploaded_by=boss.id,
    )

    nurse_headers = await _login(client, "nurse01")
    assert (
        await client.post(f"/api/v1/kb/documents/{doc.id}/publish", headers=nurse_headers)
    ).status_code == 403

    boss_headers = await _login(client, "admin01")
    published = await client.post(f"/api/v1/kb/documents/{doc.id}/publish", headers=boss_headers)
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    listed = await client.get("/api/v1/kb/documents?status=published", headers=boss_headers)
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()["items"]] == [doc.id]

    chunks = await client.get(f"/api/v1/kb/documents/{doc.id}/chunks", headers=boss_headers)
    assert chunks.status_code == 200
    chunk_id = chunks.json()["items"][0]["chunk_id"]

    # 引用原文登录即可读（医护点开 [1] 要看制度原文），但知识库管理本身仍限管理员
    citation = await client.get(f"/api/v1/kb/citations/{chunk_id}", headers=nurse_headers)
    assert citation.status_code == 200
    assert "38.5" in citation.json()["content"]

    stats = await client.get("/api/v1/kb/stats", headers=boss_headers)
    assert stats.status_code == 200


# ---------------------------------------------------------------- 审计
@pytest.mark.asyncio
async def test_audit_mark_flow_and_reports(client, nurse, boss, db) -> None:
    session = await add_session(
        db,
        user_id=nurse.id,
        messages=[
            {"role": "user", "content": "患者发烧 39 度怎么办"},
            {
                "role": "assistant",
                "content": "请联系主管医生",
                "meta": {"flags": {"red_flag_question": True}},
            },
        ],
    )
    headers = await _login(client, "admin01")

    listed = await client.get("/api/v1/audit/sessions?only_flagged=true", headers=headers)
    assert listed.status_code == 200
    assert [i["session_id"] for i in listed.json()["items"]] == [session.id]

    detail = await client.get(f"/api/v1/audit/sessions/{session.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["suspected"] is True

    marked = await client.post(
        f"/api/v1/audit/sessions/{session.id}/marks",
        headers=headers,
        json={"level": "high_risk", "note": "应建议当日就诊"},
    )
    assert marked.status_code == 200
    mark_id = marked.json()["id"]

    revoked = await client.post(f"/api/v1/audit/marks/{mark_id}/revoke", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    stats = await client.get("/api/v1/audit/stats", headers=headers)
    assert stats.status_code == 200
    assert "marks_active" in stats.json()

    summary = await client.get("/api/v1/reports/summary", headers=headers)
    assert summary.status_code == 200
    csv_resp = await client.get("/api/v1/reports/summary.csv", headers=headers)
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]

    # 医护看质控报表：403（报表里含全院数据）
    nurse_headers = await _login(client, "nurse01")
    assert (await client.get("/api/v1/reports/summary", headers=nurse_headers)).status_code == 403


# ---------------------------------------------------------------- 问答（mock 全链路）
@pytest.mark.asyncio
async def test_ask_creates_session_and_persists_messages(client, nurse) -> None:
    headers = await _login(client, "nurse01")
    resp = await client.post(
        "/api/v1/chat/ask",
        headers=headers,
        json={"question": "骨科术后第 7 天要做什么随访"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] > 0
    assert body["answer"]

    msgs = await client.get(f"/api/v1/chat/sessions/{body['session_id']}/messages", headers=headers)
    assert msgs.status_code == 200
    roles = [m["role"] for m in msgs.json()]
    assert "user" in roles and "assistant" in roles  # 问答全程落库
