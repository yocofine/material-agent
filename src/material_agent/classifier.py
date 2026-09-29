"""文件分类：规则优先，规则未命中走 LLM，LLM 低置信度落 Other 待确认。

分级策略（来自流程图「判断技巧」）：
1. 先按文件名关键词命中。
2. 文件名模糊时，扫正文首段关键词。
3. 都未命中 → LLM 语义分类。
4. LLM 置信度低或不可用 → Other + needs_confirmation。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path
from typing import Callable

from .constants import CATEGORY_RULES, FALLBACK_CATEGORY, Category
from .extraction import extract_text
from .llm import QwenClient
from .models import FileItem

logger = logging.getLogger(__name__)

# 规则命中基准置信度
_RULE_CONFIDENCE = 0.9
# LLM 判定为 Other 的置信度阈值，低于此值强制标记待确认
_LLM_CONFIRM_THRESHOLD = 0.6

# 值得抽正文 + 调 LLM 的文档类型；其余（csv/xlsx/zip...）直接落 Other
_DOC_TYPES = {
    ".pdf", ".docx", ".ppt", ".pptx", ".txt", ".md", ".html", ".htm", ".csv", ".xlsx",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp",
}

# 上传页「输入框文字」由 api_server 存成 text_input/input_<hex>.md。
# 这段文字是用户随手写的说明，不是课程资料；业务约定：一律固定归 Other，
# 既不跑关键词规则，也不调用大模型（结果可预期、不消耗 LLM 额度）。
_TEXT_INPUT_DIR = "text_input"
_TEXT_INPUT_NOTE = "来自上传页输入框的文字，按约定固定归入 Other"


def is_text_input_item(item: FileItem) -> bool:
    """判断条目是否来自上传页的「输入框文字」（text_input/input_<hex>.md）。"""
    for raw in (item.path_context, item.local_path, item.original_name):
        if not raw:
            continue
        segments = [seg for seg in str(raw).replace("\\", "/").split("/") if seg]
        if _TEXT_INPUT_DIR in segments:
            return True
    return False


def _force_other_for_text_input(item: FileItem) -> FileItem:
    """把输入框文字固定归入 Other：不跑规则、不调 LLM、不标待确认。"""
    item.category = Category.OTHER.value
    item.confidence = 1.0
    item.classified_by = "manual"
    item.matched_keyword = _TEXT_INPUT_DIR
    item.needs_confirmation = False
    item.note = _TEXT_INPUT_NOTE
    return item


# 学生作品后校验：文件名/正文含这些「作业布置」信号时，学生作品判定存疑
_STUDENT_GUARD = ("assessment", "brief", "description", "rubric", "requirement", "assignment")

# Strong filename/content signals are evaluated before the general keyword
# table. This prevents a broad word such as "reading" from overriding a more
# specific document type such as an exam paper or a lecture handout.
_STRONG_FILENAME_RULES: tuple[tuple[Category, tuple[str, ...]], ...] = (
    (
        Category.PAST_PAPER,
        (
            "past paper", "past exam", "previous exam", "exam paper", "old paper",
            "examination", "exam", "final exam", "midterm", "mid-semester",
            "真题", "试卷", "考试", "历年", "往年",
        ),
    ),
    (
        Category.REQUIREMENT,
        (
            "assessment", "assignment", "assignment brief", "assessment brief",
            "task description", "assessment description", "coursework", "essay question",
            "作业", "考核", "任务书",
        ),
    ),
    (
        Category.UNIT_GUIDE,
        (
            "syllabus", "unit guide", "course outline", "course guide", "course profile",
            "unit outline", "learning objectives", "weekly schedule", "weekly content",
            "课程大纲", "教学计划", "教学大纲", "semester outline",
        ),
    ),
    (
        Category.MARKING_CRITERIA,
        ("rubric", "marking criteria", "mark scheme", "markscheme", "评分标准", "评分细则"),
    ),
    (
        Category.STUDENT_WORK,
        (
            "student completed", "student work", "my work", "my draft", "my essay",
            "draft", "speech", "presentation script", "演讲稿", "初稿", "我的作业",
        ),
    ),
    (
        Category.SAMPLE,
        ("sample", "exemplar", "model answer", "template", "planning sheet", "answer template", "范文", "样例", "模板"),
    ),
    (
        Category.LECTURE_NOTE,
        (
            "lecture", "lecture note", "lecture notes", "lecture slide", "slides", "slide deck",
            "presentation", "handout", "class activity", "notes on", "business and management research",
            "meeting your client", "课堂讲义", "课件", "幻灯片", "讲义",
        ),
    ),
    (
        Category.TUTOR_MATERIAL,
        (
            "tutorial", "tutorials", "tutor", "exercise", "exercises", "worksheet", "workbook",
            "revision", "assumed knowledge", "solutions to", "solution", "problem sheet",
            "practice questions", "tutorial questions", "questions", "ques", "task", "tasks",
            "辅导", "练习", "习题", "复习",
        ),
    ),
    (
        Category.READINGS,
        (
            "reading", "article", "journal", "textbook", "text", "chapter", "preface", "commentary",
            "references", "参考书", "文献", "阅读材料", "教材",
        ),
    ),
)

_CONTENT_RULES: tuple[tuple[Category, tuple[str, ...]], ...] = (
    (
        Category.PAST_PAPER,
        ("time allowed", "answer any", "section a", "section b", "examination", "exam"),
    ),
    (
        Category.UNIT_GUIDE,
        ("learning objectives", "weekly schedule", "week begin", "course objectives", "course outline"),
    ),
    (
        Category.REQUIREMENT,
        ("assessment criteria", "submission deadline", "due date", "word count", "final grade", "assignment requirements"),
    ),
    (
        Category.TUTOR_MATERIAL,
        ("learning outcomes covered", "assumed knowledge", "questions marked", "tutorial", "solutions to"),
    ),
    (
        Category.LECTURE_NOTE,
        ("slide 1", "lecture 1", "lecture notes", "class activity", "handouts"),
    ),
    (
        Category.READINGS,
        ("table of contents", "acknowledgements", "annual review", "journal of", "doi:"),
    ),
)

# 结构化作业要求字段。许多学校模板不会写 "assignment brief"，而是用表格
# 或标签展示 Word limit / Deadline / Submission method 等字段；这些字段的
# 组合比单个宽泛词更可靠。
_REQUIREMENT_FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "deadline",
        ("submission deadline", "due date", "deadline", "submit by", "submission date", "截止日期", "提交截止"),
    ),
    (
        "length",
        ("word count", "word limit", "word length", "maximum words", "max words", "字数", "字数要求"),
    ),
    (
        "submission",
        ("submission method", "submission format", "how to submit", "submit via", "upload to", "提交方式", "提交格式", "提交要求"),
    ),
    (
        "assessment",
        ("assessment task", "task instructions", "assignment instructions", "task requirements", "instructions", "作业说明", "任务要求", "考核要求"),
    ),
    (
        "grading",
        ("mark allocation", "marks available", "marking", "weighting", "worth", "grading criteria", "评分标准", "分值", "占比"),
    ),
)

_NORM_RE = re.compile(r"[\W_]+")

_CLASSROOM_DECK_FILENAME_SIGNALS = (
    "day", "week", "session", "class", "lecture", "slides", "handout",
)
_CLASSROOM_DECK_CONTENT_SIGNALS = (
    "today's class", "today's activities", "what we will cover", "what you will get from today",
    "course learning outcomes", "students who successfully complete", "this unit",
    "acknowledgement of country",
)
_EXPLICIT_NON_LECTURE_FILENAME_SIGNALS = (
    "assignment", "assessment", "rubric", "mark scheme", "exam", "past paper",
    "speech", "presentation script", "my draft", "my essay", "student work",
)
_PAST_PAPER_FILENAME_SIGNALS = (
    "past paper", "past exam", "previous exam", "exam paper", "old paper",
    "mock exam", "practice exam", "sample exam", "真题", "试卷", "历年考试", "往年试卷",
)
_PAST_PAPER_CONTENT_SIGNALS = (
    "time allowed", "answer any", "section a", "section b", "exam question",
    "examination question", "question paper", "total marks", "candidate number",
    "instructions to candidates", "answer booklet", "考试时间", "试题", "考生须知",
)
_PAST_PAPER_WEAK_FILENAME_SIGNALS = (
    "p1", "p2", "p3", "ques", "question", "questions", "problem", "problems",
    "part", "practice", "worksheet", "exercise", "exercises", "week", "s1", "s2",
)
_TEACHING_CONTENT_SIGNALS = (
    "learning objectives", "learning outcomes", "key concepts", "discussion questions",
    "case study", "role play", "today's topic", "today's class", "today's activities",
    "class activity", "in class", "course content", "what we will cover", "workshop",
    "课堂目标", "学习目标", "课堂活动", "案例分析",
)
_PUBLICATION_CONTENT_SIGNALS = (
    "abstract", "keywords", "doi", "journal", "volume", "issue", "published by",
    "publication information", "received", "accepted", "bibliography",
)
_INDEPENDENT_EXERCISE_SIGNALS = (
    "worksheet", "workbook", "tutorial", "solutions", "answers", "exercise sheet",
    "练习册", "习题", "答案",
)
_SERIES_PROTECTED_CATEGORIES = {
    Category.REQUIREMENT.value,
    Category.UNIT_GUIDE.value,
    Category.MARKING_CRITERIA.value,
    Category.STUDENT_WORK.value,
}
_SERIES_RECONCILE_CATEGORIES = {
    Category.LECTURE_NOTE.value,
    Category.TUTOR_MATERIAL.value,
    Category.READINGS.value,
    Category.PAST_PAPER.value,
    Category.OTHER.value,
    Category.ADDITIONAL.value,
}


def _norm(s: str) -> str:
    """归一化：去除非字母数字字符（保留中文），转小写。"""
    return _NORM_RE.sub("", s.lower())


def _is_phrase(kw: str) -> bool:
    """是否为「词组」（含空格或中文）。正文兜底只用词组，避免单词级误命中。"""
    return " " in kw or any("\u4e00" <= c <= "\u9fff" for c in kw)


def _looks_like_classroom_deck(filename: str, body: str) -> bool:
    """识别「课堂演示文稿」，避免课件中的 exercise/activity 被误判为辅导资料。"""
    ext = Path(filename).suffix.lower()
    if ext not in {".ppt", ".pptx"}:
        return False

    fname = _norm(filename)
    if any(_norm(signal) in fname for signal in _EXPLICIT_NON_LECTURE_FILENAME_SIGNALS):
        return False

    filename_signal = any(_norm(signal) in fname for signal in _CLASSROOM_DECK_FILENAME_SIGNALS)
    content_signal = any(_norm(signal) in body for signal in _CLASSROOM_DECK_CONTENT_SIGNALS)
    has_course_code = bool(re.search(r"\b[A-Za-z]{2,}\d{3,}\b", filename))
    return content_signal or (filename_signal and has_course_code)


def _requirement_field_hit(body: str) -> str | None:
    """Return a Requirement hit when several independent brief fields are present."""
    matched_groups: list[str] = []
    matched_labels: list[str] = []
    for group, keywords in _REQUIREMENT_FIELD_GROUPS:
        for keyword in keywords:
            if _norm(keyword) in body:
                matched_groups.append(group)
                matched_labels.append(keyword)
                break
    # Two different structured fields are strong evidence. A single field such
    # as "length" or "instructions" is intentionally insufficient.
    if len(set(matched_groups)) >= 2:
        return "requirement fields: " + " + ".join(matched_labels[:3])
    return None


def _has_past_paper_evidence(filename: str, body: str) -> bool:
    """Require explicit exam evidence before allowing Past Paper."""
    fname = _norm(filename)
    normalized_body = _norm(body)
    return (
        any(_norm(signal) in fname for signal in _PAST_PAPER_FILENAME_SIGNALS)
        or any(_norm(signal) in normalized_body for signal in _PAST_PAPER_CONTENT_SIGNALS)
    )


def _has_weak_paper_like_filename(filename: str) -> bool:
    """Return whether a name only looks paper-like through weak metadata."""
    fname = _norm(filename)
    has_year = bool(re.search(r"(?:19|20)\d{2}", filename))
    return has_year or any(_norm(signal) in fname for signal in _PAST_PAPER_WEAK_FILENAME_SIGNALS)


def _series_key(filename: str) -> str:
    """Normalize filenames so consecutive yearly/numbered files group together."""
    stem = Path(filename.replace("\\", "/")).name.rsplit(".", 1)[0].lower()
    stem = re.sub(r"(?:19|20)\d{2}", " year ", stem)
    stem = re.sub(r"\b(?:s|sem|semester|term)\s*([12])\b", r" semester\1 ", stem)
    stem = re.sub(r"\b(?:week|wk|lecture|lec|chapter|ch|part|p|q)\s*\d+[a-z]?\b", lambda m: re.sub(r"\d+[a-z]?", "num", m.group(0)), stem)
    stem = re.sub(r"\b\d+[a-z]?\b", " num ", stem)
    stem = re.sub(r"\b(?:draft|final|copy|version|ver|v)\s*num\b", " version ", stem)
    return _norm(stem)


def _series_target_category(items: list[FileItem]) -> str | None:
    """Choose a stable category for a same-series group, if one is safe."""
    candidates = [
        item for item in items
        if item.category in _SERIES_RECONCILE_CATEGORIES
        and item.category not in _SERIES_PROTECTED_CATEGORIES
    ]
    if len(candidates) < 2:
        return None

    category_counts = Counter(item.category for item in candidates if item.category)
    if len(category_counts) <= 1:
        return None

    non_other_counts = Counter(
        item.category for item in candidates
        if item.category and item.category != Category.OTHER.value
    )
    if non_other_counts:
        target, _ = non_other_counts.most_common(1)[0]
    else:
        target, _ = category_counts.most_common(1)[0]

    if target == Category.PAST_PAPER.value:
        strong_exam_count = sum(
            _has_past_paper_evidence(item.original_name, item.extracted_text)
            for item in candidates
        )
        # Do not let weak year/semester/question metadata make the whole series
        # Past Paper. At least half the group needs explicit exam evidence.
        if strong_exam_count * 2 < len(candidates):
            fallback_counts = Counter(
                item.category for item in candidates
                if item.category and item.category not in {Category.PAST_PAPER.value, Category.OTHER.value}
            )
            if fallback_counts:
                target = fallback_counts.most_common(1)[0][0]
            elif any(_has_weak_paper_like_filename(item.original_name) for item in candidates):
                target = Category.OTHER.value
            else:
                return None
    return target


def reconcile_series_categories(files: list[FileItem]) -> list[FileItem]:
    """Keep highly similar consecutive filenames in one category."""
    groups: dict[str, list[FileItem]] = {}
    for item in files:
        key = _series_key(item.original_name)
        if len(key) >= 6:
            groups.setdefault(key, []).append(item)

    for group in groups.values():
        if len(group) < 2:
            continue
        target = _series_target_category(group)
        if target is None:
            continue
        for item in group:
            if item.category == target or item.category in _SERIES_PROTECTED_CATEGORIES:
                continue
            if item.category == Category.PAST_PAPER.value and _has_past_paper_evidence(
                item.original_name, item.extracted_text
            ):
                continue
            previous = item.category or Category.OTHER.value
            item.category = target
            item.confidence = min(max(item.confidence, 0.75), _RULE_CONFIDENCE)
            item.classified_by = item.classified_by or "rule"
            item.matched_keyword = item.matched_keyword or "same-series consistency"
            item.needs_confirmation = item.needs_confirmation and target == Category.OTHER.value
            note = f"同组连续文件名高度相似，按系列一致性规则从 {previous} 修正为 {target}"
            item.note = f"{item.note}；{note}" if item.note else note
    return files


def _looks_like_teaching_material(body: str) -> bool:
    """Recognize lesson handouts whose filenames lack lecture/slides keywords."""
    teaching_hits = sum(_norm(signal) in body for signal in _TEACHING_CONTENT_SIGNALS)
    publication_hits = sum(_norm(signal) in body for signal in _PUBLICATION_CONTENT_SIGNALS)
    exercise_hits = sum(_norm(signal) in body for signal in _INDEPENDENT_EXERCISE_SIGNALS)
    # A pair of teaching-structure signals outweighs a generic academic topic;
    # publication metadata or a standalone exercise pack keeps its category.
    return teaching_hits >= 2 and publication_hits < 2 and exercise_hits < 2


def classify_by_rules(filename: str, text_snippet: str) -> tuple[Category, str] | None:
    """按关键词匹配。**文件名优先**，文件名未命中才扫正文。"""
    # 特殊规则：UNSW 课程大纲标准命名 CO_xxx（Course Outline）
    base = filename.replace("\\", "/").split("/")[-1].lower()
    if base.startswith("co_"):
        return Category.UNIT_GUIDE, "CO_ (course outline)"

    fname = _norm(filename)
    body = _norm(text_snippet)

    # PPT/PPTX 课堂课件可能包含 exercises、activities 或 questions，但它仍是
    # 老师用于整堂课讲授的 deck。该判断放在 Tutor Material 规则之前。
    if _looks_like_classroom_deck(filename, body):
        return Category.LECTURE_NOTE, "classroom presentation deck"

    # 1) 强文件名信号优先。更具体的类别必须先于通用词匹配。
    for category, keywords in _STRONG_FILENAME_RULES:
        for kw in keywords:
            if _norm(kw) in fname:
                if category is Category.PAST_PAPER and not _has_past_paper_evidence(filename, text_snippet):
                    continue
                return category, kw

    # 结构化要求字段优先于正文中的 reading/article 等宽泛词。
    requirement_hit = _requirement_field_hit(body)
    if requirement_hit is not None:
        return Category.REQUIREMENT, requirement_hit

    # Teaching handouts and classroom case materials may have titles such as
    # "Business and management research" or "Meeting your client" without
    # saying lecture/slides in the filename. Keep them ahead of Readings when
    # the body has a clear lesson structure rather than publication metadata.
    if _looks_like_teaching_material(body):
        return Category.LECTURE_NOTE, "teaching material structure"

    # Course-code + semester files are usually the official unit guide even
    # when the filename does not contain "syllabus" or "outline".
    if re.search(r"\b[A-Za-z]{2,}\d{3,}\b", filename) and re.search(
        r"\b(?:semester|term)\s*[123]\b", filename, re.I
    ):
        return Category.UNIT_GUIDE, "course code + semester"

    # 2) 内容中的强结构信号。只接受短语，不接受单个宽泛单词。
    for category, keywords in _CONTENT_RULES:
        for kw in keywords:
            if _norm(kw) in body:
                if category is Category.PAST_PAPER and not _has_past_paper_evidence(filename, text_snippet):
                    continue
                return category, kw

    # Academic papers are often named only with author names and a year, so
    # they need a dedicated fallback after exam/unit-guide signals. A bare
    # year is intentionally not enough to classify a Past Paper.
    raw_base = base.rsplit(".", 1)[0]
    has_year = bool(re.search(r"(?:19|20)\d{2}|(?:^|[^0-9])\d{2}(?:$|[^0-9])", raw_base))
    has_author_pattern = bool(re.search(r"\bet\s+al\b|\band\b|[_&,]", raw_base))
    if has_year and has_author_pattern:
        return Category.READINGS, "author/year scholarly filename"

    # 3) 通用文件名关键词表（保留业务规则表的可扩展性）
    for rule in CATEGORY_RULES:
        for kw in rule.keywords:
            k = _norm(kw)
            if k and k in fname:
                if rule.category is Category.PAST_PAPER and not _has_past_paper_evidence(filename, text_snippet):
                    continue
                return rule.category, kw

    # 4) 正文首段兜底（只认词组，不认单词——单词在正文太容易误命中）
    for rule in CATEGORY_RULES:
        for kw in rule.keywords:
            if not _is_phrase(kw):
                continue
            k = _norm(kw)
            if k and k in body:
                if rule.category is Category.PAST_PAPER and not _has_past_paper_evidence(filename, text_snippet):
                    continue
                return rule.category, kw
    return None


def _extract_with_timeout(path: str) -> str:
    """Extract text in a killable child process; timeout really stops the work.

    PyInstaller 打包后 sys.executable 是 exe 本身，不能再用于启动子进程，
    因此 frozen 模式下直接在当前进程内抽取文本。
    """
    from .config import get_settings

    if getattr(sys, "frozen", False):
        return extract_text(path)

    src_root = str(Path(__file__).resolve().parents[1])
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = src_root + (";" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    code = (
        "import sys; "
        "from material_agent.extraction import extract_text; "
        "sys.stdout.write(extract_text(sys.argv[1]))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=get_settings().extract_timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"文本抽取超时：{path}") from e
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _collect_multimodal_images(item: FileItem) -> list[tuple[str, str]]:
    """为多模态模型收集图片：OCR 已识别出文字时不发图片，OCR 无结果时才让模型看图。"""
    if not item.local_path:
        return []
    if item.extracted_text and item.extracted_text.strip():
        return []
    ext = Path(item.local_path).suffix.lower()
    images: list[tuple[str, str]] = []

    def _append(mime: str, data: bytes) -> None:
        images.append((mime, base64.b64encode(data).decode("ascii")))

    try:
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
            }[ext]
            _append(mime, Path(item.local_path).read_bytes())
        elif ext == ".pdf":
            import fitz  # PyMuPDF

            doc = fitz.open(item.local_path)
            try:
                for page_index in range(min(len(doc), 2)):
                    pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    _append("image/png", pix.tobytes("png"))
            finally:
                doc.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("收集多模态图片失败 %s：%s", item.original_name, exc)
    return images


def _apply_llm_result(item: FileItem, result: dict) -> FileItem:
    """把 LLM 结果写入 item，含后校验与低置信度标记。"""
    item.category = result["category"]
    item.confidence = result["confidence"]
    item.classified_by = "llm"
    item.note = result.get("reason", "")
    usage = result.get("usage", {})
    item.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    item.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    item.total_tokens = int(usage.get("total_tokens", 0) or 0)
    # LLM 可能把课程代码、年份或版本号误当成试卷证据；Past Paper 必须
    # 同时具备明确的考试文件名或考试结构，否则降级为 Other 待确认。
    if item.category == Category.PAST_PAPER.value and not _has_past_paper_evidence(
        item.original_name, item.extracted_text
    ):
        item.category = Category.OTHER.value
        item.confidence = min(item.confidence, 0.2)
        item.needs_confirmation = True
        item.note = "缺少明确考试证据，已从 Past Paper 降级为 Other"
    # 后校验：学生作品是学生本人写的，若文件含「作业布置」信号则存疑
    if item.category == Category.STUDENT_WORK.value:
        combined = _norm(item.original_name + " " + item.extracted_text)
        if any(_norm(k) in combined for k in _STUDENT_GUARD):
            item.needs_confirmation = True
            item.note = "疑似作业布置文档，非学生作品，待人工确认"
    # 低置信度或落 Other → 待学员确认
    if item.confidence < _LLM_CONFIRM_THRESHOLD or item.category == Category.OTHER.value:
        item.needs_confirmation = True
    return item


def classify_file(item: FileItem, llm: QwenClient | None, use_rules: bool = False) -> FileItem:
    """就地分类一个文件，返回更新后的 FileItem。"""
    started = time.perf_counter()
    try:
        # -1) 上传页输入框文字：按业务约定固定归 Other，不跑规则也不调 LLM
        if is_text_input_item(item):
            return _force_other_for_text_input(item)

        # 0) 非文档类型（压缩包、音视频等）不抽正文，直接 Other
        ext = Path(item.local_path).suffix.lower() if item.local_path else ""
        if ext not in _DOC_TYPES:
            item.category = FALLBACK_CATEGORY.value
            item.confidence = 0.0
            item.classified_by = "manual"
            item.needs_confirmation = True
            item.note = "文件格式不在规则内，已归入 Other"
            return item

        # 1) 抽取文本（带超时；超时则跳过该文件）
        if not item.extracted_text:
            try:
                item.extracted_text = _extract_with_timeout(item.local_path)
            except TimeoutError:
                item.timed_out = True
                item.category = FALLBACK_CATEGORY.value
                item.classified_by = "manual"
                item.needs_confirmation = True
                item.note = "文本抽取超时，已跳过"
                return item

        # 2) 规则命中（use_rules=False 时跳过，直接走 LLM）
        if use_rules:
            hit = classify_by_rules(item.original_name, item.extracted_text)
            if hit is not None:
                category, kw = hit
                item.category = category.value
                item.confidence = _RULE_CONFIDENCE
                item.classified_by = "rule"
                item.matched_keyword = kw
                return item

        # 3) LLM 语义分类
        llm_error: Exception | None = None
        if llm is not None and llm.available:
            try:
                result = llm.classify_document(
                    item.original_name,
                    item.extracted_text,
                    context=item.path_context,
                    images=_collect_multimodal_images(item),
                )
                return _apply_llm_result(item, result)
            except TimeoutError:
                item.timed_out = True
                item.category = FALLBACK_CATEGORY.value
                item.classified_by = "manual"
                item.needs_confirmation = True
                item.note = "LLM 调用超时，已跳过"
                return item
            except Exception as e:  # noqa: BLE001
                llm_error = e
                logger.warning("LLM 分类失败 %s：%s", item.original_name, e)

        # 4) 兜底：Other + 待确认
        item.category = FALLBACK_CATEGORY.value
        item.confidence = 0.0
        item.classified_by = "manual"
        item.needs_confirmation = True
        item.note = f"无法自动归类，待人工确认（LLM 错误：{llm_error}）" if llm_error else "无法自动归类，待人工确认"
        return item
    finally:
        item.elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))


def classify_all(
    files: list[FileItem],
    llm: QwenClient | None,
    use_rules: bool = False,
    concurrency: int = 1,
    on_progress: Callable[[int, int, FileItem], None] | None = None,
) -> list[FileItem]:
    """批量分类。concurrency>1 时用线程池并发。"""
    total = len(files)

    if concurrency <= 1 or total <= 1:
        results = []
        for i, f in enumerate(files, 1):
            results.append(classify_file(f, llm, use_rules=use_rules))
            if on_progress:
                on_progress(i, total, f)
        return reconcile_series_categories(results)

    results: list[FileItem] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        it = pool.map(lambda f: classify_file(f, llm, use_rules=use_rules), files)
        for i, r in enumerate(it, 1):
            results.append(r)
            if on_progress:
                on_progress(i, total, files[i - 1])
    return reconcile_series_categories(results)


async def classify_file_async(
    item: FileItem,
    llm: QwenClient | None,
    use_rules: bool = False,
    semaphore: asyncio.Semaphore | None = None,
) -> FileItem:
    """异步版 classify_file。"""
    from .config import get_settings

    started = time.perf_counter()
    try:
        # -1) 上传页输入框文字：按业务约定固定归 Other，不跑规则也不调 LLM
        if is_text_input_item(item):
            return _force_other_for_text_input(item)

        # 0) 非文档类型（压缩包、音视频等）不抽正文，直接 Other
        ext = Path(item.local_path).suffix.lower() if item.local_path else ""
        if ext not in _DOC_TYPES:
            item.category = FALLBACK_CATEGORY.value
            item.confidence = 0.0
            item.classified_by = "manual"
            item.needs_confirmation = True
            item.note = "文件格式不在规则内，已归入 Other"
            return item

        # 1) 抽取文本（线程池 + 超时）
        if not item.extracted_text:
            try:
                item.extracted_text = await asyncio.wait_for(
                    asyncio.to_thread(_extract_with_timeout, item.local_path),
                    timeout=get_settings().extract_timeout,
                )
            except TimeoutError:
                item.timed_out = True
                item.category = FALLBACK_CATEGORY.value
                item.classified_by = "manual"
                item.needs_confirmation = True
                item.note = "文本抽取超时，已跳过"
                return item

        # 2) 规则命中（use_rules=False 时跳过，直接走 LLM）
        if use_rules:
            hit = classify_by_rules(item.original_name, item.extracted_text)
            if hit is not None:
                category, kw = hit
                item.category = category.value
                item.confidence = _RULE_CONFIDENCE
                item.classified_by = "rule"
                item.matched_keyword = kw
                return item

        # 3) LLM 语义分类（异步，semaphore 控制并发）
        llm_error: Exception | None = None
        if llm is not None and llm.available:
            try:
                images = _collect_multimodal_images(item)
                if semaphore is not None:
                    async with semaphore:
                        result = await llm.classify_document_async(
                            item.original_name,
                            item.extracted_text,
                            context=item.path_context,
                            images=images,
                        )
                else:
                    result = await llm.classify_document_async(
                        item.original_name,
                        item.extracted_text,
                        context=item.path_context,
                        images=images,
                    )
                return _apply_llm_result(item, result)
            except TimeoutError:
                item.timed_out = True
                item.category = FALLBACK_CATEGORY.value
                item.classified_by = "manual"
                item.needs_confirmation = True
                item.note = "LLM 调用超时，已跳过"
                return item
            except Exception as e:  # noqa: BLE001
                llm_error = e
                logger.warning("LLM 分类失败 %s：%s", item.original_name, e)

        # 4) 兜底：Other + 待确认
        item.category = FALLBACK_CATEGORY.value
        item.confidence = 0.0
        item.classified_by = "manual"
        item.needs_confirmation = True
        item.note = f"无法自动归类，待人工确认（LLM 错误：{llm_error}）" if llm_error else "无法自动归类，待人工确认"
        return item
    finally:
        item.elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))


async def classify_all_async(
    files: list[FileItem],
    llm: QwenClient | None,
    use_rules: bool = False,
    concurrency: int = 20,
    on_progress: Callable[[int, int, FileItem], None] | None = None,
) -> list[FileItem]:
    """异步并发分类（AsyncOpenAI），结果顺序与输入一致。"""
    total = len(files)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def _run(i: int, f: FileItem) -> tuple[int, FileItem]:
        nonlocal done
        r = await classify_file_async(f, llm, use_rules=use_rules, semaphore=semaphore)
        done += 1
        if on_progress:
            on_progress(done, total, f)
        return i, r

    results_map: dict[int, FileItem] = {}
    for coro in asyncio.as_completed([_run(i, f) for i, f in enumerate(files)]):
        i, r = await coro
        results_map[i] = r
    return reconcile_series_categories([results_map[i] for i in range(total)])
