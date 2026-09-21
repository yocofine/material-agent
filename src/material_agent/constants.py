"""业务常量：分类规则、命名模板、核对清单、异常类型。

来源：<资料归集与核对 - 标准化流程图> 第②阶段分类表、第③命名规范、第④核对清单、第⑤异常分支。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# --------------------------------------------------------------------------- #
# 优先级
# --------------------------------------------------------------------------- #
class Priority(str, Enum):
    REQUIRED = "required"    # 必传（缺了要催补）
    OPTIONAL = "optional"    # 选传
    IMPORTANT = "important"  # 重要
    STUDENT = "student"      # 学生已完成的草稿


class PriorityLabel:
    REQUIRED = "必传"
    OPTIONAL = "选传"
    IMPORTANT = "重要"
    STUDENT = "学生"


# --------------------------------------------------------------------------- #
# 文件类别（英文归类名与图中一致）
# --------------------------------------------------------------------------- #
class Category(str, Enum):
    REQUIREMENT = "Requirement"
    UNIT_GUIDE = "Unit Guide (Syllabus)"
    READINGS = "Readings"
    MARKING_CRITERIA = "Marking Criteria"
    TUTOR_MATERIAL = "Tutor Material"
    SAMPLE = "Sample"
    LECTURE_NOTE = "Lecture note"
    ADDITIONAL = "Additional"
    PAST_PAPER = "Past Paper"
    STUDENT_WORK = "Student Completed Work"
    OTHER = "Other"


# --------------------------------------------------------------------------- #
# 分类规则表：优先级 + 文件名关键词（中英）+ 中文名 + 说明
# 规则命中顺序即列表顺序，先命中先生效。
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CategoryRule:
    category: Category
    priority: Priority
    zh_name: str
    keywords: tuple[str, ...]
    description: str


CATEGORY_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        Category.REQUIREMENT, Priority.REQUIRED, "作业要求/题目",
        ("assignment brief", "assessment brief", "task description", "assessment description",
         "brief", "requirement", "assignment", "essay question", "coursework",
         "作业要求", "题目", "考核要求", "任务书"),
        "本次作业/论文的题目与要求，含字数、格式、截止日期，属必传项",
    ),
    CategoryRule(
        Category.UNIT_GUIDE, Priority.REQUIRED, "课程大纲/教学计划",
        ("syllabus", "unit guide", "course outline", "course guide", "course profile",
         "unit outline", "课程大纲", "教学计划", "教学大纲", "syllabi"),
        "对应课程的官方大纲/教学计划，属必传项",
    ),
    CategoryRule(
        Category.MARKING_CRITERIA, Priority.OPTIONAL, "评分标准/rubric",
        ("rubric", "marking criteria", "grading", "criteria", "markscheme",
         "mark scheme", "评分标准", "评分细则", "给分标准"),
        "作业评分标准，需与本次作业匹配",
    ),
    CategoryRule(
        Category.PAST_PAPER, Priority.IMPORTANT, "历年真题/旧试卷",
        ("past paper", "past exam", "previous exam", "exam paper", "old paper",
         "真题", "试卷", "历年", "往年", "旧题"),
        "历年真题，需标注清楚年份/学期",
    ),
    CategoryRule(
        Category.STUDENT_WORK, Priority.STUDENT, "学生已完成的草稿",
        ("student completed", "my work", "my draft", "my essay", "draft",
         "student work", "草稿", "我的作业", "已完成", "初稿"),
        "学生本人已完成的草稿，需确认为本人作品",
    ),
    CategoryRule(
        Category.SAMPLE, Priority.OPTIONAL, "范文/样例",
        ("sample", "exemplar", "model answer", "template", "planning sheet", "answer template",
         "范文", "样例", "范例", "模板"),
        "范文或样例，供参考",
    ),
    CategoryRule(
        Category.READINGS, Priority.OPTIONAL, "推荐阅读材料",
        ("reading", "textbook", "article", "journal", "recommended reading",
         "required reading", "阅读材料", "教材", "参考书", "阅读清单", "文献"),
        "推荐/必读的阅读材料",
    ),
    CategoryRule(
        Category.LECTURE_NOTE, Priority.OPTIONAL, "课堂讲义/PPT",
        ("lecture", "ppt", "slide", "seminar", "workshop", "workbook", "lecture note",
         "讲义", "课件", "幻灯片", "授课"),
        "课堂讲义或课件",
    ),
    CategoryRule(
        Category.TUTOR_MATERIAL, Priority.OPTIONAL, "导师额外资料",
        ("tutor", "mentor", "supervisor", "导师", "辅导资料", "补充讲义"),
        "导师额外提供的资料",
    ),
    CategoryRule(
        Category.ADDITIONAL, Priority.OPTIONAL, "补充材料",
        ("additional", "supplementary", "appendix", "faq", "how to", "upload",
         "补充", "附加", "附录", "常见问题"),
        "补充材料；版本冲突时也归入此处并备注差异",
    ),
)

# 兜底类别：所有规则都命不中时的落点
FALLBACK_CATEGORY = Category.OTHER

# 必传类别集合（用于「缺必传项」校验）
REQUIRED_CATEGORIES = frozenset(
    r.category for r in CATEGORY_RULES if r.priority is Priority.REQUIRED
)


# --------------------------------------------------------------------------- #
# 命名模板
# --------------------------------------------------------------------------- #
# 例：Requirement_ACCT1001_Essay.pdf
NAMING_TEMPLATE = "{category}_{course_code}_{assignment_type}"

# 类别名含空格/括号，做文件名时归一化（Unit Guide (Syllabus) -> UnitGuide）
def safe_category_token(category: Category) -> str:
    return (
        category.value
        .replace(" (Syllabus)", "")
        .replace(" ", "")
        .replace("-", "")
    )


# --------------------------------------------------------------------------- #
# 核对清单（第④阶段，发送学员二次确认）
# --------------------------------------------------------------------------- #
VERIFY_CHECKLIST: tuple[str, ...] = (
    "Requirement 是否完整（含字数、格式、截止日期）",
    "Unit Guide (Syllabus) 是否对应正确课程",
    "Marking Criteria 是否与本次作业匹配",
    "Past Paper 年份/学期是否标注清楚",
    "Student Completed Work 是否为该学生本人作品",
    "所有必传项（*）均已上传且无报错",
)


# --------------------------------------------------------------------------- #
# 异常类型（第⑤阶段）
# --------------------------------------------------------------------------- #
class ExceptionType(str, Enum):
    CORRUPT_OR_ENCRYPTED = "corrupt_or_encrypted"   # 文件损坏/加密
    UNCLASSIFIABLE = "unclassifiable"               # 内容模糊无法归类
    MISSING_REQUIRED = "missing_required"           # 缺少必传项
    VERSION_CONFLICT = "version_conflict"           # 文件版本冲突（新旧大纲）
    UPLOAD_FAILED = "upload_failed"                 # 订单系统上传失败
    CONFIRMATION_REJECTED = "confirmation_rejected" # 人工核对未通过


EXCEPTION_HANDLERS = {
    ExceptionType.CORRUPT_OR_ENCRYPTED: "立即联系学生重新发送，暂停后续流程",
    ExceptionType.UNCLASSIFIABLE: "归入 Other，标注「待确认」，请学生说明",
    ExceptionType.MISSING_REQUIRED: "向学生发送缺项清单，催促补充",
    ExceptionType.VERSION_CONFLICT: "全部上传至 Additional，备注版本差异",
    ExceptionType.UPLOAD_FAILED: "保留失败文件，检查订单系统后重试上传",
    ExceptionType.CONFIRMATION_REJECTED: "等待人工指出需要调整的文件或信息",
}
