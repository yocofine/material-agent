"""文档文本抽取：pdf / docx / pptx / txt，图片可选 OCR。

只抽取「前 N 字符」用于分类与核对，不保留全文，控制 token 与体积。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_CHARS = 3000
_PDF_REQUIREMENT_MARKERS = (
    "assessment", "assignment", "task", "instruction", "word count", "word limit",
    "deadline", "due date", "submit", "submission", "marking", "marks", "weighting",
    "字数", "截止", "提交", "作业", "任务", "要求", "评分", "分值", "占比",
)
SUPPORTED_EXT = {
    ".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt", ".html", ".htm",
    ".csv", ".xlsx", ".png", ".jpg", ".jpeg", ".webp", ".bmp",
}


def extract_text(path: str | Path, max_chars: int = MAX_CHARS) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            return _pdf(p, max_chars)
        if ext == ".docx":
            return _docx(p, max_chars)
        if ext == ".pptx":
            return _pptx(p, max_chars)
        if ext in {".txt", ".md"}:
            return _plain(p, max_chars)
        if ext == ".csv":
            return _plain(p, max_chars)
        if ext == ".xlsx":
            return _xlsx(p, max_chars)
        if ext in {".html", ".htm"}:
            return _html(p, max_chars)
        if ext in {".doc", ".ppt"}:
            # 老格式暂不支持，返回空由 LLM/人工兜底
            logger.warning("旧版 Office 格式暂不支持解析：%s", p.name)
            return ""
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return _ocr(p, max_chars)
        logger.warning("不支持的扩展名 %s：%s", ext, p.name)
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning("抽取失败 %s：%s", p.name, e)
        return ""


def _pdf(path: Path, max_chars: int) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(str(path))
    pages = [page.get_text() for page in doc]
    full_text = " ".join(pages)
    if not full_text.strip():
        # Some assignment briefs are scanned PDFs with no text layer. Use the
        # installed Tesseract executable when available so fields such as
        # deadline and word limit can still reach the classifier.
        text = _pdf_ocr(doc, max_chars)
        doc.close()
        return text
    doc.close()
    if len(full_text) <= max_chars:
        return full_text[:max_chars]

    # Keep the document heading plus small windows around assignment fields.
    # These fields are often in a table or on page 2+, beyond the old
    # first-3000-character cutoff.
    chunks = [full_text[: max_chars // 2]]
    lowered = full_text.lower()
    for marker in _PDF_REQUIREMENT_MARKERS:
        start = lowered.find(marker.lower())
        if start < 0:
            continue
        window_start = max(0, start - 120)
        window_end = min(len(full_text), start + len(marker) + 220)
        chunk = full_text[window_start:window_end].strip()
        if chunk and chunk not in chunks:
            chunks.append(chunk)
    return " ".join(chunks)[:max_chars]


def _pdf_ocr(doc, max_chars: int) -> str:  # noqa: ANN001
    """OCR the first pages of a scanned PDF, if Tesseract is available."""
    executable = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not executable:
        logger.warning("PDF 没有文字层且未找到 Tesseract，跳过 OCR")
        return ""

    # Assignment metadata is normally near the front; limiting pages keeps an
    # uploaded scan from turning OCR into an unexpectedly long operation.
    page_count = min(len(doc), 8)
    language = os.environ.get("TESSERACT_LANG", "eng")
    parts: list[str] = []
    for index in range(page_count):
        try:
            pixmap = doc[index].get_pixmap(matrix=__import__("fitz").Matrix(1.5, 1.5), alpha=False)
            completed = subprocess.run(
                [executable, "stdin", "stdout", "-l", language],
                input=pixmap.tobytes("png"),
                capture_output=True,
                timeout=20,
                check=False,
            )
            if completed.returncode == 0:
                parts.append(completed.stdout.decode("utf-8", errors="ignore"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("PDF OCR 第 %s 页失败：%s", index + 1, exc)
            break
    return " ".join(parts)[:max_chars]


def _docx(path: Path, max_chars: int) -> str:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for tbl in d.tables:
        for row in tbl.rows:
            parts.append(" ".join(c.text for c in row.cells))
    return " ".join(parts)[:max_chars]


def _pptx(path: Path, max_chars: int) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                parts.append(shape.text)
    return " ".join(parts)[:max_chars]


def _xlsx(path: Path, max_chars: int) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            values = [str(value) for value in row if value is not None]
            if values:
                parts.append(" ".join(values))
            if len(" ".join(parts)) >= max_chars:
                workbook.close()
                return " ".join(parts)[:max_chars]
    workbook.close()
    return " ".join(parts)[:max_chars]


def _plain(path: Path, max_chars: int) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc)[:max_chars]
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


class _HTMLTextExtractor(HTMLParser):
    """从 HTML 抽取可见文本，跳过 script/style 标签。"""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            t = data.strip()
            if t:
                self.parts.append(t)


def _html(path: Path, max_chars: int) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
    except Exception:  # noqa: BLE001
        pass
    return " ".join(parser.parts)[:max_chars]


def _ocr(path: Path, max_chars: int) -> str:
    """图片 OCR（尽力而为，失败返回空）。需要 pytesseract + tesseract 可执行文件。"""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("未安装 pytesseract/Pillow，跳过图片 OCR：%s", path.name)
        return ""
    try:
        text = pytesseract.image_to_string(Image.open(path), lang="chi_sim+eng")
        return text[:max_chars]
    except Exception as e:  # noqa: BLE001
        logger.warning("OCR 失败 %s：%s", path.name, e)
        return ""
