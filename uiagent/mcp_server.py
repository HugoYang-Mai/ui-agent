"""ui-agent MCP Server（stdio 传输）。

把 UIA + OCR 操控内核包装为本地 MCP Server，供 CowAgent / Marvis 等 MCP 客户端
通过 stdio（JSON-RPC over stdin/stdout）调用。暴露 8 个工具：

===========================  ============================================
工具                          作用
===========================  ============================================
``ui_window_list``           枚举顶层窗口（含隐藏/托盘窗口，带可见性与应用标识）
``ui_activate_window``       按 hwnd/标题/进程名把窗口置为前台（可唤醒隐藏窗口）
``ui_find``                  定位元素/界面文字（UIA 优先，OCR 兜底）
``ui_click``                 点击目标（定位式或坐标式）
``ui_type``                  输入文本（中文自动走剪贴板）
``ui_hotkey``                发送快捷键
``ui_screenshot``            截屏保存到文件
``ui_ocr``                   屏幕 OCR（文本块 / 关键词命中 / 整屏文本）
===========================  ============================================

设计要点
--------
1. **stdout 只承载 JSON-RPC**：内核日志固定走 stderr（见 ``logging_utils``），
   避免污染协议通道。
2. **COM 单线程**：UI Automation 依赖 COM，且 ``uiautomation`` 不保证跨线程安全。
   所有工具调用都被派发到**同一个**已完成 ``CoInitializeEx(APARTMENTTHREADED)``
   的工作线程串行执行（``_UiThread``），工具函数因此声明为 ``async def``。
3. **DPI 感知**：进程启动后第一件事就是 ``SetProcessDpiAwareness(2)``，保证
   UIA 边界矩形 / 截图像素 / 鼠标坐标处于同一物理像素坐标系。
4. **返回值**：统一为 JSON 字符串（``ensure_ascii=False``），恒含 ``ok`` 字段。

用法::

    .venv\\Scripts\\python.exe serve_mcp.py          # 直接启动 stdio 服务
    .venv\\Scripts\\python.exe main.py mcp           # 通过 CLI 启动
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
from typing import Annotated, Any, Callable, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from pydantic import Field

SERVER_NAME = "ui-agent"
SERVER_VERSION = "0.2.0"

_COINIT_APARTMENTTHREADED = 0x2

#: 禁止写入的系统核心目录（截图等落盘操作的前置安检）
_FORBIDDEN_WRITE_ROOTS = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
)

_KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "windows": "win",
    "cmd": "win",
    "meta": "win",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "spacebar": "space",
    "pgup": "pageup",
    "pgdn": "pagedown",
}


# ===================================================================== 基础工具
def _json(payload: Any) -> str:
    """序列化为 MCP 文本内容（保持中文可读）。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _force_utf8_stdio() -> None:
    """把 stdio 固定为 UTF-8，避免中文在 GBK 代码页下抛 UnicodeEncodeError。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 流被重定向 / 非标准流
            pass


def _default_output_dir() -> str:
    """截图默认输出目录：``UI_AGENT_OUTPUT_DIR`` 或系统临时目录。"""
    override = os.environ.get("UI_AGENT_OUTPUT_DIR")
    if override:
        return os.path.abspath(override)
    return os.path.join(tempfile.gettempdir(), "ui-agent", "shots")


def _ensure_writable(path: str) -> str:
    """校验并准备输出路径（拒绝写入系统核心目录）。"""
    target = os.path.abspath(path)
    lowered = target.lower()
    for root in _FORBIDDEN_WRITE_ROOTS:
        if lowered == root or lowered.startswith(root + os.sep):
            raise PermissionError(f"拒绝写入系统核心目录：{target}")
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return target


def _parse_region(region: Any) -> Optional[tuple]:
    """把 ``[left, top, width, height]`` / ``"l,t,w,h"`` 解析为元组。"""
    if region is None:
        return None
    if isinstance(region, str):
        parts = [p for p in re.split(r"[,\s]+", region.strip()) if p]
        values = [int(float(p)) for p in parts]
    else:
        values = [int(float(v)) for v in region]
    if len(values) != 4:
        raise ValueError(f"region 必须是 (left, top, width, height) 四元组，收到 {region!r}")
    if values[2] <= 0 or values[3] <= 0:
        raise ValueError(f"region 宽高必须为正数，收到 {region!r}")
    return tuple(values)


def _parse_hotkey(keys: Any) -> List[str]:
    """把 ``"ctrl+shift+s"`` / ``["ctrl", "s"]`` 解析为按键列表。"""
    if isinstance(keys, str):
        raw = [k.strip() for k in re.split(r"[+,]", keys) if k.strip()]
    else:
        raw = [str(k).strip() for k in (keys or []) if str(k).strip()]
    return [_KEY_ALIASES.get(k.lower(), k.lower()) for k in raw]


def _element_payload(element: Any, with_native_hwnd: bool = False) -> Dict[str, Any]:
    """UIElement → dict；可选附带 HWND。"""
    data = element.to_dict()
    if with_native_hwnd:
        data["hwnd"] = int(getattr(element.native, "NativeWindowHandle", 0) or 0)
    return data


# ===================================================================== COM 工作线程
class _UiThread:
    """专用 COM 工作线程：所有内核调用在此串行执行。

    线程与进程同生命周期（daemon 线程 + 阻塞队列），保证：

    * COM 单线程套间（STA）只初始化一次，``uiautomation`` 缓存的元素引用始终有效；
    * 所有 GUI 调用天然串行，不出现并发争抢焦点；
    * 不依赖线程池，避免「线程池提交嵌套线程池」的自锁。
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue" = queue.Queue()
        self._controller: Any = None
        self._thread = threading.Thread(target=self._main, name="ui-agent-com", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 专用线程主体
    def _main(self) -> None:
        # 1) COM 单线程套间（uiautomation / comtypes 的前置条件）
        try:
            ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        except Exception:  # pragma: no cover - 已初始化或非 Windows
            pass
        # 2) 统一坐标系（幂等；进程级设置，主线程通常已先行设置）
        try:
            from .dpi import enable_dpi_awareness

            enable_dpi_awareness()
        except Exception:  # pragma: no cover
            pass

        while True:
            item = self._queue.get()
            if item is None:  # 退出哨兵
                return
            func, box = item
            try:
                box["result"] = func()
            except BaseException as exc:  # noqa: BLE001 - 原样回抛给调用方
                box["error"] = exc
            finally:
                box["done"].set()

    # ------------------------------------------------------------ 内核控制器
    def ensure_controller(self) -> Any:
        """仅在 COM 工作线程内被调用；惰性构造内核控制器。"""
        if self._controller is None:
            # 惰性导入，确保 DPI 感知早于 PIL / uiautomation 导入
            from .controller import UniversalController

            timeout = float(os.environ.get("UI_AGENT_SEARCH_TIMEOUT", "2.0"))
            self._controller = UniversalController(search_timeout=timeout)
            self._controller.environment()  # 首次触达 UIA，尽早暴露环境问题
        return self._controller

    # ------------------------------------------------------------ 调用入口
    def run(self, func: Callable[[Any], Any], timeout: Optional[float] = None) -> Any:
        """把 ``func(controller)`` 投递到 COM 线程执行并等待结果。"""
        if timeout is None:
            timeout = float(os.environ.get("UI_AGENT_CALL_TIMEOUT", "120"))
        box: Dict[str, Any] = {"done": threading.Event()}
        self._queue.put((lambda: func(self.ensure_controller()), box))
        if not box["done"].wait(timeout):
            raise TimeoutError(f"ui-agent 内核调用超时（>{timeout}s），目标窗口可能无响应")
        if "error" in box:
            raise box["error"]
        return box["result"]


_UI = _UiThread()


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """把内核调用派发到 COM 单线程，阻塞等待结果（不阻塞事件循环）。"""
    return await asyncio.to_thread(_UI.run, lambda controller: fn(controller, *args, **kwargs))


async def _guard(
    fn: Any,
    *args: Any,
    tool: str = "",
    audit_args: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> str:
    """统一异常边界：内核异常转为 ``{"ok": false, "error": ...}``。

    同时作为审计出口：调用前落 ``tool_call``，返回前落 ``tool_result``
    （含耗时与成败）。``audit_args`` 只做入参摘要，``text`` 字段按脱敏策略处理。
    """
    from .audit import get_audit_logger, mask_args

    audit = get_audit_logger()
    started = time.time()
    if audit is not None:
        audit.log(
            "tool_call",
            tool=tool,
            args=mask_args(audit_args, audit.text_policy),
        )

    ok = True
    error = ""
    try:
        payload = await _call(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 契约要求任何失败都以 JSON 返回
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        payload = {"ok": False, "error": error}

    text = _json(payload)
    if audit is not None:
        audit.log(
            "tool_result",
            tool=tool,
            ok=ok,
            duration_ms=round((time.time() - started) * 1000.0, 3),
            result_size=len(text),
            error=error,
        )
    return text


# ===================================================================== 内核操作实现
#: 应用状态优先级（用于 apps[] 聚合状态的择优选值）
_STATE_ORDER = ("visible", "minimized", "hidden", "cloaked", "not_running", "unknown")


def _window_state_of(hwnd: Any) -> str:
    """单个 hwnd 的窗口状态（P1 改动点 #11）；异常时降级为 ``unknown``，绝不抛错。"""
    try:
        from .accessibility import win32_windows as w32

        return str(w32.window_state(hwnd=int(hwnd or 0)) or "unknown")
    except Exception:  # noqa: BLE001 - 状态字段属增强信息，失败不影响窗口枚举
        return "unknown"


def _op_window_list(ctrl: Any, include_invisible: bool, limit: int, only_windows: bool) -> Dict[str, Any]:
    listed = (
        ctrl.list_windows(include_invisible=include_invisible, include_hidden=True)
        if only_windows
        else ctrl.list_top_level(include_invisible=include_invisible, include_hidden=True)
    )
    active = ctrl.get_active_window()
    active_hwnd = int(getattr(active, "hwnd", 0) or 0) if active is not None else 0

    items: List[Dict[str, Any]] = []
    for element in listed[: max(1, int(limit))]:
        data = element.to_dict()
        data["title"] = element.name
        data["foreground"] = bool(element.hwnd and element.hwnd == active_hwnd)
        # P1（改动点 #11）：为每个窗口补 state，上层可直接按状态分派而无需再探测
        data["state"] = _window_state_of(data.get("hwnd"))
        items.append(data)

    hidden = [item for item in items if not item.get("visible")]
    topmost_windows = [item for item in items if item.get("topmost")]
    apps: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = item.get("app_alias") or item.get("process_name") or str(item.get("process_id"))
        entry = apps.setdefault(
            key,
            {
                "app_alias": item.get("app_alias", ""),
                "app_id": item.get("app_id", ""),
                "process_id": item.get("process_id", 0),
                "process_name": item.get("process_name", ""),
                "process_path": item.get("process_path", ""),
                "framework": item.get("framework", ""),
                "window_count": 0,
                "has_visible": False,
                "hwnds": [],
                "state": "unknown",
                "states": {},
            },
        )
        entry["window_count"] += 1
        entry["has_visible"] = bool(entry["has_visible"] or item.get("visible"))
        if item.get("hwnd"):
            entry["hwnds"].append(item["hwnd"])
        state = str(item.get("state") or "unknown")
        entry["states"][state] = int(entry["states"].get(state, 0)) + 1

    for entry in apps.values():
        ranked = sorted(
            entry["states"].keys(),
            key=lambda name: _STATE_ORDER.index(name) if name in _STATE_ORDER else len(_STATE_ORDER),
        )
        entry["state"] = ranked[0] if ranked else "unknown"

    return {
        "ok": True,
        "count": len(items),
        "total": len(listed),
        "visible_count": len(items) - len(hidden),
        "hidden_count": len(hidden),
        "foreground_window": active.to_dict() if active is not None else None,
        "environment": ctrl.environment(),
        "apps": list(apps.values()),
        "hidden_windows": [
            {
                "hwnd": item.get("hwnd"),
                "title": item.get("title"),
                "app_alias": item.get("app_alias"),
                "process_name": item.get("process_name"),
                "class_name": item.get("class_name"),
                "iconic": item.get("iconic"),
            }
            for item in hidden
        ],
        "topmost_count": len(topmost_windows),
        "topmost_windows": [
            {
                "hwnd": item.get("hwnd"),
                "title": item.get("title"),
                "app_alias": item.get("app_alias"),
                "process_name": item.get("process_name"),
                "class_name": item.get("class_name"),
                "bounds": item.get("bounds"),
            }
            for item in topmost_windows
        ],
        "windows": items,
        "note": (
            "visible=false 的窗口未显示（最小化到托盘/隐藏），应用仍在运行；"
            "可直接用其 hwnd 调用 ui_activate_window 唤醒。"
            "state ∈ {visible, minimized, hidden, cloaked, not_running} 为窗口状态"
            "（cloaked=窗口被 DWM 隐藏，not_running=句柄已失效），apps[].state 取该应用各窗口的"
            "最优状态，apps[].states 为状态分布。"
            "topmost=true 的窗口是置顶窗口，会盖住同区域普通窗口："
            "若目标窗口被其遮挡，坐标点击/键盘输入会落到置顶窗口上"
            "（ui_activate_window 返回的 covered / covered_by / hint 会给出提示）。"
        ),
    }


def _op_activate_window(
    ctrl: Any,
    title: Optional[str],
    process_name: Optional[str],
    hwnd: Optional[int],
    exact: bool,
    restore: bool,
    wait_ready: bool = True,
) -> Dict[str, Any]:
    """置前窗口（写操作）。

    P1（改动点 #9）：``wait_ready=True`` 时，唤醒（``SW_RESTORE`` / ``SW_SHOW``）后等待
    ``bounds`` 稳定再置前，避免「刚显示完就点击」落到未完成布局的窗口上；返回体新增
    ``state`` / ``stable`` / ``timing{show_ms, settle_ms, foreground_ms, total_ms}``。
    ``wait_ready=False`` 时保持改造前语义（不等待）。
    """
    if not title and not process_name and not hwnd:
        return {"ok": False, "error": "必须提供 hwnd / title / process_name 之一"}
    result = ctrl.activate_window(
        title=title,
        process_name=process_name,
        hwnd=int(hwnd) if hwnd else None,
        exact=exact,
        restore=restore,
        wait_ready=bool(wait_ready),
    )
    result["ok"] = bool(result.get("activated"))
    if not result["ok"]:
        if result.get("found"):
            result["error"] = result.get("detail", "窗口置前未生效")
        else:
            result["error"] = result.get("detail", "未找到匹配窗口")
            result["error"] += "；详见 hints（可执行的排查建议）与 candidates（疑似候选窗口）"
    return result


def _op_find(
    ctrl: Any,
    target: str,
    role: Optional[str],
    window_title: Optional[str],
    use_ocr_fallback: bool,
    fuzzy: bool,
    limit: int,
    app: Optional[str] = None,
    hwnd: Optional[int] = None,
    scope: Optional[str] = None,
    reuse_ttl: Optional[float] = None,
) -> Dict[str, Any]:
    """定位目标（只读）。

    P0（改动点 #3）：新增 ``app`` / ``hwnd`` / ``scope`` / ``reuse_ttl``，搜索域解析统一
    交给内核（``scope=auto`` 时 hwnd > window > AppContext 缓存 > 桌面级）。
    旧调用 ``ui_find(target)`` 不传任何新参数时行为与改造前**完全一致**（桌面级 + OCR 兜底）。
    """
    window = None
    region = None
    if window_title:
        window = ctrl.find_window(title=window_title)
        if window is None:
            return {"ok": False, "found": False, "error": f"未找到窗口 {window_title!r}"}
        if window.is_clickable:
            region = tuple(window.bounds)

    hinted_hwnd = int(hwnd) if hwnd else None
    located = ctrl.locate(
        target,
        use_ocr_fallback=use_ocr_fallback,
        window=window,
        role=role,
        fuzzy=fuzzy,
        region=region,
        scope=scope,
        hwnd=hinted_hwnd,
        app=app,
        reuse_ttl=reuse_ttl,
    )
    payload = located.to_dict()
    payload["ok"] = bool(located.found)
    if str(getattr(located, "scope", "")) == "desktop" and not (hwnd or window_title or app):
        payload["scope_hint"] = (
            "本次为桌面级搜索（未提供 app / hwnd / window_title）；"
            "传入 app 或 hwnd 可限定到目标窗口，桌面根遍历约 484 ms，窗口限定通常 <15 ms。"
        )

    if limit and int(limit) > 1:
        matches = ctrl.locate_many(
            target,
            role=role,
            window=window,
            limit=int(limit),
            scope=scope,
            hwnd=hinted_hwnd,
            app=app,
            reuse_ttl=reuse_ttl,
        )
        payload["matches"] = [m.to_dict(with_element=False) for m in matches]
        payload["match_count"] = len(matches)
    return payload


def _op_click(
    ctrl: Any,
    target: Optional[str],
    x: Optional[int],
    y: Optional[int],
    button: str,
    clicks: int,
    verify: bool,
    use_ocr_fallback: bool,
    app: Optional[str] = None,
    hwnd: Optional[int] = None,
    scope: Optional[str] = None,
    reuse_ttl: Optional[float] = None,
) -> Dict[str, Any]:
    """点击目标（写操作）。

    P0（改动点 #4）：传 ``app``（或 ``hwnd``）时，在**一次调用**内完成
    "唤起/置前（``ensure_window``）→ 窗口内定位 → 点击"，把原本 2~3 次工具往返合并为 1 次
    （R3/R4），并返回 ``timing{ensure_ms, locate_ms, click_ms}``（``click_ms`` 为点击阶段
    整体耗时，含定位与鼠标动作）与 ``state``。
    ``verify`` / ``use_ocr_fallback`` / 遮挡抬窗（``_click_guard_hwnd`` +
    ``restore_after_boost``）语义不变；不传 ``app`` / ``hwnd`` 时行为与改造前完全一致。
    """
    if target is None and (x is None or y is None):
        return {"ok": False, "error": "必须提供 target，或同时提供 x / y 坐标"}

    ensure: Dict[str, Any] = {}
    timing: Dict[str, Any] = {}
    if app or hwnd:
        ensure = ctrl.ensure_window(
            app=app,
            hwnd=int(hwnd) if hwnd else None,
            reuse_ttl=reuse_ttl,
        )
        timing["ensure_ms"] = ensure.get("ensure_ms", 0.0)
        timing["snapshot_ms"] = (ensure.get("timing") or {}).get("snapshot_ms", 0.0)
        if not ensure.get("found"):
            return {
                "ok": False,
                "found": False,
                "error": ensure.get("detail") or "未找到目标应用窗口",
                "state": ensure.get("state", ""),
                "degraded": True,
                "degraded_reason": ensure.get("degraded_reason", ""),
                "ensure": ensure,
                "timing": timing,
                "hint": (
                    "P0 不提供启动能力（未启动场景属 P2）；"
                    "可先用 ui_window_list 确认应用是否在运行，或由上层启动后再重试。"
                ),
            }
        if not hwnd:
            hwnd = int(ensure.get("hwnd") or 0) or None
        # 已锁定目标窗口 → 强制窗口限定，避免退化为桌面级全树搜索
        scope = scope or "window"

    click_started = time.perf_counter()
    located = ctrl.click(
        target=target,
        x=x,
        y=y,
        button=button,
        clicks=int(clicks),
        use_ocr_fallback=use_ocr_fallback,
        verify=verify,
        scope=scope,
        hwnd=int(hwnd) if hwnd else None,
        app=app,
        reuse_ttl=reuse_ttl,
        state=str(ensure.get("state") or ""),
        ensure_ms=ensure.get("ensure_ms"),
    )
    click_ms = (time.perf_counter() - click_started) * 1000.0
    payload = located.to_dict()
    payload["ok"] = bool(located.found)
    timing["locate_ms"] = round(float(getattr(located, "locate_ms", 0.0) or 0.0), 3)
    timing["click_ms"] = round(click_ms, 3)
    timing["total_ms"] = round(float(timing.get("ensure_ms") or 0.0) + click_ms, 3)
    payload["timing"] = timing
    if ensure:
        payload["ensure"] = {
            "app": ensure.get("app", ""),
            "state": ensure.get("state", ""),
            "hwnd": ensure.get("hwnd", 0),
            "cached": ensure.get("cached", False),
            "degraded": ensure.get("degraded", False),
            "degraded_reason": ensure.get("degraded_reason", ""),
            "ensure_ms": ensure.get("ensure_ms", 0.0),
        }
    if not located.found:
        payload["error"] = "目标未找到，未执行点击"
    else:
        payload["clicked"] = {"x": located.x, "y": located.y, "button": button, "clicks": int(clicks)}
    return payload


def _op_app_ensure(
    ctrl: Any,
    app: Optional[str],
    hwnd: Optional[int],
    title: Optional[str],
    process_name: Optional[str],
    launch_if_missing: bool,
    require_foreground: bool,
    wait_ready: bool,
    timeout: Optional[float],
    launch_method: str = "auto",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """统一唤起入口（P1 改动点 #10 + P2 改动点 #13/#15，写操作：可能启动进程、改变 Z 序）。

    一次调用完成「状态判定（S1~S4）→ 启动（S4 且 ``launch_if_missing``）/ 唤醒 → 置前 →
    就绪确认（bounds 稳定 + 可达校验）」，返回 ``state`` / ``hwnd`` / ``window`` /
    ``timing`` / ``reachable`` / ``degraded_reason``。

    - S4 未运行且 ``launch_if_missing=False``：保持 P1 行为，不启动进程，返回 ``candidates``；
    - S4 未运行且 ``launch_if_missing=True``：dispatch 到 launcher（解析 → 启动 → 按 PID 等窗口就绪）；
    - ``dry_run=True``：只解析启动入口，**不产生任何进程**（方案 3.1：写操作须支持 dry_run 预演）。
    """
    if not any([app, hwnd, title, process_name]):
        return {"ok": False, "error": "必须提供 app / hwnd / title / process_name 之一"}
    payload = ctrl.ensure_app(
        app=app,
        hwnd=int(hwnd) if hwnd else None,
        title=title,
        process_name=process_name,
        launch_if_missing=bool(launch_if_missing),
        require_foreground=bool(require_foreground),
        wait_ready=bool(wait_ready),
        timeout=float(timeout) if timeout else None,
        launch_method=str(launch_method or "auto"),
        dry_run=bool(dry_run),
    )
    if not payload.get("ok") and not payload.get("found"):
        payload.setdefault(
            "error",
            payload.get("degraded_reason")
            or payload.get("detail")
            or "应用未运行且未授权启动（launch_if_missing=False）",
        )
    return payload


def _op_launch_app(
    ctrl: Any,
    app: str,
    dry_run: bool,
    timeout: Optional[float],
    launch_method: str,
    args: str,
    working_dir: str,
    wait: bool,
) -> Dict[str, Any]:
    """启动应用并按 PID 等待窗口就绪（P2 改动点 #15，**写操作**：解析 → 启动 → 等就绪）。

    权限与降级：``UIAGENT_LAUNCH_ENABLED=0`` → 直接拒绝（``degraded_reason="launch_disabled"``）；
    ``UIAGENT_LAUNCH_ALLOWLIST`` 非空时严格校验（``not_allowlisted``）；解析多候选时不自动选择
    （``ambiguous_candidates``）；等待超时返回 ``state="launching"`` 而非异常（方案 RF7）。
    """
    if not str(app or "").strip():
        return {"ok": False, "error": "app 不能为空"}
    return ctrl.launch_app(
        str(app).strip(),
        args=str(args or ""),
        working_dir=str(working_dir or ""),
        method=str(launch_method or "auto"),
        dry_run=bool(dry_run),
        timeout=float(timeout) if timeout else None,
        wait=bool(wait),
    )


def _op_wait_window(
    ctrl: Any,
    pid: Optional[int],
    hwnd: Optional[int],
    app: Optional[str],
    timeout: float,
    stable_ms: float,
) -> Dict[str, Any]:
    """等待目标窗口出现并完成布局（P2 改动点 #15，**只读**）。

    三选一定位：``hwnd`` > ``pid`` > ``app``。超时返回 ``ok=false`` / ``state="launching"``
    （不是错误，供上层决定继续等待或放弃），替代上层「睡眠 + 反复查询」的轮询往返。
    """
    if not any([pid, hwnd, app]):
        return {"ok": False, "error": "必须提供 pid / hwnd / app 之一"}
    return ctrl.wait_app_window(
        pid=int(pid or 0),
        hwnd=int(hwnd or 0),
        app=str(app or ""),
        timeout=float(timeout),
        stable_ms=float(stable_ms),
    )



def _op_app_status(
    ctrl: Any,
    apps: Optional[List[str]],
    include_not_running: bool,
) -> Dict[str, Any]:
    """批量查询应用状态（P1 改动点 #10，**只读**：不改 Z 序、不启动进程）。"""
    return ctrl.app_status(apps=apps or [], include_not_running=bool(include_not_running))


def _op_type(ctrl: Any, text: str, interval: float, method: str) -> Dict[str, Any]:
    if text is None or text == "":
        return {"ok": False, "error": "text 不能为空"}
    started = time.perf_counter()
    mode = (method or "auto").lower()
    if mode == "clipboard":
        ctrl.type_chinese(text)
    elif mode in ("auto", "keystroke"):
        ctrl.type_text(text, interval=float(interval))
    else:
        return {"ok": False, "error": f"method 仅支持 auto / keystroke / clipboard，收到 {method!r}"}
    return {
        "ok": True,
        "chars": len(text),
        "method": mode,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _op_hotkey(ctrl: Any, keys: Any, delay: float) -> Dict[str, Any]:
    combo = _parse_hotkey(keys)
    if not combo:
        return {"ok": False, "error": "keys 不能为空，例如 'ctrl+s'"}
    if delay and float(delay) > 0:
        time.sleep(float(delay))
    ctrl.hotkey(*combo)
    return {"ok": True, "keys": combo, "sent_at": time.time()}


def _op_screenshot(ctrl: Any, path: Optional[str], region: Any) -> Dict[str, Any]:
    rect = _parse_region(region)
    if path:
        target = _ensure_writable(path)
        ext = os.path.splitext(target)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            raise ValueError(f"不支持的图片扩展名：{ext or '(空)'}")
    else:
        target = os.path.join(
            _default_output_dir(), f"ui_screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        )
        target = _ensure_writable(target)

    image = ctrl.screenshot(rect)
    image.save(target)
    return {
        "ok": True,
        "path": target,
        "width": int(image.size[0]),
        "height": int(image.size[1]),
        "region": list(rect) if rect else None,
    }


def _op_ocr(
    ctrl: Any,
    region: Any,
    keyword: Optional[str],
    mode: str,
    limit: int,
    fuzzy: bool,
) -> Dict[str, Any]:
    rect = _parse_region(region)
    mode = (mode or "blocks").lower()

    if mode == "text":
        text = ctrl.ocr_text(rect)
        clipped = text[: int(limit)] if limit and int(limit) > 0 else text
        return {
            "ok": True,
            "mode": "text",
            "text": clipped,
            "length": len(text),
            "truncated": len(clipped) < len(text),
            "region": list(rect) if rect else None,
        }

    if mode == "keyword" or keyword:
        if not keyword:
            return {"ok": False, "error": "mode=keyword 时必须提供 keyword"}
        hits = ctrl.find_text_on_screen(keyword, fuzzy=fuzzy, region=rect)
        matched = hits[: int(limit)] if limit and int(limit) > 0 else hits
        return {
            "ok": True,
            "mode": "keyword",
            "keyword": keyword,
            "count": len(hits),
            "matches": [h.to_dict() for h in matched],
        }

    if mode != "blocks":
        return {"ok": False, "error": f"mode 仅支持 blocks / keyword / text，收到 {mode!r}"}

    blocks = ctrl.ocr_screen(rect)
    shown = blocks[: int(limit)] if limit and int(limit) > 0 else blocks
    return {
        "ok": True,
        "mode": "blocks",
        "count": len(blocks),
        "blocks": [b.to_dict() for b in shown],
        "region": list(rect) if rect else None,
    }


# ===================================================================== MCP Server
server = MCPServer(
    name=SERVER_NAME,
    title="ui-agent (Windows UIA + OCR)",
    version=SERVER_VERSION,
    instructions=(
        "Windows 桌面自动化内核：UI Automation 优先、OCR 兜底，坐标为屏幕绝对物理像素。"
        "ui_click / ui_type / ui_hotkey / ui_activate_window 会真实操控桌面，调用前请确认目标。"
    ),
)


@server.tool(
    name="ui_window_list",
    description=(
        "枚举当前桌面的顶层窗口（只读）。返回窗口标题、类名、边界矩形、中心点、进程、"
        "hwnd 句柄、是否前台、是否可见（visible）以及应用别名（app_alias，如「微信」）等；"
        "同时返回 hidden_windows（应用在运行但窗口未显示的隐藏/托盘窗口）、apps（按应用聚合）"
        "与 DPI / 屏幕环境快照。默认包含隐藏窗口：若窗口标题是登录昵称（如微信的 Hugo_ever），"
        "可通过 app_alias / process_name 对应到具体应用，并用 hwnd 精确激活。"
    ),
)
async def ui_window_list(
    include_invisible: Annotated[
        bool,
        Field(
            description=(
                "是否连同辅助/工具/消息类不可见窗口一起返回（默认 false）。"
                "注意：隐藏或最小化到托盘的应用主窗口始终会返回，并带 visible=false 标识"
            )
        ),
    ] = False,
    only_windows: Annotated[
        bool, Field(description="仅返回窗口角色的顶层控件（默认 true）；false 时含任务栏等全部顶层控件")
    ] = True,
    limit: Annotated[int, Field(description="返回窗口数量上限（1-300）", ge=1, le=300)] = 50,
) -> str:
    """枚举顶层窗口（含隐藏 / 托盘窗口）。"""
    return await _guard(
        _op_window_list,
        include_invisible,
        limit,
        only_windows,
        tool="ui_window_list",
        audit_args={
            "include_invisible": include_invisible,
            "only_windows": only_windows,
            "limit": limit,
        },
    )


@server.tool(
    name="ui_activate_window",
    description=(
        "把窗口置为前台（写操作：改变 Z 序，隐藏/最小化时先唤醒显示）。三种定位方式："
        "hwnd（最稳，推荐；可直接用 ui_window_list 返回的 hwnd）、title（默认子串匹配）、"
        "process_name（如 'weixin.exe' / 'notepad.exe'）。注意微信/QQ 等窗口标题常为登录昵称，"
        "此时用 hwnd 或 process_name 定位。未命中时返回 hints（可执行的排查建议）与"
        "candidates（疑似候选窗口）供下一步使用。"
    ),
)
async def ui_activate_window(
    title: Annotated[Optional[str], Field(description="窗口标题（默认子串匹配；也可传应用别名如 '微信'）")] = None,
    process_name: Annotated[
        Optional[str], Field(description="进程名或可执行路径片段，如 'notepad.exe' / 'weixin'")
    ] = None,
    hwnd: Annotated[
        Optional[int], Field(description="窗口句柄（优先级最高，可激活隐藏/托盘窗口，不含 0）", ge=1)
    ] = None,
    exact: Annotated[bool, Field(description="标题是否精确匹配，默认 false（子串）")] = False,
    restore: Annotated[bool, Field(description="窗口最小化时是否先还原，默认 true")] = True,
    wait_ready: Annotated[
        bool,
        Field(
            description=(
                "唤醒（还原/显示）后是否等待窗口 bounds 稳定再置前，默认 true；"
                "传 false 可跳过等待（提速约 150 ms，但刚唤醒的窗口可能尚未完成布局）"
            )
        ),
    ] = True,
) -> str:
    """激活窗口（支持 hwnd，可唤醒隐藏窗口）。"""
    return await _guard(
        _op_activate_window,
        title,
        process_name,
        hwnd,
        exact,
        restore,
        wait_ready,
        tool="ui_activate_window",
        audit_args={
            "title": title,
            "process_name": process_name,
            "hwnd": hwnd,
            "exact": exact,
            "restore": restore,
            "wait_ready": wait_ready,
        },
    )


@server.tool(
    name="ui_app_ensure",
    description=(
        "把应用推进到可操作状态（写操作：可能启动进程、还原/显示窗口并改变 Z 序）。这是操作某个应用的"
        "首选入口：一次调用完成「状态判定 → 启动/唤醒 → 置前 → 就绪确认」，取代"
        "「ui_window_list 找 hwnd → ui_activate_window → 再定位」的多步往返。"
        "传 app（应用别名/进程名，如 '微信' / 'weixin.exe' / '记事本'）或 hwnd（最稳）。"
        "返回 state ∈ {visible, minimized, hidden, cloaked, not_running}（S1~S4 四状态分层）、"
        "hwnd、window、timing{snapshot_ms, show_ms, settle_ms, foreground_ms, launch_ms, total_ms}、"
        "reachable / covered_by。置前被系统拦截时返回 degraded=true 与"
        "degraded_reason='foreground_locked'（不是失败）。"
        "应用未启动时：launch_if_missing=true 会自动解析启动入口并启动（等同 ui_launch_app，"
        "受 UIAGENT_LAUNCH_ENABLED / UIAGENT_LAUNCH_ALLOWLIST 约束）；默认 false 则只返回"
        "state='not_running' 与 candidates，不启动任何进程。dry_run=true 可先预演启动解析。"
    ),
)
async def ui_app_ensure(
    app: Annotated[
        Optional[str], Field(description="应用别名 / 进程名 / exe 名，如 '微信' / 'weixin.exe' / '记事本'")
    ] = None,
    hwnd: Annotated[
        Optional[int], Field(description="窗口句柄（优先级最高，最稳；可用 ui_window_list 获取）", ge=1)
    ] = None,
    title: Annotated[Optional[str], Field(description="窗口标题（子串匹配，兼容既有定位方式）")] = None,
    process_name: Annotated[Optional[str], Field(description="进程名片段，如 'weixin' / 'notepad.exe'")] = None,
    launch_if_missing: Annotated[
        bool,
        Field(
            description=(
                "应用未运行时是否允许启动，默认 false（只返回状态与候选）；"
                "true 时走 launcher 解析+启动+等窗口就绪（写操作，需 UIAGENT_LAUNCH_ENABLED 未关闭且通过白名单校验）"
            )
        ),
    ] = False,
    require_foreground: Annotated[
        bool, Field(description="是否要求最终位于前台，默认 true；false 时仅唤醒不强制置前")
    ] = True,
    wait_ready: Annotated[
        bool, Field(description="窗口从最小化/隐藏唤醒后是否等待 bounds 稳定（默认 true）")
    ] = True,
    timeout: Annotated[
        Optional[float], Field(description="整体耗时预算（秒）；超预算则跳过就绪等待并降级返回", gt=0)
    ] = None,
    launch_method: Annotated[
        str, Field(description="启动方式（仅 S4 启动时生效）：auto / exec（ShellExecuteEx）/ process（CreateProcess）/ uwp")
    ] = "auto",
    dry_run: Annotated[
        bool, Field(description="预演：仅解析启动入口并返回，不启动任何进程（默认 false）")
    ] = False,
) -> str:
    """统一应用唤起入口（四状态分层 + S4 启动）。"""
    return await _guard(
        _op_app_ensure,
        app,
        hwnd,
        title,
        process_name,
        launch_if_missing,
        require_foreground,
        wait_ready,
        timeout,
        launch_method,
        dry_run,
        tool="ui_app_ensure",
        audit_args={
            "app": app,
            "hwnd": hwnd,
            "title": title,
            "process_name": process_name,
            "launch_if_missing": launch_if_missing,
            "require_foreground": require_foreground,
            "wait_ready": wait_ready,
            "timeout": timeout,
            "launch_method": launch_method,
            "dry_run": dry_run,
        },
    )


@server.tool(
    name="ui_app_status",
    description=(
        "批量查询应用状态（只读：不改 Z 序、不启动进程、不唤醒窗口）。用于动作前做一次"
        "廉价决策：apps 为空时返回当前所有「像应用主窗口」的聚合，传 apps（如 ['微信','记事本']）"
        "则逐个查询（未启动的应用会返回 state='not_running'）。"
        "每项包含 state ∈ {visible, minimized, hidden, cloaked, not_running}、hwnd、window、"
        "foreground、cached、multi_instance、candidates。"
    ),
)
async def ui_app_status(
    apps: Annotated[
        Optional[List[str]], Field(description="待查询应用列表（别名/进程名），留空则返回全部应用窗口聚合")
    ] = None,
    include_not_running: Annotated[
        bool, Field(description="是否返回未启动应用的记录（state='not_running'），默认 true")
    ] = True,
) -> str:
    """批量查询应用状态（只读）。"""
    return await _guard(
        _op_app_status,
        apps,
        include_not_running,
        tool="ui_app_status",
        audit_args={"apps": apps, "include_not_running": include_not_running},
    )


@server.tool(
    name="ui_launch_app",
    description=(
        "启动应用并等待窗口就绪（写操作：会创建新进程，属高风险能力）。用于「应用当前未运行」"
        "场景，替代人工模拟「Win 键 + 搜索 + 回车」。解析顺序：内置别名表 → 注册表 App Paths → "
        "开始菜单 .lnk → UWP；多候选时**不自动选择**，返回 candidates 与 resolved_by 由上层决策。"
        "返回 state ∈ {launching, visible, minimized, hidden, not_running}、hwnd、pid、"
        "resolved_by、target_path、launch_ms、wait_ms。"
        "dry_run=true 只解析不启动（返回 resolved_by / target_path，不产生任何进程）。"
        "安全约束：UIAGENT_LAUNCH_ENABLED=0 时直接拒绝（degraded_reason='launch_disabled'）；"
        "UIAGENT_LAUNCH_ALLOWLIST 非空时严格校验（'not_allowlisted'）；等待窗口超时返回 "
        "state='launching'（不是错误）。"
    ),
)
async def ui_launch_app(
    app: Annotated[
        str, Field(description="应用别名 / exe 名 / 完整路径，如 '记事本' / 'notepad.exe' / 'C:/Windows/System32/calc.exe'")
    ],
    dry_run: Annotated[
        bool, Field(description="预演：只解析启动入口并返回，不启动任何进程（默认 false）")
    ] = False,
    timeout: Annotated[
        Optional[float], Field(description="启动后等待窗口就绪的超时（秒）；留空按分级默认（轻量 1.5~20 s）", gt=0)
    ] = None,
    launch_method: Annotated[
        str, Field(description="启动方式：auto / exec（ShellExecuteEx）/ process（CreateProcess）/ uwp")
    ] = "auto",
    args: Annotated[str, Field(description="附加命令行参数（默认空；仅在被启动应用确需时使用）")] = "",
    working_dir: Annotated[str, Field(description="工作目录（默认空，即不指定）")] = "",
    wait: Annotated[
        bool, Field(description="是否等待窗口就绪（默认 true）；false 只发起启动立即返回 pid")
    ] = True,
) -> str:
    """启动应用并等待窗口就绪（写操作）。"""
    return await _guard(
        _op_launch_app,
        app,
        dry_run,
        timeout,
        launch_method,
        args,
        working_dir,
        wait,
        tool="ui_launch_app",
        audit_args={
            "app": app,
            "dry_run": dry_run,
            "timeout": timeout,
            "launch_method": launch_method,
            "args": args,
            "working_dir": working_dir,
            "wait": wait,
        },
    )


@server.tool(
    name="ui_wait_window",
    description=(
        "等待目标窗口出现并完成布局（只读：不启动进程、不改 Z 序）。替代上层「睡眠 + 反复查询」"
        "的轮询往返。三选一定位：hwnd > pid > app（app 走别名/进程名匹配）。"
        "返回 ok / state（ready 视为就绪；超时返回 state='launching'，不是错误）/ hwnd / window / "
        "waited_ms / stable。适用：启动较慢的应用（浏览器/Office/IDE）或刚发起的启动操作后确认窗口可用。"
    ),
)
async def ui_wait_window(
    pid: Annotated[Optional[int], Field(description="目标进程 ID（启动后由 ui_launch_app 返回）", ge=1)] = None,
    hwnd: Annotated[Optional[int], Field(description="目标窗口句柄（优先级最高）", ge=1)] = None,
    app: Annotated[Optional[str], Field(description="应用别名 / 进程名，如 '记事本' / 'notepad.exe'")] = None,
    timeout: Annotated[float, Field(description="最长等待时间（秒），默认 10", gt=0)] = 10.0,
    stable_ms: Annotated[
        float, Field(description="窗口 bounds 需保持稳定的时长（毫秒），默认 150", ge=0)
    ] = 150.0,
) -> str:
    """等待窗口出现并完成布局（只读）。"""
    return await _guard(
        _op_wait_window,
        pid,
        hwnd,
        app,
        timeout,
        stable_ms,
        tool="ui_wait_window",
        audit_args={"pid": pid, "hwnd": hwnd, "app": app, "timeout": timeout, "stable_ms": stable_ms},
    )


@server.tool(
    name="ui_find",
    description=(
        "定位界面目标并返回屏幕坐标（只读，不点击）。先用 UI Automation 按名称/角色查找，"
        "未命中时可回退到 OCR 文字定位。搜索范围默认自动限定到目标窗口：传 app（应用别名/"
        "进程名）或 hwnd 即可把搜索收敛到该窗口内（窗口限定通常 <15 ms，桌面级全树遍历"
        "约 484 ms）。不传任何窗口线索时保持桌面级搜索（与旧行为一致）。"
    ),
)
async def ui_find(
    target: Annotated[str, Field(description="元素名称或界面文字，如 '保存' / '文本编辑器'")],
    role: Annotated[
        Optional[str], Field(description="限定元素角色，如 'Button' / 'Edit' / 'Window'")
    ] = None,
    window_title: Annotated[
        Optional[str], Field(description="限定搜索窗口标题（子串匹配），缺省搜索整个桌面")
    ] = None,
    use_ocr_fallback: Annotated[
        bool, Field(description="UIA 未命中时是否启用 OCR 兜底，默认 true")
    ] = True,
    fuzzy: Annotated[bool, Field(description="OCR 文字是否模糊匹配，默认 true")] = True,
    limit: Annotated[
        int, Field(description="返回的候选命中数量上限（1-50），>1 时附带 matches 列表", ge=1, le=50)
    ] = 1,
    app: Annotated[
        Optional[str],
        Field(
            description=(
                "应用线索（别名 / 进程名 / 标题片段，如 '微信' / 'notepad.exe'）："
                "命中缓存时把搜索限定到该应用窗口，推荐优先使用"
            )
        ),
    ] = None,
    hwnd: Annotated[
        Optional[int],
        Field(description="目标窗口句柄（优先级最高，可用 ui_window_list / ui_click 返回值获取）", ge=1),
    ] = None,
    scope: Annotated[
        Optional[str],
        Field(
            description=(
                "搜索域：auto（默认，有 app/hwnd/window_title 或缓存命中则窗口限定，否则桌面级）/ "
                "window（强制窗口限定）/ desktop（强制桌面级）"
            ),
            pattern="^(auto|window|desktop)$",
        ),
    ] = None,
    reuse_ttl: Annotated[
        Optional[float],
        Field(description="AppContext 缓存复用秒数（缺省走全局配置 UIAGENT_CTX_TTL；0 表示不使用缓存）", ge=0.0, le=600.0),
    ] = None,
) -> str:
    """定位元素 / 界面文字。"""
    return await _guard(
        _op_find,
        target,
        role,
        window_title,
        use_ocr_fallback,
        fuzzy,
        limit,
        app,
        hwnd,
        scope,
        reuse_ttl,
        tool="ui_find",
        audit_args={
            "target": target,
            "role": role,
            "window_title": window_title,
            "use_ocr_fallback": use_ocr_fallback,
            "fuzzy": fuzzy,
            "limit": limit,
            "app": app,
            "hwnd": hwnd,
            "scope": scope,
            "reuse_ttl": reuse_ttl,
        },
    )


@server.tool(
    name="ui_click",
    description=(
        "点击目标（写操作）。推荐传 app（应用别名/进程名）+ target：一次调用内完成"
        "「唤起/置前 → 窗口内定位 → 点击」，避免同一目标被重复搜索，并返回 "
        "timing{ensure_ms, locate_ms, click_ms}。也可传 hwnd 精确指定窗口，"
        "或直接传坐标 x/y。不传 app / hwnd 时走旧链路（桌面级定位 + OCR 兜底）。"
    ),
)
async def ui_click(
    target: Annotated[
        Optional[str], Field(description="要点击的元素名称或界面文字；与 x/y 二选一")
    ] = None,
    x: Annotated[Optional[int], Field(description="屏幕绝对 X 坐标（物理像素）")] = None,
    y: Annotated[Optional[int], Field(description="屏幕绝对 Y 坐标（物理像素）")] = None,
    button: Annotated[
        str, Field(description="鼠标按键：left / right / middle", pattern="^(left|right|middle)$")
    ] = "left",
    clicks: Annotated[int, Field(description="点击次数（1=单击，2=双击）", ge=1, le=3)] = 1,
    verify: Annotated[bool, Field(description="点击后回读该坐标点元素信息，默认 false")] = False,
    use_ocr_fallback: Annotated[
        bool, Field(description="target 定位失败时是否用 OCR 兜底，默认 true")
    ] = True,
    app: Annotated[
        Optional[str],
        Field(
            description=(
                "应用线索（别名 / 进程名，如 '微信' / 'notepad.exe'）：传入后本次调用会在"
                "点击前自动唤醒并置前该应用窗口，再把定位限定在窗口内（推荐）"
            )
        ),
    ] = None,
    hwnd: Annotated[
        Optional[int],
        Field(description="目标窗口句柄（可按应用别名解析出窗口后使用，优先级最高）", ge=1),
    ] = None,
    scope: Annotated[
        Optional[str],
        Field(
            description=(
                "搜索域：auto（默认）/ window（强制窗口限定）/ desktop（强制桌面级）；"
                "传了 app / hwnd 时缺省视为 window"
            ),
            pattern="^(auto|window|desktop)$",
        ),
    ] = None,
    reuse_ttl: Annotated[
        Optional[float],
        Field(description="AppContext 缓存复用秒数（缺省走全局配置 UIAGENT_CTX_TTL；0 表示不使用缓存）", ge=0.0, le=600.0),
    ] = None,
) -> str:
    """点击目标。"""
    return await _guard(
        _op_click,
        target,
        x,
        y,
        button,
        clicks,
        verify,
        use_ocr_fallback,
        app,
        hwnd,
        scope,
        reuse_ttl,
        tool="ui_click",
        audit_args={
            "target": target,
            "x": x,
            "y": y,
            "button": button,
            "clicks": clicks,
            "verify": verify,
            "use_ocr_fallback": use_ocr_fallback,
            "app": app,
            "hwnd": hwnd,
            "scope": scope,
            "reuse_ttl": reuse_ttl,
        },
    )


@server.tool(
    name="ui_type",
    description=(
        "向当前焦点处输入文本（写操作）。纯 ASCII 走键盘逐字符输入，含中文时自动改用剪贴板粘贴，"
        "调用前请确保目标输入框已获得焦点（可先用 ui_click 点击）。"
    ),
)
async def ui_type(
    text: Annotated[str, Field(description="要输入的文本（支持中文与换行）")],
    interval: Annotated[
        float, Field(description="逐字符间隔秒数（仅键盘输入模式生效）", ge=0.0, le=1.0)
    ] = 0.02,
    method: Annotated[
        str,
        Field(
            description="输入方式：auto（默认，含中文自动走剪贴板）/ keystroke（强制逐字符键盘输入）/ clipboard（强制剪贴板粘贴）",
            pattern="^(auto|keystroke|clipboard)$",
        ),
    ] = "auto",
) -> str:
    """输入文本。"""
    return await _guard(
        _op_type,
        text,
        interval,
        method,
        tool="ui_type",
        audit_args={"text": text, "interval": interval, "method": method},
    )


@server.tool(
    name="ui_hotkey",
    description=(
        "发送组合快捷键（写操作），如 'ctrl+s'、'alt+f4'、'ctrl+shift+esc'。"
        "按键名不区分大小写，支持 ctrl / alt / shift / win / esc / enter / tab / f1-f12 / a-z / 0-9 等。"
    ),
)
async def ui_hotkey(
    keys: Annotated[str, Field(description="快捷键字符串，多个键用 + 分隔，如 'ctrl+shift+s'")],
    delay: Annotated[float, Field(description="发送前等待秒数，默认 0", ge=0.0, le=5.0)] = 0.0,
) -> str:
    """发送快捷键。"""
    return await _guard(
        _op_hotkey,
        keys,
        delay,
        tool="ui_hotkey",
        audit_args={"keys": keys, "delay": delay},
    )


@server.tool(
    name="ui_screenshot",
    description=(
        "截屏并保存为图片文件（只读，不改变桌面）。region 缺省截取整个虚拟桌面，"
        "传 [left, top, width, height] 可截取指定区域；path 缺省写入临时目录。"
    ),
)
async def ui_screenshot(
    path: Annotated[
        Optional[str], Field(description="保存的绝对路径（.png/.jpg/.bmp/.webp），缺省为临时目录下的时间戳文件名")
    ] = None,
    region: Annotated[
        Optional[List[int]],
        Field(description="截图区域 [left, top, width, height]（屏幕绝对物理像素），缺省全屏"),
    ] = None,
) -> str:
    """截屏保存。"""
    return await _guard(
        _op_screenshot,
        path,
        region,
        tool="ui_screenshot",
        audit_args={"path": path, "region": region},
    )


@server.tool(
    name="ui_ocr",
    description=(
        "对屏幕（或指定区域）做 OCR（只读）。mode=blocks 返回全部文本块及坐标；"
        "mode=keyword 按关键词定位文字并返回命中坐标；mode=text 返回整屏拼接文本。"
    ),
)
async def ui_ocr(
    region: Annotated[
        Optional[List[int]],
        Field(description="识别区域 [left, top, width, height]，缺省全屏"),
    ] = None,
    keyword: Annotated[
        Optional[str], Field(description="要查找的文字（传入即按关键词模式返回命中坐标）")
    ] = None,
    mode: Annotated[
        str,
        Field(
            description="返回模式：blocks（文本块）/ keyword（关键词命中）/ text（整屏文本）",
            pattern="^(blocks|keyword|text)$",
        ),
    ] = "blocks",
    limit: Annotated[
        int, Field(description="返回条目上限（blocks/matches 条数或 text 截断字符数），0 表示不限", ge=0, le=500)
    ] = 50,
    fuzzy: Annotated[bool, Field(description="关键词是否模糊匹配，默认 true")] = True,
) -> str:
    """屏幕 OCR。"""
    return await _guard(
        _op_ocr,
        region,
        keyword,
        mode,
        limit,
        fuzzy,
        tool="ui_ocr",
        audit_args={
            "region": region,
            "keyword": keyword,
            "mode": mode,
            "limit": limit,
            "fuzzy": fuzzy,
        },
    )


# ===================================================================== 启动
def run_stdio(warmup: Optional[bool] = None) -> int:
    """以 stdio 传输启动 MCP Server（阻塞直到客户端断开）。

    :param warmup: 是否预热 COM 工作线程与 UIA 内核，缺省读环境变量 ``UI_AGENT_WARMUP``
    """
    _force_utf8_stdio()

    # 1. 统一坐标系必须早于任何 GDI / 截图调用
    from .dpi import enable_dpi_awareness

    enable_dpi_awareness()

    # 2. 日志固定走 stderr，避免污染 stdout 的 JSON-RPC 通道
    from .logging_utils import setup_logging

    setup_logging(os.environ.get("UI_AGENT_LOG_LEVEL", "INFO"))

    # 3. 预热（默认开启）：提前初始化 COM 线程与 UIA 内核，降低首次调用延迟
    if warmup is None:
        warmup = os.environ.get("UI_AGENT_WARMUP", "1").lower() not in ("0", "false", "no")
    if warmup:
        try:
            _UI.run(lambda controller: None, timeout=60.0)
        except Exception as exc:  # pragma: no cover - 预热失败不影响服务启动
            print(f"[ui-agent] 预热失败：{exc}", file=sys.stderr, flush=True)

    server.run("stdio")
    return 0


def main() -> int:
    """entry point。"""
    return run_stdio()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
