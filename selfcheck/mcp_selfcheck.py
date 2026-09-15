"""ui-agent MCP Server 协议自测脚本。

以真实 MCP 客户端身份拉起 ``serve_mcp.py`` 子进程，走完整 stdio 协议链路：

1. **握手**：``initialize`` → 校验协议版本 / serverInfo / capabilities
2. **列工具**：``tools/list`` → 校验 12 个预期工具齐备并导出 inputSchema 摘要
3. **真实调用**：``tools/call``
   - 只读链路（默认）：``ui_window_list``（含 state 字段）/ ``ui_app_status`` / ``ui_launch_app``
     （``dry_run`` 预演）/ ``ui_wait_window``（超时降级）/ ``ui_find`` / ``ui_screenshot`` / ``ui_ocr``
   - 写链路（``--include-write``）：启动记事本打开自建临时文档 → 激活窗口 → **P1 四状态用例**
     （S1 可见 / S2 最小化 / S3 隐藏 / S4 未启动 + 耗时断言）→ 点击编辑区
     → 中文输入 → ``ctrl+s`` 保存 → 截图 → OCR 核验 → **P2 冷启动用例**
     （``ui_launch_app`` 启动 + 就绪等待 + 启动后可定位）→ 关闭进程

用法::

    .venv\\Scripts\\python.exe selfcheck\\mcp_selfcheck.py
    .venv\\Scripts\\python.exe selfcheck\\mcp_selfcheck.py --include-write

报告（JSON）与截图写入 ``out/`` 目录，退出码 0 表示全部检查通过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = [
    "ui_window_list",
    "ui_activate_window",
    "ui_app_ensure",
    "ui_app_status",
    "ui_launch_app",
    "ui_wait_window",
    "ui_find",
    "ui_click",
    "ui_type",
    "ui_hotkey",
    "ui_screenshot",
    "ui_ocr",
]

#: P1 四状态耗时预算（P95，单位毫秒，见方案第 4 章）
P1_STATE_BUDGETS_MS = {"visible": 150.0, "minimized": 650.0, "hidden": 900.0}
#: P2 冷启动分级 SLA（轻量级，单位毫秒，见方案 4.2）
P2_COLD_START_BUDGET_MS = 1500.0
#: P2 就绪等待超时用例的等待时长（秒）
P2_WAIT_TIMEOUT_S = 1.0
#: P1 四状态 → 方案状态编号
P1_STATE_LABELS = {"visible": "S1 可见", "minimized": "S2 最小化", "hidden": "S3 隐藏", "not_running": "S4 未启动"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _asdict(obj: Any) -> Any:
    """把 pydantic 模型 / 普通对象转为可 JSON 化的 dict。"""
    if obj is None:
        return None
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:  # pragma: no cover
                pass
    if isinstance(obj, (str, int, float, bool, list, dict)):
        return obj
    return {k: _asdict(v) for k, v in vars(obj).items() if not k.startswith("_")}


def _truncate(payload: Any, limit: int = 4000) -> Any:
    """截断过大的响应，保证报告可读。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover
        return str(payload)[:limit]
    if len(text) <= limit:
        return payload
    return {"_truncated": True, "length": len(text), "preview": text[:limit]}


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """兼容读取 pydantic 模型的 camelCase / snake_case 字段名。"""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _check(report: Dict[str, Any], name: str, passed: bool, detail: str = "") -> bool:
    report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(' | ' + detail) if detail else ''}")
    return bool(passed)


async def _readonly_scenario(call: Callable, report: Dict[str, Any], out_dir: str, ts: str) -> None:
    """只读链路：窗口枚举 → 元素定位 → 截图 → OCR。"""
    windows = await call("ui_window_list", {"limit": 10})
    items = windows.get("windows") or []
    _check(
        report,
        "ui_window_list 枚举窗口",
        bool(windows.get("ok")) and len(items) > 0,
        f"count={windows.get('count')} total={windows.get('total')}",
    )

    # P1（改动点 #11）：windows[] / apps[] 必须带 state 字段
    window_states = [str(item.get("state") or "") for item in items]
    app_states = [str(item.get("state") or "") for item in (windows.get("apps") or [])]
    _check(
        report,
        "ui_window_list 输出 state 字段（windows[] / apps[]）",
        bool(items) and all(window_states) and bool(app_states) and all(app_states),
        f"states={sorted(set(window_states))} app_states={sorted(set(app_states))}",
    )

    # P1（改动点 #10）：ui_app_status 为只读批量状态查询
    status = await call("ui_app_status", {"include_not_running": True})
    status_apps = status.get("apps") or []
    _check(
        report,
        "ui_app_status 批量查询应用状态（只读）",
        bool(status.get("ok")) and len(status_apps) > 0,
        f"count={status.get('count')} elapsed_ms={status.get('elapsed_ms')}",
    )
    not_running = await call("ui_app_status", {"apps": ["ui-agent-no-such-app-xyz"]})
    entry = (not_running.get("apps") or [{}])[0]
    _check(
        report,
        "ui_app_status 未启动应用返回 not_running",
        bool(not_running.get("ok")) and entry.get("state") == "not_running",
        f"state={entry.get('state')} hwnd={entry.get('hwnd')}",
    )

    # P2（改动点 #15）：ui_launch_app dry_run 预演 —— 只解析入口，不产生任何进程
    dry = await call("ui_launch_app", {"app": "notepad.exe", "dry_run": True})
    _check(
        report,
        "ui_launch_app dry_run 预演（不产生进程）",
        bool(dry.get("dry_run")) and not int(dry.get("pid") or 0),
        f"ok={dry.get('ok')} state={dry.get('state')} resolved_by={dry.get('resolved_by')} "
        f"target_path={dry.get('target_path')} pid={dry.get('pid')}",
    )
    report["p2_launch_dry_run"] = dry

    # P2（改动点 #15 / RF7）：ui_wait_window 超时降级为 state=launching，而非抛异常（只读）
    waited = await call(
        "ui_wait_window", {"app": "ui-agent-no-such-app-xyz", "timeout": P2_WAIT_TIMEOUT_S}
    )
    _check(
        report,
        "ui_wait_window 超时返回 launching（只读、非异常）",
        waited.get("ok") is False and waited.get("state") == "launching",
        f"state={waited.get('state')} waited_ms={waited.get('waited_ms')} "
        f"degraded_reason={waited.get('degraded_reason')}",
    )
    report["p2_wait_timeout"] = waited

    foreground = windows.get("foreground_window") or {}
    title = (foreground.get("title") or "").strip()
    if not title and items:
        title = (items[0].get("title") or "").strip()
    if title:
        found = await call("ui_find", {"target": title, "limit": 3})
        _check(
            report,
            "ui_find 定位前台窗口",
            bool(found.get("found")),
            f"target={title!r} source={found.get('source')} xy=({found.get('x')},{found.get('y')})",
        )
    else:
        _check(report, "ui_find 定位前台窗口", False, "无法取得前台窗口标题，跳过")

    shot_path = os.path.join(out_dir, f"mcp_selftest_screen_{ts}.png")
    shot = await call("ui_screenshot", {"path": shot_path})
    exists = os.path.exists(shot_path)
    _check(
        report,
        "ui_screenshot 截屏落盘",
        bool(shot.get("ok")) and exists,
        f"{shot.get('width')}x{shot.get('height')} -> {shot_path}",
    )

    ocr = await call("ui_ocr", {"region": [0, 0, 900, 360], "mode": "blocks", "limit": 20})
    blocks = ocr.get("blocks")
    _check(
        report,
        "ui_ocr 区域识别（blocks）",
        bool(ocr.get("ok")) and isinstance(blocks, list),
        f"blocks={ocr.get('count')}",
    )

    text_mode = await call("ui_ocr", {"region": [0, 0, 900, 360], "mode": "text", "limit": 200})
    _check(
        report,
        "ui_ocr 整屏文本（text）",
        bool(text_mode.get("ok")),
        f"length={text_mode.get('length')}",
    )


def _win32_show_window(hwnd: int, cmd: int) -> bool:
    """本进程内直接调用 ``ShowWindow``（仅用于构造 S2 最小化 / S3 隐藏前置状态）。"""
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(int(hwnd), int(cmd))
        return True
    except Exception:  # pragma: no cover - 非 Windows 或权限受限
        return False


async def _p1_state_scenario(
    call: Callable,
    report: Dict[str, Any],
    hwnd: Optional[int],
    app_key: Optional[str],
) -> None:
    """P1 四状态用例（改动点 #12）：S1 可见 / S2 最小化 / S3 隐藏 / S4 未启动 + 耗时断言。

    S2/S3 通过本进程 ``ShowWindow``（``SW_MINIMIZE``=6 / ``SW_HIDE``=0）构造前置状态，
    再断言 ``ui_app_ensure`` 的 ``state``、``hwnd`` 非空与 ``timing.total_ms`` 落入 P95 预算。
    """
    cases: Dict[str, Any] = {}
    report["p1_states"] = cases
    if not hwnd:
        _check(report, "P1 四状态用例（S1~S3）", False, "缺少可用 hwnd，跳过")
        return

    events = [("visible", None), ("minimized", 6), ("hidden", 0)]
    for expect, show_cmd in events:
        label = P1_STATE_LABELS[expect]
        if show_cmd is not None:
            if not _win32_show_window(int(hwnd), int(show_cmd)):
                _check(report, f"ui_app_ensure {label} 状态与耗时", False, "构造前置状态失败")
                continue
            await asyncio.sleep(0.5)
        arguments = {"app": app_key} if app_key else {"hwnd": int(hwnd)}
        payload = await call("ui_app_ensure", arguments)
        timing = payload.get("timing") or {}
        total_ms = float(timing.get("total_ms") or 0.0)
        budget = P1_STATE_BUDGETS_MS[expect]
        cases[expect] = {
            "state": payload.get("state"),
            "state_before": payload.get("state_before"),
            "hwnd": payload.get("hwnd"),
            "activated": payload.get("activated"),
            "degraded_reason": payload.get("degraded_reason"),
            "timing": timing,
        }
        _check(
            report,
            f"ui_app_ensure {label} 状态与耗时（≤{budget:.0f} ms）",
            bool(payload.get("hwnd")) and payload.get("state") == "visible" and total_ms <= budget,
            f"state={payload.get('state')} before={payload.get('state_before')} "
            f"hwnd={payload.get('hwnd')} total_ms={total_ms} "
            f"show_ms={timing.get('show_ms')} settle_ms={timing.get('settle_ms')} "
            f"foreground_ms={timing.get('foreground_ms')}",
        )

    missing = await call("ui_app_ensure", {"app": "ui-agent-no-such-app-xyz"})
    cases["not_running"] = {
        "state": missing.get("state"),
        "degraded_reason": missing.get("degraded_reason"),
        "candidates": missing.get("candidates"),
    }
    _check(
        report,
        "ui_app_ensure S4 未启动应用返回 not_running（不启动进程）",
        missing.get("state") == "not_running",
        f"state={missing.get('state')} degraded_reason={missing.get('degraded_reason')} "
        f"candidates={len(missing.get('candidates') or [])}",
    )


async def _close_app_via_tools(call: Callable, app: str, rounds: int = 3, poll_s: float = 4.0) -> str:
    """用 MCP 工具关闭目标应用（确认前台 → ``alt+f4``），返回轮询到的最终状态。

    先激活并核对前台窗口确实属于目标应用，再发送 ``alt+f4``；每轮最多等 ``poll_s`` 秒。
    """
    state = ""
    for idx in range(max(1, rounds)):
        try:
            await call("ui_activate_window", {"process_name": app})
        except Exception as exc:  # pragma: no cover - 清理路径
            print(f"  [warn] 激活 {app} 失败：{exc}")
        listing = await call("ui_window_list", {"include_hidden": True})
        foreground = listing.get("foreground_window") or {}
        fg = f"{foreground.get('process_name', '')} {foreground.get('title', '')}".lower()
        if app.rsplit(".", 1)[0].lower() not in fg:
            print(f"  [warn] 前台窗口不是目标应用（fg={fg!r}），仍尝试发送 alt+f4")
        try:
            await call("ui_hotkey", {"keys": "alt+f4"})
        except Exception as exc:  # pragma: no cover - 清理路径
            print(f"  [warn] 发送 alt+f4 失败：{exc}")
        deadline = time.time() + poll_s
        while time.time() < deadline:
            item = ((await call("ui_app_status", {"apps": [app]})).get("apps") or [{}])[0]
            state = str(item.get("state") or "")
            if state == "not_running":
                return state
            await asyncio.sleep(0.4)
        print(f"  [warn] 第 {idx + 1} 轮 alt+f4 未关闭 {app}（state={state}），重试")
    return state


async def _p2_cold_start_scenario(call: Callable, report: Dict[str, Any], proc: Any) -> None:
    """P2 冷启动用例（改动点 #13/#15）：关闭前置记事本 → ``ui_launch_app`` 启动 → 断言窗口可定位。

    覆盖方案 P2 验收项：② 启动后 ``hwnd`` 可定位；并记录轻量应用冷启动耗时（方案 4.2 分级 SLA）。
    ``dry_run`` 不产生进程的断言放在只读链路（``_readonly_scenario``）。
    """
    cases: Dict[str, Any] = {}
    report["p2_cold_start"] = cases

    # 1) 构造 S4 未运行：走 MCP 工具关闭前置记事本（不使用终止进程等硬手段）
    state = await _close_app_via_tools(call, "notepad.exe")
    if state != "not_running" and proc.poll() is None:
        # 兜底：终止**本自检自己拉起**的记事本进程（仅限本脚本 Popen 句柄）
        print("  [warn] 工具链路未关闭记事本，回退终止本自检自建进程")
        proc.terminate()
        deadline = time.time() + 6.0
        while time.time() < deadline:
            item = ((await call("ui_app_status", {"apps": ["notepad.exe"]})).get("apps") or [{}])[0]
            state = str(item.get("state") or "")
            if state == "not_running":
                break
            await asyncio.sleep(0.4)
    if state != "not_running":
        # 无法构造 S4：如实跳过冷启动断言（不计入通过/失败，避免用「已运行应用」冒充冷启动）
        cases["skipped"] = True
        cases["skip_reason"] = f"前置条件未满足：记事本仍为 state={state}，无法构造 S4 未启动态"
        print(f"  [SKIP] P2 冷启动用例：{cases['skip_reason']}")
        return
    _check(report, "P2 前置：记事本已退出（构造 S4 未启动）", True, "state=not_running")

    # 2) 由内核发起冷启动（写操作）：解析 → 启动 → 等窗口就绪
    wall_start = time.perf_counter()
    launched = await call("ui_launch_app", {"app": "notepad.exe", "timeout": 15.0})
    wall_ms = (time.perf_counter() - wall_start) * 1000.0
    cold_ms = float(launched.get("launch_ms") or 0.0) + float(launched.get("wait_ms") or 0.0)
    wait_detail = launched.get("wait_detail") or {}
    cases["launch"] = {
        "ok": launched.get("ok"),
        "state": launched.get("state"),
        "resolved_by": launched.get("resolved_by"),
        "target_path": launched.get("target_path"),
        "pid": launched.get("pid"),
        "hwnd": launched.get("hwnd"),
        "launch_ms": launched.get("launch_ms"),
        "wait_ms": launched.get("wait_ms"),
        "cold_ms": round(cold_ms, 1),
        "wall_ms": round(wall_ms, 1),
        "matched_by": wait_detail.get("matched_by"),
        "degraded_reason": launched.get("degraded_reason"),
    }
    _check(
        report,
        "ui_launch_app 冷启动记事本（启动 + 窗口就绪）",
        bool(launched.get("ok")) and bool(launched.get("hwnd")),
        f"state={launched.get('state')} resolved_by={launched.get('resolved_by')} "
        f"target_path={launched.get('target_path')} pid={launched.get('pid')} "
        f"hwnd={launched.get('hwnd')} matched_by={wait_detail.get('matched_by')} "
        f"launch_ms={launched.get('launch_ms')} wait_ms={launched.get('wait_ms')}",
    )
    _check(
        report,
        f"P2 轻量应用冷启动 SLA（launch_ms + wait_ms ≤ {P2_COLD_START_BUDGET_MS:.0f} ms）",
        bool(launched.get("ok")) and cold_ms <= P2_COLD_START_BUDGET_MS,
        f"cold_ms={round(cold_ms, 1)} wall_ms={round(wall_ms, 1)}",
    )
    _check(
        report,
        "P2 启动发起段预算（launch_ms ≤ 300 ms）",
        bool(launched.get("ok")) and float(launched.get("launch_ms") or 0.0) <= 300.0,
        f"launch_ms={launched.get('launch_ms')}",
    )

    # 3) 启动后 hwnd 可定位（方案 P2 验收 ②）：只读入口复核同一窗口
    ensured = await call("ui_app_ensure", {"app": "notepad.exe"})
    ensured_timing = ensured.get("timing") or {}
    cases["ensure_after_launch"] = {
        "state": ensured.get("state"),
        "hwnd": ensured.get("hwnd"),
        "activated": ensured.get("activated"),
        "total_ms": ensured_timing.get("total_ms"),
    }
    _check(
        report,
        "启动后 hwnd 可定位（ui_app_ensure 命中同一窗口）",
        bool(ensured.get("hwnd")) and int(ensured.get("hwnd") or 0) == int(launched.get("hwnd") or 0),
        f"state={ensured.get('state')} hwnd={ensured.get('hwnd')} "
        f"launched_hwnd={launched.get('hwnd')} total_ms={ensured_timing.get('total_ms')}",
    )

    # 4) 只读就绪等待：pid + app 双线索（launcher 的真实调用形态，含打包应用兜底）
    waited = await call(
        "ui_wait_window",
        {"pid": int(launched.get("pid") or 0), "app": "notepad.exe", "timeout": 5.0},
    )
    cases["wait_by_pid_app"] = {
        "ok": waited.get("ok"),
        "state": waited.get("state"),
        "hwnd": waited.get("hwnd"),
        "waited_ms": waited.get("waited_ms"),
        "matched_by": waited.get("matched_by"),
    }
    _check(
        report,
        "ui_wait_window 命中已就绪窗口（pid + app 双线索）",
        bool(waited.get("ok")) and bool(waited.get("hwnd")),
        f"state={waited.get('state')} hwnd={waited.get('hwnd')} "
        f"waited_ms={waited.get('waited_ms')} matched_by={waited.get('matched_by')}",
    )

    # 5) 清理：关闭本次由内核启动的记事本（走 MCP 工具，不硬杀进程）
    state_after = await _close_app_via_tools(call, "notepad.exe", rounds=2, poll_s=3.0)
    cases["cleanup_state"] = state_after
    if state_after != "not_running":
        print(f"  [warn] P2 启动的记事本未关闭（state={state_after}），请人工确认")


async def _write_scenario(call: Callable, report: Dict[str, Any], out_dir: str, ts: str) -> None:
    """写链路：记事本端到端（自建临时文档，无用户数据风险）。"""
    doc = os.path.join(tempfile.gettempdir(), f"ui_agent_mcp_selftest_{ts}.txt")
    with open(doc, "w", encoding="utf-8") as handle:
        handle.write("")
    proc = subprocess.Popen(["notepad.exe", doc])
    report["write_scenario"] = {"document": doc, "pid": proc.pid}
    print(f"  [info] 已启动记事本 pid={proc.pid} doc={doc}")

    try:
        await asyncio.sleep(3.0)

        activated = await call("ui_activate_window", {"process_name": "notepad.exe"})
        _check(
            report,
            "ui_activate_window 激活记事本",
            bool(activated.get("activated")),
            str(activated.get("detail") or ""),
        )

        windows = await call("ui_window_list", {"limit": 30})
        target = None
        for window in windows.get("windows") or []:
            title = window.get("title") or ""
            if "记事本" in title or "Notepad" in title:
                target = window
                break
        if target is None:
            _check(report, "定位记事本窗口", False, "窗口列表中未找到记事本")
            return

        center = target.get("center") or [0, 0]
        report["write_scenario"]["window"] = {"title": target.get("title"), "bounds": target.get("bounds")}

        # P1（改动点 #10 / #12）：四状态用例 + 耗时断言（先跑状态迁移，再回到可点击状态）
        await _p1_state_scenario(call, report, target.get("hwnd"), "notepad.exe")

        clicked = await call("ui_click", {"x": int(center[0]), "y": int(center[1])})
        _check(
            report,
            "ui_click 坐标点击（置焦编辑区）",
            bool(clicked.get("ok")),
            f"xy=({center[0]},{center[1]})",
        )

        sample = "MCP 自测：中文输入通路 OK"
        typed = await call("ui_type", {"text": sample})
        _check(report, "ui_type 输入中文", bool(typed.get("ok")), f"chars={typed.get('chars')}")

        saved = await call("ui_hotkey", {"keys": "ctrl+s"})
        _check(report, "ui_hotkey 保存（ctrl+s）", bool(saved.get("ok")), str(saved.get("keys")))
        await asyncio.sleep(1.0)

        with open(doc, "r", encoding="utf-8") as handle:
            saved_text = handle.read()
        _check(
            report,
            "编辑器内容已写回磁盘",
            sample in saved_text,
            f"file_len={len(saved_text)}",
        )

        shot_path = os.path.join(out_dir, f"mcp_selftest_notepad_{ts}.png")
        shot = await call("ui_screenshot", {"path": shot_path})
        _check(
            report,
            "ui_screenshot 记事本截图",
            bool(shot.get("ok")) and os.path.exists(shot_path),
            shot_path,
        )

        region = target.get("bounds")
        hits = await call("ui_ocr", {"keyword": "MCP 自测", "region": region, "mode": "keyword"})
        if not hits.get("count"):
            hits = await call("ui_ocr", {"keyword": "MCP", "region": region, "mode": "keyword"})
        _check(
            report,
            "ui_ocr 关键词核验输入内容",
            bool(hits.get("count")),
            f"count={hits.get('count')} keyword={hits.get('keyword')}",
        )

        # P2（改动点 #13/#15）：冷启动实测 —— 关闭记事本后由内核发起启动并断言可定位
        await _p2_cold_start_scenario(call, report, proc)
    finally:
        try:
            await call("ui_activate_window", {"process_name": "notepad.exe"})
            await call("ui_hotkey", {"keys": "alt+f4"})
            await asyncio.sleep(1.2)
        except Exception as exc:  # pragma: no cover - 清理路径
            print(f"  [warn] 关闭记事本失败：{exc}")
        if proc.poll() is None:
            proc.terminate()
            report.setdefault("write_scenario", {})["force_terminated"] = True
            print("  [warn] 记事本未自行退出，已强制终止")


async def run_selftest(args: argparse.Namespace) -> Dict[str, Any]:
    server_script = os.path.join(_PROJECT_ROOT, "serve_mcp.py")
    env = dict(os.environ)
    env["UI_AGENT_LOG_LEVEL"] = env.get("UI_AGENT_LOG_LEVEL", "WARNING")
    env["UI_AGENT_WARMUP"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    base = {"command": args.python, "args": [server_script], "env": env}
    try:
        params = StdioServerParameters(cwd=_PROJECT_ROOT, **base)
    except TypeError:  # 该版本不支持 cwd 参数
        params = StdioServerParameters(**base)

    ts = _stamp()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    report: Dict[str, Any] = {
        "started_at": _now(),
        "server": {"command": args.python, "args": [server_script], "cwd": _PROJECT_ROOT},
        "handshake": {},
        "tools": {},
        "calls": [],
        "checks": [],
        "include_write": bool(args.include_write),
        "artifacts": [],
        "passed": False,
    }
    started = time.time()

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # ---------------------------------------------------- 1. 握手
            init = await asyncio.wait_for(session.initialize(), timeout=args.timeout)
            report["handshake"] = {
                "protocol_version": _field(init, "protocolVersion", "protocol_version"),
                "server_info": _asdict(_field(init, "serverInfo", "server_info")),
                "capabilities": _asdict(_field(init, "capabilities")),
                "instructions": _field(init, "instructions"),
            }
            server_info = report["handshake"]["server_info"] or {}
            print(
                "  [handshake] protocol=%s server=%s %s"
                % (
                    report["handshake"]["protocol_version"],
                    server_info.get("name"),
                    server_info.get("version"),
                )
            )
            _check(
                report,
                "initialize 握手成功且 serverInfo 正确",
                server_info.get("name") == "ui-agent",
                f"name={server_info.get('name')} version={server_info.get('version')}",
            )

            # ---------------------------------------------------- 2. 列工具
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            missing = [n for n in EXPECTED_TOOLS if n not in names]
            extra = [n for n in names if n not in EXPECTED_TOOLS]
            report["tools"] = {
                "count": len(names),
                "names": names,
                "missing": missing,
                "extra": extra,
                "schemas": {
                    tool.name: {
                        "description": tool.description,
                        "required": (_field(tool, "inputSchema", "input_schema", default={}) or {}).get(
                            "required", []
                        ),
                        "properties": sorted(
                            (
                                (_field(tool, "inputSchema", "input_schema", default={}) or {}).get(
                                    "properties"
                                )
                                or {}
                            ).keys()
                        ),
                    }
                    for tool in listed.tools
                },
            }
            _check(
                report,
                f"tools/list 返回 {len(EXPECTED_TOOLS)} 个预期工具",
                not missing and not extra,
                f"missing={missing} extra={extra}",
            )
            print("  [tools] " + ", ".join(names))

            # ---------------------------------------------------- 3. 真实调用
            async def call(name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> Any:
                t0 = time.perf_counter()
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments), timeout=timeout or args.timeout
                )
                elapsed = round(time.perf_counter() - t0, 3)
                text = "".join(
                    getattr(item, "text", "")
                    for item in result.content
                    if getattr(item, "type", None) == "text"
                )
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {"raw": text}
                report["calls"].append(
                    {
                        "tool": name,
                        "arguments": arguments,
                        "is_error": bool(_field(result, "isError", "is_error", default=False)),
                        "elapsed_seconds": elapsed,
                        "ok": payload.get("ok") if isinstance(payload, dict) else None,
                        "response": _truncate(payload),
                    }
                )
                print(
                    "  [call] %s ok=%s isError=%s %.2fs"
                    % (name, payload.get("ok") if isinstance(payload, dict) else None,
                       bool(_field(result, "isError", "is_error", default=False)), elapsed)
                )
                return payload

            await _readonly_scenario(call, report, out_dir, ts)
            if args.include_write:
                await _write_scenario(call, report, out_dir, ts)

    report["finished_at"] = _now()
    report["elapsed_seconds"] = round(time.time() - started, 2)
    report["passed"] = all(item["passed"] for item in report["checks"])
    report["artifacts"] = sorted(
        os.path.join(out_dir, name) for name in os.listdir(out_dir) if ts in name
    )

    report_path = os.path.join(out_dir, f"mcp_selfcheck_report_{ts}.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    report["report_path"] = report_path
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-selfcheck", description="ui-agent MCP Server 协议自测")
    parser.add_argument("--python", default=sys.executable, help="启动 MCP Server 的解释器")
    parser.add_argument("--out", default=os.path.join(_PROJECT_ROOT, "out"), help="报告与截图输出目录")
    parser.add_argument("--include-write", action="store_true", help="附加记事本端到端写链路自测")
    parser.add_argument("--timeout", type=float, default=120.0, help="单次调用超时（秒）")
    args = parser.parse_args(argv)

    report = asyncio.run(run_selftest(args))
    print(
        "\n[summary] passed=%s checks=%d/%d elapsed=%.2fs\n[report] %s"
        % (
            report["passed"],
            sum(1 for c in report["checks"] if c["passed"]),
            len(report["checks"]),
            report["elapsed_seconds"],
            report["report_path"],
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
