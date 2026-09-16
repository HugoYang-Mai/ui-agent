"""Windows UI Automation (UIA) 无障碍后端。

依赖 ``uiautomation``（COM 封装）。
**线程约定**：UIA / COM 调用必须在已初始化 COM 的线程中执行；``uiautomation``
在导入时会为当前线程执行 ``CoInitializeEx``。多线程使用时请在每个工作线程内
重新导入或调用 ``uiautomation`` 的初始化。
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import uiautomation as auto

from ..base import AccessibilityBase, Rect, UIElement, normalize_role
from ..logging_utils import get_logger
from . import win32_windows as _w32

logger = get_logger("uia")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _query_process_name(pid: int) -> str:
    """通过 PID 查询进程可执行文件全路径（失败返回空串，带缓存）。

    实现下沉到 :mod:`uiagent.accessibility.win32_windows`。
    """
    return _w32.query_process_name(pid)


def _force_foreground(hwnd: int, restore: bool = True, attempts: int = 3) -> bool:
    """把指定 HWND 置为前台窗口（含前台锁定绕过与回读校验）。"""
    return _w32.force_foreground(hwnd, restore=restore, attempts=attempts)


def _bounds_ready(hwnd: int) -> bool:
    """窗口 bounds 是否已非空（宽高 > 0）——``wait_ready`` 的触发前置判断（P1 改动点 #9）。"""
    try:
        rect = _w32.get_window_rect(int(hwnd or 0))
    except Exception:  # pragma: no cover - 查询失败按"未就绪"处理
        return False
    return bool(rect and int(rect[2]) > 0 and int(rect[3]) > 0)


def win32_to_element(win: Any) -> UIElement:
    """``Win32Window`` → :class:`UIElement`（窗口列表统一输出格式）。

    ``process_name`` 只放**可执行文件名**（如 ``Weixin.exe``），完整路径放在
    ``process_path``，避免调用方误把长路径当成进程名做匹配。
    """
    cls = (win.class_name or "").lower()
    role = "PaneControl" if cls in ("shell_traywnd", "progman", "workerw") else "WindowControl"
    raw_proc = str(getattr(win, "process_name", "") or "")
    proc_base = raw_proc.replace("/", "\\").rsplit("\\", 1)[-1] or raw_proc
    return UIElement(
        name=win.title,
        role=role,
        bounds=win.bounds,
        enabled=True,
        focused=int(_w32.get_foreground_hwnd()) == int(win.hwnd),
        class_name=win.class_name,
        process_id=int(win.pid),
        process_name=proc_base,
        process_path=raw_proc,
        visible=bool(win.visible),
        iconic=bool(win.iconic),
        topmost=bool(getattr(win, "topmost", False)),
        hwnd=int(win.hwnd),
        app_alias=win.app_alias,
        app_id=win.app_id,
        framework=win.framework,
    )


def _attach_uia_native(element: UIElement, hwnd: int) -> UIElement:
    """尽力为窗口元素补一个 UIA 原生控件引用（失败不影响结果）。"""
    try:
        control = auto.ControlFromHandle(int(hwnd))
    except Exception:
        return element
    if control is not None:
        element.native = control
    return element


def window_element_from_hwnd(hwnd: int) -> Optional[UIElement]:
    """按 HWND 构造窗口元素（含隐藏 / 托盘窗口，不依赖 UIA 可见性）。"""
    win = _w32.window_from_hwnd(int(hwnd))
    if win is None:
        return None
    return _attach_uia_native(win32_to_element(win), int(hwnd))


class WindowsAccessibility(AccessibilityBase):
    """基于 UI Automation 的 Windows 无障碍实现（只读能力为主）。"""

    def __init__(self, search_timeout: float = 2.0) -> None:
        self._auto = auto
        self._search_timeout = search_timeout
        self._proc_cache: Dict[int, str] = {}
        try:
            auto.SetGlobalSearchTimeout(search_timeout)
        except Exception:  # pragma: no cover
            pass

    # ============================================================ 内部工具
    @staticmethod
    def _safe(control: Any, attr: str, default: Any = None) -> Any:
        try:
            value = getattr(control, attr)
            return default if value is None else value
        except Exception:
            return default

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        if pid not in self._proc_cache:
            self._proc_cache[pid] = _query_process_name(pid)
        return self._proc_cache[pid]

    def _rect_of(self, control: Any) -> Rect:
        try:
            rect = control.BoundingRectangle
            left, top = int(rect.left), int(rect.top)
            width = max(0, int(rect.right) - left)
            height = max(0, int(rect.bottom) - top)
            return (left, top, width, height)
        except Exception:
            return (0, 0, 0, 0)

    def _to_element(self, control: Any, depth: int = 0) -> Optional[UIElement]:
        if control is None:
            return None
        pid = int(self._safe(control, "ProcessId", 0) or 0)
        name = str(self._safe(control, "Name", "") or "")
        # process_name 统一放可执行文件名（Weixin.exe），完整路径放 process_path，
        # 避免同一字段在不同枚举路径下一会儿是基名一会儿是全路径。
        process_path = self._process_name(pid)
        process_name = process_path.replace("/", "\\").rsplit("\\", 1)[-1] or process_path
        class_name = str(self._safe(control, "ClassName", "") or "")
        hwnd = int(self._safe(control, "NativeWindowHandle", 0) or 0)
        alias, app_id, framework = _w32.app_alias_for(process_name, class_name)
        visible = not bool(self._safe(control, "IsOffscreen", False))
        iconic = False
        if hwnd:
            # 顶层窗口：以 Win32 真实可见性为准（UIA 对隐藏窗口只报 IsOffscreen 不可信）
            truth = _w32.is_window_visible(hwnd)
            if truth is not None:
                visible = bool(truth) or int(_w32.get_foreground_hwnd()) == hwnd
            iconic = _w32.is_iconic(hwnd)
        return UIElement(
            name=name,
            role=str(self._safe(control, "ControlTypeName", "Unknown") or "Unknown"),
            bounds=self._rect_of(control),
            enabled=bool(self._safe(control, "IsEnabled", True)),
            focused=bool(self._safe(control, "HasKeyboardFocus", False)),
            automation_id=str(self._safe(control, "AutomationId", "") or ""),
            class_name=class_name,
            control_type=int(self._safe(control, "ControlType", 0) or 0),
            process_id=pid,
            process_name=process_name,
            process_path=process_path,
            visible=visible,
            iconic=iconic,
            hwnd=hwnd,
            app_alias=alias,
            app_id=app_id,
            framework=framework,
            depth=depth,
            native=control,
        )

    @staticmethod
    def _native_of(target: Any) -> Any:
        """从 UIElement / 原生控件中取出原生控件引用。

        窗口元素由 Win32 枚举构造，天然没有 UIA 控件引用；这里按 ``hwnd`` **懒加载**，
        避免枚举时对几百个窗口逐个做跨进程 COM 调用（会显著拖慢枚举）。
        """
        if target is None:
            return None
        if isinstance(target, UIElement):
            if target.native is None and target.hwnd:
                try:
                    target.native = auto.ControlFromHandle(int(target.hwnd))
                except Exception:
                    logger.debug(
                        "ControlFromHandle(hwnd=%s) 失败", target.hwnd, exc_info=True
                    )
            return target.native
        return target

    def _children_of(self, control: Any) -> List[Any]:
        try:
            return list(control.GetChildren())
        except Exception:
            return []

    def _walk(
        self,
        control: Any,
        max_depth: int,
        limit: int,
        timeout: float,
        include_self: bool = False,
    ) -> Iterator[Tuple[Any, int]]:
        """深度优先遍历控件树，受深度 / 数量 / 超时三重保护。"""
        deadline = time.monotonic() + max(0.5, timeout)
        stack: List[Tuple[Any, int]] = [(control, 0)]
        produced = 0
        while stack:
            if produced >= limit or time.monotonic() > deadline:
                logger.debug("walk 提前结束：produced=%s limit=%s", produced, limit)
                return
            node, depth = stack.pop()
            if include_self or depth > 0:
                produced += 1
                yield node, depth
            if depth >= max_depth:
                continue
            children = self._children_of(node)
            for child in reversed(children):
                stack.append((child, depth + 1))

    @staticmethod
    def _matches(
        element: UIElement,
        name: Optional[str],
        role: Optional[str],
        automation_id: Optional[str],
        class_name: Optional[str],
        exact: bool,
    ) -> bool:
        if role and normalize_role(role) != normalize_role(element.role):
            return False
        if name:
            haystack = element.name.lower()
            needle = name.lower()
            if exact:
                if haystack != needle:
                    return False
            elif needle not in haystack:
                return False
        if automation_id and element.automation_id.lower() != automation_id.lower():
            return False
        if class_name and class_name.lower() not in element.class_name.lower():
            return False
        return True

    # ============================================================ 只读查询
    def get_root(self) -> UIElement:
        """桌面根节点。"""
        return self._to_element(auto.GetRootControl())

    def get_active_window(self) -> Optional[UIElement]:
        """当前前台窗口。"""
        try:
            control = auto.GetForegroundControl()
        except Exception:
            logger.debug("GetForegroundControl 失败", exc_info=True)
            return None
        return self._to_element(control)

    def get_window_handle(self, target: Any) -> int:
        """取窗口原生句柄（HWND），失败返回 0。"""
        control = self._native_of(target)
        if control is None:
            return 0
        return int(self._safe(control, "NativeWindowHandle", 0) or 0)

    def _element_for_win32(self, win: Any) -> UIElement:
        """Win32 窗口 → UIElement（纯本地构造，不触碰 UIA，保证枚举速度）。"""
        return win32_to_element(win)

    @staticmethod
    def _pick_best(elements: List[UIElement]) -> UIElement:
        """从多个候选中挑最像「应用主窗口」的一个（排除托盘/消息窗口）。"""

        def key(element: UIElement):
            helper = 1 if _w32.is_helper_window(element.class_name, element.name) else 0
            if element.visible and not element.iconic:
                tier = 0
            elif element.visible:
                tier = 1
            else:
                tier = 2
            return (helper, tier, -int(element.bounds[2]) * int(element.bounds[3]))

        return sorted(elements, key=key)[0]

    def window_from_hwnd(self, hwnd: int) -> Optional[UIElement]:
        """按 HWND 取窗口元素（含隐藏窗口，只读）。"""
        return window_element_from_hwnd(int(hwnd))

    # ------------------------------------------------------------ 遮挡诊断 / 可点性
    def foreground_hwnd(self) -> int:
        """当前前台窗口 HWND（只读，纯 Win32，不触碰 UIA）。"""
        return int(_w32.get_foreground_hwnd() or 0)

    def window_rect(self, hwnd: int) -> Rect:
        """窗口矩形 (left, top, width, height)（只读，纯 Win32）。"""
        return _w32.get_window_rect(int(hwnd))

    def probe_cover(self, hwnd: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """检测窗口某点（默认中心点）是否被其它窗口遮挡（只读）。"""
        return _w32.probe_window_cover(int(hwnd), x, y)

    def window_at_point(self, x: int, y: int) -> Optional[UIElement]:
        """屏幕坐标处最顶层的顶层窗口（只读）；用于核验"点到底落在谁身上"。"""
        win = _w32.top_window_at_point(int(x), int(y))
        return self._element_for_win32(win) if win is not None else None

    def ensure_clickable(self, hwnd: int, x: int, y: int) -> Dict[str, Any]:
        """确保坐标点击能落到 ``hwnd``：被上层/置顶窗口遮挡时临时抬升目标窗口。

        :return: 状态 dict，调用方点击结束后必须调 :meth:`restore_clickable` 复原。
        """
        return _w32.ensure_reachable(int(hwnd), int(x), int(y))

    def restore_clickable(self, hwnd: int, state: Optional[Dict[str, Any]]) -> None:
        """复原 :meth:`ensure_clickable` 造成的临时置顶。"""
        _w32.restore_after_boost(int(hwnd), state)

    def find_window(
        self,
        title: Optional[str] = None,
        process_name: Optional[str] = None,
        exact: bool = False,
        include_invisible: bool = True,
        class_name: Optional[str] = None,
        hwnd: Optional[int] = None,
        include_hidden: bool = True,
        limit: int = 2000,
    ) -> Optional[UIElement]:
        """查找首个顶层窗口（只读）。

        与 UIA 不同，这里以 Win32 ``EnumWindows`` 为基准，**隐藏 / 最小化到托盘的
        窗口同样参与匹配**，因此「标题是登录昵称、窗口藏在托盘」的应用（如微信）
        也能命中；匹配不到时还会回退用应用别名（``微信``）匹配。

        :param title: 窗口标题（默认子串匹配，``exact=True`` 时精确匹配）
        :param process_name: 进程名或可执行路径片段，如 ``notepad.exe`` / ``weixin``
        :param exact: 标题是否精确匹配
        :param include_invisible: 是否包含不可见窗口（默认 True，否则可能漏掉托盘窗口）
        :param class_name: 窗口类名片段（如 ``Qt51514QWindowIcon``）
        :param hwnd: 直接按句柄定位（优先级最高）
        :param include_hidden: 是否包含隐藏窗口（默认 True）
        :param limit: 参与匹配的候选窗口上限
        """
        if hwnd:
            return self.window_from_hwnd(int(hwnd))

        needle_title = (title or "").strip().lower()
        needle_proc = (process_name or "").strip().replace("\\", "/").lower()
        needle_class = (class_name or "").strip().lower()
        if not needle_title and not needle_proc and not needle_class:
            return None

        candidates = self.list_top_level(
            include_invisible=True,
            include_hidden=include_hidden,
            limit=max(1, int(limit)),
        )

        def _proc_hay(element: UIElement) -> str:
            return (
                f"{element.process_name} {getattr(element, 'process_path', '')}".replace(
                    "\\", "/"
                ).lower()
            )

        def _proc_ok(element: UIElement) -> bool:
            if not needle_proc:
                return True
            return needle_proc in _proc_hay(element)

        def _title_score(text: str) -> int:
            text = (text or "").strip().lower()
            if not needle_title or not text:
                return 0
            if text == needle_title:
                return 100
            if exact:
                return 0
            if text.startswith(needle_title):
                return 70
            if needle_title in text:
                return 60
            return 0

        def _alias_score(text: str) -> int:
            text = (text or "").strip().lower()
            if not needle_title or not text:
                return 0
            if text == needle_title:
                return 85
            if not exact and needle_title in text:
                return 50
            return 0

        scored: List[Tuple[int, UIElement]] = []
        for element in candidates:
            if needle_class and needle_class not in element.class_name.lower():
                continue
            if not _proc_ok(element):
                continue
            # 标题优先；标题未命中时回退用应用别名兜底（title='微信' → app_alias='微信'）。
            # 精确别名（85）高于标题子串（60），避免「微信输入法」这类同前缀窗口抢走微信。
            score = max(_title_score(element.name), _alias_score(element.app_alias))
            if score > 0:
                scored.append((score, element))
            elif not needle_title and (needle_proc or needle_class):
                scored.append((1, element))

        if not scored:
            return None
        best = sorted(scored, key=lambda pair: pair[0], reverse=True)[:14]
        return self._pick_best([element for _, element in best])

    def activate_window(
        self,
        target: Any = None,
        hwnd: Optional[int] = None,
        restore: bool = True,
        show_hidden: bool = True,
        wait_ready: bool = True,
    ) -> Dict[str, Any]:
        """把窗口置为前台（写操作：仅改变 Z 序，必要时还原最小化 / 显示隐藏窗口）。

        三种定位方式：``hwnd`` 直给 / ``target`` 传 UIElement（或原生控件、整数句柄）。
        隐藏或最小化到托盘的窗口会先 ``SW_SHOW`` / ``SW_RESTORE`` 再置前。

        P1（改动点 #9）：``wait_ready=True`` 时在"显示之后、置前之前"确认 bounds 已稳定
        （连续采样尺寸差 ≤2 px），避免"刚 ``SW_SHOW`` 完就点击"落到未完成布局的窗口；
        bounds 未稳定按**降级**处理（``degraded_reason="window_unstable"``），不抛异常。
        已可见且 bounds 完整的窗口（S1）不会产生等待开销。

        :return: ``{"activated", "hwnd", "window", "foreground_hwnd", "show", "state",
            "timing"{show_ms, settle_ms, foreground_ms, total_ms}, "stable", "detail"}``
        """
        result: Dict[str, Any] = {"activated": False, "hwnd": 0, "detail": ""}

        element: Optional[UIElement] = None
        handle = int(hwnd or 0)
        if not handle and target is not None:
            if isinstance(target, int):
                handle = int(target)
            else:
                control = self._native_of(target)
                if control is None:
                    result["detail"] = "目标不是有效的窗口控件"
                    return result
                element = target if isinstance(target, UIElement) else self._to_element(control)
                handle = int(self._safe(control, "NativeWindowHandle", 0) or 0)
                if not handle:
                    handle = int(getattr(element, "hwnd", 0) or 0)
            if handle:
                element = element or self.window_from_hwnd(handle)
        elif handle:
            element = self.window_from_hwnd(handle)

        if not handle:
            result["detail"] = "必须提供 target（窗口元素）或 hwnd"
            return result

        result["hwnd"] = handle
        if element is not None:
            result["window"] = element.to_dict()

        if not _w32.is_window(handle):
            result["detail"] = f"hwnd={handle} 不是有效窗口（可能已关闭）"
            return result

        show_state: Dict[str, Any] = {}
        if show_hidden:
            show_state = _w32.show_window(handle)
            result["show"] = show_state

        # P1（改动点 #9）：状态切换（SW_RESTORE / SW_SHOW）往往伴随窗口重排，先确认 bounds
        # 稳定再置前，避免上层紧接着的定位/点击落到未完成布局的窗口上。S1 已就绪窗口零开销。
        timing: Dict[str, Any] = {
            "show_ms": round(float(show_state.get("waited_ms") or 0.0), 3),
            "settle_ms": 0.0,
            "foreground_ms": 0.0,
            "total_ms": 0.0,
        }
        result["stable"] = True
        if wait_ready and (show_state.get("shown") or not _bounds_ready(handle)):
            settle_started = time.perf_counter()
            settle = _w32.wait_bounds_stable(handle)
            timing["settle_ms"] = round((time.perf_counter() - settle_started) * 1000.0, 3)
            result["stable"] = bool(settle.get("stable"))
            result["bounds"] = settle.get("bounds")
            result["settle_samples"] = settle.get("samples")
            if not result["stable"]:
                result["degraded"] = True
                result["degraded_reason"] = str(settle.get("degraded_reason") or "window_unstable")

        foreground_started = time.perf_counter()
        ok = _w32.force_foreground(handle, restore=restore)
        timing["foreground_ms"] = round((time.perf_counter() - foreground_started) * 1000.0, 3)
        timing["total_ms"] = round(
            timing["show_ms"] + timing["settle_ms"] + timing["foreground_ms"], 3
        )
        result["timing"] = timing
        result["state"] = _w32.window_state(hwnd=handle)
        result["activated"] = bool(ok)
        result["foreground_hwnd"] = _w32.get_foreground_hwnd()
        if ok:
            result["detail"] = "窗口已置为前台"
            if show_state.get("shown"):
                result["detail"] += "（已从隐藏 / 最小化状态恢复显示）"
        else:
            result["detail"] = "置前未生效（可能被系统前台锁定策略拦截）"

        final = _w32.window_from_hwnd(handle)
        if final is not None:
            result["window"] = final.to_dict()

        # ---- 遮挡诊断：置前 ≠ 点得到（被置顶的微信主窗口等盖住的坐标，点击会落到遮挡者身上）
        try:
            cover = _w32.probe_window_cover(handle)
        except Exception:  # pragma: no cover - 诊断失败不影响置前结果
            cover = {"checked": False, "reachable": True, "covered_by": None, "point": None}
        if cover.get("checked"):
            result["covered"] = not bool(cover.get("reachable"))
            result["probe_point"] = cover.get("point")
            covered_by = cover.get("covered_by")
            if result["covered"] and covered_by:
                result["covered_by"] = covered_by
                who = (
                    covered_by.get("app_alias")
                    or covered_by.get("process_name")
                    or covered_by.get("title")
                    or "其它窗口"
                )
                topmost_note = "，该窗口为置顶窗口(WS_EX_TOPMOST)" if covered_by.get("topmost") else ""
                result["hint"] = (
                    "窗口已置为前台，但它的中心点当前被 "
                    + str(who)
                    + "(hwnd="
                    + str(covered_by.get("hwnd"))
                    + ") 遮挡"
                    + topmost_note
                    + "：坐标点击/键盘输入可能落到遮挡窗口上。建议改用元素定位点击（ui_find 取 center 后再点、或先关闭/最小化遮挡窗口），"
                    "必要时 ui_window_list(include_invisible=true) 查看 topmost 标记确认遮挡者。"
                )
        return result

    def _dialog_text(self, hwnd: int, limit: int = 12) -> str:
        """读取对话框内的可见文本（UIA Name，只读；失败返回空串，不抛异常）。"""
        if not hwnd:
            return ""
        try:
            control = auto.ControlFromHandle(int(hwnd))
        except Exception:  # pragma: no cover - 句柄失效 / COM 异常
            return ""
        if control is None:
            return ""
        parts: List[str] = []

        def _walk(node: Any, depth: int) -> None:
            if depth > 3 or len(parts) >= limit:
                return
            try:
                children = node.GetChildren()
            except Exception:  # pragma: no cover
                return
            for child in children:
                if len(parts) >= limit:
                    return
                name = self._safe(child, "Name", "") or ""
                if isinstance(name, str) and name.strip():
                    parts.append(name.strip())
                _walk(child, depth + 1)

        try:
            _walk(control, 0)
        except Exception:  # pragma: no cover
            pass
        return " / ".join(parts)[:200]

    def close_window(
        self,
        target: Any = None,
        hwnd: Optional[int] = None,
        timeout: float = _w32.CLOSE_POLL_TIMEOUT,
        poll_interval: float = _w32.CLOSE_POLL_INTERVAL_MS / 1000.0,
        dialog_scan: bool = True,
    ) -> Dict[str, Any]:
        """向窗口投递 ``WM_CLOSE`` 并轮询确认句柄消失（写操作：程序化关闭通道）。

        P3（关闭可靠性根治 改动点 C1/C2）：关闭不再依赖 ``alt+f4`` 的前台送达，
        而是把 ``WM_CLOSE`` 直接投递到目标 HWND；成功判定以 ``IsWindow`` 为假
        （句柄消失）为准，**不要求进程退出**（窗口关闭后应用驻留后台属正常）。

        C3：若句柄未消失，只做**只读**模态框诊断并返回 ``blocked=True`` + ``dialogs``，
        交上层决策；本方法**绝不**点击对话框、**绝不**重试、**绝不**强杀进程。

        :return: ``{"closed", "hwnd", "window", "signal", "wait", "dialogs", "blocked",
            "blocked_reason", "timing", "detail"}``
        """
        result: Dict[str, Any] = {"closed": False, "hwnd": 0, "detail": ""}

        element: Optional[UIElement] = None
        handle = int(hwnd or 0)
        if not handle and target is not None:
            if isinstance(target, int):
                handle = int(target)
            else:
                control = self._native_of(target)
                if control is None:
                    result["detail"] = "目标不是有效的窗口控件"
                    return result
                element = target if isinstance(target, UIElement) else self._to_element(control)
                handle = int(self._safe(control, "NativeWindowHandle", 0) or 0)
                if not handle:
                    handle = int(getattr(element, "hwnd", 0) or 0)
        if not handle:
            result["detail"] = "必须提供 target（窗口元素）或 hwnd"
            return result

        result["hwnd"] = handle
        element = element or self.window_from_hwnd(handle)
        if element is not None:
            result["window"] = element.to_dict()

        started = time.perf_counter()
        if not _w32.is_window(handle):
            result["closed"] = True
            result["already_closed"] = True
            result["detail"] = f"hwnd={handle} 已不是有效窗口（无需关闭）"
            result["timing"] = {
                "signal_ms": 0.0,
                "wait_ms": 0.0,
                "total_ms": round((time.perf_counter() - started) * 1000.0, 3),
            }
            return result

        signal_started = time.perf_counter()
        signal = _w32.post_close(handle)
        signal_ms = round((time.perf_counter() - signal_started) * 1000.0, 3)
        result["signal"] = signal

        wait = _w32.wait_window_closed(handle, timeout=timeout, interval=poll_interval)
        result["wait"] = wait
        result["closed"] = bool(wait.get("closed"))
        result["timing"] = {
            "signal_ms": signal_ms,
            "wait_ms": float(wait.get("waited_ms") or 0.0),
            "total_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        if result["closed"]:
            result["detail"] = "窗口已关闭（句柄消失）"
            return result

        # C3：未关闭 → 只读诊断同进程可见模态框（对话框文本经 UIA 富化，便于上层决策）
        dialogs: List[Dict[str, Any]] = []
        if dialog_scan:
            try:
                dialogs = _w32.modal_window_candidates(handle)
            except Exception:  # pragma: no cover - 诊断失败不影响关闭结论
                dialogs = []
            for item in dialogs:
                text = self._dialog_text(int(item.get("hwnd") or 0))
                if text:
                    item["text"] = text
        result["dialogs"] = dialogs

        if dialogs:
            result["blocked"] = True
            result["blocked_reason"] = "modal_dialog"
            names = "、".join(
                str(d.get("title") or d.get("text") or f"hwnd={d.get('hwnd')}") for d in dialogs[:3]
            )
            result["detail"] = (
                f"窗口未关闭：检测到 {len(dialogs)} 个可能阻塞的对话框（{names}）；"
                "多为未保存确认类模态框，已交上层决策，未做任何点击/重试。"
            )
        else:
            result["blocked"] = False
            result["blocked_reason"] = "window_still_alive"
            result["degraded"] = True
            result["degraded_reason"] = "close_not_confirmed"
            result["detail"] = (
                f"WM_CLOSE 已投递（channel={signal.get('channel') or '无'}）但 "
                f"{float(wait.get('waited_ms') or 0.0) / 1000.0:.2f}s 内句柄仍存活，"
                "且未发现同进程模态框：窗口可能仍在关闭中，或应用忽略了 WM_CLOSE。"
            )
        return result

    def get_focused_element(self) -> Optional[UIElement]:
        """当前键盘焦点元素。"""
        try:
            control = auto.GetFocusedControl()
        except Exception:
            return None
        return self._to_element(control)

    def _select_windows(
        self,
        include_invisible: bool = False,
        include_hidden: bool = True,
        limit: int = 300,
    ) -> List[UIElement]:
        """Win32 全量枚举 + 过滤，返回窗口元素列表（含隐藏的应用窗口）。

        过滤规则：

        * ``include_invisible=False``（默认）→ 保留「像应用主窗口的窗口」+
          可见且有标题的顶层窗口；隐藏 / 托盘中的应用主窗口**不会被丢弃**，
          而是带 ``visible=False`` 一并返回（避免调用方误判应用未运行）。
        * ``include_invisible=True`` → 连辅助 / 消息 / 工具窗口一起返回。
        * ``include_hidden=False`` → 只要真实可见的窗口。
        """
        windows = _w32.enum_top_level_windows()
        if not include_hidden:
            windows = [w for w in windows if w.visible]
        if not include_invisible:
            selected = [
                w for w in windows if w.is_app_window or (w.visible and w.title and not w.cloaked)
            ]
            if not selected:
                selected = [w for w in windows if w.visible]
        else:
            selected = list(windows)
        selected.sort(key=_w32.window_rank)
        return [self._element_for_win32(w) for w in selected[: max(1, int(limit))]]

    def list_top_level(
        self,
        include_invisible: bool = False,
        limit: int = 300,
        include_hidden: bool = True,
    ) -> List[UIElement]:
        """枚举桌面顶层控件（不限 ControlType，含隐藏窗口）。

        以 Win32 ``EnumWindows`` 为基准，每项带 ``visible`` / ``iconic`` /
        ``process_name`` / ``class_name`` / ``app_alias`` / ``hwnd``。
        """
        return self._select_windows(
            include_invisible=include_invisible,
            include_hidden=include_hidden,
            limit=limit,
        )

    def list_windows(
        self,
        include_invisible: bool = False,
        include_non_window: bool = False,
        limit: int = 200,
        include_hidden: bool = True,
    ) -> List[UIElement]:
        """枚举顶层窗口（含隐藏 / 托盘窗口）。

        :param include_invisible: 是否连同辅助 / 工具类不可见窗口一起返回
        :param include_non_window: 是否包含任务栏等非 Window 角色的顶层控件
        :param limit: 返回数量上限
        :param include_hidden: 是否包含隐藏窗口（默认 True；隐藏的应用窗口会带
            ``visible=False`` 返回，方便调用方判断「应用在运行但窗口没显示」）
        """
        pool = self._select_windows(
            include_invisible=include_invisible,
            include_hidden=include_hidden,
            limit=max(limit * 3, 300),
        )
        if include_non_window:
            return pool[:limit]
        return [element for element in pool if normalize_role(element.role) == "window"][:limit]

    def find_all_elements(
        self,
        name: Optional[str] = None,
        role: Optional[str] = None,
        window: Any = None,
        automation_id: Optional[str] = None,
        class_name: Optional[str] = None,
        exact: bool = False,
        max_depth: int = 8,
        limit: int = 100,
        timeout: float = 8.0,
        include_invisible: bool = True,
        include_self: bool = True,
    ) -> List[UIElement]:
        """在指定窗口（默认整个桌面）中查找全部匹配元素。"""
        root = self._native_of(window) or auto.GetRootControl()
        results: List[UIElement] = []
        for control, depth in self._walk(root, max_depth, max(limit * 20, 2000), timeout, include_self):
            element = self._to_element(control, depth)
            if element is None:
                continue
            if not include_invisible and (not element.visible or not element.is_clickable):
                continue
            if self._matches(element, name, role, automation_id, class_name, exact):
                results.append(element)
                if len(results) >= limit:
                    break
        return results

    def find_element(
        self,
        name: Optional[str] = None,
        role: Optional[str] = None,
        window: Any = None,
        **kwargs: Any,
    ) -> Optional[UIElement]:
        """查找首个匹配元素（参数同 :meth:`find_all_elements`）。"""
        kwargs.setdefault("limit", 1)
        found = self.find_all_elements(name=name, role=role, window=window, **kwargs)
        return found[0] if found else None

    def get_element_tree(self, root: Any = None, depth: int = 3) -> Optional[UIElement]:
        """构建元素树（返回带 children 的根节点）。"""
        native = self._native_of(root) or auto.GetRootControl()
        return self._build_tree(native, depth, 0)

    def _build_tree(self, control: Any, max_depth: int, depth: int) -> Optional[UIElement]:
        element = self._to_element(control, depth)
        if element is None:
            return None
        if depth >= max_depth:
            return element
        for child in self._children_of(control):
            node = self._build_tree(child, max_depth, depth + 1)
            if node is not None:
                element.children.append(node)
        return element

    def element_from_point(self, x: int, y: int) -> Optional[UIElement]:
        """返回屏幕坐标点处的元素（物理像素坐标）。"""
        try:
            control = auto.ControlFromPoint(int(x), int(y))
        except Exception:
            return None
        return self._to_element(control)

    def get_value(self, target: Any) -> Optional[str]:
        """读取控件当前文本值（ValuePattern，只读）。

        适用于 Edit / Document 等支持 ValuePattern 的控件，例如记事本编辑区。
        控件不支持该模式或读取失败时返回 ``None``。
        """
        control = self._native_of(target)
        if control is None:
            return None
        try:
            pattern = control.GetValuePattern()
        except Exception:
            return None
        if pattern is None:
            return None
        try:
            return str(pattern.Value)
        except Exception:
            return None

    def window_tree_summary(self, window: Any = None, max_depth: int = 5, limit: int = 60) -> Dict[str, Any]:
        """对某个窗口做一次轻量元素盘点，返回统计信息（只读）。"""
        native = self._native_of(window) or auto.GetForegroundControl() or auto.GetRootControl()
        counter: Dict[str, int] = {}
        named: List[Dict[str, Any]] = []
        total = 0
        for control, depth in self._walk(native, max_depth, limit * 40, 8.0, include_self=True):
            element = self._to_element(control, depth)
            if element is None:
                continue
            total += 1
            key = normalize_role(element.role) or "unknown"
            counter[key] = counter.get(key, 0) + 1
            if element.name and len(named) < limit:
                named.append(
                    {
                        "name": element.name,
                        "role": element.role,
                        "center": list(element.center),
                        "depth": depth,
                    }
                )
        return {"total": total, "roles": counter, "named": named}


_default: Optional[WindowsAccessibility] = None


def get_accessibility() -> WindowsAccessibility:
    """获取进程内共享的 WindowsAccessibility 实例。"""
    global _default
    if _default is None:
        _default = WindowsAccessibility()
    return _default
