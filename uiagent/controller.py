"""统一控制器：UIA 优先 + OCR 兜底。

定位策略（三层漏斗）
--------------------
1. **UI Automation**：毫秒级、100% 精度，能读到元素真实边界矩形 → 直接取中心点。
2. **OCR 兜底**：UIA 查不到（自绘 UI / 标签为空 / 非标控件）时，全屏 OCR 找文字，
   用文字块中心点作为点击坐标。
3. 两者都失败 → 明确报"未找到"，不做任何猜测性点击。

坐标系：全部为屏幕绝对物理像素，前提是调用方已执行
:func:`uiagent.dpi.enable_dpi_awareness`。

P0（应用唤起与定位优化方案 改动点 #1/#2）
----------------------------------------
- ``locate`` / ``locate_many`` 新增 ``scope`` / ``hwnd`` / ``app`` / ``reuse_ttl`` 参数，
  窗口限定默认启用（``scope`` 缺省 ``auto``）；旧调用不传新参数时行为按
  ``UIAGENT_SCOPE_DEFAULT`` 决定，默认值 ``auto`` 在无任何窗口线索时保持桌面级搜索，
  **旧调用语义不变**。
- 新增 ``AppContext`` TTL 缓存（``UIAGENT_CTX_TTL``，默认 30 s；设 0 关闭），
  缓存 ``app → {hwnd, bounds, state}``，供定位复用已确定的目标窗口。
- 回退开关：``UIAGENT_SCOPE_DEFAULT=desktop`` 一键恢复旧语义。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    scope: str = "desktop",
    scope_hwnd: int = 0,
    cached: bool = False,
    locate_ms: Optional[float] = None,
    state: str = "",
    ensure_ms: Optional[float] = None,
    launch_ms: Optional[float] = None,
    degraded_reason: str = "",
) -> None:
    """落 ``locate`` 审计事件（只读，不改变 ``LocateResult`` 与工具返回结构）。

    P0（改动点 #5）新增字段：``scope`` / ``scope_hwnd`` / ``cached`` / ``locate_ms`` /
    ``state`` / ``degraded_reason``；``ensure_ms`` / ``launch_ms`` 仅在由
    ``ui_click(app=...)`` 的一次调用合并链路触发时携带。
    """
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
        # ---- P0 新增（均为新增列，不参与旧口径计算）
        "scope": scope or "desktop",
        "scope_hwnd": int(scope_hwnd or 0),
        "cached": bool(cached),
        "locate_ms": round(float(locate_ms if locate_ms is not None else (uia_ms + ocr_ms)), 3),
    }
    if result.state:
        payload["state"] = result.state
    elif state:
        payload["state"] = state
    if result.degraded:
        payload["degraded"] = bool(result.degraded)
    if degraded_reason:
        payload["degraded_reason"] = degraded_reason
    if ensure_ms is not None:
        payload["ensure_ms"] = round(float(ensure_ms), 3)
    if launch_ms is not None:
        payload["launch_ms"] = round(float(launch_ms), 3)
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


# ---------------------------------------------------------------- P0：scope 工程
_SCOPE_MODES = ("auto", "window", "desktop")

#: ``AppContextEntry`` 允许的状态（P0 只区分"窗口就绪"与"未就绪"，P1 扩展为 S1~S4）
_CTX_STATES = ("ready", "unknown")


def _scope_default() -> str:
    """定位默认搜索域：``UIAGENT_SCOPE_DEFAULT=desktop`` 可一键恢复旧语义。"""
    value = str(os.environ.get("UIAGENT_SCOPE_DEFAULT", "auto") or "auto").strip().lower()
    return value if value in _SCOPE_MODES else "auto"


def _context_ttl() -> float:
    """``AppContext`` 缓存 TTL（秒）；``UIAGENT_CTX_TTL=0`` 关闭缓存。"""
    raw = str(os.environ.get("UIAGENT_CTX_TTL", "30") or "30").strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


# ---------------------------------------------------------------- P1：四状态分层
#: 统一入口 ``ensure_app`` 对外状态：S1 ``visible`` / S2 ``minimized`` / S3 ``hidden``
#: / ``cloaked``（UWP 挂起，不计入 S1~S3）/ ``not_running``（S4，P1 只如实上报不启动）
_APP_STATES = ("visible", "minimized", "hidden", "cloaked", "not_running")


def _ensure_v2_enabled() -> bool:
    """``UIAGENT_ENSURE_V2=0`` 时 ``ensure_app`` 回退为 ``find_window`` + ``activate_window`` 旧链路。"""
    raw = str(os.environ.get("UIAGENT_ENSURE_V2", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- 关闭可靠性根治（改动点 C1~C5）
#: 关闭通道：``wm_close``（默认，程序化直投 WM_CLOSE）/ ``alt_f4``（强制走键盘回退路径）
_CLOSE_METHODS = ("wm_close", "alt_f4")

#: 回退通道取值：``alt_f4``（默认，"WM_CLOSE 未收敛时补一次前台核验 + alt+f4"）/ ``off``（关闭）
_CLOSE_FALLBACK_MODES = ("alt_f4", "off")


def _close_fallback_mode() -> str:
    """``UIAGENT_CLOSE_FALLBACK=off`` 时禁用 alt+f4 回退通道（改动点 C5）。

    默认 ``alt_f4``：WM_CLOSE 未在轮询窗内收敛时，补"前台核验 → alt+f4"一次；
    设 ``off`` 则完全不做任何键盘注入，未收敛直接返回阻断/降级结论。
    """
    raw = str(os.environ.get("UIAGENT_CLOSE_FALLBACK", "alt_f4") or "alt_f4").strip().lower()
    return raw if raw in _CLOSE_FALLBACK_MODES else "alt_f4"


# ---------------------------------------------------------------- 关闭能力权限闸（改动点 C6）
#: 关闭能力总开关（``UIAGENT_CLOSE_ENABLED``，默认 ``1``）：设 ``0`` 时 ``ui_close`` 整条链路
#: 直接拒绝——不做任何窗口定位、不投递 ``WM_CLOSE``、不注入任何键盘按键。
CLOSE_ENABLED_ENV = "UIAGENT_CLOSE_ENABLED"

#: 关闭白名单（``UIAGENT_CLOSE_ALLOWLIST``，逗号分隔）：按「应用别名 / 进程名 / 窗口标题」匹配。
CLOSE_ALLOWLIST_ENV = "UIAGENT_CLOSE_ALLOWLIST"

#: 内置默认关闭白名单（**安全默认**，改动点 C6）：``UIAGENT_CLOSE_ALLOWLIST`` 未设置或为空白时
#: 生效，只含常用 GUI 应用；**刻意不含** ``cmd`` / ``powershell`` / ``pwsh`` / ``wt``（Windows 终端）
#: / ``taskmgr`` / ``regedit`` 等命令解释器与系统管理工具——关闭这类窗口等价于中断正在运行的
#: 命令或系统配置，需要时必须由调用方在 env 中显式配置（或设为 ``*`` / ``all`` 显式解除限制）。
DEFAULT_CLOSE_ALLOWLIST: Tuple[str, ...] = (
    "notepad.exe",
    "记事本",
    "calc.exe",
    "计算器",
    "mspaint.exe",
    "画图",
    "explorer.exe",
    "资源管理器",
    "文件资源管理器",
    "msedge.exe",
    "weixin.exe",
    "微信",
)

#: 显式解除关闭白名单限制的取值（``UIAGENT_CLOSE_ALLOWLIST`` 设为其中之一 → 不校验）
CLOSE_ALLOWLIST_UNRESTRICTED_TOKENS: Tuple[str, ...] = ("*", "all")

#: 关闭开关的假值集合（与 :data:`uiagent.launcher._flag` 同口径）
_CLOSE_FALSE_VALUES: Tuple[str, ...] = ("0", "false", "no", "off", "n", "f")


def _close_flag(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        raw = default
    return str(raw).strip().lower() not in _CLOSE_FALSE_VALUES


def close_enabled() -> bool:
    """关闭能力总开关（``UIAGENT_CLOSE_ENABLED``，默认 ``1``）。"""
    return _close_flag(CLOSE_ENABLED_ENV, "1")


def _close_token_norm(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'").lower()


def _close_token_stem(value: Any) -> str:
    text = _close_token_norm(value)
    for suffix in (".exe", ".lnk", ".com", ".bat"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _close_basename(value: Any) -> str:
    return str(value or "").replace("/", "\\").rsplit("\\", 1)[-1].lower()


def close_allowlist_raw() -> Tuple[str, ...]:
    """解析 ``UIAGENT_CLOSE_ALLOWLIST`` 原值（逗号分隔；未设置 / 空 → 空元组，**不套默认值**）。"""
    raw = str(os.environ.get(CLOSE_ALLOWLIST_ENV, "") or "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def close_allowlist_config() -> Dict[str, Any]:
    """计算**生效**关闭白名单配置（安全默认化的唯一入口）。

    :return: ``{"items": 生效条目, "source": "default" | "env" | "unrestricted",
        "unrestricted": bool, "explicit": bool}``

    语义（与 ``UIAGENT_LAUNCH_ALLOWLIST`` 对齐）：

    * env 未设置 / 空白 → ``source="default"``，套用 :data:`DEFAULT_CLOSE_ALLOWLIST`
      （**安全默认：不放开任意窗口关闭**）；
    * env 显式配置（非空） → ``source="env"``，以配置为准；
    * env 显式设为 ``*`` / ``all`` → ``unrestricted=True``，不校验（显式解除限制入口，需显式操作）。
    """
    items = close_allowlist_raw()
    explicit = bool(items)
    if not explicit:
        return {
            "items": tuple(DEFAULT_CLOSE_ALLOWLIST),
            "source": "default",
            "unrestricted": False,
            "explicit": False,
        }
    if any(item in CLOSE_ALLOWLIST_UNRESTRICTED_TOKENS for item in items):
        return {"items": (), "source": "unrestricted", "unrestricted": True, "explicit": True}
    return {"items": items, "source": "env", "unrestricted": False, "explicit": True}


def close_allowlist() -> Tuple[str, ...]:
    """生效关闭白名单条目（解除限制时为 ``()``）。"""
    return tuple(close_allowlist_config()["items"])


def close_allowlist_check(
    app: Any = "",
    title: Any = "",
    process_name: Any = "",
    target: Any = "",
) -> Dict[str, Any]:
    """校验目标窗口是否允许关闭（**默认启用安全白名单**，改动点 C6）。

    命中口径：进程名 / 应用别名 / 目标文件名（含去后缀形式）**精确命中**白名单条目，
    或窗口标题**包含**白名单条目（如 ``记事本`` 命中标题 ``未命名 - 记事本``）。

    :return: ``{"ok", "checked", "allowlist", "source", "tokens"?, "matched"?}``
    """
    config = close_allowlist_config()
    if config["unrestricted"]:
        return {"ok": True, "checked": False, "allowlist": [], "source": "unrestricted"}
    allow = tuple(config["items"])
    allow_set = {item for item in allow} | {_close_token_stem(item) for item in allow}
    process_tokens = {
        token
        for token in (
            _close_token_norm(process_name),
            _close_token_stem(process_name),
            _close_token_norm(_close_basename(target)),
            _close_token_stem(_close_basename(target)),
            _close_token_norm(app),
            _close_token_stem(app),
        )
        if token
    }
    title_tokens = {token for token in (_close_token_norm(title),) if token}
    matched = sorted(process_tokens & allow_set)
    if not matched:
        for keyword in sorted(allow_set):
            if not keyword:
                continue
            if any(keyword in token for token in title_tokens):
                matched.append(f"title~{keyword}")
                break
    return {
        "ok": bool(matched),
        "checked": True,
        "allowlist": list(allow),
        "source": str(config["source"]),
        "tokens": sorted(process_tokens | title_tokens),
        "matched": matched,
    }


@dataclass
class AppContextEntry:
    """``app → {hwnd, bounds, state}`` 缓存条目（只读快照，不含任何控件句柄）。"""

    app: str
    hwnd: int
    bounds: Optional[Tuple[int, int, int, int]] = None
    title: str = ""
    process_name: str = ""
    state: str = "unknown"
    cached_at: float = field(default_factory=time.time)

    def age(self) -> float:
        return max(0.0, time.time() - float(self.cached_at))

    def expired(self, ttl: float) -> bool:
        return ttl <= 0 or self.age() > float(ttl)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app": self.app,
            "hwnd": int(self.hwnd),
            "bounds": list(self.bounds) if self.bounds else None,
            "title": self.title,
            "process_name": self.process_name,
            "state": self.state,
            "age_ms": round(self.age() * 1000.0, 1),
        }


class AppContext:
    """``app`` 级别的轻量 TTL 缓存（P0 改动点 #2）。

    只缓存"应用 → 当前可用窗口"这一条信息，供后续定位复用，避免每次定位都退化为
    桌面级全树搜索。命中后仍由调用方校验 hwnd 是否仍然有效，失效即自动清除。

    - ``UIAGENT_CTX_TTL=0`` → 完全关闭（不读不写）；
    - 仅在 ``scope=auto`` 时参与决策，显式 ``scope`` 永不读缓存。
    """

    def __init__(self, ttl: Optional[float] = None) -> None:
        self.ttl = _context_ttl() if ttl is None else max(0.0, float(ttl))
        self._items: Dict[str, AppContextEntry] = {}
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------ 基础访问
    @staticmethod
    def _key(app: Any) -> str:
        return str(app or "").strip().lower()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def get(self, app: Any = None, window: Any = None, hwnd: Optional[int] = None) -> Optional[AppContextEntry]:
        """取缓存条目；传入 ``window`` / ``hwnd`` 时要求与缓存 hwnd 一致。"""
        if not self.enabled:
            return None
        key = self._key(app)
        if not key:
            return None
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expired(self.ttl):
            self._items.pop(key, None)
            self.misses += 1
            return None
        if hwnd and int(hwnd) != int(entry.hwnd):
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def set(
        self,
        app: Any,
        hwnd: int,
        bounds: Any = None,
        title: str = "",
        process_name: str = "",
        state: str = "ready",
    ) -> Optional[AppContextEntry]:
        """写入缓存条目；``app`` 为空或缓存关闭时不写。"""
        if not self.enabled:
            return None
        key = self._key(app)
        if not key or not hwnd:
            return None
        entry = AppContextEntry(
            app=str(app).strip(),
            hwnd=int(hwnd),
            bounds=tuple(int(v) for v in bounds) if bounds else None,
            title=str(title or ""),
            process_name=str(process_name or ""),
            state=state if state in _CTX_STATES else "unknown",
        )
        self._items[key] = entry
        return entry

    def invalidate(self, app: Any = None) -> None:
        """清除指定应用（``app`` 为空时清空全部）缓存。"""
        if app is None:
            self._items.clear()
            return
        self._items.pop(self._key(app), None)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "ttl": self.ttl,
            "size": len(self._items),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
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
    # ---- P0（改动点 #1）：定位搜索域与耗时归因
    #: 本次定位实际使用的搜索域："window" | "desktop"
    scope: str = "desktop"
    #: 窗口限定所用的 hwnd（desktop 域为 0）
    scope_hwnd: int = 0
    #: scope 是否来自 ``AppContext`` 缓存命中
    cached: bool = False
    #: 定位段耗时（uia_ms + ocr_ms）
    locate_ms: float = 0.0
    #: 应用状态（P1 起为 S1~S4；P0 由 ensure/缓存回填，可为空）
    state: str = ""
    #: 是否降级返回（如窗口线索解析失败而退化为桌面级）
    degraded: bool = False
    #: 降级原因（见 ``uiagent.audit.DEGRADED_REASONS``）
    degraded_reason: str = ""

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
        # ---- P0（改动点 #1/#5）：只增字段，不删既有字段
        data["scope"] = self.scope
        data["scope_hwnd"] = int(self.scope_hwnd)
        data["cached"] = bool(self.cached)
        data["locate_ms"] = round(float(self.locate_ms), 3)
        if self.state:
            data["state"] = self.state
        if self.degraded:
            data["degraded"] = True
        if self.degraded_reason:
            data["degraded_reason"] = self.degraded_reason
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
        # P0（改动点 #2）：app → 目标窗口的 TTL 缓存，供定位复用
        self._ctx = AppContext()

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
        wait_ready: bool = True,
    ) -> Dict[str, Any]:
        """定位并激活窗口（写操作：把窗口置为前台，隐藏/最小化时先唤醒）。

        P1（改动点 #9）：``wait_ready=True`` 时置前后确认 bounds 稳定，避免刚唤醒就定位/点击。

        :return: ``{"found", "activated", "hwnd", "window", "state", "timing", "detail", "hints"?}``
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
            window, restore=restore, show_hidden=show_hidden, wait_ready=wait_ready
        )
        result["found"] = True
        if audit is not None:
            result_timing = result.get("timing") or {}
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
                state=str(result.get("state") or ""),
                wait_ready=bool(wait_ready),
                settle_ms=float(result_timing.get("settle_ms") or 0.0),
                covered=bool(result.get("covered")),
                degraded=bool(result.get("degraded")),
                degraded_reason=str(result.get("degraded_reason") or ""),
                detail=str(result.get("detail") or ""),
            )
        return result

    def close_window(
        self,
        title: Optional[str] = None,
        process_name: Optional[str] = None,
        exact: bool = False,
        hwnd: Optional[int] = None,
        class_name: Optional[str] = None,
        method: str = "wm_close",
        timeout: float = 3.0,
        poll_interval: float = 0.025,
        require_foreground: bool = False,
        fallback: Optional[str] = None,
        dialog_scan: bool = True,
    ) -> Dict[str, Any]:
        """关闭窗口（写操作：程序化 ``WM_CLOSE`` 直投 + 轮询收敛，改动点 C1~C5）。

        口径（关闭可靠性根治）：

        * **C1 程序化通道**：``method="wm_close"``（默认）把 ``WM_CLOSE`` 直接投递到目标
          HWND，不再依赖 ``alt+f4`` 的前台送达；``method="alt_f4"`` 强制走键盘回退路径。
        * **C2 判定口径**：关闭成功以 **目标窗口句柄消失**（``IsWindow`` 为假）为准，
          **不要求进程退出**——应用驻留后台属正常。
        * **C3 模态框阻断**：句柄仍在且诊断到同进程模态对话框（未保存确认等）时，
          返回 ``blocked=True`` + ``blocked_reason="modal_dialog"`` + ``dialogs``（含可见文本），
          **交上层决策，绝不点击、绝不盲重试**。
        * **C4 收敛等待**：关闭后按 ``poll_interval`` 轮询至 ``timeout``，替换旧的固定单次判定。
        * **C5 回退开关**：``UIAGENT_CLOSE_FALLBACK=off``（或 ``fallback="off"``）是**键盘通道硬开关**——
          任何键盘关闭按键都不发送（含显式 ``method="alt_f4"``：此时降级回 WM_CLOSE 通道并标注
          ``degraded_reason="close_fallback_disabled"``）；默认 ``alt_f4`` 仅在 **无模态框** 且
          **前台核验通过** 时补一次 ``alt+f4``（前台核验不通过则不发键，避免误关其它窗口）。
        * **C6 权限闸（安全默认收紧）**：``UIAGENT_CLOSE_ENABLED=0`` → 整条链路直接拒绝
          （``degraded_reason="close_disabled"``，不定位、不投递、不发键）；
          ``UIAGENT_CLOSE_ALLOWLIST`` **默认启用安全白名单**——未设置 / 空白时套用
          :data:`DEFAULT_CLOSE_ALLOWLIST`（常用 GUI 应用，不含 ``cmd`` / ``powershell`` /
          终端 / ``taskmgr`` 等），命中失败返回 ``degraded_reason="close_not_allowlisted"``
          且不发送任何关闭信号；显式配置以配置为准，设为 ``*`` / ``all`` 表示不校验。

        :return: ``{"ok", "closed", "hwnd", "window", "method_requested", "method_used",
            "signal", "wait", "dialogs", "blocked", "blocked_reason", "degraded",
            "degraded_reason", "foreground", "timing", "detail", "hints"?,
            "allowlist"?, "allowlist_source"?, "allowlist_tokens"?}``
        """
        from .accessibility import win32_windows as w32

        requested = str(method or "wm_close").strip().lower()
        if requested not in _CLOSE_METHODS:
            return {
                "ok": False,
                "closed": False,
                "hwnd": 0,
                "error": f"method 仅支持 wm_close / alt_f4，收到 {method!r}",
            }
        fallback_mode = (
            str(fallback).strip().lower() if fallback else _close_fallback_mode()
        )
        if fallback_mode not in _CLOSE_FALLBACK_MODES:
            fallback_mode = _close_fallback_mode()
        # C5：回退开关为 off 时属于"键盘通道硬开关"——任何按键都不发送；此时显式要求
        # alt+f4 也不再拼装按键，而是降级回程序化 WM_CLOSE 通道，并标注降级原因（绝不空转）。
        keyboard_disabled = fallback_mode == "off"
        use_wm_close = requested == "wm_close" or keyboard_disabled
        audit = get_audit_logger()
        started = time.perf_counter()

        # ---------------------------------------------------------- C6：能力总开关（先于任何定位与发送）
        if not close_enabled():
            payload = {
                "ok": False,
                "closed": False,
                "hwnd": int(hwnd or 0),
                "method_requested": requested,
                "method_used": "none",
                "degraded": True,
                "degraded_reason": "close_disabled",
                "detail": (
                    f"关闭能力已禁用（{CLOSE_ENABLED_ENV}=0）：未做任何窗口定位、未投递 "
                    "WM_CLOSE、未注入任何键盘按键"
                ),
                "hint": f"如需恢复关闭能力：设置 {CLOSE_ENABLED_ENV}=1 并重启 MCP 服务",
                "timing": {"total_ms": round((time.perf_counter() - started) * 1000.0, 3)},
            }
            if audit is not None:
                audit.log_action(
                    "close_window",
                    title=title or "",
                    process_name=process_name or "",
                    hwnd=int(hwnd or 0),
                    found=False,
                    closed=False,
                    method_requested=requested,
                    method_used="none",
                    degraded=True,
                    degraded_reason="close_disabled",
                    detail=payload["detail"],
                )
            return payload

        # ---------------------------------------------------------- 定位与句柄核验（C4）
        window = self.find_window(
            title=title,
            process_name=process_name,
            exact=exact,
            class_name=class_name,
            hwnd=hwnd,
        )
        if window is None:
            handle = int(hwnd or 0)
            if handle and not w32.is_window(handle):
                payload = {
                    "ok": True,
                    "closed": True,
                    "already_closed": True,
                    "hwnd": handle,
                    "method_requested": requested,
                    "method_used": "none",
                    "detail": f"hwnd={handle} 已不是有效窗口（句柄消失，视为已关闭）",
                    "timing": {"total_ms": round((time.perf_counter() - started) * 1000.0, 3)},
                }
                if audit is not None:
                    audit.log_action(
                        "close_window",
                        title=title or "",
                        process_name=process_name or "",
                        hwnd=handle,
                        found=False,
                        closed=True,
                        already_closed=True,
                        method_requested=requested,
                        method_used="none",
                        detail=payload["detail"],
                    )
                return payload
            report = window_miss_report(
                title=title, process_name=process_name, hwnd=hwnd, exact=exact
            )
            payload = {
                "ok": False,
                "closed": False,
                "hwnd": 0,
                "method_requested": requested,
                "method_used": "none",
                "detail": (
                    f"未找到匹配窗口（title={title!r} process_name={process_name!r} "
                    f"hwnd={hwnd!r}）"
                ),
                "hints": report["hints"],
                "candidates": report["candidates"],
                "hidden_apps": report["hidden_apps"],
                "total_windows": report["total_windows"],
                "timing": {"total_ms": round((time.perf_counter() - started) * 1000.0, 3)},
            }
            if audit is not None:
                audit.log_action(
                    "close_window",
                    title=title or "",
                    process_name=process_name or "",
                    hwnd=int(hwnd or 0),
                    found=False,
                    closed=False,
                    method_requested=requested,
                    method_used="none",
                    detail=payload["detail"],
                )
            return payload

        handle = int(getattr(window, "hwnd", 0) or hwnd or 0)

        # ---------------------------------------------------------- C6：白名单闸（发送前的最后一道门）
        window_title = str(getattr(window, "title", "") or title or "")
        window_process = str(getattr(window, "process_name", "") or process_name or "")
        allowed = close_allowlist_check(
            app=process_name or "",
            title=window_title,
            process_name=window_process,
            target=process_name or "",
        )
        if not allowed["ok"]:
            source_label = (
                "内置默认关闭白名单"
                if allowed.get("source") == "default"
                else f"{CLOSE_ALLOWLIST_ENV} 显式配置"
            )
            payload: Dict[str, Any] = {
                "ok": False,
                "closed": False,
                "hwnd": handle,
                "window": window.to_dict(),
                "method_requested": requested,
                "method_used": "none",
                "degraded": True,
                "degraded_reason": "close_not_allowlisted",
                "allowlist": list(allowed.get("allowlist") or []),
                "allowlist_source": str(allowed.get("source") or ""),
                "allowlist_tokens": list(allowed.get("tokens") or ()),
                "detail": (
                    f"目标窗口不在{source_label}内（title={window_title!r}，"
                    f"process={window_process!r}）：未投递 WM_CLOSE，未注入任何键盘按键"
                ),
                "hint": (
                    f"如需放开：设置 {CLOSE_ALLOWLIST_ENV}（逗号分隔，应用别名 / 进程名 / 窗口标题"
                    f"关键词，如 notepad.exe,记事本）覆盖默认白名单；设为 "
                    f"{' / '.join(CLOSE_ALLOWLIST_UNRESTRICTED_TOKENS)} 表示不校验（显式解除限制）。"
                ),
                "timing": {"total_ms": round((time.perf_counter() - started) * 1000.0, 3)},
            }
            if audit is not None:
                audit.log_action(
                    "close_window",
                    title=window_title,
                    process_name=window_process,
                    hwnd=handle,
                    found=True,
                    closed=False,
                    method_requested=requested,
                    method_used="none",
                    degraded=True,
                    degraded_reason="close_not_allowlisted",
                    detail=payload["detail"],
                )
            return payload

        payload = {
            "ok": False,
            "closed": False,
            "hwnd": handle,
            "window": window.to_dict(),
            "method_requested": requested,
            "method_used": "none",
        }

        # 关闭前目标窗口核验（C4）：句柄必须仍然有效
        if not handle or not w32.is_window(handle):
            payload.update(
                {
                    "ok": True,
                    "closed": True,
                    "already_closed": True,
                    "detail": f"hwnd={handle} 在关闭前已不是有效窗口（视为已关闭）",
                }
            )
            payload["timing"] = {"total_ms": round((time.perf_counter() - started) * 1000.0, 3)}
            if audit is not None:
                audit.log_action(
                    "close_window",
                    title=title or "",
                    process_name=process_name or "",
                    hwnd=handle,
                    found=True,
                    closed=True,
                    already_closed=True,
                    method_requested=requested,
                    method_used="none",
                    detail=payload["detail"],
                )
            return payload

        # 关闭前快照（供审计与上层判断"关的是哪个窗口"）
        before = w32.window_from_hwnd(handle)
        foreground_before = w32.get_foreground_hwnd()
        payload["foreground_before"] = foreground_before
        payload["foreground_matched_before"] = bool(foreground_before == handle)
        payload["state_before"] = (
            before.to_dict().get("state") if before is not None else w32.window_state(hwnd=handle)
        )

        # 可选：关闭前把目标置前（需要键盘回退、或宿主显式要求时使用）
        foreground_step: Dict[str, Any] = {}
        if require_foreground:
            act = self.activate_window(hwnd=handle, wait_ready=True)
            fg = w32.get_foreground_hwnd()
            foreground_step = {
                "activated": bool(act.get("activated")),
                "foreground_hwnd": fg,
                "matched": bool(fg == handle),
                "state": act.get("state"),
                "degraded": bool(act.get("degraded")),
                "degraded_reason": str(act.get("degraded_reason") or ""),
            }
        payload["foreground"] = foreground_step

        # ---------------------------------------------------------- C1/C4：WM_CLOSE 直投 + 轮询
        if use_wm_close:
            closed_result = self.accessibility.close_window(
                hwnd=handle,
                timeout=float(timeout),
                poll_interval=float(poll_interval),
                dialog_scan=bool(dialog_scan),
            )
            payload["signal"] = closed_result.get("signal")
            payload["wait"] = closed_result.get("wait")
            payload["timing"] = dict(closed_result.get("timing") or {})
            payload["method_used"] = "wm_close"
            if closed_result.get("closed"):
                payload["closed"] = True
            payload["dialogs"] = list(closed_result.get("dialogs") or [])
            payload["close_detail"] = closed_result.get("detail")
            if keyboard_disabled and requested == "alt_f4":
                payload["degraded"] = True
                payload["degraded_reason"] = "close_fallback_disabled"
        else:
            payload["dialogs"] = []

        # ---------------------------------------------------------- C3/C5：回退与阻断
        if not payload["closed"]:
            if payload["dialogs"] and use_wm_close:
                payload["blocked"] = True
                payload["blocked_reason"] = "modal_dialog"
                payload["degraded"] = True
                payload["degraded_reason"] = "close_blocked_by_dialog"
                payload["hint"] = (
                    "检测到可能阻塞关闭的模态对话框：请先人工确认对话框内容（保存/放弃/取消），"
                    "或由上层决定点哪个按钮；本能力不做任何猜测性点击与重试。"
                )
            elif keyboard_disabled:
                payload["blocked"] = False
                payload["blocked_reason"] = "window_still_alive"
                payload["degraded"] = True
                payload["degraded_reason"] = "close_fallback_disabled"
                payload["hint"] = (
                    "回退通道已由 UIAGENT_CLOSE_FALLBACK=off（或 fallback='off'）关闭，未发送任何键盘"
                    "按键；WM_CLOSE 未在超时内收敛，窗口可能仍在关闭流程中，或应用忽略了关闭消息。"
                    "如需键盘回退，请先确认目标窗口前台状态，并在开关允许时改用 require_foreground=true。"
                )
            else:
                # 仅在"无模态框 + 前台核验通过"时补一次 alt+f4，避免误关其它窗口
                act = self.activate_window(hwnd=handle, wait_ready=True)
                fg = w32.get_foreground_hwnd()
                matched = bool(fg == handle)
                payload["foreground"] = dict(
                    foreground_step or {},
                    fallback_activated=bool(act.get("activated")),
                    foreground_hwnd=fg,
                    matched=matched,
                    state=act.get("state"),
                )
                if not matched:
                    payload["blocked"] = True
                    payload["blocked_reason"] = "foreground_not_matched"
                    payload["degraded"] = True
                    payload["degraded_reason"] = "close_not_confirmed"
                    payload["hint"] = (
                        "关闭未收敛且目标窗口无法置为前台（前台锁定），已放弃 alt+f4 回退以免误关"
                        "其它窗口；建议稍后重试，或先用 ui_activate_window 确认前台后再关闭。"
                    )
                else:
                    self.hotkey("alt", "f4")
                    payload["signal"] = {
                        "channel": "keyboard_alt_f4",
                        "delivered": True,
                        "keys": ["alt", "f4"],
                        "front_verified": True,
                        "foreground_hwnd": fg,
                    }
                    wait = w32.wait_window_closed(
                        handle,
                        timeout=float(timeout),
                        interval=float(poll_interval),
                    )
                    payload["wait"] = wait
                    payload["method_used"] = (
                        "alt_f4" if requested == "alt_f4" else "wm_close+alt_f4"
                    )
                    if wait.get("closed"):
                        payload["closed"] = True
                        payload.pop("blocked", None)
                    else:
                        rescan: List[Dict[str, Any]] = []
                        if dialog_scan:
                            try:
                                rescan = w32.modal_window_candidates(handle)
                            except Exception:  # pragma: no cover
                                rescan = []
                        if rescan:
                            payload["dialogs"] = rescan
                            payload["blocked"] = True
                            payload["blocked_reason"] = "modal_dialog"
                            payload["degraded"] = True
                            payload["degraded_reason"] = "close_blocked_by_dialog"
                        else:
                            payload["blocked"] = False
                            payload["blocked_reason"] = "window_still_alive"
                            payload["degraded"] = True
                            payload["degraded_reason"] = "close_not_confirmed"
                            payload["hint"] = (
                                "程序化关闭与 alt+f4 回退均未在超时内使句柄消失；窗口可能仍在关闭"
                                "流程中，或应用忽略了关闭消息（未发现模态框），建议稍后重试。"
                            )

        # ---------------------------------------------------------- 收尾：状态与结论
        payload["ok"] = bool(payload["closed"])
        if payload["closed"]:
            payload["state_after"] = "not_found"
            payload["detail"] = (
                f"窗口已关闭（句柄消失，method_used={payload['method_used']}）；"
                "应用进程可能仍在后台驻留，属正常现象。"
            )
        else:
            after = w32.window_from_hwnd(handle)
            payload["state_after"] = (
                after.to_dict().get("state") if after is not None else w32.window_state(hwnd=handle)
            )
            payload["detail"] = str(
                payload.get("close_detail")
                or f"窗口未关闭（state={payload['state_after']}，method_used={payload['method_used']}）"
            )
        timing = dict(payload.get("timing") or {})
        timing["total_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        timing.setdefault("signal_ms", 0.0)
        timing.setdefault("wait_ms", float((payload.get("wait") or {}).get("waited_ms") or 0.0))
        payload["timing"] = timing

        if audit is not None:
            wait_timing = payload.get("wait") or {}
            audit.log_action(
                "close_window",
                title=title or "",
                process_name=process_name or "",
                hwnd=handle,
                found=True,
                closed=bool(payload["closed"]),
                method_requested=requested,
                method_used=str(payload["method_used"]),
                signal_channel=str((payload.get("signal") or {}).get("channel") or ""),
                signal_delivered=bool((payload.get("signal") or {}).get("delivered")),
                waited_ms=float(wait_timing.get("waited_ms") or 0.0),
                polls=int(wait_timing.get("polls") or 0),
                blocked=bool(payload.get("blocked")),
                blocked_reason=str(payload.get("blocked_reason") or ""),
                dialogs_count=len(payload.get("dialogs") or []),
                degraded=bool(payload.get("degraded")),
                degraded_reason=str(payload.get("degraded_reason") or ""),
                state_before=str(payload.get("state_before") or ""),
                state_after=str(payload.get("state_after") or ""),
                foreground_matched=bool(
                    (payload.get("foreground") or {}).get("matched")
                    if payload.get("foreground")
                    else payload.get("foreground_matched_before")
                ),
                elapsed_ms=payload["timing"]["total_ms"],
                detail=str(payload.get("detail") or ""),
            )
        return payload

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
        scope: Optional[str] = None,
        hwnd: Optional[int] = None,
        app: Any = None,
        reuse_ttl: Optional[float] = None,
        state: str = "",
        ensure_ms: Optional[float] = None,
        launch_ms: Optional[float] = None,
    ) -> LocateResult:
        """定位目标（只查询坐标，不执行点击）。

        :param target: 元素名称 / 界面文字
        :param use_ocr_fallback: UIA 未命中时是否启用 OCR 兜底
        :param window: 限定搜索窗口（UIElement 或原生控件）
        :param role: 限定元素角色（如 "Button"）
        :param scope: 搜索域。缺省 ``None`` 时取 ``UIAGENT_SCOPE_DEFAULT``（默认 ``auto``）：

            - ``auto``：有 ``hwnd`` / ``window`` 线索或 ``AppContext`` 缓存命中 → 窗口限定；
              都没有 → 桌面级（**与改造前行为一致**）；
            - ``window``：强制窗口限定；``desktop``：强制桌面级。
        :param hwnd: 显式目标窗口句柄（优先级最高）
        :param app: 应用线索（别名 / 进程名 / 标题），用于命中 ``AppContext`` 缓存
        :param reuse_ttl: 本次调用的缓存 TTL 覆盖（秒）；``0`` 表示不读写缓存
        :param state: 上游已判定的应用状态（仅审计回填）
        """
        scope_mode, scope_hwnd, cached, scope_degraded, from_auto = self._resolve_locate_scope(
            scope, app=app, hwnd=hwnd, window=window, reuse_ttl=reuse_ttl
        )
        if scope_mode == "window" and scope_hwnd:
            window = self.window_from_hwnd(scope_hwnd) or window
            if region is None and window is not None:
                region = tuple(window.bounds)
        elif scope_mode == "desktop":
            window = None

        window_title = _window_title_of(window)

        def _finish(
            result: LocateResult,
            *,
            uia_ms: float = 0.0,
            ocr_ms: float = 0.0,
            candidates_count: int = 0,
            done_scope: Optional[str] = None,
            done_hwnd: Optional[int] = None,
            degraded_reason: str = "",
        ) -> LocateResult:
            """回填 P0 字段（scope / 耗时 / 降级）并统一落 ``locate`` 审计。"""
            final_scope = done_scope or scope_mode
            final_hwnd = int(scope_hwnd if done_hwnd is None else done_hwnd)
            result.scope = final_scope
            result.scope_hwnd = final_hwnd
            result.cached = bool(cached)
            result.locate_ms = round(float(uia_ms) + float(ocr_ms), 3)
            result.state = str(state or "")
            final_reason = degraded_reason or scope_degraded
            if final_reason:
                result.degraded = True
                result.degraded_reason = final_reason
            _emit_locate(
                result,
                role=role,
                window_title=window_title if final_scope == "window" else "",
                uia_ms=uia_ms,
                ocr_ms=ocr_ms,
                candidates_count=candidates_count,
                scope=final_scope,
                scope_hwnd=final_hwnd,
                cached=cached,
                state=str(state or ""),
                ensure_ms=ensure_ms,
                launch_ms=launch_ms,
                degraded_reason=final_reason,
            )
            if final_scope == "window" and app and final_hwnd:
                # 命中即刷新缓存（TTL 滚动），供同一应用后续定位复用
                self._ctx.set(
                    app,
                    final_hwnd,
                    bounds=getattr(window, "bounds", None),
                    title=window_title,
                    state="ready",
                )
            return result

        # 第 1 优先级：UIA
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
            return _finish(result, uia_ms=uia_ms, candidates_count=1)

        # 第 2 优先级：OCR（窗口限定时收敛到窗口 bounds 内，避免整屏 OCR）
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
                return _finish(
                    result, uia_ms=uia_ms, ocr_ms=ocr_ms, candidates_count=candidates
                )

        # RF1 兜底：窗口限定未命中、且 scope 由 auto 解析而来 → 允许一次桌面级升级尝试
        if scope_mode == "window" and from_auto and not scope_degraded:
            started = time.perf_counter()
            upgrade = self.accessibility.find_element(name=target, role=role, window=None, limit=1)
            upgrade_ms = (time.perf_counter() - started) * 1000.0
            if upgrade is not None and upgrade.is_clickable:
                x, y = upgrade.center
                result = LocateResult(
                    target=target,
                    found=True,
                    source="uia",
                    x=x,
                    y=y,
                    confidence=1.0,
                    element=upgrade,
                    detail=f"窗口内未命中，桌面级升级命中 role={upgrade.role} name={upgrade.name!r}",
                )
                return _finish(
                    result,
                    uia_ms=uia_ms + upgrade_ms,
                    ocr_ms=ocr_ms,
                    candidates_count=1,
                    done_scope="desktop",
                    done_hwnd=0,
                    degraded_reason="scope_upgrade_desktop",
                )

        result = LocateResult(target=target, found=False, detail="UIA 与 OCR 均未命中")
        return _finish(result, uia_ms=uia_ms, ocr_ms=ocr_ms, candidates_count=candidates)

    def locate_many(
        self,
        target: str,
        role: Optional[str] = None,
        window: Any = None,
        limit: int = 20,
        scope: Optional[str] = None,
        hwnd: Optional[int] = None,
        app: Any = None,
        reuse_ttl: Optional[float] = None,
    ) -> List[LocateResult]:
        """定位全部匹配目标（仅 UIA + OCR 结果合并，按来源分组）。

        P0（改动点 #1）：与 :meth:`locate` 共用同一套 ``scope`` 解析规则；
        ``scope=auto`` 且无窗口线索时保持桌面级（旧行为不变）。
        """
        scope_mode, scope_hwnd, cached, scope_degraded, _from_auto = self._resolve_locate_scope(
            scope, app=app, hwnd=hwnd, window=window, reuse_ttl=reuse_ttl
        )
        if scope_mode == "window" and scope_hwnd:
            window = self.window_from_hwnd(scope_hwnd) or window
        elif scope_mode == "desktop":
            window = None
        # 窗口限定时 OCR 只扫窗口 bounds，避免整屏 OCR（R2/R3）
        ocr_region = tuple(window.bounds) if (scope_mode == "window" and window is not None) else None

        window_title = _window_title_of(window)

        def _stamp(item: LocateResult, *, uia_ms: float, ocr_ms: float, total: int) -> None:
            item.scope = scope_mode
            item.scope_hwnd = int(scope_hwnd)
            item.cached = bool(cached)
            item.locate_ms = round(float(uia_ms) + float(ocr_ms), 3)
            if scope_degraded:
                item.degraded = True
                item.degraded_reason = scope_degraded
            _emit_locate(
                item,
                role=role,
                window_title=window_title if scope_mode == "window" else "",
                uia_ms=uia_ms,
                ocr_ms=ocr_ms,
                candidates_count=total,
                multi=True,
                scope=scope_mode,
                scope_hwnd=int(scope_hwnd),
                cached=cached,
                degraded_reason=scope_degraded,
            )

        results: List[LocateResult] = []
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
            for block in self.find_text_on_screen(target, region=ocr_region):
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
            _stamp(result, uia_ms=uia_ms, ocr_ms=ocr_ms, total=total)
        if not results:
            _stamp(
                LocateResult(target=target, found=False, detail="UIA 与 OCR 均未命中"),
                uia_ms=uia_ms,
                ocr_ms=ocr_ms,
                total=0,
            )
        return results

    # ------------------------------------------------------------ P0：scope / 缓存 / 唤起
    def _resolve_locate_scope(
        self,
        scope: Optional[str] = None,
        *,
        app: Any = None,
        hwnd: Optional[int] = None,
        window: Any = None,
        reuse_ttl: Optional[float] = None,
    ) -> Tuple[str, int, bool, str, bool]:
        """解析定位搜索域 → ``(mode, hwnd, cached, degraded_reason, from_auto)``。

        ``mode`` ∈ ``{"window", "desktop"}``。``from_auto`` 表示 mode 由 ``auto`` 推导而来，
        用于 RF1 的一次受限桌面级升级；显式声明的 ``scope`` 永不自动升级。
        ``scope=auto`` 且无任何窗口线索时返回 ``desktop``——**保持改造前的旧行为**。
        """
        requested = str(scope or "").strip().lower()
        from_auto = False
        if requested not in _SCOPE_MODES:
            requested = _scope_default()
            from_auto = True
        hinted_hwnd = int(hwnd or 0) or int(getattr(window, "hwnd", 0) or 0)
        if requested == "window":
            if hinted_hwnd:
                return "window", hinted_hwnd, False, "", from_auto
            # 显式要求窗口限定却拿不到窗口句柄 → 显式降级，禁止静默换成桌面级
            return "desktop", 0, False, "scope_window_unresolved", from_auto
        if requested == "desktop":
            return "desktop", 0, False, "", from_auto
        # auto：hwnd > window > AppContext 缓存 > 桌面级（旧行为）
        if hinted_hwnd:
            return "window", hinted_hwnd, False, "", from_auto
        entry = self._ctx_lookup(app, reuse_ttl)
        if entry is not None:
            return "window", int(entry.hwnd), True, "", from_auto
        return "desktop", 0, False, "", from_auto

    def _ctx_lookup(self, app: Any, reuse_ttl: Optional[float] = None) -> Optional[AppContextEntry]:
        """按应用取缓存窗口；命中前校验 hwnd 仍有效，失效即清除（避免点到已关闭窗口）。"""
        if not app:
            return None
        if reuse_ttl is not None and float(reuse_ttl) <= 0:
            return None
        entry = self._ctx.get(app)
        if entry is None:
            return None
        if reuse_ttl is not None and float(reuse_ttl) > 0 and entry.expired(float(reuse_ttl)):
            return None
        if self.window_from_hwnd(int(entry.hwnd)) is None:
            self._ctx.invalidate(app)
            return None
        return entry

    def app_context_stats(self) -> Dict[str, Any]:
        """``AppContext`` 缓存统计（只读，供自检与诊断）。"""
        return self._ctx.stats()

    def ensure_window(
        self,
        app: Any = None,
        hwnd: Optional[int] = None,
        title: Optional[str] = None,
        process_name: Optional[str] = None,
        restore: bool = True,
        activate: bool = True,
        reuse_ttl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """P0-lite 唤起：一次调用内完成"解析目标窗口 → 唤醒/置前 → 回填缓存"。

        P0 阶段只覆盖 S1（可见）/ S2（任务栏最小化）/ S3（托盘隐藏）；S4（未启动）返回
        ``state="not_running"`` + ``degraded_reason="app_not_running"``，**不尝试启动**
        （启动能力属 P2，见方案改动点 #13~#16）。全部复用既有 ``find_window`` +
        ``activate_window``，不新增 Win32 原语。
        """
        started = time.perf_counter()
        audit = get_audit_logger()
        app_name = str(app or "").strip()
        cached = False
        window = None
        if hwnd:
            window = self.window_from_hwnd(int(hwnd))
        if window is None and app_name:
            entry = self._ctx_lookup(app_name, reuse_ttl)
            if entry is not None:
                window = self.window_from_hwnd(int(entry.hwnd))
                cached = window is not None
        if window is None and (title or process_name):
            window = self.find_window(title=title, process_name=process_name)
        if window is None and app_name:
            window = self.find_window(title=app_name) or self.find_window(process_name=app_name)
        resolve_ms = (time.perf_counter() - started) * 1000.0

        if window is None:
            total_ms = (time.perf_counter() - started) * 1000.0
            payload: Dict[str, Any] = {
                "found": False,
                "activated": False,
                "hwnd": 0,
                "activated_hwnd": 0,
                "app": app_name,
                "state": "not_running",
                "cached": False,
                "degraded": True,
                "degraded_reason": "app_not_running",
                "ensure_ms": round(total_ms, 3),
                "timing": {
                    "snapshot_ms": round(resolve_ms, 3),
                    "activate_ms": 0.0,
                    "total_ms": round(total_ms, 3),
                    "launch_ms": None,
                },
                "detail": (
                    f"未找到应用窗口（app={app_name!r} title={title!r} "
                    f"process_name={process_name!r}）；P0 不提供启动能力"
                ),
            }
            if audit is not None:
                audit.log_ensure(
                    app=app_name,
                    state="not_running",
                    hwnd=0,
                    activated=False,
                    ensure_ms=round(total_ms, 3),
                    degraded=True,
                    degraded_reason="app_not_running",
                    detail=payload["detail"],
                )
            return payload

        target_hwnd = int(getattr(window, "hwnd", 0) or 0)
        visible = bool(getattr(window, "visible", True))
        iconic = bool(getattr(window, "iconic", False))
        cloaked = bool(getattr(window, "cloaked", False))
        if cloaked:
            state = "cloaked"       # UWP 挂起：不计入 S1~S3
        elif not visible:
            state = "hidden"        # S3 托盘驻留 / 隐藏
        elif iconic:
            state = "minimized"     # S2 任务栏最小化
        else:
            state = "visible"       # S1 可见窗口

        activate_ms = 0.0
        result: Dict[str, Any] = {"activated": False, "hwnd": target_hwnd, "detail": "未请求置前"}
        if activate:
            act_started = time.perf_counter()
            result = self.accessibility.activate_window(window, restore=restore, show_hidden=True) or {}
            activate_ms = (time.perf_counter() - act_started) * 1000.0
        activated = bool(result.get("activated"))
        activated_hwnd = int(result.get("hwnd") or target_hwnd)
        total_ms = (time.perf_counter() - started) * 1000.0
        degraded = not activated
        degraded_reason = ""
        if not activated:
            degraded_reason = "activate_failed"
        elif cloaked:
            degraded_reason = "app_window_cloaked"
        if target_hwnd and state in ("visible", "minimized", "hidden"):
            self._ctx.set(
                app_name
                or getattr(window, "app_alias", "")
                or str(getattr(window, "process_name", "") or ""),
                target_hwnd,
                bounds=getattr(window, "bounds", None),
                title=str(getattr(window, "name", "") or ""),
                process_name=str(getattr(window, "process_name", "") or ""),
                state="ready",
            )
        payload = {
            "found": True,
            "activated": activated,
            "hwnd": target_hwnd,
            "activated_hwnd": activated_hwnd,
            "app": app_name,
            "state": state,
            "cached": cached,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "ensure_ms": round(total_ms, 3),
            "timing": {
                "snapshot_ms": round(resolve_ms, 3),
                "activate_ms": round(activate_ms, 3),
                "total_ms": round(total_ms, 3),
                "launch_ms": None,
            },
            "hint": str(result.get("hint") or ""),
            "covered": result.get("covered"),
            "detail": str(result.get("detail") or ""),
        }
        if audit is not None:
            audit.log_ensure(
                app=app_name,
                state=state,
                hwnd=target_hwnd,
                activated=activated,
                activated_hwnd=activated_hwnd,
                cached=cached,
                ensure_ms=round(total_ms, 3),
                timing=payload["timing"],
                degraded=degraded,
                degraded_reason=degraded_reason,
                detail=payload["detail"],
            )
        return payload

    # ============================================================ P1：统一入口 + 状态分层
    @staticmethod
    def _app_pool(app_name: Any) -> List[Any]:
        """按多档线索键聚合应用窗口候选（P2 兜底：别名表别名，覆盖打包应用宿主进程窗口）。

        依次用「原形 → 去后缀 → 别名」尝试 ``list_app_windows``，首个命中即返回
        （首个命中已由 ``select_best_window`` 取最优，继续扩大键不会更准，反而更慢）。
        """
        from .accessibility import win32_windows as w32
        from .launcher import app_match_keys

        text = str(app_name or "").strip()
        if not text:
            return []
        for key in app_match_keys(text):
            pool = w32.list_app_windows(key)
            if pool:
                return pool
        return []

    def ensure_app(
        self,
        app: Any = None,
        hwnd: Optional[int] = None,
        title: Optional[str] = None,
        process_name: Optional[str] = None,
        launch_if_missing: bool = False,
        require_foreground: bool = True,
        wait_ready: bool = True,
        timeout: Optional[float] = None,
        restore: bool = True,
        launch_method: str = "auto",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """统一入口（P1 改动点 #6 + P2 改动点 #13）：一次调用完成「状态判定 → 启动/唤醒/置前 →
        就绪确认 → 回填缓存」。

        四状态分层（方案 2.4）：

        - **S1** ``visible``：已可见窗口直接置前，零等待（不再固定 ``sleep(0.25)``）；
        - **S2** ``minimized``：``SW_RESTORE`` → bounds 稳定等待 → 置前；
        - **S3** ``hidden``：``SW_SHOW`` → bounds 稳定等待 → 置前；
        - **S4** 未运行：``launch_if_missing=True`` 时走 :mod:`uiagent.launcher`（方案 2.4 第 3 步
          dispatch）——解析入口 → 启动 → 按 PID 等待窗口就绪 → 回到 S1~S3 置前流程；
          ``launch_if_missing=False`` 时保持 P1 行为（只上报 ``not_running`` +
          ``degraded_reason="app_not_running"``，返回 ``candidates``）。
          ``dry_run=True`` 仅解析启动入口、**不产生进程**。

        ``UIAGENT_ENSURE_V2=0`` 时退化为 ``find_window`` + ``activate_window`` 旧链路
        （``state="unknown"``，``timing`` 仅保留 ``total_ms``）。

        未稳定 / 置前被拦截 / 超时均为**降级返回**，不抛异常。

        :return: ``{"ok", "found", "app", "state", "hwnd", "window", "activated", "cached",
            "timing"{snapshot_ms, show_ms, settle_ms, foreground_ms, launch_ms, total_ms},
            "reachable", "covered_by", "degraded", "degraded_reason", "candidates", "detail"}``
        """
        from .accessibility import win32_windows as w32

        started = time.perf_counter()
        audit = get_audit_logger()
        app_name = str(app or "").strip()
        budget = float(timeout) if timeout else 0.0

        # ---- 解析目标窗口：hwnd 直给 → AppContext 缓存 → 应用候选聚合 → 标题/进程名
        cached = False
        pool: List[Any] = []
        resolved_hwnd = 0
        if hwnd:
            resolved_hwnd = int(hwnd)
        if not resolved_hwnd and app_name:
            entry = self._ctx_lookup(app_name)
            if entry is not None and w32.is_window(int(entry.hwnd)):
                resolved_hwnd = int(entry.hwnd)
                cached = True
        if not resolved_hwnd and app_name:
            pool = self._app_pool(app_name)
            best = w32.select_best_window(pool) if pool else None
            if best is not None:
                resolved_hwnd = int(best.hwnd)
        if not resolved_hwnd and (title or process_name or app_name):
            found = (
                self.find_window(title=title, process_name=process_name)
                if (title or process_name)
                else None
            ) or self.find_window(title=title or app_name or None) or self.find_window(
                process_name=process_name or app_name or None
            )
            if found is not None:
                resolved_hwnd = int(getattr(found, "hwnd", 0) or 0)
        snapshot_ms = (time.perf_counter() - started) * 1000.0
        if not resolved_hwnd and not pool:
            pool = self._app_pool(app_name) if app_name else []

        # ---- S4 未运行 → dispatch 到 launcher（P2 改动点 #13/#15；需显式授权 launch_if_missing）
        launch_info: Optional[Dict[str, Any]] = None
        launch_ms: Optional[float] = None
        if not resolved_hwnd and app_name and (launch_if_missing or dry_run):
            launch_info = self.launch_app(
                app_name,
                method=launch_method,
                dry_run=dry_run,
                timeout=budget or None,
                wait=True,
            )
            launch_ms = float(launch_info.get("launch_ms") or 0.0)
            if launch_info.get("hwnd"):
                resolved_hwnd = int(launch_info["hwnd"])
                pool = self._app_pool(app_name) or pool

        if not _ensure_v2_enabled():
            return self._ensure_app_legacy(
                app_name=app_name,
                resolved_hwnd=resolved_hwnd,
                title=title,
                process_name=process_name,
                restore=restore,
                snapshot_ms=snapshot_ms,
                started=started,
                audit=audit,
            )

        if not resolved_hwnd:
            # ---- S4 未运行：如实上报 + 返回候选（P1 不启动）
            report = window_miss_report(
                title=title or app_name or None, process_name=process_name, hwnd=hwnd
            )
            candidates = list(report.get("candidates") or [])
            for win in pool[:8]:
                item = win.to_dict() if hasattr(win, "to_dict") else {"hwnd": int(win.hwnd)}
                item["state"] = w32.window_state(win)
                if all(int(c.get("hwnd") or 0) != int(item.get("hwnd") or 0) for c in candidates):
                    candidates.append(item)
            total_ms = (time.perf_counter() - started) * 1000.0
            if launch_info is not None:
                # P2：启动已尝试（或 dry_run 预演），以 launcher 的结论为准
                degraded_reason = str(launch_info.get("degraded_reason") or "")
                if launch_info.get("state") == "launching" and not degraded_reason:
                    degraded_reason = "launch_timeout"
                ok_flag = bool(launch_info.get("ok"))
            else:
                degraded_reason = "launch_unavailable" if launch_if_missing else "app_not_running"
                ok_flag = False
            payload: Dict[str, Any] = {
                "ok": ok_flag,
                "found": False,
                "activated": False,
                "app": app_name,
                "state": "not_running",
                "hwnd": 0,
                "window": None,
                "cached": False,
                "reachable": False,
                "covered_by": None,
                "degraded": bool(degraded_reason),
                "degraded_reason": degraded_reason,
                "dry_run": bool(dry_run),
                "launch": launch_info,
                "timing": {
                    "snapshot_ms": round(snapshot_ms, 3),
                    "show_ms": 0.0,
                    "settle_ms": 0.0,
                    "foreground_ms": 0.0,
                    "launch_ms": round(launch_ms, 3) if launch_ms is not None else None,
                    "total_ms": round(total_ms, 3),
                },
                "candidates": candidates,
                "hints": report.get("hints") or [],
                "detail": (
                    f"未找到应用窗口（app={app_name!r} title={title!r} process_name={process_name!r}）；"
                    + (
                        "已按 launcher 尝试启动，但该窗口仍未就绪，详见 launch 字段"
                        if launch_info is not None
                        else "未请求启动（launch_if_missing=False），可从上文 candidates 指定 hwnd"
                    )
                ),
            }
            if audit is not None:
                audit.log_ensure(
                    app=app_name,
                    state="not_running",
                    hwnd=0,
                    activated=False,
                    cached=False,
                    ensure_ms=round(total_ms, 3),
                    timing=payload["timing"],
                    degraded=True,
                    degraded_reason=degraded_reason,
                    candidates_count=len(candidates),
                    detail=payload["detail"],
                )
            return payload

        # ---- 状态判定（S1~S3；cloaked 单独标注，不计入 S1~S3）
        state_before = w32.window_state(hwnd=resolved_hwnd)
        state = "not_running" if state_before == "not_found" else state_before

        remaining = (budget - (time.perf_counter() - started)) if budget > 0 else -1.0
        # S1（已可见）零等待：仅「还原 / 显示」的状态迁移（S2/S3）才等待 bounds 稳定
        transition = state_before != "visible"
        wait = bool(wait_ready) and transition and (True if budget <= 0 else remaining > 0.05)
        timeout_hit = bool(budget > 0 and remaining <= 0.05)

        act: Dict[str, Any] = {"activated": False, "hwnd": resolved_hwnd, "detail": "未请求置前"}
        if state in ("visible", "minimized", "hidden", "cloaked"):
            act = (
                self.accessibility.activate_window(
                    hwnd=resolved_hwnd,
                    restore=restore,
                    show_hidden=True,
                    wait_ready=wait,
                )
                or {}
            )
        activated = bool(act.get("activated"))
        activated_hwnd = int(act.get("hwnd") or resolved_hwnd)
        state_after = w32.window_state(hwnd=activated_hwnd or resolved_hwnd)
        if state_after != "not_found":
            state = state_after

        element = self.window_from_hwnd(activated_hwnd or resolved_hwnd)
        window_dict: Optional[Dict[str, Any]] = None
        if element is not None:
            window_dict = element.to_dict()
        elif resolved_hwnd:
            raw = w32.window_from_hwnd(resolved_hwnd)
            if raw is not None:
                window_dict = raw.to_dict()
        if not window_dict:
            window_dict = {"hwnd": int(activated_hwnd or resolved_hwnd), "state": state}

        # ---- 多实例：返回 candidates 供上层复核（方案 RF9）
        candidates: List[Dict[str, Any]] = []
        if not pool and app_name:
            pool = self._app_pool(app_name)
        for win in pool[:8]:
            item = win.to_dict() if hasattr(win, "to_dict") else {"hwnd": int(win.hwnd)}
            item["state"] = w32.window_state(win)
            item["selected"] = int(item.get("hwnd") or 0) == int(resolved_hwnd)
            candidates.append(item)

        covered = act.get("covered")
        reachable = True if covered is None else not bool(covered)
        act_timing = act.get("timing") or {}
        total_ms = (time.perf_counter() - started) * 1000.0
        timing = {
            "snapshot_ms": round(snapshot_ms, 3),
            "show_ms": round(float(act_timing.get("show_ms") or 0.0), 3),
            "settle_ms": round(float(act_timing.get("settle_ms") or 0.0), 3),
            "foreground_ms": round(float(act_timing.get("foreground_ms") or 0.0), 3),
            "launch_ms": round(launch_ms, 3) if launch_ms is not None else None,
            "total_ms": round(total_ms, 3),
        }

        degraded = False
        degraded_reason = ""
        if state == "cloaked":
            degraded, degraded_reason = True, "app_window_cloaked"
        if not activated and require_foreground:
            degraded, degraded_reason = True, "foreground_locked"
        elif not activated and not degraded:
            degraded, degraded_reason = True, "activate_failed"
        if act.get("degraded") and not degraded:
            degraded, degraded_reason = True, str(act.get("degraded_reason") or "window_unstable")
        if timeout_hit and not activated:
            degraded, degraded_reason = True, "timeout"

        # ---- 回填缓存（仅 S1~S3 且 hwnd 有效）
        if state in ("visible", "minimized", "hidden") and (activated_hwnd or resolved_hwnd):
            self._ctx.set(
                app_name
                or str(window_dict.get("app_alias") or "")
                or str(window_dict.get("process_name") or ""),
                int(activated_hwnd or resolved_hwnd),
                bounds=window_dict.get("bounds"),
                title=str(window_dict.get("title") or window_dict.get("name") or ""),
                process_name=str(window_dict.get("process_name") or ""),
                state="ready",
            )

        payload = {
            "ok": True,
            "found": True,
            "app": app_name,
            "state": state,
            "state_before": state_before,
            "ready": bool(state in ("visible", "minimized") and (activated or not require_foreground)),
            "hwnd": int(activated_hwnd or resolved_hwnd),
            "window": window_dict,
            "activated": activated,
            "cached": cached,
            "launch": launch_info,
            "timing": timing,
            "reachable": reachable,
            "covered_by": act.get("covered_by"),
            "hint": str(act.get("hint") or ""),
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "candidates": candidates,
            "multi_instance": len(pool) > 1,
            "detail": str(act.get("detail") or ""),
        }
        if audit is not None:
            audit.log_ensure(
                app=app_name,
                state=state,
                hwnd=int(activated_hwnd or resolved_hwnd),
                activated=activated,
                activated_hwnd=int(activated_hwnd or resolved_hwnd),
                cached=cached,
                ensure_ms=round(total_ms, 3),
                timing=timing,
                reachable=reachable,
                covered=bool(covered),
                candidates_count=len(candidates),
                degraded=degraded,
                degraded_reason=degraded_reason,
                detail=payload["detail"],
            )
        return payload

    def _ensure_app_legacy(
        self,
        app_name: str,
        resolved_hwnd: int,
        title: Optional[str],
        process_name: Optional[str],
        restore: bool,
        snapshot_ms: float,
        started: float,
        audit: Any,
    ) -> Dict[str, Any]:
        """``UIAGENT_ENSURE_V2=0`` 的回退链路：``find_window`` + ``activate_window``（P0 语义）。"""
        if not resolved_hwnd:
            total_ms = (time.perf_counter() - started) * 1000.0
            payload = {
                "ok": False,
                "found": False,
                "activated": False,
                "app": app_name,
                "state": "not_running",
                "hwnd": 0,
                "cached": False,
                "degraded": True,
                "degraded_reason": "app_not_running",
                "fallback": "legacy",
                "timing": {"snapshot_ms": round(snapshot_ms, 3), "total_ms": round(total_ms, 3)},
                "detail": "未找到应用窗口（回退链路 UIAGENT_ENSURE_V2=0）",
            }
            if audit is not None:
                audit.log_ensure(
                    app=app_name,
                    state="not_running",
                    hwnd=0,
                    activated=False,
                    ensure_ms=round(total_ms, 3),
                    degraded=True,
                    degraded_reason="app_not_running",
                    detail=payload["detail"],
                )
            return payload
        result = (
            self.activate_window(
                title=title, process_name=process_name, hwnd=resolved_hwnd, restore=restore
            )
            or {}
        )
        total_ms = (time.perf_counter() - started) * 1000.0
        result.update(
            {
                "ok": bool(result.get("activated")),
                "app": app_name,
                "state": "unknown",
                "cached": False,
                "degraded": not bool(result.get("activated")),
                "degraded_reason": "" if result.get("activated") else "activate_failed",
                "fallback": "legacy",
                "timing": {"snapshot_ms": round(snapshot_ms, 3), "total_ms": round(total_ms, 3)},
            }
        )
        return result

    # ============================================================ P2：未启动路径 + 就绪等待
    def launch_app(
        self,
        app: str,
        args: str = "",
        working_dir: str = "",
        method: str = "auto",
        dry_run: bool = False,
        timeout: Optional[float] = None,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """解析启动入口 → 启动进程 → 按 PID 等待窗口就绪（P2 改动点 #13，**写操作**）。

        薄封装 :class:`uiagent.launcher.AppLauncher`：解析链与白名单校验都在 launcher 内，
        controller 只负责转发，避免两处规则漂移。``dry_run=True`` 时不产生进程。
        """
        from .launcher import get_launcher

        return get_launcher().launch(
            app,
            args=args,
            working_dir=working_dir,
            method=method,
            dry_run=dry_run,
            timeout=timeout,
            wait=wait,
        )

    def resolve_app(self, app: str) -> Dict[str, Any]:
        """只解析应用启动入口（别名 / 注册表 / 开始菜单 / UWP），**只读**，不启动任何进程。"""
        from .launcher import get_launcher

        return get_launcher().resolve(app)

    def wait_app_window(
        self,
        pid: int = 0,
        hwnd: int = 0,
        app: str = "",
        timeout: float = 10.0,
        stable_ms: float = 150.0,
    ) -> Dict[str, Any]:
        """等待目标窗口出现并完成布局（P2 改动点 #13，**只读**）；超时返回 ``state="launching"``。"""
        from .launcher import app_match_keys, get_launcher

        return get_launcher().wait_window(
            pid=pid,
            hwnd=hwnd,
            app=app_match_keys(app) if (app and not pid and not hwnd) else app,
            timeout=timeout,
            stable_ms=stable_ms,
        )

    def app_status(
        self,
        apps: Optional[Sequence[Any]] = None,
        include_not_running: bool = True,
    ) -> Dict[str, Any]:
        """批量查询应用状态（改动点 #6，**只读**）：返回每个应用的 S1~S4 状态与候选窗口。

        ``apps`` 留空时实时枚举全部「像应用窗口」的顶层窗口，按应用别名分组各取一个最优窗口。
        """
        from .accessibility import win32_windows as w32

        started = time.perf_counter()
        audit = get_audit_logger()
        names = [str(item).strip() for item in (apps or []) if str(item).strip()]
        items: List[Dict[str, Any]] = []
        if names:
            for name in names:
                entry = self._ctx_lookup(name)
                pool = self._app_pool(name)
                item = self._app_status_item(name, pool, cached=entry is not None)
                if entry is not None:
                    item["cached_hwnd"] = int(entry.hwnd)
                    item["cached_age_ms"] = round(entry.age() * 1000.0, 1)
                items.append(item)
        else:
            buckets: Dict[str, List[Any]] = {}
            for win in w32.app_windows():
                key = str(win.app_alias or win.process_name or win.title or win.hwnd)
                buckets.setdefault(key, []).append(win)
            for key in sorted(buckets):
                if key.strip().lower() in ("", "none"):
                    continue
                items.append(self._app_status_item(key, buckets[key], cached=False))
        if not include_not_running:
            items = [item for item in items if item.get("state") != "not_running"]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        payload = {
            "ok": True,
            "count": len(items),
            "apps": items,
            "elapsed_ms": round(elapsed_ms, 3),
            "context": self._ctx.stats(),
        }
        if audit is not None:
            audit.log_action(
                "app_status",
                count=len(items),
                apps=[str(item.get("app") or "") for item in items],
                elapsed_ms=round(elapsed_ms, 3),
            )
        return payload

    @staticmethod
    def _app_status_item(app: str, pool: Sequence[Any], cached: bool = False) -> Dict[str, Any]:
        """单个应用的状态条目：最优候选窗口 + S1~S4 状态 + 多实例候选列表。"""
        from .accessibility import win32_windows as w32

        best = w32.select_best_window(pool) if pool else None
        if best is None:
            return {
                "app": app,
                "state": "not_running",
                "hwnd": 0,
                "window": None,
                "foreground": False,
                "cached": cached,
                "multi_instance": False,
                "candidates": [],
            }
        foreground_hwnd = int(w32.get_foreground_hwnd() or 0)
        return {
            "app": app,
            "state": w32.window_state(best),
            "hwnd": int(best.hwnd),
            "window": best.to_dict(),
            "foreground": int(best.hwnd) == foreground_hwnd,
            "cached": cached,
            "multi_instance": len(pool) > 1,
            "candidates": [
                {
                    "hwnd": int(win.hwnd),
                    "title": str(win.title or ""),
                    "process_name": str(win.process_name or ""),
                    "state": w32.window_state(win),
                    "selected": int(win.hwnd) == int(best.hwnd),
                }
                for win in list(pool)[:8]
            ],
        }

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
        window: Any = None,
        region: Optional[Rect] = None,
        scope: Optional[str] = None,
        hwnd: Optional[int] = None,
        app: Any = None,
        reuse_ttl: Optional[float] = None,
        state: str = "",
        ensure_ms: Optional[float] = None,
        launch_ms: Optional[float] = None,
    ) -> LocateResult:
        """点击目标：既可传 ``target``（走 UIA→OCR 定位），也可传坐标 ``x``/``y``。

        P0（改动点 #4）：新增 ``window`` / ``region`` / ``scope`` / ``hwnd`` / ``app`` /
        ``reuse_ttl``，与 :meth:`locate` 共用同一套搜索域解析。上层因此可以在一次调用里
        完成"唤起（:meth:`ensure_window`）→ 窗口内定位 → 点击"，避免同一目标被独立搜索
        两次（R3/R4）。``ensure_ms`` / ``launch_ms`` / ``state`` 为上游唤起阶段的审计回填。
        """
        if target is not None:
            located = self.locate(
                target,
                use_ocr_fallback=use_ocr_fallback,
                window=window,
                region=region,
                scope=scope,
                hwnd=hwnd,
                app=app,
                reuse_ttl=reuse_ttl,
                state=state,
                ensure_ms=ensure_ms,
                launch_ms=launch_ms,
            )
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
            # ---- P0（改动点 #5）：新增列，不参与旧口径计算 ----
            payload["scope"] = str(getattr(located, "scope", "") or "")
            if int(getattr(located, "scope_hwnd", 0) or 0):
                payload["scope_hwnd"] = int(located.scope_hwnd)
            payload["cached"] = bool(getattr(located, "cached", False))
            payload["locate_ms"] = round(float(getattr(located, "locate_ms", 0.0) or 0.0), 3)
            payload["state"] = str(state or getattr(located, "state", "") or "")
            if ensure_ms is not None:
                payload["ensure_ms"] = round(float(ensure_ms), 3)
            if launch_ms is not None:
                payload["launch_ms"] = round(float(launch_ms), 3)
            if getattr(located, "degraded_reason", ""):
                payload["degraded"] = True
                payload["degraded_reason"] = located.degraded_reason
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

    # ------------------------------------------------------------ 文本输入
    def _focus_mismatch(self, target_hwnd: Optional[int]) -> bool:
        """前置焦点校验（方案 3.4）：调用方**未声明目标窗口**则不校验。

        声明 ``target_hwnd`` 且当前前台不符 → ``True``（默认仅留痕、不阻断；
        ``UIAGENT_TYPE_REQUIRE_FOCUS=1`` 时由 keyboard 层据此拒写）。
        """
        if not target_hwnd:
            return False
        try:
            current = int(self.accessibility.foreground_hwnd() or 0)
        except Exception:  # pragma: no cover - 诊断失败不阻断
            logger.debug("前台窗口读取失败", exc_info=True)
            return False
        return bool(current) and current != int(target_hwnd)

    def _readback_probe(self, element: Any = None) -> Optional[Callable[[], Optional[str]]]:
        """构造回读探针（仅 ``UIAGENT_TYPE_READBACK=1`` 时被 keyboard 层消费）。

        读取目标控件当前文本（ValuePattern，只读）；控件不可读时返回 ``None``，
        由 keyboard 层记为 ``readback={"checked": true, "ok": null, ...}``，不触发降级。
        """
        element = element if element is not None else self.get_focused_element()
        if element is None:
            return None

        def _probe() -> Optional[str]:
            return self.get_element_value(element)

        return _probe

    @staticmethod
    def _type_audit_fields(outcome: Any) -> Dict[str, Any]:
        """把 ``TypeOutcome`` 摊平为审计字段（字段字典见方案 4.3）。"""
        return {
            "ok": bool(getattr(outcome, "ok", False)),
            "chars": int(getattr(outcome, "chars", 0)),
            "method_requested": getattr(outcome, "method_requested", ""),
            "method_planned": getattr(outcome, "method_planned", ""),
            "method_used": getattr(outcome, "method_used", ""),
            "degraded": bool(getattr(outcome, "degraded", False)),
            "degrade_reason": getattr(outcome, "degrade_reason", ""),
            "degrade_chain": getattr(outcome, "degrade_chain", []),
            "clipboard": getattr(outcome, "clipboard", {}),
            "readback": getattr(outcome, "readback", {"checked": False}),
            "focus_mismatch": bool(getattr(outcome, "focus_mismatch", False)),
            "app_unknown": bool(getattr(outcome, "app_unknown", False)),
        }

    def type_text(
        self,
        text: str,
        interval: float = 0.02,
        method: str = "auto",
        app_process: Optional[str] = None,
        target_hwnd: Optional[int] = None,
    ) -> Any:
        """输入文本（``method``: auto 分层路由 / unicode / keystroke / clipboard）。

        审计口径（方案 4.3）：``method_requested`` / ``method_used`` / ``degraded`` /
        ``degrade_reason`` / ``degrade_chain`` / ``clipboard`` / ``readback`` /
        ``focus_mismatch`` 全部由 keyboard 层返回值填充，**不再写死 ``method="keystroke"``**。
        """
        from .executor.keyboard import type_readback_enabled

        audit = get_audit_logger()
        started = time.perf_counter()
        focus_mismatch = self._focus_mismatch(target_hwnd)
        readback = self._readback_probe() if type_readback_enabled() else None
        outcome = self.keyboard.type_text(
            text,
            interval=interval,
            method=method,
            app_process=app_process,
            readback=readback,
            focus_mismatch=focus_mismatch,
        )
        if audit is not None:
            audit.log_action(
                "type",
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                **self._type_audit_fields(outcome),
                **describe_text(text, audit.text_policy),
            )
        return outcome

    def type_chinese(self, text: str) -> Any:
        """通过剪贴板输入中文（等价 ``method="clipboard"``，保留既有入口）。"""
        return self.type_text(text, method="clipboard")

    def type_unicode(self, text: str, interval: float = 0.02) -> Any:
        """Unicode 直落输入（新增能力，审计 ``method_requested="unicode"``）。"""
        return self.type_text(text, interval=interval, method="unicode")

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

