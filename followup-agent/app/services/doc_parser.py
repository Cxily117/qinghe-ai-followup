"""文档正文抽取：md / txt / pdf → 纯文本。

PDF 只做**文本层抽取**（pypdf），不做 OCR。这是个刻意的边界：
  扫描件抽出来是空的，如果静默入库，管理员会以为"上传成功了"，
  而实际上知识库里多了一份 0 切片的文档，问答永远检索不到它——
  这种"看起来成功"的失败，比直接报错危险得多。
所以抽不到文字时必须明确报错，让管理员去补文字版。
"""
from __future__ import annotations

import logging
from io import BytesIO

from app.core.errors import AppError

logger = logging.getLogger(__name__)

SUPPORTED_EXT = {".md", ".markdown", ".txt", ".pdf"}

# 文本层少于这个长度，基本可以判定为扫描件或空白页（正常制度条文不会这么短）
PDF_MIN_TEXT = 20


def _decode_text(raw: bytes) -> str:
    """中文制度文件常见 UTF-8 / GBK 两种编码，都试一次。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")  # GBK 的超集，兼容性更好
    except UnicodeDecodeError:
        raise AppError("UNSUPPORTED_FORMAT", "无法识别文件编码，请另存为 UTF-8 后再上传")


def _extract_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted:
            # 加密 PDF 直接拒绝：不猜密码，也不交给上层"看起来成功"
            raise AppError("PDF_PARSE_ERROR", "PDF 已加密，无法读取；请提供未加密的文件")
        texts = [(page.extract_text() or "") for page in reader.pages]
    except AppError:
        raise
    except Exception as exc:  # 损坏、乱码、非 PDF 伪装扩展名都落在这里
        logger.warning("PDF 解析失败：%s", exc)
        raise AppError("PDF_PARSE_ERROR")

    content = "\n\n".join(t.strip() for t in texts if t.strip())
    if len(content) < PDF_MIN_TEXT:
        raise AppError("PDF_NO_TEXT")
    logger.info("PDF 抽取完成：%s 页，%s 字", len(texts), len(content))
    return content


def extract_text(raw: bytes, ext: str) -> str:
    """按扩展名抽正文；不支持的格式抛 415。"""
    ext = ext.lower()
    if ext not in SUPPORTED_EXT:
        raise AppError("UNSUPPORTED_FORMAT")
    if ext == ".pdf":
        return _extract_pdf(raw)
    return _decode_text(raw)
