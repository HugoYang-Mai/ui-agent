"""ui-agent 命令行入口。

⚠️ **坐标系约束（第一优先级）**：脚本第 1 步即调用 ``SetProcessDpiAwareness(2)``，
必须早于 ``PIL`` / ``pyautogui`` / ``uiautomation`` 的导入，否则屏幕坐标会带 DPI 缩放偏差。

用法::

    .venv\\Scripts\\python.exe main.py info          # 打印 DPI / 屏幕环境
    .venv\\Scripts\\python.exe main.py selfcheck     # 跑只读自检（UIA + OCR）
    .venv\\Scripts\\python.exe main.py windows       # 枚举顶层窗口
    .venv\\Scripts\\python.exe main.py tree          # 打印前台窗口元素树
    .venv\\Scripts\\python.exe main.py find --match 保存
    .venv\\Scripts\\python.exe main.py text          # 全屏 OCR（返回文字及坐标）
    .venv\\Scripts\\python.exe main.py locate --match 保存
    .venv\\Scripts\\python.exe main.py click --match 保存 --yes   # 写操作，需显式授权
    .venv\\Scripts\\python.exe main.py audit --stats    # 审计统计（工具调用次数 / 兜底率 / 失败率）
    .venv\\Scripts\\python.exe main.py audit --tail     # 实时跟读最新审计文件
    .venv\\Scripts\\python.exe main.py audit --run <run_id>   # 按 run 回放调用序列
    .venv\\Scripts\\python.exe main.py mcp           # 以 stdio 启动 MCP Server（等价 serve_mcp.py）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _force_utf8_stdio() -> None:
    """把 stdout / stderr 固定为 UTF-8。

    Windows 控制台默认 GBK，直接打印界面上的中文文本会抛
    ``UnicodeEncodeError``；这里统一改为 UTF-8 并对不可编码字符做替换，
    保证 CLI 在任何代码页下都不会因输出编码崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 非标准流（重定向到管道）
            pass


_force_utf8_stdio()

# ----------------------------------------------------------- 1. 统一坐标系
from uiagent import dpi  # noqa: E402

dpi.enable_dpi_awareness(dpi.DPI_AWARENESS_PER_MONITOR_AWARE)

# ----------------------------------------------------------- 2. 其余导入
from uiagent import __version__  # noqa: E402
from uiagent.controller import UniversalController  # noqa: E402
from uiagent.logging_utils import setup_logging  # noqa: E402


def _controller(args) -> UniversalController:
    return UniversalController(search_timeout=getattr(args, "search_timeout", 2.0))


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------- 子命令
def cmd_info(args) -> int:
    ctrl = _controller(args)
    env = ctrl.environment()
    env["virtual_screen"] = list(ctrl.screen_rect())
    _dump(env)
    return 0


def cmd_windows(args) -> int:
    ctrl = _controller(args)
    if args.non_window:
        items = ctrl.list_top_level(include_invisible=args.all)
    else:
        items = ctrl.list_windows(include_invisible=args.all, include_non_window=args.all)
    _dump(
        {
            "count": len(items),
            "items": [w.to_dict() for w in items[: args.limit]],
        }
    )
    return 0


def cmd_tree(args) -> int:
    ctrl = _controller(args)

    def render(element, depth=0):
        if element is None:
            return []
        flag = "!" if not element.visible else " "
        lines = [
            f"{'  ' * depth}{flag}[{element.role}] {element.name[:60]!r} "
            f"bounds={tuple(element.bounds)} id={element.automation_id!r}"
        ]
        for child in element.children:
            lines.extend(render(child, depth + 1))
        return lines

    root = ctrl.get_active_window() if args.active else None
    tree = ctrl.get_element_tree(window=root, depth=args.depth)
    print("\n".join(render(tree)) or "(空树)")
    return 0


def cmd_find(args) -> int:
    ctrl = _controller(args)
    elements = ctrl.find_all_elements(
        name=args.match,
        role=args.role,
        exact=args.exact,
        max_depth=args.depth,
        limit=args.limit,
    )
    _dump({"count": len(elements), "items": [e.to_dict() for e in elements]})
    return 0


def cmd_text(args) -> int:
    ctrl = _controller(args)
    region = None
    if args.region:
        region = tuple(int(v) for v in args.region.split(","))
    blocks = ctrl.ocr_screen(region)
    _dump({"count": len(blocks), "items": [b.to_dict() for b in blocks[: args.limit]]})
    return 0


def cmd_locate(args) -> int:
    ctrl = _controller(args)
    result = ctrl.locate(args.match, use_ocr_fallback=not args.no_ocr, role=args.role)
    _dump(result.to_dict())
    return 0 if result.found else 1


def cmd_click(args) -> int:
    if not args.yes:
        print("拒绝执行：点击属于写操作，请追加 --yes 显式授权。")
        return 2
    ctrl = _controller(args)
    result = ctrl.click(
        target=args.match,
        button=args.button,
        clicks=args.clicks,
        use_ocr_fallback=not args.no_ocr,
        verify=args.verify,
    )
    _dump(result.to_dict())
    return 0 if result.found else 1


def cmd_type(args) -> int:
    if not args.yes:
        print("拒绝执行：键盘输入属于写操作，请追加 --yes 显式授权。")
        return 2
    ctrl = _controller(args)
    ctrl.type_text(args.text)
    print(f"已输入 {len(args.text)} 个字符")
    return 0


def _audit_tail(directory: str, lines: int) -> int:
    """实时跟读最新审计文件（先回显末尾 ``lines`` 行）。"""
    from uiagent import audit

    path = audit.latest_log_file(directory)
    if not path:
        print(
            f"未找到审计文件：{os.path.join(os.path.abspath(directory), 'audit-*.jsonl')}",
            flush=True,
        )
        return 1
    print(f"# 跟读 {path}（每行一条 JSON 记录，Ctrl+C 退出）", flush=True)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle.readlines()[-max(1, lines):]:
                print(line.rstrip(), flush=True)
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                print(line.rstrip(), flush=True)
    except KeyboardInterrupt:
        print("\n# 已停止跟读")
        return 0
    return 0


def cmd_audit(args) -> int:
    """审计日志视图：--stats（默认）/ --tail / --run。"""
    from uiagent import audit

    directory = args.dir or audit.audit_dir()
    if args.tail:
        return _audit_tail(directory, args.lines)
    if args.run:
        records = list(audit.iter_records(directory, run_id=args.run, include_rotated=True))
        _dump({"run_id": args.run, "count": len(records), "records": records})
        return 0 if records else 1

    records = list(audit.iter_records(directory, date=args.date))
    summary = audit.summarize(records)
    summary["directory"] = os.path.abspath(directory)
    summary["files"] = [os.path.basename(path) for path in audit.list_log_files(directory)]
    _dump(summary)
    return 0


def cmd_selfcheck(args) -> int:
    from selfcheck.readonly_selfcheck import main as selfcheck_main

    argv: list = []
    if args.out:
        argv += ["--out", args.out]
    if args.json_only:
        argv.append("--json-only")
    if args.ocr_limit:
        argv += ["--ocr-limit", str(args.ocr_limit)]
    if args.top_windows:
        argv += ["--top-windows", str(args.top_windows)]
    return selfcheck_main(argv)


def cmd_mcp(args) -> int:
    """以 stdio 启动 MCP Server（阻塞直到客户端断开）。"""
    from uiagent.mcp_server import run_stdio

    return run_stdio()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ui-agent", description="Windows UIA + OCR 通用操控内核")
    parser.add_argument("--version", action="version", version=f"ui-agent {__version__}")
    parser.add_argument("--search-timeout", type=float, default=2.0, help="UIA 单次搜索超时（秒）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    sub = parser.add_subparsers(dest="command")

    p_info = sub.add_parser("info", help="打印 DPI / 屏幕环境")
    p_info.set_defaults(func=cmd_info)

    p_win = sub.add_parser("windows", help="枚举顶层窗口")
    p_win.add_argument("--all", action="store_true", help="包含不可见窗口 / 非 Window 顶层节点")
    p_win.add_argument("--non-window", action="store_true", help="列出所有顶层控件（含任务栏等非 Window 节点）")
    p_win.add_argument("--limit", type=int, default=50)
    p_win.set_defaults(func=cmd_windows)

    p_tree = sub.add_parser("tree", help="打印元素树")
    p_tree.add_argument("--depth", type=int, default=2)
    p_tree.add_argument("--active", action="store_true", help="仅前台窗口（默认也是前台窗口）")
    p_tree.set_defaults(func=cmd_tree)

    p_find = sub.add_parser("find", help="查找元素（UIA）")
    p_find.add_argument("--match", default=None, help="元素名称（模糊）")
    p_find.add_argument("--role", default=None, help="元素角色，如 Button / Edit")
    p_find.add_argument("--exact", action="store_true", help="名称精确匹配")
    p_find.add_argument("--depth", type=int, default=8)
    p_find.add_argument("--limit", type=int, default=30)
    p_find.set_defaults(func=cmd_find)

    p_text = sub.add_parser("text", help="全屏 OCR，返回文字及坐标")
    p_text.add_argument("--region", default=None, help="识别区域 x,y,w,h（默认全屏）")
    p_text.add_argument("--limit", type=int, default=50)
    p_text.set_defaults(func=cmd_text)

    p_loc = sub.add_parser("locate", help="定位目标（UIA 优先，OCR 兜底）")
    p_loc.add_argument("--match", required=True, help="元素名称或界面文字")
    p_loc.add_argument("--role", default=None)
    p_loc.add_argument("--no-ocr", action="store_true", help="禁用 OCR 兜底")
    p_loc.set_defaults(func=cmd_locate)

    p_click = sub.add_parser("click", help="点击目标（写操作）")
    p_click.add_argument("--match", required=True)
    p_click.add_argument("--button", default="left", choices=["left", "right", "middle"])
    p_click.add_argument("--clicks", type=int, default=1)
    p_click.add_argument("--no-ocr", action="store_true")
    p_click.add_argument("--verify", action="store_true", help="点击后回读该点元素")
    p_click.add_argument("--yes", action="store_true", help="确认执行写操作")
    p_click.set_defaults(func=cmd_click)

    p_type = sub.add_parser("type", help="输入文本（写操作）")
    p_type.add_argument("--text", required=True)
    p_type.add_argument("--yes", action="store_true", help="确认执行写操作")
    p_type.set_defaults(func=cmd_type)

    p_sc = sub.add_parser("selfcheck", help="只读自检（UIA + OCR）")
    p_sc.add_argument("--out", default=None, help="报告输出目录")
    p_sc.add_argument("--json-only", action="store_true")
    p_sc.add_argument("--ocr-limit", type=int, default=30, help="报告中展示的 OCR 文本块数量")
    p_sc.add_argument("--top-windows", type=int, default=15, help="报告中展示的窗口数量")
    p_sc.set_defaults(func=cmd_selfcheck)

    p_audit = sub.add_parser("audit", help="审计日志：统计 / 跟读 / 按 run 回放")
    p_audit.add_argument("--stats", action="store_true", help="输出统计汇总（默认动作）")
    p_audit.add_argument("--date", default=None, help="仅统计指定日期（YYYY-MM-DD）")
    p_audit.add_argument("--tail", action="store_true", help="实时跟读最新审计文件")
    p_audit.add_argument("--run", default=None, help="按 run_id 回放该次任务的调用序列")
    p_audit.add_argument("--lines", type=int, default=20, help="--tail 首次回显的历史行数")
    p_audit.add_argument("--dir", default=None, help="审计目录（默认项目下 logs）")
    p_audit.set_defaults(func=cmd_audit)

    p_mcp = sub.add_parser("mcp", help="以 stdio 启动 MCP Server（供 CowAgent / Marvis 接入）")
    p_mcp.set_defaults(func=cmd_mcp)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "log_level", "INFO"))
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
