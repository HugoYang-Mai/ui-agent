"""ui-agent MCP Server 协议自测脚本。

以真实 MCP 客户端身份拉起 ``serve_mcp.py`` 子进程，走完整 stdio 协议链路：

1. **握手**：``initialize`` → 校验协议版本 / serverInfo / capabilities
2. **列工具**：``tools/list`` → 校验 8 个预期工具齐备并导出 inputSchema 摘要
3. **真实调用**：``tools/call``
   - 只读链路（默认）：``ui_window_list`` / ``ui_find`` / ``ui_screenshot`` / ``ui_ocr``
   - 写链路（``--include-write``）：启动记事本打开自建临时文档 → 激活窗口 → 点击编辑区
     → 中文输入 → ``ctrl+s`` 保存 → 截图 → OCR 核验 → 关闭进程

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
    "ui_find",
    "ui_click",
    "ui_type",
    "ui_hotkey",
    "ui_screenshot",
    "ui_ocr",
]


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
                "tools/list 返回 8 个预期工具",
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
