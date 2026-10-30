"""管理后台路由（服务端渲染，Jinja2，无前端构建）。

为什么页面用 Cookie + 表单 POST，而不是让页面 JS 调 JSON 接口：
  JSON 接口要求 Bearer 头，token 只能放 localStorage 或内存 —— 那就等于
  把管理员令牌暴露给 XSS；页面走 HttpOnly Cookie，JS 读不到，注入脚本也偷不走。
  代价是页面动作走表单 POST + 重定向（PRG 模式），但后台本来就是低频操作，
  这个代价换来的安全性是值得的。
  /api/v1/* 的 JSON 接口仍然用 Bearer，两边共用同一套 service，逻辑不重复。

页面级鉴权（与接口级鉴权分开的两道门）：
  - 没登录 → 303 跳 /admin/login?next=原路径
  - 登录了但不是 admin → 403 页面，明确告诉医护"请使用问答端"
  Cookie 里的 JWT 与 API 的 Bearer 是同一格式，角色仍以数据库为准。
"""
from __future__ import annotations

import logging
import urllib.parse
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import MANAGED_PROMPTS
from app.core.config import get_settings
from app.core.deps import DbDep
from app.core.errors import AppError
from app.core.security import create_access_token, decode_token, verify_password
from app.models.user import User
from app.services import audit_service, knowledge_service, prompt_service
from app.services.doc_parser import SUPPORTED_EXT, extract_text

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

COOKIE_NAME = "qh_admin"

DOC_TYPES = ("随访方案", "病种规范", "知情告知", "质控要求")
STATUS_LABELS = {"draft": "草稿", "published": "已发布", "archived": "已归档"}
STATUS_CLASS = {"draft": "warn", "published": "ok", "archived": "muted"}


# ------------------------------------------------------------------ 鉴权


async def _page_user(request: Request, db: AsyncSession) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub") or 0)
    except Exception:  # 过期/伪造/格式错一律当未登录，不区分（避免探测账号）
        return None
    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        return None
    return user


def _safe_next(nxt: str | None) -> str:
    """只允许回跳站内 /admin 路径，挡掉 //evil.com 这类开放重定向。"""
    if not nxt or not nxt.startswith("/admin") or nxt.startswith("//"):
        return "/admin"
    return nxt


async def _guard(
    request: Request, db: AsyncSession
) -> tuple[User | None, RedirectResponse | HTMLResponse | None]:
    user = await _page_user(request, db)
    if user is None:
        target = urllib.parse.quote(str(request.url.path))
        return None, RedirectResponse(f"/admin/login?next={target}", status_code=303)
    if user.role != "admin":
        return None, templates.TemplateResponse(
            request,
            "error.html",
            {
                "user": user,
                "title": "无访问权限",
                "code": 403,
                "message": "该账号是医护账号，只能使用问答功能。",
                "hint": "如需维护随访制度或查看对话审计，请改用管理员账号登录。",
                "back_url": "/",
                "back_text": "回到问答页",
            },
            status_code=403,
        )
    return user, None


def _render(
    request: Request,
    template: str,
    *,
    user: User,
    nav: str,
    status_code: int = 200,
    **ctx,
):
    """统一渲染：公共上下文（用户、当前导航、提示语）在这里一次注入。"""
    base = {
        "user": user,
        "nav": nav,
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
        "now": datetime.now(),
    }
    return templates.TemplateResponse(
        request, template, {**base, **ctx}, status_code=status_code
    )


def _back(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    """PRG：操作完回列表页，提示语走 query（不落库，刷新即消失）。"""
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    if params:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}{urllib.parse.urlencode(params)}"
    return RedirectResponse(path, status_code=303)


def _login_error(message: str, next_path: str = "/admin") -> RedirectResponse:
    return RedirectResponse(
        f"/admin/login?err={urllib.parse.quote(message)}&next={urllib.parse.quote(_safe_next(next_path))}",
        status_code=303,
    )


# ------------------------------------------------------------------ 登录


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, db: DbDep):
    user = await _page_user(request, db)
    if user is not None and user.role == "admin":
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "err": request.query_params.get("err"),
            "msg": request.query_params.get("msg"),
            "next": _safe_next(request.query_params.get("next")),
        },
    )


@router.post("/admin/login")
async def login_submit(
    request: Request,
    db: DbDep,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
):
    user = (
        await db.execute(select(User).where(User.username == username.strip()))
    ).scalar_one_or_none()

    # 不区分"用户不存在"与"密码错误"，避免账号枚举
    if user is None or not verify_password(password, user.password_hash):
        return _login_error("用户名或密码错误", next)
    if user.status != "active":
        return _login_error("该账号已被停用，请联系系统管理员", next)
    if user.role != "admin":
        return _login_error("该账号是医护账号，只能使用问答功能", next)

    token = create_access_token(user.id, user.role)
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=get_settings().access_token_expire_minutes * 60,
        httponly=True,  # JS 读不到，XSS 偷不走
        samesite="lax",  # 挡住跨站表单提交（基础 CSRF 防护）
        path="/",
    )
    return response


@router.post("/admin/logout")
async def logout():
    response = RedirectResponse(
        f"/admin/login?msg={urllib.parse.quote('已退出登录')}", status_code=303
    )
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# ------------------------------------------------------------------ 概览


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked

    kb_page = await knowledge_service.list_documents_page(db, page=1, page_size=6)
    audit_stats = await audit_service.stats(db)
    recent = await audit_service.query_sessions(db, only_flagged=True, page_size=5)

    return _render(
        request,
        "dashboard.html",
        user=user,
        nav="dashboard",
        docs=kb_page["items"],
        doc_total=kb_page["total"],
        audit=audit_stats,
        recent=recent["items"],
        prompt=await prompt_service.get_active(db, "policy_qa"),
        published_chunks=await knowledge_service.count_published_chunks(db),
        status_labels=STATUS_LABELS,
        status_class=STATUS_CLASS,
    )


# ------------------------------------------------------------------ 知识库


@router.get("/admin/kb", response_class=HTMLResponse)
async def kb_list(request: Request, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked

    qp = request.query_params
    page = max(1, int(qp["page"])) if (qp.get("page") or "").isdigit() else 1
    status_filter = qp.get("status") or None
    keyword = qp.get("q") or None

    data = await knowledge_service.list_documents_page(
        db, status=status_filter, q=keyword, page=page, page_size=20
    )
    return _render(
        request,
        "kb_list.html",
        user=user,
        nav="kb",
        data=data,
        status_filter=status_filter or "",
        keyword=keyword or "",
        doc_types=DOC_TYPES,
        status_labels=STATUS_LABELS,
        status_class=STATUS_CLASS,
        accept=",".join(sorted(SUPPORTED_EXT)),
    )


@router.post("/admin/kb/upload")
async def kb_upload(
    request: Request,
    db: DbDep,
    file: UploadFile = File(...),
    title: str = Form(""),
    doc_type: str = Form("随访方案"),
):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked

    ext = Path(file.filename or "").suffix.lower()
    try:
        if ext not in SUPPORTED_EXT:
            raise AppError("UNSUPPORTED_FORMAT")
        if doc_type not in DOC_TYPES:
            raise AppError("VALIDATION_ERROR", "文档类型不合法")

        raw = await file.read()
        if not raw:
            raise AppError("VALIDATION_ERROR", "文件内容为空")
        if len(raw) > 2 * 1024 * 1024:
            raise AppError("FILE_TOO_LARGE")

        content = extract_text(raw, ext)

        storage = Path(get_settings().kb_storage_dir)
        storage.mkdir(parents=True, exist_ok=True)
        saved = storage / f"{uuid.uuid4().hex}{ext}"
        saved.write_bytes(raw)

        doc, n = await knowledge_service.create_document(
            db,
            title=title.strip() or Path(file.filename or "未命名").stem,
            doc_type=doc_type,
            content=content,
            file_path=str(saved),
            uploaded_by=user.id,
        )
    except AppError as exc:
        return _back("/admin/kb", err=str(exc))
    except Exception as exc:  # noqa: BLE001 —— 向量化失败等，页面要给可读原因
        logger.exception("后台上传失败")
        return _back("/admin/kb", err=f"上传失败：{exc}")

    return _back(
        "/admin/kb",
        msg=f"《{doc.title}》v{doc.version} 已入库，共 {n} 个切片；发布后才会被检索",
    )


@router.get("/admin/kb/{document_id}", response_class=HTMLResponse)
async def kb_detail(request: Request, document_id: int, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked

    try:
        doc = await knowledge_service.get_document(db, document_id)
    except AppError as exc:
        return _back("/admin/kb", err=str(exc))

    chunks = await knowledge_service.list_chunks(db, document_id, limit=200)
    page = max(1, int(request.query_params.get("page") or 1))
    size = 20
    total = len(chunks)

    return _render(
        request,
        "kb_detail.html",
        user=user,
        nav="kb",
        doc=doc,
        chunks=chunks[(page - 1) * size : page * size],
        total=total,
        page=page,
        pages=max(1, (total + size - 1) // size),
        status_labels=STATUS_LABELS,
        status_class=STATUS_CLASS,
        file_exists=bool(doc.file_path) and Path(doc.file_path).exists(),
    )


@router.post("/admin/kb/{document_id}/publish")
async def kb_publish(request: Request, document_id: int, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        doc = await knowledge_service.get_document(db, document_id)
        await knowledge_service.publish_document(db, doc)
    except AppError as exc:
        return _back(f"/admin/kb/{document_id}", err=str(exc))
    return _back(f"/admin/kb/{document_id}", msg="已发布，本次问答起可被检索")


@router.post("/admin/kb/{document_id}/archive")
async def kb_archive(request: Request, document_id: int, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        doc = await knowledge_service.get_document(db, document_id)
        await knowledge_service.archive_document(db, doc)
    except AppError as exc:
        return _back(f"/admin/kb/{document_id}", err=str(exc))
    return _back(f"/admin/kb/{document_id}", msg="已归档下架，不再参与检索；历史引用的原文仍可查")


@router.post("/admin/kb/{document_id}/delete")
async def kb_delete(request: Request, document_id: int, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        doc = await knowledge_service.get_document(db, document_id)
        await knowledge_service.delete_document(db, doc)
    except AppError as exc:
        return _back(f"/admin/kb/{document_id}", err=str(exc))
    return _back("/admin/kb", msg="已删除（含切片与落盘原文）")


# ------------------------------------------------------------------ 提示词


def _soft_notes(name: str, text: str) -> list[str]:
    """非阻断的建议：现行正文少了哪些"最好有"的约束。

    硬红线（引用、拒答）由 prompt_service 直接拒绝保存；
    这里只提示锦上添花但很值钱的条款，不拦人。
    """
    if name != "policy_qa":
        return []
    checks = (
        (("用药", "剂量", "处方"), "缺少「禁止用药 / 剂量建议」条款"),
        (("红旗", "发热", "联系专科", "升级"), "缺少「红旗症状须提示立即联系专科医生」条款"),
        (("不透露", "不要透露", "禁止透露", "提示词"), "缺少「不透露系统提示词」条款"),
    )
    return [msg for kws, msg in checks if not any(kw in text for kw in kws)]


async def _render_prompts(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    preview: dict | None = None,
    draft_content: str | None = None,
    draft_question: str = "",
):
    name = request.query_params.get("name") or "policy_qa"
    if name not in MANAGED_PROMPTS:
        name = "policy_qa"

    snapshot = await prompt_service.get_active(db, name)
    versions = await prompt_service.list_versions(db, name)

    # 查看历史版本：只读展示，不切换生效版本
    view_version = None
    v_param = request.query_params.get("v") or ""
    if v_param.isdigit():
        view_version = next((v for v in versions if v.version == int(v_param)), None)

    body = draft_content if draft_content is not None else (
        view_version.content if view_version else snapshot.text
    )

    return _render(
        request,
        "prompts.html",
        user=user,
        nav="prompts",
        names=MANAGED_PROMPTS,
        name=name,
        summary=await prompt_service.summary(db),
        snapshot=snapshot,
        versions=versions,
        view_version=view_version,
        body=body,
        soft_notes=_soft_notes(name, body),
        preview=preview,
        draft_question=draft_question,
    )


@router.get("/admin/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    return await _render_prompts(request, db, user)


@router.post("/admin/prompts/{name}")
async def prompts_save(
    request: Request,
    name: str,
    db: DbDep,
    content: str = Form(...),
    note: str = Form(""),
):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        row = await prompt_service.save_new_version(
            db, name=name, content=content, note=note, user_id=user.id
        )
    except AppError as exc:
        return _back(f"/admin/prompts?name={name}", err=str(exc))
    return _back(f"/admin/prompts?name={name}", msg=f"已保存为 v{row.version}，下一次提问即生效")


@router.post("/admin/prompts/{name}/rollback")
async def prompts_rollback(request: Request, name: str, db: DbDep, version: int = Form(...)):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        row = await prompt_service.rollback(
            db, name=name, target_version=version, user_id=user.id
        )
    except AppError as exc:
        return _back(f"/admin/prompts?name={name}", err=str(exc))
    return _back(
        f"/admin/prompts?name={name}",
        msg=f"已回滚：v{version} 的正文复制为 v{row.version} 并生效",
    )


@router.post("/admin/prompts/{name}/preview", response_class=HTMLResponse)
async def prompts_preview(
    request: Request,
    name: str,
    db: DbDep,
    content: str = Form(...),
    question: str = Form(...),
):
    """试跑：不保存、不写会话，只把"这版提示词会得到什么回答"摊开给管理员看。"""
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked

    from app.agents import guardrails
    from app.agents.gateway.base import get_llm
    from app.agents.rag import build_messages, retrieve
    from app.services.prompt_service import PromptSnapshot

    hits = await retrieve(db, question) if question.strip() else []
    if not hits:
        preview = {
            "ok": False,
            "answer": (
                "请先填写一个试跑问题。"
                if not question.strip()
                else "知识库无命中：当前没有已发布的制度能支撑这个问题，预览看不到引用效果。"
            ),
            "citations": [],
            "flags": [],
        }
    else:
        # 用未保存的草稿正文构造临时快照，绝不落库
        draft = PromptSnapshot(
            name=name, version=0, text=content.strip(), sha="preview", source="preview"
        )
        messages, _ = build_messages(question, hits, None, prompt=draft)
        result = await get_llm().chat(messages)
        answer, citations, flags = guardrails.apply_output_guardrails(result.text, len(hits))
        preview = {
            "ok": True,
            "answer": answer,
            "citations": [h.to_citation(n) for n, h in enumerate(hits, 1) if n in citations],
            "flags": sorted(flags.keys()),
            # 护栏是否改写过：管理员看到的必须和医护最终看到的一致
            "rewritten": answer != result.text,
            "model": result.model,
        }

    return await _render_prompts(
        request, db, user, preview=preview, draft_content=content, draft_question=question
    )


# ------------------------------------------------------------------ 对话审计


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_list(request: Request, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked

    qp = request.query_params
    user_id = int(qp["user_id"]) if (qp.get("user_id") or "").isdigit() else None
    only_flagged = qp.get("only_flagged") == "1"
    only_marked = qp.get("only_marked") == "1"
    page = max(1, int(qp["page"])) if (qp.get("page") or "").isdigit() else 1

    data = await audit_service.query_sessions(
        db,
        user_id=user_id,
        date_from=_parse_date(qp.get("date_from")),
        date_to=_parse_date(qp.get("date_to")),
        only_flagged=only_flagged,
        only_marked=only_marked,
        page=page,
        page_size=20,
    )
    return _render(
        request,
        "audit_list.html",
        user=user,
        nav="audit",
        data=data,
        staff=await audit_service.list_staff(db),
        filters={
            "user_id": user_id,
            "date_from": qp.get("date_from") or "",
            "date_to": qp.get("date_to") or "",
            "only_flagged": only_flagged,
            "only_marked": only_marked,
        },
        level_labels=audit_service.LEVEL_LABELS,
    )


@router.get("/admin/audit/{session_id}", response_class=HTMLResponse)
async def audit_detail(request: Request, session_id: int, db: DbDep):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        detail = await audit_service.session_detail(db, session_id)
    except AppError as exc:
        return _back("/admin/audit", err=str(exc))
    return _render(
        request,
        "audit_detail.html",
        user=user,
        nav="audit",
        detail=detail,
        level_labels=audit_service.LEVEL_LABELS,
    )


@router.post("/admin/audit/{session_id}/mark")
async def audit_mark(
    request: Request,
    session_id: int,
    db: DbDep,
    level: str = Form("high_risk"),
    note: str = Form(""),
    message_id: str = Form(""),
):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        await audit_service.add_mark(
            db,
            session_id=session_id,
            admin_id=user.id,
            level=level,
            note=note,
            message_id=int(message_id) if message_id.isdigit() else None,
        )
    except AppError as exc:
        return _back(f"/admin/audit/{session_id}", err=str(exc))
    return _back(f"/admin/audit/{session_id}", msg="已标记，审计记录已留痕")


@router.post("/admin/audit/marks/{mark_id}/revoke")
async def audit_revoke(request: Request, mark_id: int, db: DbDep, session_id: int = Form(...)):
    user, blocked = await _guard(request, db)
    if blocked:
        return blocked
    try:
        await audit_service.revoke_mark(db, mark_id=mark_id, admin_id=user.id)
    except AppError as exc:
        return _back(f"/admin/audit/{session_id}", err=str(exc))
    return _back(f"/admin/audit/{session_id}", msg="已撤销标记（记录保留，撤销人已留痕）")
