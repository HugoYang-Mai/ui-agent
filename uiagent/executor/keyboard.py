"""键盘执行层（pyautogui + 剪贴板 + Unicode 直落）。

三条注入通道（详见《ui_type 输入法吞字根治方案》第 2 章）：

* ``unicode``：``SendInput`` + ``KEYEVENTF_UNICODE`` 逐字符直落，**不经键盘布局与
  输入法**，从机制上消除 IME 组字吞字（本次治理主通道）；
* ``clipboard``：剪贴板 + ``Ctrl/Cmd+V``，用于中文 / emoji / 控制字符 / 超长文本；
* ``keystroke``：``pyautogui.typewrite``（底层 ``keybd_event``）的 VK 注入，保留为
  「必须有真实按键语义」场景的强制档，**不承诺在 IME 激活态不吞字**。

回退开关（第 4 章）：``UIAGENT_CLIPBOARD_PRESERVE`` / ``UIAGENT_TYPE_FALLBACK`` /
``UIAGENT_TYPE_READBACK`` / ``UIAGENT_TYPE_KEYSTROKE_APPS`` / ``UIAGENT_TYPE_LONG_THRESHOLD``。
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..logging_utils import get_logger

logger = get_logger("keyboard")

# Windows 上 pyperclip 依赖剪贴板句柄，连续读写之间需要短暂间隔
_CLIPBOARD_SETTLE = 0.08

#: 粘贴后落定窗口（秒）：Chromium 系（Edge/Chrome）粘贴为异步投递，紧贴粘贴还原会被
#: 其抢回剪贴板（M3 矩阵 Edge 档 2/80 失败），故还原前留出更长窗口。
_CLIPBOARD_PASTE_SETTLE = 0.25

#: 剪贴板还原的有界重试（次数 / 退避基数秒）；重试后仍不一致 → ``restore_raced``
_CLIPBOARD_RESTORE_ATTEMPTS = 4
_CLIPBOARD_RESTORE_GAP = 0.12

# ---------------------------------------------------------------- 通道与常量
#: ``method`` 合法取值（决策③：保留 ``keystroke`` 强制档，新增 ``unicode``）
METHODS = ("auto", "unicode", "keystroke", "clipboard")

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
#: 单次 ``SendInput`` 的事件数上限（超出则分批，避免数组过大被系统拒绝）
_SENDINPUT_EVENTS = 256

#: P4「按键优先」内置集合（终端类对粘贴有特殊处理，按键注入反而是预期行为）
_DEFAULT_KEYSTROKE_APPS = (
    "wt.exe",
    "windowsterminal.exe",
    "cmd.exe",
    "powershell.exe",
    "conhost.exe",
)

#: P3 超长阈值默认值（建议值，待第 5 章矩阵实测校准）
_DEFAULT_LONG_THRESHOLD = 256

#: 回读校验判定失败时的留痕原因（方案 4.4 降级原因分类）
_READBACK_FAIL_REASON = {
    "unicode": "unicode_rejected",
    "clipboard": "paste_not_verified",
    "keystroke": "keystroke_not_verified",
}

#: 回读重试：控件文本刷新滞后于注入时的补偿（次数 / 间隔秒）
_READBACK_ATTEMPTS = 3
_READBACK_DELAY = 0.12


_FALSE_VALUES = ("0", "false", "no", "off")


def _env_flag(name: str, default: bool = True) -> bool:
    """读取布尔型环境变量开关（``0 / false / no / off`` 视为关闭）。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in _FALSE_VALUES


def clipboard_preserve_enabled() -> bool:
    """``UIAGENT_CLIPBOARD_PRESERVE=0`` 关闭「保存并还原用户剪贴板」（决策①，默认开）。"""
    return _env_flag("UIAGENT_CLIPBOARD_PRESERVE", True)


def type_fallback_enabled() -> bool:
    """``UIAGENT_TYPE_FALLBACK=0`` 关闭隐式降级（决策④，默认开）。"""
    return _env_flag("UIAGENT_TYPE_FALLBACK", True)


def type_readback_enabled() -> bool:
    """``UIAGENT_TYPE_READBACK=1`` 开启写入后回读校验（决策②，默认关）。"""
    return _env_flag("UIAGENT_TYPE_READBACK", False)


def type_focus_required() -> bool:
    """``UIAGENT_TYPE_REQUIRE_FOCUS=1``：焦点不符时直接不写入（方案 3.4；默认仅留痕）。"""
    return _env_flag("UIAGENT_TYPE_REQUIRE_FOCUS", False)


def type_long_threshold() -> int:
    """``UIAGENT_TYPE_LONG_THRESHOLD``：超长文本阈值（``<=0`` 表示不限长）。"""
    raw = str(os.environ.get("UIAGENT_TYPE_LONG_THRESHOLD", "") or "").strip()
    try:
        return int(float(raw)) if raw else _DEFAULT_LONG_THRESHOLD
    except (TypeError, ValueError):
        return _DEFAULT_LONG_THRESHOLD


def type_keystroke_apps() -> tuple:
    """``UIAGENT_TYPE_KEYSTROKE_APPS``：按键优先应用集合（逗号分隔进程名）。

    未设置时使用内置集合；显式设置（含空串）时以配置为准——空串即该分支不生效。
    """
    raw = os.environ.get("UIAGENT_TYPE_KEYSTROKE_APPS")
    if raw is None:
        return _DEFAULT_KEYSTROKE_APPS
    return tuple(part.strip().lower() for part in str(raw).split(",") if part.strip())


def normalize_method(method: Any) -> str:
    """规范化 ``method``；非法值回落到 ``auto``（合法性校验由上层负责）。"""
    value = str(method or "auto").strip().lower()
    return value if value in METHODS else "auto"


def utf16_units(text: str) -> List[int]:
    """把文本拆成 UTF-16 代码单元序列（非 BMP 字符 → 两个单元）。

    单独抽出便于离线验证：``KEYEVENTF_UNICODE`` 的 ``wScan`` 是 16 位字段，
    非 BMP 字符必须先展开成代理对，否则会被截断。
    """
    units: List[int] = []
    for char in text or "":
        raw = char.encode("utf-16-le")
        units.extend(int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2))
    return units


class UnicodeInjectError(RuntimeError):
    """Unicode 直落失败（被 UIPI / 钩子吞掉，或注入调用异常）。"""

    def __init__(self, reason: str, sent: int = 0, expected: int = 0) -> None:
        super().__init__(f"{reason} (sent={sent}, expected={expected})")
        self.reason = reason
        self.sent = sent
        self.expected = expected


@dataclass
class TypeOutcome:
    """一次文本输入的实际通道与留痕（字段口径见方案 4.3）。"""

    ok: bool = False
    chars: int = 0
    method_requested: str = "auto"
    method_used: str = "none"
    degraded: bool = False
    degrade_reason: str = ""
    degrade_chain: List[Dict[str, Any]] = field(default_factory=list)
    clipboard: Dict[str, Any] = field(default_factory=dict)
    readback: Dict[str, Any] = field(default_factory=lambda: {"checked": False})
    app_unknown: bool = False
    method_planned: str = ""
    focus_mismatch: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转为可直接进返回值 / 审计日志的字典。"""
        return {
            "ok": self.ok,
            "chars": self.chars,
            "method_requested": self.method_requested,
            "method_planned": self.method_planned,
            "method_used": self.method_used,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "degrade_chain": [dict(item) for item in self.degrade_chain],
            "clipboard": dict(self.clipboard),
            "readback": dict(self.readback),
            "app_unknown": self.app_unknown,
            "focus_mismatch": self.focus_mismatch,
            "error": self.error,
        }


class KeyboardExecutor:
    """键盘操作封装。"""

    def __init__(self, pause: float = 0.05, failsafe: bool = True) -> None:
        import pyautogui

        self._gui = pyautogui
        self._clipboard = None
        pyautogui.FAILSAFE = failsafe
        pyautogui.PAUSE = pause

    @property
    def clipboard(self):
        """剪贴板执行层（惰性创建，供剪贴板保存 / 还原复用）。"""
        if self._clipboard is None:
            from .clipboard import ClipboardExecutor

            self._clipboard = ClipboardExecutor()
        return self._clipboard

    # ------------------------------------------------------------ 文本
    @staticmethod
    def _is_ascii(text: str) -> bool:
        try:
            text.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    # ------------------------------------------------------------ 分层路由
    @staticmethod
    def _has_control_chars(text: str) -> bool:
        """P2 判据：含 ``\\n / \\t / \\r`` 等控制字符（Unicode 直落语义与控件不一致）。"""
        return any(ch in "\r\n\t" for ch in text or "")

    def _foreground_process(self) -> str:
        """回退读取前台窗口进程名；失败返回空串（应用识别失败不得抛异常）。"""
        try:
            from ..accessibility import win32_windows as w32

            hwnd = int(w32.get_foreground_hwnd() or 0)
            pid = int(w32.get_pid_of_hwnd(hwnd) or 0) if hwnd else 0
            if not pid:
                return ""
            raw = str(w32.query_process_name(pid) or "")
            return raw.replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()
        except Exception:  # pragma: no cover - 诊断失败不阻断输入
            logger.debug("前台进程读取失败", exc_info=True)
            return ""

    def _route(self, text: str, app_process: Optional[str] = None) -> Dict[str, Any]:
        """``auto`` 分层规则（方案 2.2）：P0~P5，命中即返回，不再继续判定。

        :return: ``{"method", "reason", "app_unknown"}``
        """
        if not text:
            return {"method": "none", "reason": "empty_text", "app_unknown": False}
        if not self._is_ascii(text):  # P1 非 ASCII → 剪贴板
            return {"method": "clipboard", "reason": "non_ascii", "app_unknown": False}
        if self._has_control_chars(text):  # P2 控制字符 → 剪贴板
            return {"method": "clipboard", "reason": "control_chars", "app_unknown": False}
        threshold = type_long_threshold()
        if threshold > 0 and len(text) > threshold:  # P3 超长 → 剪贴板
            return {"method": "clipboard", "reason": "long_text", "app_unknown": False}
        app = str(app_process or "").strip().lower().replace("/", "\\").rsplit("\\", 1)[-1]
        unknown = False
        if not app:
            app = self._foreground_process()
            unknown = not app
            if unknown:
                logger.debug("应用识别失败，按 P5（unicode）处理并在返回值标记 app_unknown")
        if app and app in type_keystroke_apps():  # P4 终端类按键优先
            return {"method": "keystroke", "reason": "keystroke_app", "app_unknown": False}
        return {"method": "unicode", "reason": "", "app_unknown": unknown}  # P5 默认直落

    def type_text(
        self,
        text: str,
        interval: float = 0.02,
        method: str = "auto",
        app_process: Optional[str] = None,
        fallback: Optional[bool] = None,
        readback: Optional[Callable[[], Optional[str]]] = None,
        focus_mismatch: bool = False,
    ) -> TypeOutcome:
        """按 ``method`` + 分层规则选择注入通道，返回实际通道与降级痕迹（方案 3.2）。

        :param method: ``auto`` / ``unicode`` / ``keystroke`` / ``clipboard``
        :param app_process: 上游 ``ui_app_ensure`` / ``ui_activate_window`` 返回的进程名，
            用于 P4 终端类判定；为空时回退读取前台窗口进程名
        :param fallback: 是否允许隐式降级；``None`` 时取 ``UIAGENT_TYPE_FALLBACK``
        :param readback: 回读探针（仅 ``UIAGENT_TYPE_READBACK=1`` 时生效），
            返回目标控件当前文本、``None`` 表示控件不可读
        :param focus_mismatch: 前置焦点校验结论（由 controller 侧判定）
        """
        outcome = TypeOutcome(
            method_requested=normalize_method(method),
            chars=len(text or ""),
        )
        outcome.focus_mismatch = bool(focus_mismatch)
        if focus_mismatch and type_focus_required():
            outcome.ok = False
            outcome.method_used = "none"
            outcome.error = "focus_mismatch"
            outcome.degrade_reason = "focus_mismatch"
            return outcome
        if not text:
            outcome.ok = False
            outcome.method_used = "none"
            outcome.error = "empty_text"
            return outcome
        if outcome.method_requested == "auto":
            route = self._route(text, app_process)
            channel = str(route["method"])
            outcome.app_unknown = bool(route["app_unknown"])
        else:
            channel = outcome.method_requested
        outcome.method_planned = channel
        allow_fallback = type_fallback_enabled() if fallback is None else bool(fallback)
        probe = readback if type_readback_enabled() else None
        return self._dispatch(channel, text, interval, outcome, probe, allow_fallback)

    #: 固定降级链（方案 2.3）：unicode → clipboard → keystroke
    _DEGRADE_CHAIN: Dict[str, Tuple[str, ...]] = {
        "unicode": ("clipboard", "keystroke"),
        "clipboard": ("keystroke",),
        "keystroke": (),
    }

    def _attempt(
        self, channel: str, text: str, interval: float, outcome: TypeOutcome
    ) -> Tuple[bool, str]:
        """执行单个通道，返回 ``(ok, reason)``；**不向上抛异常**。"""
        if channel == "unicode":
            try:
                self.type_via_unicode(text)
            except UnicodeInjectError as exc:
                logger.warning("unicode 通道失败：%s", exc.reason)
                return False, str(exc.reason or "unicode_inject_error")
            except Exception:  # pragma: no cover - 平台异常兜底
                logger.warning("unicode 通道异常", exc_info=True)
                return False, "unicode_inject_error"
            return True, ""

        if channel == "clipboard":
            result = self.type_via_clipboard(text)
            outcome.clipboard = dict(result)
            if result.get("ok"):
                return True, ""
            return False, str(result.get("reason") or "clipboard_error")

        try:
            self.type_via_keystroke(text, interval=interval)
        except Exception:  # pragma: no cover - 平台异常兜底
            logger.warning("keystroke 通道异常", exc_info=True)
            return False, "keystroke_failed"
        return True, ""

    @staticmethod
    def _readback_check(probe: Callable[[], Optional[str]], text: str) -> Dict[str, Any]:
        """回读校验（方案 4.5）：读目标控件文本尾部并与预期比对。

        控件不可读 → ``ok=None`` 且 ``reason="not_readable"``，**不触发降级**（避免误判）。
        控件文本刷新可能滞后于注入（UIA 异步），故不符时按 ``_READBACK_ATTEMPTS``
        重试后再判负，避免"假失败"引发无谓降级。
        """
        report: Dict[str, Any] = {"checked": True, "ok": None, "reason": "not_readable"}
        for attempt in range(_READBACK_ATTEMPTS):
            try:
                value = probe()
            except Exception:  # pragma: no cover - 回读失败不阻断
                logger.debug("回读探针异常", exc_info=True)
                value = None
            if value is None:
                return {"checked": True, "ok": None, "reason": "not_readable"}
            if not isinstance(value, str):
                value = str(value)
            actual = value[-len(text):] if len(text) <= len(value) else value
            report = {"checked": True, "ok": actual == text, "expected": text, "actual": actual}
            if report["ok"]:
                return report
            if attempt < _READBACK_ATTEMPTS - 1:
                time.sleep(_READBACK_DELAY)
        return report

    def _dispatch(
        self,
        channel: str,
        text: str,
        interval: float,
        outcome: TypeOutcome,
        readback: Optional[Callable[[], Optional[str]]] = None,
        fallback: bool = True,
    ) -> TypeOutcome:
        """按固定降级链执行通道，逐步回填 ``degrade_chain`` / 剪贴板 / 回读留痕。

        降级规则（方案 2.3）：``unicode`` 失败降 ``clipboard``，再降 ``keystroke``；
        ``clipboard`` 失败降 ``keystroke``；``keystroke`` 失败即 ``ok=false``
        （不回头改用 ``unicode``，避免"按键语义"被意外替换）。``fallback=False``
        （``UIAGENT_TYPE_FALLBACK=0`` 严格模式）时不降级，直接返回失败原因。
        """
        if channel == "none":
            outcome.ok = False
            outcome.method_used = "none"
            outcome.error = outcome.error or "empty_text"
            return outcome

        chain: Tuple[str, ...] = (channel,) + (
            self._DEGRADE_CHAIN.get(channel, ()) if fallback else ()
        )
        last_reason = ""
        for item in chain:
            ok, reason = self._attempt(item, text, interval, outcome)
            if ok and readback is not None:
                report = self._readback_check(readback, text)
                outcome.readback = report
                if report.get("ok") is False:
                    ok = False
                    reason = _READBACK_FAIL_REASON.get(item, "readback_mismatch")
            entry: Dict[str, Any] = {"to": item, "result": "ok" if ok else "failed"}
            if reason:
                entry["reason"] = reason
            outcome.degrade_chain.append(entry)
            if ok:
                outcome.ok = True
                outcome.method_used = item
                break
            last_reason = reason or "inject_failed"
        else:
            outcome.ok = False
            outcome.method_used = "none"
            outcome.error = last_reason or "inject_failed"

        planned = outcome.method_planned or outcome.method_requested
        outcome.degraded = bool(outcome.ok) and outcome.method_used != planned
        if outcome.degraded and not outcome.degrade_reason:
            outcome.degrade_reason = last_reason or "channel_changed"
        return outcome

    def type_via_unicode(self, text: str) -> int:
        """Unicode 直落通道：逐字符以 ``KEYEVENTF_UNICODE`` 注入。

        不加载键盘布局、不产生 VK，因此不会被 IME 组字——这是吞字治理的技术核心。
        非 BMP 字符（emoji 等）按 UTF-16 代理对拆成两个事件下发。

        :return: 实际注入的**代码单元**数（BMP 字符 1 个 / 非 BMP 2 个）
        :raises UnicodeInjectError: ``SendInput`` 返回值小于请求事件数，或调用异常
        """
        units = utf16_units(text)
        if not units:
            return 0

        user32 = ctypes.windll.user32
        expected = 2 * len(units)
        sent_total = 0
        batch_units = max(1, _SENDINPUT_EVENTS // 2)
        for start in range(0, len(units), batch_units):
            chunk = units[start : start + batch_units]
            events = (_INPUT * (2 * len(chunk)))()
            for index, code in enumerate(chunk):
                down = events[2 * index]
                down.type = INPUT_KEYBOARD
                down.u.ki = _KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
                up = events[2 * index + 1]
                up.type = INPUT_KEYBOARD
                up.u.ki = _KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            try:
                sent = int(
                    user32.SendInput(
                        len(events),
                        ctypes.byref(events),
                        ctypes.sizeof(_INPUT),
                    )
                )
            except Exception as exc:  # pragma: no cover - 平台异常兜底
                raise UnicodeInjectError("sendinput_call_failed", sent_total, expected) from exc
            sent_total += sent
            if sent < len(events):
                # 被 UIPI / 反作弊 / 钩子吞掉，交由上层决定降级
                raise UnicodeInjectError("sendinput_short_return", sent_total, expected)
        return len(units)

    def type_via_keystroke(self, text: str, interval: float = 0.02) -> None:
        """强制 VK 按键注入（``pyautogui.typewrite`` → ``keybd_event``）。

        **语义修正**（决策③）：该档不再对含非 ASCII 的文本自动改走剪贴板，
        而是真正逐字符按键注入；在 IME 激活态可能被组字 / 吞字，属已知局限。
        """
        if not text:
            return
        logger.info("type_via_keystroke len=%d", len(text))
        self._gui.typewrite(text, interval=interval)

    def type_via_clipboard(self, text: str, preserve: Optional[bool] = None) -> Dict[str, Any]:
        """剪贴板 + 粘贴（决策①：默认保存并在粘贴后还原用户原剪贴板）。

        :param preserve: ``None`` 表示读开关 ``UIAGENT_CLIPBOARD_PRESERVE``
        :return: ``{"ok", "saved", "restored", "reason"}``；还原失败不抛出
            （方案 4.2 硬约束一：剪贴板回滚失败不得让输入主流程报错）
        """
        import pyperclip

        keep = clipboard_preserve_enabled() if preserve is None else bool(preserve)
        logger.info("type_via_clipboard len=%d preserve=%s", len(text), keep)

        snapshot: Dict[str, Any] = {"has_content": False, "is_text": True, "text": ""}
        saved = True
        reason = ""
        if keep:
            snapshot = self.clipboard.snapshot()
            saved = bool(snapshot.get("is_text", False))
            if not saved:
                reason = str(snapshot.get("reason") or "non_text_content")

        try:
            pyperclip.copy(text)
        except Exception as exc:
            logger.warning("写入剪贴板失败：%s", exc)
            return {"ok": False, "saved": False, "restored": False, "reason": "clipboard_error"}

        time.sleep(_CLIPBOARD_SETTLE)
        self.hotkey("ctrl", "v")
        time.sleep(_CLIPBOARD_PASTE_SETTLE)

        restored = False
        restore_attempts = 0
        if keep and saved:
            # 有界重试：目标应用（尤其 Chromium 系）异步粘贴可能在我方写回后短暂抢回
            # 剪贴板；逐次校验，一致即止，次数用尽才判定 restore_raced 并留痕。
            for restore_attempts in range(1, _CLIPBOARD_RESTORE_ATTEMPTS + 1):
                restored = bool(self.clipboard.restore(snapshot))
                if not restored:
                    reason = "restore_failed"
                else:
                    check = self.clipboard.verify_restore(snapshot)
                    if check is not False:
                        reason = ""
                        break
                    reason = "restore_raced"
                time.sleep(_CLIPBOARD_RESTORE_GAP * restore_attempts)
            else:
                restored = False
                if reason != "restore_failed":
                    reason = "restore_raced"
        return {
            "ok": True,
            "saved": saved,
            "restored": restored,
            "restore_attempts": restore_attempts,
            "reason": reason,
        }

    def type_chinese(self, text: str) -> Dict[str, Any]:
        """:meth:`type_via_clipboard` 的别名（兼容既有调用方命名）。"""
        return self.type_via_clipboard(text)

    # ------------------------------------------------------------ 按键
    def press(self, key: str, presses: int = 1, interval: float = 0.05) -> None:
        logger.info("press %s x%s", key, presses)
        self._gui.press(key, presses=presses, interval=interval)

    def hotkey(self, *keys: str) -> None:
        logger.info("hotkey %s", "+".join(keys))
        self._gui.hotkey(*keys)

    def key_down(self, key: str) -> None:
        self._gui.keyDown(key)

    def key_up(self, key: str) -> None:
        self._gui.keyUp(key)

    def write(self, keys: Iterable[str], interval: float = 0.05) -> None:
        """按数组顺序输入按键序列。"""
        for key in keys:
            self._gui.press(key)
            time.sleep(interval)

    # ------------------------------------------------------------ 编辑器常用
    def select_all(self) -> None:
        self.hotkey("ctrl", "a")

    def copy(self) -> None:
        self.hotkey("ctrl", "c")
        time.sleep(_CLIPBOARD_SETTLE)

    def paste(self) -> None:
        self.hotkey("ctrl", "v")

    def cut(self) -> None:
        self.hotkey("ctrl", "x")

    def undo(self) -> None:
        self.hotkey("ctrl", "z")

    def enter(self) -> None:
        self.press("enter")

    def escape(self) -> None:
        self.press("esc")


# ---------------------------------------------------------------- SendInput 结构体
# 说明：``SendInput`` 要求 ``cbSize`` 等于 ``INPUT`` 的完整尺寸，因此 union 必须
# 同时声明鼠标 / 键盘 / 硬件三种形态，否则调用会被系统以参数非法拒绝。
_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]
