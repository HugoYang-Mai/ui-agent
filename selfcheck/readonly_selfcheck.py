"""只读自检：验证 UIA + OCR 内核可用性。

覆盖项（**全部只读**，不做任何鼠标 / 键盘 / 文件系统写操作，仅落盘截图与报告）：

1. DPI 感知与统一坐标系（``SetProcessDpiAwareness(2)`` 是否生效）
2. 枚举顶层窗口
3. 读取前台窗口 / 焦点元素
4. 元素查找（UIA，含 role 过滤与按名精确查找）
5. 元素树采样（深度、节点统计）
6. 截屏 + OCR（返回文字及屏幕坐标）

运行::

    .venv\\Scripts\\python.exe selfcheck\\readonly_selfcheck.py
    .venv\\Scripts\\python.exe main.py selfcheck
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------- 路径与坐标系
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from uiagent import dpi  # noqa: E402  （轻量模块，无副作用）

# ⚠️ 必须在导入 PIL / pyautogui / uiautomation 之前统一坐标系
DPI_HRESULT = dpi.enable_dpi_awareness(dpi.DPI_AWARENESS_PER_MONITOR_AWARE)

from uiagent import __version__  # noqa: E402
from uiagent.base import normalize_role  # noqa: E402
from uiagent.controller import UniversalController  # noqa: E402


def _now_text() -> str:
    dt = time.localtime()
    return (
        f"{dt.tm_year}年{dt.tm_mon:02d}月{dt.tm_mday:02d}日 "
        f"{dt.tm_hour:02d}:{dt.tm_min:02d}:{dt.tm_sec:02d}"
    )


def _stamp() -> str:
    dt = time.localtime()
    return f"{dt.tm_year:04d}{dt.tm_mon:02d}{dt.tm_mday:02d}_{dt.tm_hour:02d}{dt.tm_min:02d}{dt.tm_sec:02d}"


class Step:
    """单个自检步骤的记录器。"""

    def __init__(self, key: str, title: str) -> None:
        self.key = key
        self.title = title
        self.status = "ok"
        self.error = ""
        self.data: Dict[str, Any] = {}
        self.started = time.perf_counter()

    def finish(self) -> Dict[str, Any]:
        elapsed = (time.perf_counter() - self.started) * 1000
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "elapsed_ms": round(elapsed, 1),
            "error": self.error,
            "data": self.data,
        }


def _pick_probe_window(ctrl: UniversalController, top_level: List[Any]) -> Optional[Any]:
    """选择一个稳定的探测窗口用于元素查找验证：优先任务栏，其次前台窗口。"""
    for win in top_level:
        if win.class_name == "Shell_TrayWnd":
            return win
    active = ctrl.get_active_window()
    if active is not None:
        return active
    return top_level[0] if top_level else None


#: 元素查找探测时按序尝试的角色（命中即停）
_PROBE_ROLES = ("button", "menuitem", "edit", "hyperlink", "listitem")


def _probe_elements(ctrl: UniversalController, probe: Any) -> tuple:
    """在探测窗口中查找元素：按角色优先，全部落空时退化为"有名字且可点击"。"""
    for role in _PROBE_ROLES:
        found = ctrl.find_all_elements(
            role=role, window=probe, max_depth=6, limit=20, timeout=6.0
        )
        if found:
            return role, found, ""
    everything = ctrl.find_all_elements(window=probe, max_depth=5, limit=400, timeout=6.0)
    clicked = [e for e in everything if e.name and e.is_clickable]
    return "*", clicked[:20], f"未命中预设角色（{'/'.join(_PROBE_ROLES)}），退化为任意具名可点击元素" if clicked else ""


def run_selfcheck(
    out_dir: Optional[str] = None,
    top_windows: int = 15,
    ocr_limit: int = 30,
    ocr_region: Optional[tuple] = None,
    save_screenshot: bool = True,
) -> Dict[str, Any]:
    """执行只读自检，返回结构化报告（同时落盘 JSON + Markdown）。"""
    out_dir = out_dir or os.path.join(_PROJECT_ROOT, "out")
    os.makedirs(out_dir, exist_ok=True)

    ctrl = UniversalController()
    steps: List[Dict[str, Any]] = []

    # ---------------------------------------------------------- 1. DPI / 坐标系
    step = Step("dpi", "DPI 感知与统一坐标系")
    env = ctrl.environment()
    env["set_process_dpi_awareness_hresult"] = DPI_HRESULT
    vx, vy, vw, vh = ctrl.screen_rect()
    env["virtual_screen"] = {"x": vx, "y": vy, "width": vw, "height": vh}
    step.data = env
    if not env.get("dpi_aware"):
        step.status = "warn"
        step.error = "进程未处于物理像素坐标系，坐标可能出现缩放偏差"
    steps.append(step.finish())

    # ---------------------------------------------------------- 2. 枚举窗口
    step = Step("windows", "枚举顶层窗口")
    top_level: List[Any] = []
    try:
        top_level = ctrl.list_top_level(include_invisible=False)
        windows = [e for e in top_level if normalize_role(e.role) == "window"]
        step.data = {
            "top_level_count": len(top_level),
            "count": len(windows),
            "other_top_level": [
                {"name": e.name, "class_name": e.class_name, "role": e.role}
                for e in top_level
                if normalize_role(e.role) != "window"
            ][:10],
            "items": [
                {
                    "name": w.name,
                    "class_name": w.class_name,
                    "role": w.role,
                    "bounds": list(w.bounds),
                    "center": list(w.center),
                    "process_id": w.process_id,
                    "process_name": os.path.basename(w.process_name or ""),
                }
                for w in windows[:top_windows]
            ],
        }
        if not windows:
            step.status = "fail"
            step.error = "未枚举到任何顶层窗口"
    except Exception as exc:  # pragma: no cover
        step.status = "fail"
        step.error = f"{type(exc).__name__}: {exc}"
        step.data["traceback"] = traceback.format_exc(limit=3)
    steps.append(step.finish())

    # ---------------------------------------------------------- 3. 前台窗口
    step = Step("active_window", "读取前台窗口与焦点元素")
    try:
        active = ctrl.get_active_window()
        focused = ctrl.get_focused_element()
        step.data = {
            "active_window": active.to_dict() if active else None,
            "focused_element": focused.to_dict() if focused else None,
        }
        if active is None:
            step.status = "warn"
            step.error = "未取到前台窗口"
    except Exception as exc:
        step.status = "fail"
        step.error = f"{type(exc).__name__}: {exc}"
    steps.append(step.finish())

    # ---------------------------------------------------------- 4. 元素查找
    step = Step("element_search", "元素查找（UIA）")
    try:
        probe = _pick_probe_window(ctrl, top_level)
        if probe is None:
            step.status = "fail"
            step.error = "无可用探测窗口"
        else:
            started = time.perf_counter()
            role_used, found, degrade_note = _probe_elements(ctrl, probe)
            search_ms = (time.perf_counter() - started) * 1000
            items = [
                {"name": e.name, "role": e.role, "center": list(e.center), "bounds": list(e.bounds)}
                for e in found
            ]
            exact: Dict[str, Any] = {}
            named = next((e for e in found if e.name), None)
            if named is not None:
                hit = ctrl.find_element(name=named.name, window=probe, exact=True, limit=1)
                exact = {
                    "query": named.name,
                    "matched": hit is not None,
                    "center": list(hit.center) if hit else None,
                }
            step.data = {
                "probe_window": {
                    "name": probe.name,
                    "class_name": probe.class_name,
                    "role": probe.role,
                },
                "role_filter": role_used,
                "count": len(found),
                "search_ms": round(search_ms, 1),
                "items": items,
                "exact_lookup": exact,
                "degrade_note": degrade_note,
            }
            if not found:
                step.status = "warn"
                step.error = "探测窗口中未找到可点击元素"
    except Exception as exc:
        step.status = "fail"
        step.error = f"{type(exc).__name__}: {exc}"
        step.data["traceback"] = traceback.format_exc(limit=3)
    steps.append(step.finish())

    # ---------------------------------------------------------- 5. 元素树
    step = Step("element_tree", "元素树采样")
    try:
        active = ctrl.get_active_window()
        tree = ctrl.get_element_tree(window=active, depth=2)
        counter: Dict[str, int] = {}

        def _count(node, acc):
            if node is None:
                return 0
            acc[node.role] = acc.get(node.role, 0) + 1
            total = 1
            for child in node.children:
                total += _count(child, acc)
            return total

        total = _count(tree, counter)
        step.data = {
            "root": {"name": tree.name, "role": tree.role} if tree else None,
            "depth": 2,
            "node_count": total,
            "role_histogram": dict(sorted(counter.items(), key=lambda kv: -kv[1])[:12]),
        }
        if not total:
            step.status = "warn"
            step.error = "元素树为空"
    except Exception as exc:
        step.status = "fail"
        step.error = f"{type(exc).__name__}: {exc}"
    steps.append(step.finish())

    # ---------------------------------------------------------- 6. 截屏 + OCR
    step = Step("screenshot_ocr", "截屏 + OCR（文字及坐标）")
    screenshot_path = ""
    try:
        full_rect = ctrl.screen_rect()
        region = ocr_region or full_rect
        grab_started = time.perf_counter()
        image = ctrl.screenshot(region)
        grab_ms = (time.perf_counter() - grab_started) * 1000
        if save_screenshot:
            screenshot_path = os.path.join(out_dir, f"selfcheck_screenshot_{_stamp()}.png")
            image.save(screenshot_path)

        load_seconds = ctrl.warmup_ocr()
        ocr_started = time.perf_counter()
        blocks = ctrl.ocr_screen(region)
        ocr_ms = (time.perf_counter() - ocr_started) * 1000

        step.data = {
            "region": list(region),
            "image_size": list(image.size),
            "grab_ms": round(grab_ms, 1),
            "ocr_model_load_seconds": load_seconds,
            "ocr_ms": round(ocr_ms, 1),
            "block_count": len(blocks),
            "blocks": [b.to_dict() for b in blocks[:ocr_limit]],
            "screenshot_path": screenshot_path or None,
            "note": "坐标为屏幕绝对物理像素，(x, y) 为文本块中心点",
        }
        if not blocks:
            step.status = "fail"
            step.error = "OCR 未识别到任何文本"
    except Exception as exc:
        step.status = "fail"
        step.error = f"{type(exc).__name__}: {exc}"
        step.data["traceback"] = traceback.format_exc(limit=5)
    steps.append(step.finish())

    # ---------------------------------------------------------- 汇总
    ok = sum(1 for s in steps if s["status"] == "ok")
    warn = sum(1 for s in steps if s["status"] == "warn")
    fail = sum(1 for s in steps if s["status"] == "fail")
    report: Dict[str, Any] = {
        "project": "ui-agent",
        "version": __version__,
        "mode": "readonly-selfcheck",
        "generated_at": _now_text(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "steps": steps,
        "summary": {
            "total": len(steps),
            "ok": ok,
            "warn": warn,
            "fail": fail,
            "passed": fail == 0,
        },
    }

    stamp = _stamp()
    json_path = os.path.join(out_dir, f"selfcheck_report_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    md_path = os.path.join(out_dir, f"selfcheck_report_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))

    report["report_files"] = {"json": json_path, "markdown": md_path, "screenshot": screenshot_path or None}
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    """把报告渲染为 Markdown。"""
    lines: List[str] = []
    lines.append(f"# ui-agent 只读自检报告（v{report['version']}）")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 运行环境：Python {report['python']} @ {report['executable']}")
    lines.append(f"- 平台：{report['platform']}")
    lines.append("- 自检模式：只读（无鼠标 / 键盘 / 系统状态改动）")
    lines.append("")

    s = report["summary"]
    lines.append("## 总览")
    lines.append("")
    lines.append("| 步骤 | 状态 | 耗时(ms) | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    for step in report["steps"]:
        flag = {"ok": "通过", "warn": "警告", "fail": "失败"}.get(step["status"], step["status"])
        lines.append(
            f"| {step['title']} | {flag} | {step['elapsed_ms']} | {step['error'] or '-'} |"
        )
    lines.append("")
    lines.append(f"结果：**{s['ok']} 通过 / {s['warn']} 警告 / {s['fail']} 失败**")
    lines.append("")

    for step in report["steps"]:
        lines.append(f"## {step['title']}")
        lines.append("")
        data = step["data"]

        if step["key"] == "dpi":
            lines.append("| 项 | 值 |")
            lines.append("| --- | --- |")
            for key in (
                "dpi_awareness_name",
                "dpi_aware",
                "scale_factor",
                "screen_width",
                "screen_height",
                "monitor_count",
            ):
                lines.append(f"| {key} | {data.get(key)} |")
            vs = data.get("virtual_screen", {})
            lines.append(f"| virtual_screen | ({vs.get('x')}, {vs.get('y')}, {vs.get('width')}, {vs.get('height')}) |")
            lines.append("")

        elif step["key"] == "windows":
            lines.append(
                f"桌面顶层控件 **{data.get('top_level_count')}** 个，"
                f"其中 ControlType=Window 的窗口 **{data.get('count')}** 个"
                f"（展示前 {len(data.get('items', []))} 个）"
            )
            lines.append("")
            lines.append("| 窗口标题 | 类名 | 进程 | 位置(x, y, w, h) |")
            lines.append("| --- | --- | --- | --- |")
            for item in data.get("items", []):
                b = item["bounds"]
                lines.append(
                    f"| {item['name'][:60] or '(无标题)'} | {item['class_name']} | "
                    f"{item['process_name']}({item['process_id']}) | ({b[0]}, {b[1]}, {b[2]}, {b[3]}) |"
                )
            others = data.get("other_top_level", [])
            if others:
                lines.append("")
                lines.append("非 Window 顶层节点（示例）：")
                lines.append("")
                for item in others:
                    lines.append(f"- `{item['name'][:50] or '(无标题)'}` [{item['class_name']}] role={item['role']}")
            lines.append("")

        elif step["key"] == "active_window":
            aw = data.get("active_window") or {}
            fo = data.get("focused_element") or {}
            lines.append(f"- 前台窗口：`{aw.get('name', '-')}` (role={aw.get('role')}, bounds={aw.get('bounds')})")
            lines.append(f"- 焦点元素：`{fo.get('name', '-')}` (role={fo.get('role')}, center={fo.get('center')})")
            lines.append("")

        elif step["key"] == "element_search":
            pw = data.get("probe_window", {})
            lines.append(f"探测窗口：`{pw.get('name', '-')}`（类名 {pw.get('class_name')}）")
            lines.append("")
            lines.append(
                f"按 role={data.get('role_filter')} 命中 **{data.get('count')}** 个元素，"
                f"检索耗时 {data.get('search_ms')} ms"
            )
            lines.append("")
            if data.get("degrade_note"):
                lines.append(f"> {data['degrade_note']}")
                lines.append("")
            lines.append("| 元素名 | 角色 | 中心坐标 |")
            lines.append("| --- | --- | --- |")
            for item in data.get("items", []):
                lines.append(f"| {item['name'][:50] or '(无名)'} | {item['role']} | {tuple(item['center'])} |")
            exact = data.get("exact_lookup") or {}
            if exact:
                lines.append("")
                lines.append(f"按名精确查找 `{exact.get('query')}` -> {exact.get('matched')}，坐标 {exact.get('center')}")
            lines.append("")

        elif step["key"] == "element_tree":
            root = data.get("root") or {}
            lines.append(f"- 根节点：`{root.get('name', '-')}` (role={root.get('role')})")
            lines.append(f"- 深度 {data.get('depth')} 内节点总数：{data.get('node_count')}")
            lines.append(f"- 角色分布：{data.get('role_histogram')}")
            lines.append("")

        elif step["key"] == "screenshot_ocr":
            lines.append(f"- 区域：{data.get('region')}，图像尺寸：{data.get('image_size')}")
            lines.append(f"- 截屏耗时 {data.get('grab_ms')} ms；OCR 模型加载 {data.get('ocr_model_load_seconds')} s；识别耗时 {data.get('ocr_ms')} ms")
            lines.append(f"- 识别文本块总数：**{data.get('block_count')}**（下表展示前 {len(data.get('blocks', []))} 条）")
            lines.append("")
            lines.append("| # | 文字 | 置信度 | 中心坐标 | 包围盒(x, y, w, h) |")
            lines.append("| --- | --- | --- | --- | --- |")
            for idx, block in enumerate(data.get("blocks", []), start=1):
                lines.append(
                    f"| {idx} | {block['text'][:40]} | {block['confidence']} | "
                    f"{tuple(block['center'])} | {tuple(block['bounds'])} |"
                )
            lines.append("")

        else:
            lines.append("```json")
            lines.append(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
            lines.append("```")
            lines.append("")

    return "\n".join(lines)


def print_summary(report: Dict[str, Any]) -> None:
    """控制台摘要输出。"""
    s = report["summary"]
    print("=" * 68)
    print(f"ui-agent 只读自检  v{report['version']}  {report['generated_at']}")
    print(f"python: {report['python']}  |  {report['executable']}")
    print("=" * 68)
    for step in report["steps"]:
        flag = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}.get(step["status"], "[????]")
        print(f"{flag} {step['title']:<28} {step['elapsed_ms']:>9.1f} ms  {step['error']}")
        data = step["data"]
        if step["key"] == "dpi":
            vs = data.get("virtual_screen", {})
            print(f"        DPI={data.get('dpi_awareness_name')} scale={data.get('scale_factor')} "
                  f"主屏={data.get('screen_width')}x{data.get('screen_height')} "
                  f"虚拟桌面=({vs.get('x')},{vs.get('y')},{vs.get('width')},{vs.get('height')})")
        elif step["key"] == "windows":
            print(
                f"        顶层控件 {data.get('top_level_count')} 个，其中窗口 {data.get('count')} 个"
            )
            for item in data.get("items", [])[:5]:
                print(f"          - {item['name'][:52] or '(无标题)'}  [{item['class_name']}]  {item['process_name']}")
        elif step["key"] == "active_window":
            aw = data.get("active_window") or {}
            print(f"        前台窗口: {aw.get('name', '-')}  bounds={aw.get('bounds')}")
        elif step["key"] == "element_search":
            pw = data.get("probe_window", {})
            print(f"        探测窗口: {pw.get('name', '-')} [{pw.get('class_name')}]  "
                  f"role={data.get('role_filter')} x{data.get('count')}  耗时 {data.get('search_ms')} ms")
            for item in data.get("items", [])[:5]:
                print(f"          - {item['role']} {item['name'][:36]!r} center={tuple(item['center'])}")
        elif step["key"] == "element_tree":
            print(f"        节点数(深度2)={data.get('node_count')}  角色分布Top={list((data.get('role_histogram') or {}).items())[:5]}")
        elif step["key"] == "screenshot_ocr":
            print(f"        图像 {data.get('image_size')}  文本块 {data.get('block_count')} 个  "
                  f"OCR {data.get('ocr_ms')} ms (模型加载 {data.get('ocr_model_load_seconds')} s)")
            for block in data.get("blocks", [])[:8]:
                print(f"          - {block['text'][:40]!r:<44} conf={block['confidence']} center={tuple(block['center'])}")
    print("-" * 68)
    print(f"结果: {s['ok']} 通过 / {s['warn']} 警告 / {s['fail']} 失败  ->  passed={s['passed']}")
    files = report.get("report_files", {})
    print(f"报告(JSON): {files.get('json')}")
    print(f"报告(MD)  : {files.get('markdown')}")
    if files.get("screenshot"):
        print(f"截图      : {files.get('screenshot')}")
    print("=" * 68)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

    parser = argparse.ArgumentParser(description="ui-agent 只读自检")
    parser.add_argument("--out", default=None, help="报告输出目录（默认 <项目>/out）")
    parser.add_argument("--top-windows", type=int, default=15, help="展示的窗口数量")
    parser.add_argument("--ocr-limit", type=int, default=30, help="报告中展示的 OCR 文本块数量")
    parser.add_argument("--json-only", action="store_true", help="仅输出 JSON 报告")
    args = parser.parse_args(argv)

    report = run_selfcheck(out_dir=args.out, top_windows=args.top_windows, ocr_limit=args.ocr_limit)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_summary(report)
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
