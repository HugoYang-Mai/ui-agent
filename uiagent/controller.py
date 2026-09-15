"""统一控制器：UIA 优先 + OCR 兜底。

定位策略（三层漏斗）
--------------------
1. **UI Automation**：毫秒级、100% 精度，能读到元素真实边界矩形 → 直接取中心点。
2. **OCR 兜底**：UIA 查不到（自绘 UI / 标签为空 / 非标控件）时，全屏 OCR 找文字，
   用文字块中心点作为点击坐标。
3. 两者都失败 → 明确报"未找到"，不做任何猜测性点击。

坐标系：全部为屏幕绝对物理像素，前提是调用方已执行
:func:`uiagent.dpi.enable_dpi_awareness`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .audit import describe_text, get_audit_logger
from .base import Rect, UIElement, OCRText
from .logging_utils import get_logger
from .screenshot import Screener

logger = get_logger("controller")


def _window_title_of(window: Any) -> str:
    """取限定窗口的标题（仅用于审计字段，取不到返回空串）。"""
    if window is None:
        return ""
    for attr in ("name", "Name", "title"):
        value = getattr(window, attr, None)
        if value:
            return str(value)
    return ""


def _emit_locate(
    result: LocateResult,
    *,
    role: Optional[str] = None,
    window_title: str = "",
    uia_ms: float = 0.0,
    ocr_ms: float = 0.0,
    candidates_count: int = 0,
    multi: bool = False,
) -> None:
    """落 ``locate`` 审计事件（只读，不改变 ``LocateResult`` 与工具返回结构）。"""
    audit = get_audit_logger()
    if audit is None:
        return
    payload: Dict[str, Any] = {
        "target": result.target,
        "role": role or "",
        "window_title": window_title,
        "source": result.source,
        "found": bool(result.found),
        "x": int(result.x),
        "y": int(result.y),
        "confidence": round(float(result.confidence), 4),
        "fallback": result.source == "ocr",
        "uia_ms": round(float(uia_ms), 3),
        "ocr_ms": round(float(ocr_ms), 3),
        "candidates_count": int(candidates_count),
    }
    if multi:
        payload["multi"] = True
    element = result.element
    if element is not None:
        payload["element"] = {
            "name": element.name,
            "role": element.role,
            "automation_id": element.automation_id,
            "class_name": element.class_name,
            "bounds": list(element.bounds),
            "hwnd": int(element.hwnd or 0),
        }
    if result.ocr_block is not None:
        payload["ocr_block"] = {
            "text": result.ocr_block.text,
            "confidence": round(float(result.ocr_block.confidence), 4),
            "bounds": list(result.ocr_block.bounds),
        }
    if result.hit_window:
        payload["hit_window"] = result.hit_window
    if result.auto_raise:
        payload["auto_raise"] = True
    if result.detail:
        payload["detail"] = result.detail
    audit.log("locate", **payload)


def _element_snapshot(element: Optional[UIElement]) -> Optional[Dict[str, Any]]:
    """元素的精简快照（写操作回读结果用）。"""
    if element is None:
        return None
    return {
        "name": element.name,
        "role": element.role,
        "automation_id": element.automation_id,
        "class_name": element.class_name,
        "bounds": list(element.bounds),
        "hwnd": int(element.hwnd or 0),
    }


@dataclass
class LocateResult:
    """目标定位结果（只描述"在哪"，不产生任何副作用）。"""

    target: str
    found: bool
    source: str = ""            # "uia" | "ocr" | ""
    x: int = 0
    y: int = 0
    confidence: float = 0.0
    element: Optional[UIElement] = None
    ocr_block: Optional[OCRText] = None
    detail: str = ""
    #: 点击后该坐标真实命中的顶层窗口（hwnd/title/app_alias/topmost…）
    hit_window: Optional[Dict[str, Any]] = None
    #: 是否为了命中目标窗口而临时抬升过 Z 序（被上层/置顶窗口遮挡时）
    auto_raise: bool = False

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x, self.y)

    def to_dict(self, with_element: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "target": self.target,
            "found": self.found,
            "source": self.source,
            "x": self.x,
            "y": self.y,
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
        }
        if with_element and self.element is not None:
            data["element"] = self.element.to_dict()
        if self.ocr_block is not None:
            data["ocr_block"] = self.ocr_block.to_dict()
        if self.hit_window is not None:
            data["hit_window"] = self.hit_window
        if self.auto_raise:
            data["auto_raise"] = True
        return data


class UniversalController:
    """Accessibility API + OCR 通用控制器。"""

    def __init__(
        self,
        search_timeout: float = 2.0,
        ocr_engine: Any = None,
        ocr_kwargs: Optional[Dict[str, Any]] = None,
        screener: Optional[Screener] = None,
        device: str = "cpu",
    ) -> None:
        from .accessibility.windows_uia import WindowsAccessibility

        self.device = device
        self.accessibility = WindowsAccessibility(search_timeout=search_timeout)
        self.screener = screener or Screener()
        self._ocr_engine = ocr_engine
        self._ocr_kwargs = dict(ocr_kwargs or {})
        self._mouse = None
        self._keyboard = None
        self._clipboard = None

    # ============================================================ 惰性组件
    @property
    def ocr(self):
        """OCR 引擎（首次访问时加载模型）。"""
        if self._ocr_engine is None:
            from .ocr.rapidocr_engine import RapidOCREngine

            self._ocr_engine = RapidOCREngine(screener=self.screener, **self._ocr_kwargs)
        return self._ocr_engine

    @property
    def mouse(self):
        if self._mouse is None:
            from .executor.mouse import MouseExecutor

            self._mouse = MouseExecutor()
        return self._mouse

    @property
    def keyboard(self):
        if self._keyboard is None:
            from .executor.keyboard import KeyboardExecutor

            self._keyboard = KeyboardExecutor()
        return self._keyboard

    @property
    def clipboard(self):
        if self._clipboard is None:
            from .executor.clipboard import ClipboardExecutor

            self._clipboard = ClipboardExecutor()
        return self._clipboard

    def warmup_ocr(self) -> float:
        """预热 OCR 引擎，返回模型加载耗时（秒）。"""
        return float(getattr(self.ocr, "load_seconds", 0.0))

    # ============================================================ 只读：环境
    def environment(self) -> Dict[str, Any]:
        """DPI / 坐标系 / 显示器环境快照。"""
        from . import dpi

        return dpi.describe()

    def screen_rect(self) -> Rect:
        """虚拟桌面矩形。"""
        return self.screener.virtual_rect()

    # ============================================================ 只读：UIA
    def list_windows(
        self,
        include_invisible: bool = False,
        include_non_window: bool = False,
        include_hidden: bool = True,
    ) -> List[UIElement]:
        """枚举顶层窗口（含隐藏 / 最小化到托盘的窗口，带 visible 标识）。"""
        return self.accessibility.list_windows(
            include_invisible=include_invisible,
            include_non_window=include_non_window,
            include_hidden=include_hidden,
        )

    def list_top_level(self, include_invisible: bool = False, include_hidden: bool = True) -> List[UIElement]:
        """枚举桌面直属顶层控件（不限 ControlType，含任务栏与隐藏窗口）。"""
        return self.accessibility.list_top_level(
            include_invisible=include_invisible, include_hidden=include_hidden
        )

    def get_active_window(self) -> Optional[UIElement]:
        """当前前台窗口。"""
        return self.accessibility.get_active_window()

    def find_window(
        self,
        title: Optional[str] = None,
        process_name: Optional[str] = None,
        exact: bool = False,
        include_invisible: bool = True,
        class_name: Optional[str] = None,
        hwnd: Optional[int] = None,
        include_hidden: bool = True,
    ) -> Optional[UIElement]:
        """按 hwnd / 标题 / 进程名 / 类名查找首个顶层窗口（只读，含隐藏窗口）。"""
        return self.accessibility.find_window(
            title=title,
            process_name=process_name,
            exact=exact,
            include_invisible=include_invisible,
            class_name=class_name,
            hwnd=hwnd,
            include_hidden=include_hidden,
        )

    def window_from_hwnd(self, hwnd: int) -> Optional[UIElement]:
        """按 HWND 取窗口元素（只读，含隐藏窗口）。"""
        return self.accessibility.window_from_hwnd(int(hwnd))

    def activate_window(
        self,
        title: Optional[str] = None,
        process_name: Optional[str] = None,
        exact: bool = False,
        restore: bool = True,
        hwnd: Optional[int] = None,
        class_name: Optional[str] = None,
        show_hidden: bool = True,
    ) -> Dict[str, Any]:
        """定位并激活窗口（写操作：把窗口置为前台，隐藏/最小化时先唤醒）。

        :return: ``{"found", "activated", "hwnd", "window", "detail", "hints"?}``
            未命中时额外返回 ``hints``（可执行的排查建议）与 ``candidates``。
        """
        window = self.find_window(
            title=title,
            process_name=process_name,
            exact=exact,
            class_name=class_name,
            hwnd=hwnd,
        )
        audit = get_audit_logger()
        if window is None:
            report = window_miss_report(
                title=title, process_name=process_name, hwnd=hwnd, exact=exact
            )
            payload = {
                "found": False,
                "activated": False,
                "hwnd": 0,
                "detail": (
                    f"未找到匹配窗口（title={title!r} process_name={process_name!r} "
                    f"hwnd={hwnd!r}）"
                ),
                "hints": report["hints"],
                "candidates": report["candidates"],
                "hidden_apps": report["hidden_apps"],
                "total_windows": report["total_windows"],
            }
            if audit is not None:
                audit.log_action(
                    "activate",
                    title=title or "",
                    process_name=process_name or "",
                    hwnd=int(hwnd or 0),
                    found=False,
                    activated=False,
                    activated_hwnd=0,
                    candidates_count=len(report["candidates"]),
                    detail=payload["detail"],
                )
            return payload
        result = self.accessibility.activate_window(
            window, restore=restore, show_hidden=show_hidden
        )
        result["found"] = True
        if audit is not None:
            audit.log_action(
                "activate",
                title=title or "",
                process_name=process_name or "",
                hwnd=int(hwnd or 0),
                found=True,
                activated=bool(result.get("activated")),
                activated_hwnd=int(result.get("hwnd") or 0),
                hit_title=str(getattr(window, "name", "") or ""),
                hit_app_alias=str(getattr(window, "app_alias", "") or ""),
                hit_process_name=str(getattr(window, "process_name", "") or ""),
                detail=str(result.get("detail") or ""),
            )
        return result

    def get_focused_element(self) -> Optional[UIElement]:
        """当前键盘焦点元素。"""
        return self.accessibility.get_focused_element()

    def get_root(self) -> UIElement:
        """桌面根节点。"""
        return self.accessibility.get_root()

    def find_element(
        self,
        name: Optional[str] = None,
        role: Optional[str] = None,
        window: Any = None,
        **kwargs: Any,
    ) -> Optional[UIElement]:
        """查找首个匹配元素。"""
        return self.accessibility.find_element(name=name, role=role, window=window, **kwargs)

    def find_all_elements(
        self,
        name: Optional[str] = None,
        role: Optional[str] = None,
        window: Any = None,
        **kwargs: Any,
    ) -> List[UIElement]:
        """查找全部匹配元素。"""
        return self.accessibility.find_all_elements(name=name, role=role, window=window, **kwargs)

    def get_element_tree(self, window: Any = None, depth: int = 3) -> Optional[UIElement]:
        """获取元素树。"""
        return self.accessibility.get_element_tree(root=window, depth=depth)

    def element_from_point(self, x: int, y: int) -> Optional[UIElement]:
        """屏幕坐标处的元素。"""
        return self.accessibility.element_from_point(x, y)

    def get_element_value(self, element: Any) -> Optional[str]:
        """读取控件当前文本值（ValuePattern，只读）。"""
        return self.accessibility.get_value(element)

    # ============================================================ 只读：截图 / OCR
    def screenshot(self, region: Optional[Rect] = None):
        """截屏，返回 PIL Image。"""
        return self.screener.grab(region)

    def save_screenshot(self, path: str, region: Optional[Rect] = None) -> str:
        """截屏并保存。"""
        return self.screener.save(path, region)

    def ocr_screen(self, region: Optional[Rect] = None) -> List[OCRText]:
        """识别屏幕（默认全屏）文本块。"""
        return self.ocr.recognize_all(region)

    def ocr_text(self, region: Optional[Rect] = None) -> str:
        """识别屏幕（默认全屏）并拼接为字符串。"""
        return self.ocr.recognize_region(region)

    def find_text_on_screen(
        self, text: str, fuzzy: bool = True, region: Optional[Rect] = None
    ) -> List[OCRText]:
        """在屏幕上按文字定位，返回全部命中（含坐标）。"""
        return self.ocr.find_text(text, image=None, region=region, fuzzy=fuzzy)

    # ============================================================ 只读：统一定位
    def locate(
        self,
        target: str,
        use_ocr_fallback: bool = True,
        window: Any = None,
        role: Optional[str] = None,
        fuzzy: bool = True,
        region: Optional[Rect] = None,
    ) -> LocateResult:
        """定位目标（只查询坐标，不执行点击）。

        :param target: 元素名称 / 界面文字
        :param use_ocr_fallback: UIA 未命中时是否启用 OCR 兜底
        :param window: 限定搜索窗口（UIElement 或原生控件）；None 表示整个桌面
        :param role: 限定元素角色（如 "Button"）
        """
        # 第 1 优先级：UIA
        window_title = _window_title_of(window)
        started = time.perf_counter()
        element = self.accessibility.find_element(name=target, role=role, window=window, limit=1)
        uia_ms = (time.perf_counter() - started) * 1000.0

        ocr_ms = 0.0
        candidates = 0
        if element is not None and element.is_clickable:
            x, y = element.center
            result = LocateResult(
                target=target,
                found=True,
                source="uia",
                x=x,
                y=y,
                confidence=1.0,
                element=element,
                detail=f"UIA 命中 role={element.role} name={element.name!r}",
            )
            _emit_locate(
                result,
                role=role,
                window_title=window_title,
                uia_ms=uia_ms,
                candidates_count=1,
            )
            return result

        # 第 2 优先级：OCR
        if use_ocr_fallback:
            started = time.perf_counter()
            hits = self.find_text_on_screen(target, fuzzy=fuzzy, region=region)
            ocr_ms = (time.perf_counter() - started) * 1000.0
            candidates = len(hits)
            if hits:
                best = hits[0]
                result = LocateResult(
                    target=target,
                    found=True,
                    source="ocr",
                    x=best.center[0],
                    y=best.center[1],
                    confidence=float(best.confidence),
                    ocr_block=best,
                    detail=f"OCR 命中文本={best.text!r} bounds={best.bounds}",
                )
                _emit_locate(
                    result,
                    role=role,
                    window_title=window_title,
                    uia_ms=uia_ms,
                    ocr_ms=ocr_ms,
                    candidates_count=candidates,
                )
                return result

        result = LocateResult(target=target, found=False, detail="UIA 与 OCR 均未命中")
        _emit_locate(
            result,
            role=role,
            window_title=window_title,
            uia_ms=uia_ms,
            ocr_ms=ocr_ms,
            candidates_count=candidates,
        )
        return result

    def locate_many(
        self,
        target: str,
        role: Optional[str] = None,
        window: Any = None,
        limit: int = 20,
    ) -> List[LocateResult]:
        """定位全部匹配目标（仅 UIA + OCR 结果合并，按来源分组）。"""
        results: List[LocateResult] = []
        window_title = _window_title_of(window)
        started = time.perf_counter()
        for element in self.accessibility.find_all_elements(
            name=target, role=role, window=window, limit=limit
        ):
            if not element.is_clickable:
                continue
            x, y = element.center
            results.append(
                LocateResult(
                    target=target,
                    found=True,
                    source="uia",
                    x=x,
                    y=y,
                    confidence=1.0,
                    element=element,
                    detail=f"role={element.role}",
                )
            )
        uia_ms = (time.perf_counter() - started) * 1000.0
        ocr_ms = 0.0
        if not results:
            started = time.perf_counter()
            for block in self.find_text_on_screen(target):
                results.append(
                    LocateResult(
                        target=target,
                        found=True,
                        source="ocr",
                        x=block.center[0],
                        y=block.center[1],
                        confidence=float(block.confidence),
                        ocr_block=block,
                        detail=f"text={block.text!r}",
                    )
                )
            ocr_ms = (time.perf_counter() - started) * 1000.0

        total = len(results)
        for result in results:
            _emit_locate(
                result,
                role=role,
                window_title=window_title,
                uia_ms=uia_ms,
                ocr_ms=ocr_ms,
                candidates_count=total,
                multi=True,
            )
        if not results:
            _emit_locate(
                LocateResult(target=target, found=False, detail="UIA 与 OCR 均未命中"),
                role=role,
                window_title=window_title,
                uia_ms=uia_ms,
                ocr_ms=ocr_ms,
                candidates_count=0,
                multi=True,
            )
        return results

    # ============================================================ 写操作
    def click(
        self,
        target: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        use_ocr_fallback: bool = True,
        verify: bool = False,
    ) -> LocateResult:
        """点击目标：既可传 ``target``（走 UIA→OCR 定位），也可传坐标 ``x``/``y``。"""
        if target is not None:
            located = self.locate(target, use_ocr_fallback=use_ocr_fallback)
            if not located.found:
                return located
            x, y = located.x, located.y
        else:
            if x is None or y is None:
                raise ValueError("必须提供 target，或同时提供 x / y 坐标")
            located = LocateResult(target=f"({x}, {y})", found=True, source="coords", x=int(x), y=int(y))

        # ---- 遮挡保护：坐标点属于目标窗口、却被别的窗口（典型：置顶的微信主窗口）
        #      盖住时，先临时把目标窗口抬到最前，避免点击落到遮挡者身上；点击后自动复原 Z 序。
        guard_hwnd = 0
        boost: Dict[str, Any] = {}
        try:
            guard_hwnd = self._click_guard_hwnd(located)
            if guard_hwnd:
                left, top, width, height = self.accessibility.window_rect(guard_hwnd)
                if left <= located.x < left + width and top <= located.y < top + height:
                    boost = self.accessibility.ensure_clickable(guard_hwnd, located.x, located.y)
                    if boost.get("boosted"):
                        located.auto_raise = True
                        covered = boost.get("covered_by_before") or {}
                        who = (
                            covered.get("app_alias")
                            or covered.get("process_name")
                            or covered.get("title")
                            or "其它窗口"
                        )
                        located.detail += f" | 目标点被 {who} 遮挡，已临时抬升目标窗口后点击"
        except Exception:  # pragma: no cover - 遮挡诊断失败不阻断点击
            logger.debug("点击遮挡保护失败", exc_info=True)

        try:
            self.mouse.click(located.x, located.y, button=button, clicks=clicks)
            # 命中窗口必须在「抬升仍生效」时采样，否则会被复原后的 Z 序掩盖
            self._record_hit_window(located, guard_hwnd)
        finally:
            if guard_hwnd and boost:
                try:
                    self.accessibility.restore_clickable(guard_hwnd, boost)
                except Exception:  # pragma: no cover - 复原失败不影响点击结果
                    logger.debug("恢复 Z 序失败", exc_info=True)

        after: Optional[UIElement] = None
        if verify:
            after = self.element_from_point(located.x, located.y)
            located.detail += f" | 点击后该点元素={after.role}/{after.name!r}" if after else ""

        audit = get_audit_logger()
        if audit is not None:
            payload: Dict[str, Any] = {
                "target": located.target,
                "source": located.source,
                "x": int(located.x),
                "y": int(located.y),
                "button": button,
                "clicks": int(clicks),
                "auto_raise": bool(located.auto_raise),
            }
            if located.hit_window:
                payload["hit_window"] = located.hit_window
            if verify:
                payload["verified"] = _element_snapshot(after)
            audit.log_action("click", **payload)
        return located

    # ------------------------------------------------------------ 点击辅助
    def _click_guard_hwnd(self, located: LocateResult) -> int:
        """坐标点击的「预期窗口」：优先点击元素所属窗口，其次当前前台窗口。"""
        element = located.element
        hwnd = int(getattr(element, "hwnd", 0) or 0) if element is not None else 0
        if hwnd and self.accessibility.window_from_hwnd(hwnd) is not None:
            return hwnd
        return int(self.accessibility.foreground_hwnd() or 0)

    def _record_hit_window(self, located: LocateResult, guard_hwnd: int) -> None:
        """记录坐标点击真正命中的顶层窗口；命中非预期窗口时在 detail 中告警。"""
        try:
            hit = self.accessibility.window_at_point(located.x, located.y)
        except Exception:  # pragma: no cover - 诊断失败不阻断结果返回
            return
        if hit is None:
            return
        located.hit_window = hit.to_dict()
        if guard_hwnd and int(getattr(hit, "hwnd", 0) or 0) != int(guard_hwnd):
            who = hit.app_alias or hit.process_name or hit.name or "其它窗口"
            located.detail += (
                f" | 警告：该点实际命中 {who}(hwnd={hit.hwnd})，不是预期窗口(hwnd={guard_hwnd})"
            )

    def double_click(self, target: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None) -> LocateResult:
        """双击目标。"""
        located = self._resolve_target(target, x, y)
        if not located.found:
            return located
        self.mouse.double_click(located.x, located.y)
        return located

    def right_click(self, target: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None) -> LocateResult:
        """右键点击目标。"""
        located = self._resolve_target(target, x, y)
        if not located.found:
            return located
        self.mouse.right_click(located.x, located.y)
        return located

    def _resolve_target(
        self, target: Optional[str], x: Optional[int], y: Optional[int]
    ) -> LocateResult:
        if target is not None:
            return self.locate(target)
        if x is None or y is None:
            raise ValueError("必须提供 target，或同时提供 x / y 坐标")
        return LocateResult(target=f"({x}, {y})", found=True, source="coords", x=int(x), y=int(y))

    def type_text(self, text: str, interval: float = 0.02) -> None:
        """输入文本（含中文时自动走剪贴板）。"""
        audit = get_audit_logger()
        started = time.perf_counter()
        self.keyboard.type_text(text, interval=interval)
        if audit is not None:
            audit.log_action(
                "type",
                method="keystroke",
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                **describe_text(text, audit.text_policy),
            )

    def type_chinese(self, text: str) -> None:
        """通过剪贴板输入中文。"""
        audit = get_audit_logger()
        started = time.perf_counter()
        self.keyboard.type_via_clipboard(text)
        if audit is not None:
            audit.log_action(
                "type",
                method="clipboard",
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                **describe_text(text, audit.text_policy),
            )

    def hotkey(self, *keys: str) -> None:
        """发送快捷键。"""
        audit = get_audit_logger()
        started = time.perf_counter()
        self.keyboard.hotkey(*keys)
        if audit is not None:
            audit.log_action(
                "hotkey",
                keys=[str(key) for key in keys],
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )

    def scroll(self, clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        """滚动鼠标滚轮。"""
        self.mouse.scroll(clicks, x, y)

    def move_to(self, x: int, y: int, duration: Optional[float] = None) -> None:
        """移动鼠标。"""
        self.mouse.move_to(x, y, duration)

    def drag_to(self, start: Tuple[int, int], end: Tuple[int, int], duration: float = 0.5) -> None:
        """拖拽。"""
        self.mouse.drag_to(start, end, duration)

    def wait_for_target(
        self,
        target: str,
        timeout: float = 10.0,
        interval: float = 0.5,
        use_ocr_fallback: bool = True,
    ) -> Optional[LocateResult]:
        """轮询等待目标出现（只读，不点击）。"""
        import time

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            located = self.locate(target, use_ocr_fallback=use_ocr_fallback)
            if located.found:
                return located
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)


# ================================================================ 未命中排查
def window_miss_report(
    title: Optional[str] = None,
    process_name: Optional[str] = None,
    hwnd: Optional[int] = None,
    exact: bool = False,
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """窗口未命中时生成可执行的排查建议（只读，不产生副作用）。

    :return: ``{"hints": [...], "candidates": [...], "hidden_apps": [...],
        "total_windows": int（应用窗口数）, "raw_windows": int（含系统辅助窗的原始枚举数）}``
    """
    import difflib

    from .accessibility import win32_windows as w32

    report: Dict[str, Any] = {
        "hints": [],
        "candidates": [],
        "hidden_apps": [],
        "total_windows": 0,
    }
    hints: List[str] = []

    try:
        windows = w32.enum_top_level_windows()
    except Exception:  # pragma: no cover - 极端环境
        logger.debug("枚举窗口失败", exc_info=True)
        windows = []
    report["total_windows"] = len([win for win in windows if win.is_app_window])
    report["raw_windows"] = len(windows)

    if hwnd:
        hints.append(
            f"hwnd={hwnd} 不是有效窗口（可能已关闭或句柄失效）："
            "请重新调用 ui_window_list 获取最新 hwnd 后再激活。"
        )
    if exact:
        hints.append(
            "当前 exact=true 要求标题完全相等；窗口标题常带状态后缀"
            "（如“微信 - 1 条新消息”），建议先试 exact=false。"
        )

    needle = (title or "").strip().lower()
    proc_needle = (process_name or "").strip().lower()

    # 1) 标题 / 别名 / 相似度候选
    scored: List[Tuple[int, Any]] = []
    for win in windows:
        score = 0
        title_l = (win.title or "").lower()
        alias_l = (win.app_alias or "").lower()
        proc_l = (win.process_name or "").lower()
        if needle:
            if needle and (needle in title_l or needle in alias_l or needle in proc_l):
                score = 3
            elif title_l and difflib.SequenceMatcher(None, needle, title_l).ratio() >= 0.6:
                score = 2
        if proc_needle and proc_needle in proc_l and score < 3:
            score = max(score, 3 if win.is_app_window else 2)
        if score == 0 and win.is_app_window:
            score = 1
        if score:
            scored.append((score, win))
    scored.sort(key=lambda item: (-item[0], w32.window_rank(item[1])))

    def _candidate(win: Any) -> Dict[str, Any]:
        raw_proc = str(getattr(win, "process_name", "") or "")
        return {
            "hwnd": int(win.hwnd),
            "title": win.title,
            "app_alias": win.app_alias,
            "app_id": getattr(win, "app_id", ""),
            "process_name": raw_proc.replace("/", "\\").rsplit("\\", 1)[-1] or raw_proc,
            "process_path": raw_proc,
            "class_name": win.class_name,
            "visible": bool(win.visible),
            "iconic": bool(win.iconic),
            "hidden_app": bool(win.hidden_app),
        }

    top = [win for score, win in scored if score >= 2][:max_candidates]
    strong_match = bool(top)
    if not top:
        top = [win for _score, win in scored][:max_candidates]
    report["candidates"] = [_candidate(win) for win in top]

    # 只统计「像应用主窗口」的隐藏窗口：枚举池里还有大量系统消息窗 / 托盘窗 /
    # 渲染辅助窗（本机可达 400+ 个），全量统计会把提示淹没成噪声。
    hidden_apps_all = [
        win for win in windows if win.hidden_app and win.is_app_window
    ]
    hidden_apps = hidden_apps_all[:max_candidates]
    report["hidden_apps"] = [_candidate(win) for win in hidden_apps]

    if strong_match and top:
        best = top[0]
        proc_display = best.process_name or ""
        if proc_display and not proc_display.lower().endswith(".exe"):
            proc_display = proc_display.replace("\\", "/").rsplit("/", 1)[-1]
        hints.append(
            f"疑似目标窗口：hwnd={best.hwnd} 标题={best.title!r} "
            f"应用={best.app_alias or best.process_name}（{proc_display}）"
            f"可见={'是' if best.visible else '否'}；"
            f"建议直接按句柄激活：ui_activate_window(hwnd={best.hwnd})。"
        )
    if hidden_apps_all:
        aliases = sorted({w.app_alias or w.process_name for w in hidden_apps_all})
        samples = "；".join(
            f"{w.app_alias or w.process_name} hwnd={w.hwnd}" for w in hidden_apps[:5]
        )
        hints.append(
            f"检测到 {len(hidden_apps_all)} 个「应用在运行但窗口未显示」的隐藏窗口"
            f"（涉及 {len(aliases)} 个应用：{'、'.join(aliases)}；"
            f"例如 {samples}）：这类窗口在常规 UIA 枚举中不可见，"
            "请用 ui_window_list(include_invisible=true) 查看，或直接 ui_activate_window(hwnd=...) 唤醒。"
        )

    # 2) 进程在跑但窗口匹配不上（典型：微信窗口标题是登录昵称）
    searched = proc_needle or needle
    if searched:
        procs = w32.find_processes(searched, limit=5)
        if procs:
            detail = "、".join(
                f"{p.get('name')}（PID {p.get('pid')}，别名 {p.get('alias')}）" for p in procs
            )
            hints.append(
                f"检测到相关进程正在运行：{detail}；若窗口标题与进程名不一致"
                "（如微信标题是登录昵称 Hugo_ever），请改用 process_name 或 hwnd 定位。"
            )
    if not hints:
        hints.append(
            "未发现相近窗口；可先调用 ui_window_list(include_invisible=true) 查看全部窗口，"
            "再用返回的 hwnd 精确激活。"
        )
    hints.append(
        "经验：QQ / 微信等应用的窗口标题常为登录昵称而非应用名，"
        "匹配时优先使用 process_name 或 hwnd，不要只依赖标题。"
    )
    report["hints"] = hints
    return report

