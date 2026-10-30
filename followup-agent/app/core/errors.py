"""统一错误码与异常处理（前端只认这一套）。

为什么要统一：
  医护端不需要知道 FastAPI 的 422 校验细节，也不需要看英文的
  "String should have at least 1 character"。所有对外错误收敛成
  `{code, message, request_id}` 三段式：
    code       —— 稳定标识，前端可分支（AUTH_REQUIRED 就跳登录）
    message    —— 中文，可直接展示给医护
    request_id —— 报障时能定位到这一条日志

两条规则（对应接口设计的约定）：
  1. 响应头发出前的错误 → HTTP 状态码 + 本 envelope；
  2. SSE 已经开始输出后的错误 → 只能走 `event: error`，code 保持同名。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# code -> (HTTP 状态码, 默认中文信息)
ERRORS: dict[str, tuple[int, str]] = {
    # 认证与授权
    "AUTH_REQUIRED": (401, "未登录或登录已过期，请重新登录"),
    "AUTH_INVALID": (401, "登录凭证无效，请重新登录"),
    "FORBIDDEN": (403, "没有权限执行该操作"),
    # 资源
    "NOT_FOUND": (404, "请求的资源不存在"),
    "SESSION_NOT_FOUND": (404, "会话不存在或无权访问"),
    "DRAFT_NOT_FOUND": (404, "草稿不存在或无权访问"),
    "CHUNK_NOT_FOUND": (404, "引用片段不存在，或所属文档已归档"),
    "DOC_NOT_FOUND": (404, "文档不存在"),
    # 入参
    "QUESTION_EMPTY": (422, "问题不能为空"),
    "QUESTION_TOO_LONG": (422, "问题最多 2000 字，请精简后再问"),
    "VALIDATION_ERROR": (422, "请求参数不合法"),
    "CONFLICT": (409, "当前状态不允许该操作"),
    # 上游与服务
    "LLM_UPSTREAM": (502, "模型调用失败，请稍后重试"),
    "KB_UNAVAILABLE": (502, "知识库服务异常，请稍后重试"),
    "INTERNAL": (500, "服务内部错误，已记录日志"),
}

# 状态码 → 业务码（HTTPException 兜底映射）
_STATUS_TO_CODE = {
    400: "VALIDATION_ERROR",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    500: "INTERNAL",
}

# 字段名 → 中文标签（校验错误的人话翻译）
FIELD_LABELS = {
    "question": "问题",
    "session_id": "会话 id",
    "patient_ref": "患者标识",
    "base_date": "出院日",
    "items": "随访节点",
    "plan_type": "随访方案",
    "scheduled_at": "计划时间",
    "username": "用户名",
    "password": "密码",
    "file": "文件",
    "title": "文档标题",
}

# Pydantic 错误类型 → 中文描述
_TYPE_HINTS = {
    "missing": "不能为空",
    "string_too_short": "不能为空",
    "string_too_long": "过长",
    "string_type": "必须是文本",
    "int_parsing": "必须是整数",
    "int_type": "必须是整数",
    "date_from_datetime_parsing": "日期格式不正确（示例 2026-10-20）",
    "datetime_parsing": "时间格式不正确",
    "enum": "取值不在允许范围内",
    "greater_than_equal": "小于允许的最小值",
    "less_than_equal": "超过允许的最大值",
}


class AppError(Exception):
    """业务异常：路由里 `raise AppError("SESSION_NOT_FOUND")` 即可。"""

    def __init__(self, code: str, message: str | None = None, *, extra: dict | None = None):
        self.code = code
        self.status_code, default_message = ERRORS.get(code, (500, "服务内部错误"))
        self.message = message or default_message
        self.extra = extra or {}
        super().__init__(self.message)


def _body(code: str, message: str, request_id: str, extra: dict | None = None) -> dict:
    body = {"code": code, "message": message, "request_id": request_id}
    if extra:
        body.update(extra)
    return body


def _request_id(request: Request) -> str:
    """优先用上游传入的 X-Request-ID（便于网关串联），否则生成一个短 id。"""
    return getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID"
    ) or uuid.uuid4().hex[:8]


def _translate_validation(exc: RequestValidationError) -> tuple[str, str]:
    """把 Pydantic 的错误翻成中文，并给问题字段配上专用错误码。"""
    errors = exc.errors()
    if not errors:
        return "VALIDATION_ERROR", "请求参数不合法"
    first = errors[0]
    loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
    field = loc[-1] if loc else ""
    label = FIELD_LABELS.get(field, field or "参数")
    etype = str(first.get("type", ""))
    hint = _TYPE_HINTS.get(etype, "不合法")

    # 问题字段单独给码：前端可以据此聚焦输入框并提示"不能为空/太长"
    if field == "question":
        if etype in {"string_too_short", "missing"}:
            return "QUESTION_EMPTY", ERRORS["QUESTION_EMPTY"][1]
        if etype == "string_too_long":
            return "QUESTION_TOO_LONG", ERRORS["QUESTION_TOO_LONG"][1]
        return "QUESTION_EMPTY", f"问题格式不正确：{hint}"
    return "VALIDATION_ERROR", f"{label}{hint}"


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def register_exception_handlers(app: FastAPI) -> None:
    """在 create_app() 里调用一次。

    顺序上必须覆盖 Starlette 默认处理器，否则 404 会返回 {"detail": "Not Found"}，
    前端拿到的 envelope 就不一致了。
    """

    @app.middleware("http")
    async def _attach_request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        rid = _request_id(request)
        # 5xx 才算异常；业务性 4xx 只记 info，避免日志被正常的"会话不存在"刷满
        log = logger.error if exc.status_code >= 500 else logger.info
        log("[%s] %s %s -> %s", rid, request.method, request.url.path, exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, rid, exc.extra),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        rid = _request_id(request)
        code, message = _translate_validation(exc)
        logger.info("[%s] 参数校验失败 %s：%s", rid, request.url.path, message)
        return JSONResponse(status_code=422, content=_body(code, message, rid))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        rid = _request_id(request)
        code = _STATUS_TO_CODE.get(exc.status_code, "INTERNAL")
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
        # 已有中文 detail（如"会话不存在或无权访问"）直接沿用；
        # Starlette 自带的 "Not Found" / "Method Not Allowed" 是英文，换成中文默认值
        message = detail if detail and _has_chinese(detail) else ERRORS.get(
            code, (exc.status_code, "请求失败")
        )[1]
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(code, message, rid),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = _request_id(request)
        logger.exception("[%s] 未捕获异常 %s", rid, request.url.path)
        return JSONResponse(status_code=500, content=_body("INTERNAL", ERRORS["INTERNAL"][1], rid))
