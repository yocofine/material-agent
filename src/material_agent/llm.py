"""Qwen (通义千问) 客户端封装，走 DashScope OpenAI 兼容接口。

同时提供同步（OpenAI）与异步（AsyncOpenAI）两种客户端。
默认模型 qwen3.7-plus，通过环境变量 QWEN_MODEL 覆盖。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI, OpenAI

from .config import get_settings
from .constants import Category

logger = logging.getLogger(__name__)

_CLASSIFY_SYSTEM = """你是教辅资料归档分类器。请根据 filename、path 和 content_snippet，选择一个且仅一个类别。

合法类别及判定标准：
- Requirement：学校/老师布置的作业、考核或提交要求。除了 assessment、assignment、assignment brief、coursework、task description 外，只要正文同时出现两个或更多独立的结构化字段，例如 Word count/Word limit、Deadline/Due date、Submission method/How to submit、Instructions、Marks/Weighting，就判为 Requirement。中文字段“字数、截止日期、提交方式、评分标准、分值、占比”也同样有效。不要因为正文出现 reading 就改成 Readings。
- Unit Guide (Syllabus)：课程大纲、课程说明、教学计划、学习目标、周计划。信号：syllabus、unit guide、course outline、course profile、learning objectives、weekly schedule、week begin、course objectives。
- Marking Criteria：评分标准、rubric、mark scheme、grading criteria。
- Past Paper 必须有明确考试场景证据，不能只因为文件是题目、练习、问题清单、带分值、带编号、带年份或带课程号就归为 Past Paper。文件名中的 P1、P2、P3、Ques、Question、Questions、Problem、Part、Practice、Worksheet、Exercise 不是 Past Paper 的充分证据。特别注意：除非内容中出现明确考试信号，否则应归为 Tutor Material。只有出现以下强考试信号时，才允许归为 Past Paper：past exam、exam paper、final exam、mid-semester exam、sample exam、mock exam、time allowed、reading time、writing time、candidate number、student number、do not open until instructed、closed book、open book exam、examination、invigilator、考试、试卷、考试时间、考生号、闭卷、开卷。如果文件主要是数学/统计/课程练习题、problem sheet、practice questions、tutorial questions、worksheet、workshop questions，并且没有明确考试封面、考试时间、考生信息、开闭卷说明或考试指令，应归为 Tutor Material。
- Lecture note：老师的课堂讲义、PPT、幻灯片、课堂 handout、课堂案例或技能教学材料。信号：lecture、lecture notes、slides、slide deck、presentation、handout、class activity、learning objectives、key concepts、discussion questions、case study、workshop、activity、role play。课程教学主题型 PDF，即使文件名没有 lecture，也可以是 Lecture note；例如 `Business and management research-1.pdf`、`Meeting your client.pdf` 都应判为 Lecture note。PPT/PPTX 文件如果具有课程代码，并按 Day/Week/Session/课堂顺序组织，即使正文含 exercise、activity 或 questions，也优先判为 Lecture note，因为这些是课堂演示中的环节。注意：学生自己的 presentation script/speech 属于 Student Completed Work。
- Tutor Material：独立的辅导课、tutorial、练习题、worksheet、workbook、revision、assumed knowledge、solutions、exercise 或 answers。只有单独用于练习、答题或讲解答案的资料才归 Tutor Material；嵌在完整课堂 PPT 里的 exercise/activity 不改变 Lecture note 分类。
- Readings：学术论文、期刊文章、新闻文章、教材、教材章节、preface、commentary、references、推荐阅读。必须有论文/出版物证据，例如 author、abstract、keywords、journal、volume、issue、DOI、publication information、references 或明确的 textbook/chapter 结构。`research`、`client`、`management`、`business` 等普通主题词不能单独触发 Readings；如果内容是教师用于课堂讲授、案例分析、技能训练或课堂活动的材料，应判为 Lecture note。
- Sample：范文、样例、可套用模板、planning sheet、model answer、exemplar。
- Student Completed Work：学生自己已经写完或准备提交的作品，例如 my essay、my draft、speech、presentation script、演讲稿。不能仅因为文件名有 presentation 就判 Student Completed Work，需有学生作品语气或 script/speech 信号。
- Additional：补充材料、FAQ、appendix、supplementary、how to、upload instructions，且不符合上面更具体的类别。
- Other：信息不足，无法可靠判断。

用户输入框正文规则：当 path 包含 `[USER_PASTED_TEXT]` 时，表示内容由用户直接粘贴到输入框。必须忽略 `input_xxx.md`、`用户输入文字.md` 等系统生成文件名，只根据 content_snippet 判断。只有正文明确出现补充材料、supplementary、appendix、FAQ、how to、upload instructions、附录、补充说明等证据时才允许归为 Additional；证据不足时归为 Other，禁止把输入框内容默认归为 Additional。

判定优先级（从高到低）：
1. Past Paper 的明确考试证据；年份只能作为辅助信息，不能单独触发。
2. Requirement 的 assignment/assessment 信号。
3. Unit Guide 的课程大纲、学习目标和周计划信号。
4. Lecture note、Tutor Material、Sample、Student Completed Work 的专属信号。
5. 学术论文、教材章节和新闻材料归 Readings。
6. 仍不确定才归 Other。

典型示例：
- Assessment.docx，正文含 final grade、weekly seminar、required reading -> Requirement。
- Media Analysis.pdf，正文含 Word limit、Deadline、Submission method 等多个要求字段 -> Requirement。
- psych746_S2_2024.pdf，正文含 examination、time allowed、answer any five -> Past Paper。
- ENGM90016 Engineering Management_gt_03_23.pdf，只有课程代码和 gt_03_23 版本编号、没有考试结构 -> Other。
- Learning Objectives.docx -> Unit Guide (Syllabus)。
- Weekly Schedule and Content.docx -> Unit Guide (Syllabus)。
- Lecture 1.pdf、Lecture-Notes-Part-1.pdf -> Lecture note。
- Business and management research-1.pdf、Meeting your client.pdf -> Lecture note（课程教学材料/课堂技能讲义，即使文件名没有 lecture）。
- LAW5081 - day nine.pptx、LAW5081 - Exercise2 - Secondary Sources draft to Research Essay.pptx -> Lecture note（完整课堂教学 deck，虽然包含 exercises）。
- calculus-exercises-1-week-1.pdf、solutions-to-calculus-exercises-1-week-1.pdf -> Tutor Material。
- bracci and op de beeck 2023.pdf、MATH1062-Calculus.pdf、Publics chapter.pdf -> Readings。
- 演讲稿.docx、presentation script.docx -> Student Completed Work。

重要约束：
1. 文件名专属信号优先于正文中的宽泛词。例如 Assessment 文件正文提到 reading，仍是 Requirement；Lecture 文件正文提到 article，仍是 Lecture note。
2. 年份、学期、课程代码和 gt_03_23 这类编号本身不能证明是 Past Paper；必须结合明确 exam/examination/paper/time allowed/answer any 等信号。普通年份论文仍归 Readings；无论文结构且无考试结构时归 Other。
3. 同一批上传文件之间存在命名关系时，必须保持系列一致性。如果多个文件名称高度相似，只在年份、周次、编号、大小写、空格、下划线或连字符上不同，应视为同一系列文件。同一系列文件通常应归入同一个分类，不应因为单个文件名大小写、年份或抽取文本多少不同而分到不同类别。
4. 对于 `课程号 + 学期 + 年份` 这类连续年份文件名，例如 `psych746_S2_2019.pdf`、`psych746_S2_2020.pdf`、`PSYCH746_S2_2024.pdf`，年份和 S1/S2 只是课程资料版本/学期标记，不是 Past Paper 证据。除非内容中出现明确考试场景信号，否则不要归为 Past Paper；如果同系列中大多数文件不是 Past Paper，应保持同系列分类一致。
5. 只输出 JSON，不要 Markdown，不要额外解释。JSON 必须严格使用：{\"category\": \"合法类别\", \"confidence\": 0到1之间的数字, \"reason\": \"不超过一句话\"}。
6.Readings 与 Lecture note 边界规则：

不要仅凭文件标题像书籍章节、概念标题、章节标题，就判断为 Readings。

Readings 必须有较强的外部教材/出版物/指定阅读来源证据，例如：textbook、chapter from textbook、publisher、author、edition、ISBN、copyright、book title、journal article、abstract、DOI、volume、issue、references、bibliography、reading list。

Lecture note 的强信号包括：课程内部连续编号讲义、lecture notes、course notes、subject notes、week/topic/module notes、definitions、theorems、proofs、worked examples、class examples、课堂讲授结构、课程内部页眉页脚、同一课程下版式一致的系列文件。

如果当前文件与同一上传包、同一课程或同一目录中的其他文件形成连续编号系列，例如 `1 Title.pdf`、`2 Title.pdf`、`3 Title.pdf`，并且这些文件版式、命名和内容结构高度相似，则应优先保持同一分类。

对于数学、统计、工程等课程，`1 The Language of Mathematics`、`2 The Fundamental Theorem of Algebra` 这类连续编号 PDF，如果没有出版社、作者、ISBN、版权页、DOI、期刊、参考文献等外部出版物证据，应优先归为 Lecture note，而不是 Readings。

标题像教材章节不等于 Readings；Readings 需要外部来源证据。Lecture note 需要课程讲授组织证据或同组连续性证据。"""


def _classify_messages(
    filename: str,
    text_snippet: str,
    context: str,
    images: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """构造分类消息。

    images 参数格式为 [(mime, base64_data), ...]，
    按 OpenAI 兼容的 content 数组方式发送，支持多模态模型直接看图。
    """
    snippet_chars = min(max(get_settings().classification_snippet_chars, 500), 12000)
    text_payload = json.dumps(
        {
            "filename": filename,
            "path": context,
            "content_snippet": text_snippet[:snippet_chars],
        },
        ensure_ascii=False,
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": text_payload}]
    if images:
        for mime, data in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                }
            )
    return [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": content},
    ]


def _parse_classify_result(raw: str, usage: Any = None) -> dict[str, Any]:
    """把 LLM 返回的 JSON 解析并归一化成 {category, confidence, reason}。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM 分类输出非 JSON: %r", raw[:200])
        return {
            "category": Category.OTHER.value,
            "confidence": 0.0,
            "reason": "parse_error",
            "usage": _usage_dict(usage),
        }

    category = data.get("category", Category.OTHER.value)
    legal = {c.value for c in Category}
    if category not in legal:
        # 尝试匹配别名（如 "Unit Guide" 或 "unit_guide"）
        for c in Category:
            norm = c.value.lower().replace(" ", "").replace("(syllabus)", "").replace("-", "")
            if category.lower().replace(" ", "").replace("_", "") == norm:
                category = c.value
                break
        else:
            category = Category.OTHER.value
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "category": category,
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": data.get("reason", ""),
        "usage": _usage_dict(usage),
    }


def _usage_dict(usage: Any) -> dict[str, int]:
    """Normalize OpenAI-compatible usage objects and dicts."""
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if isinstance(usage, dict):
        get = usage.get
    else:
        get = lambda key, default=0: getattr(usage, key, default)
    prompt = int(get("prompt_tokens", 0) or 0)
    completion = int(get("completion_tokens", 0) or 0)
    total = int(get("total_tokens", prompt + completion) or (prompt + completion))
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


class QwenClient:
    def __init__(self) -> None:
        s = get_settings()
        if not s.qwen_api_key:
            logger.warning("QWEN_API_KEY 未配置，LLM 分类将不可用（规则分类仍可运行）")
        self._client = OpenAI(api_key=s.qwen_api_key or "EMPTY", base_url=s.qwen_base_url)
        self._aclient = AsyncOpenAI(api_key=s.qwen_api_key or "EMPTY", base_url=s.qwen_base_url)
        self.model = s.qwen_model
        self.timeout = s.qwen_timeout

    @property
    def available(self) -> bool:
        return bool(get_settings().qwen_api_key)

    # ------------------------------------------------------------------ #
    # 同步
    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self.timeout,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except TimeoutError:
            raise
        except Exception as e:  # noqa: BLE001
            if "timeout" in type(e).__name__.lower():
                raise TimeoutError(f"LLM 调用超时：{e}") from e
            raise
        return resp

    def classify_document(
        self,
        filename: str,
        text_snippet: str,
        context: str = "",
        images: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """同步调用 LLM 分类，返回 {category, confidence, reason}。"""
        response = self.chat(
            _classify_messages(filename, text_snippet, context, images=images),
            json_mode=True,
        )
        raw = response.choices[0].message.content or ""
        return _parse_classify_result(raw, response.usage)

    # ------------------------------------------------------------------ #
    # 异步
    # ------------------------------------------------------------------ #
    async def achat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self.timeout,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._aclient.chat.completions.create(**kwargs)
        except TimeoutError:
            raise
        except Exception as e:  # noqa: BLE001
            if "timeout" in type(e).__name__.lower():
                raise TimeoutError(f"LLM 调用超时：{e}") from e
            raise
        return resp

    async def classify_document_async(
        self,
        filename: str,
        text_snippet: str,
        context: str = "",
        images: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """异步调用 LLM 分类，返回 {category, confidence, reason}。"""
        response = await self.achat(
            _classify_messages(filename, text_snippet, context, images=images),
            json_mode=True,
        )
        raw = response.choices[0].message.content or ""
        return _parse_classify_result(raw, response.usage)
