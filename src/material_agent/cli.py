"""命令行入口。

用法：
    material-agent run --order-id O001 --source ./files.zip --course-code ACCT1001 --assignment-type Essay
    material-agent status --order-id O001
    material-agent resume --order-id O001 --approved
    material-agent list
    material-agent serve-mcp
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .graph import run_order, resume_order
from .models import OrderContext, OrderStatus
from .store import list_orders, load_order


def _cmd_run(args: argparse.Namespace) -> int:
    order = OrderContext(
        order_id=args.order_id,
        course_code=args.course_code or "",
        assignment_type=args.assignment_type or "",
        student_id=args.student_id or "",
        source=args.source,
    )
    result = run_order(order)
    o: OrderContext = result["order"]
    print(f"\n订单 {o.order_id} 状态：{o.status.value}")
    for f in result.get("files", []):
        mark = "✔" if f.uploaded else " "
        print(f"  [{mark}] {f.original_name} -> {f.category or '?'} (by {f.classified_by or '-'})")
    for e in result.get("exceptions", []):
        print(f"  ⚠ {e.type}: {e.message}")
    if o.status in (OrderStatus.AWAITING_CONFIRM, OrderStatus.EXCEPTION):
        print("\n流程已暂停，等待人工处理。")
        if o.status is OrderStatus.AWAITING_CONFIRM:
            print('  学员确认后执行：material-agent resume --order-id %s --approved' % o.order_id)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    order = load_order(args.order_id)
    if order is None:
        print(f"未找到订单 {args.order_id}")
        return 1
    print(f"订单 {order.order_id}：{order.status.value}")
    for f in order.files:
        print(f"  - {f.original_name} -> {f.category or '?'} (uploaded={f.uploaded})")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    result = resume_order(args.order_id, {"approved": args.approved})
    o = result.get("order")
    print(f"订单 {args.order_id} 恢复后状态：{o.status.value if o else 'unknown'}")
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    """对目录下所有文档做分类验证（递归，忽略 Mac 垃圾文件），打印结果。"""
    from pathlib import Path

    from .classifier import classify_all_async
    from .llm import QwenClient
    from .models import FileItem

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1

    unzipped = _unzip_recursive(root)
    if unzipped:
        print(f"已解压 {unzipped} 个 zip 文件")

    supported = {".pdf", ".docx", ".ppt", ".pptx", ".txt", ".md", ".html", ".htm", ".csv", ".xlsx", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    paths = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__MACOSX" in p.parts or p.name == ".DS_Store" or p.name.startswith("._"):
            continue
        if p.suffix.lower() not in supported:
            continue
        paths.append(p)
    if not paths:
        print(f"{root} 下没有可分类的文档")
        return 1

    items = [
        FileItem(
            order_id="CLASSIFY",
            original_name=p.name,
            local_path=str(p),
            path_context=str(p.relative_to(root)),
        )
        for p in paths
    ]
    llm = QwenClient()
    print(f"\n分类 {len(items)} 个文档（并发 {args.concurrency}）...")

    def _progress(done: int, _total: int, item: FileItem) -> None:
        print(f"\r  进度 {done}/{_total}  {item.original_name[:40]:<40}", end="", flush=True)

    results = asyncio.run(
        classify_all_async(
            items, llm, use_rules=args.use_rules,
            concurrency=args.concurrency, on_progress=_progress,
        )
    )
    print()

    print(f"\n共 {len(results)} 个文档：")
    for f in results:
        rel = str(Path(f.local_path).relative_to(root)) if f.local_path else f.original_name
        flag = "  [待确认]" if f.needs_confirmation else ""
        kw = f"  命中:{f.matched_keyword}" if f.matched_keyword else ""
        print(f"  {rel}")
        print(f"      -> {f.category or '?'}   (来源:{f.classified_by or '-'}, 置信度:{f.confidence:.2f}){kw}{flag}")
    _report_timeouts(results, "classify")
    return 0


def _common_parts(rels: list) -> tuple:
    """所有相对路径的公共前缀（目录层级）。"""
    parts_list = [list(p.parts) for p in rels]
    common = parts_list[0]
    for ps in parts_list[1:]:
        n = 0
        for a, b in zip(common, ps):
            if a != b:
                break
            n += 1
        common = common[:n]
    return tuple(common)


def _safe_dirname(name: str) -> str:
    """清理文件夹名中的 Windows 非法字符。"""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip() or "_"


def _report_timeouts(results, task_id: str) -> None:  # noqa: ARG001
    """打印超时跳过的文件。"""
    timed_out = [f for f in results if getattr(f, "timed_out", False)]
    if not timed_out:
        return
    print(f"\n⚠️ {len(timed_out)} 个文件超时被跳过：")
    for f in timed_out:
        print(f"  - {f.original_name}  ({f.note})")


def _unzip_recursive(root: Path) -> int:
    """原地递归解压 root 下所有 zip（含嵌套），解压后删除 zip 本体。

    返回解压的 zip 数量。幂等：跑完没有 zip 残留，下次再跑不重复解压。
    """
    from .archive_utils import extract_zip_recursive

    queue = [p for p in root.rglob("*.zip")]
    count = 0
    while queue:
        zp = queue.pop(0)
        if not zp.exists():
            continue
        try:
            extract_zip_recursive(zp, zp.parent)
            zp.unlink()
            count += 1
        except Exception as e:  # noqa: BLE001
            print(f"解压失败 {zp}：{e}")
    return count


def _cmd_organize(args: argparse.Namespace) -> int:
    """分类后按「课程/分类名」复制原文件到输出目录。"""
    import shutil
    from pathlib import Path

    from .classifier import classify_all_async
    from .llm import QwenClient
    from .models import FileItem

    root = Path(args.dir)
    out = Path(args.out)
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1

    unzipped = _unzip_recursive(root)
    if unzipped:
        print(f"已解压 {unzipped} 个 zip 文件")

    supported = {".pdf", ".docx", ".ppt", ".pptx", ".txt", ".md", ".html", ".htm", ".csv", ".xlsx", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    paths = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__MACOSX" in p.parts or p.name == ".DS_Store" or p.name.startswith("._"):
            continue
        if p.suffix.lower() not in supported:
            continue
        paths.append(p)
    if not paths:
        print(f"{root} 下没有可分类的文档")
        return 1

    # 分类（支持 --llm-only 纯 LLM 模式）
    items = [
        FileItem(
            order_id="ORGANIZE",
            original_name=p.name,
            local_path=str(p),
            path_context=str(p.relative_to(root)),
        )
        for p in paths
    ]
    # 并发分类并显示进度（--llm-only 时每个文档都调一次 LLM，较慢）
    llm = QwenClient()
    total = len(items)
    mode = "规则+LLM" if args.use_rules else "纯LLM"
    print(f"\n开始分类 {total} 个文档（{mode}，并发 {args.concurrency}）...")

    def _progress(done: int, _total: int, item: FileItem) -> None:
        print(f"\r  进度 {done}/{_total}  {item.original_name[:40]:<40}", end="", flush=True)

    results = asyncio.run(
        classify_all_async(
            items, llm, use_rules=args.use_rules,
            concurrency=args.concurrency, on_progress=_progress,
        )
    )
    print("\n分类完成")

    # 确定「课程」层级：公共前缀之后的第一段
    rels = [p.relative_to(root) for p in paths]
    common = _common_parts(rels)
    depth = len(common)

    # 复制到 out/课程/分类名/原文件名
    out.mkdir(parents=True, exist_ok=True)
    used: dict[tuple[str, str], set[str]] = {}
    counts: dict[str, int] = {}
    for f, p in zip(results, paths):
        rel = p.relative_to(root)
        course = rel.parts[depth] if len(rel.parts) > depth else "_root"
        cat_dir = _safe_dirname(f.category or "Other")
        dest_dir = out / course / cat_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        # 同名去重
        key = (course, cat_dir)
        used.setdefault(key, set())
        dest_name = p.name
        if dest_name in used[key]:
            stem, suffix = Path(dest_name).stem, Path(dest_name).suffix
            i = 2
            while f"{stem}_{i}{suffix}" in used[key]:
                i += 1
            dest_name = f"{stem}_{i}{suffix}"
        used[key].add(dest_name)

        shutil.copy2(p, dest_dir / dest_name)
        counts[f"{course}/{cat_dir}"] = counts.get(f"{course}/{cat_dir}", 0) + 1

    print(f"\n已复制 {len(results)} 个文件到 {out}")
    for k in sorted(counts):
        print(f"  {k}: {counts[k]} 个")
    _report_timeouts(results, "organize")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    for line in list_orders():
        print(line)
    return 0


def _cmd_serve_mcp(args: argparse.Namespace) -> int:  # noqa: ARG001
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="material-agent", description="资料归集与核对 AI Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="启动一条订单的全流程")
    pr.add_argument("--order-id", required=True)
    pr.add_argument("--source", required=True, help="压缩包路径 / 目录 / 直链 URL")
    pr.add_argument("--course-code", default="")
    pr.add_argument("--assignment-type", default="")
    pr.add_argument("--student-id", default="")
    pr.set_defaults(func=_cmd_run)

    ps = sub.add_parser("status", help="查询订单状态")
    ps.add_argument("--order-id", required=True)
    ps.set_defaults(func=_cmd_status)

    pm = sub.add_parser("resume", help="学员确认后恢复流程")
    pm.add_argument("--order-id", required=True)
    pm.add_argument("--approved", action="store_true", help="确认通过")
    pm.set_defaults(func=_cmd_resume)

    px = sub.add_parser("classify", help="对目录下文件做分类验证")
    px.add_argument("--dir", required=True, help="包含待分类文件的目录")
    px.set_defaults(use_rules=False)
    px.add_argument("--use-rules", action="store_true", help="显式启用规则优先分类（默认全部走 LLM）")
    px.add_argument("--llm-only", action="store_false", dest="use_rules", help=argparse.SUPPRESS)
    px.add_argument("--concurrency", type=int, default=20, help="并发数（默认 20）")
    px.set_defaults(func=_cmd_classify)

    po = sub.add_parser("organize", help="分类后按「课程/分类名」复制文件到输出目录")
    po.add_argument("--dir", required=True, help="包含待分类文件的目录")
    po.add_argument("--out", required=True, help="输出目录")
    po.set_defaults(use_rules=False)
    po.add_argument("--use-rules", action="store_true", help="显式启用规则优先分类（默认全部走 LLM）")
    po.add_argument("--llm-only", action="store_false", dest="use_rules", help=argparse.SUPPRESS)
    po.add_argument("--concurrency", type=int, default=20, help="并发数（默认 20）")
    po.set_defaults(func=_cmd_organize)

    pl = sub.add_parser("list", help="列出所有订单")
    pl.set_defaults(func=_cmd_list)

    pc = sub.add_parser("serve-mcp", help="启动 MCP Server")
    pc.set_defaults(func=_cmd_serve_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    # 根日志设 WARNING，挡住第三方库（httpx/httpcore/openai）的逐请求 INFO 刷屏；
    # 只放行本项目 material_agent 的 INFO/WARNING。
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("material_agent").setLevel(logging.INFO)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
