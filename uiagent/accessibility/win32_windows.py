"""Win32 顶层窗口枚举与应用身份识别（仅依赖 ctypes 标准库）。

背景：UIA 的桌面根节点只暴露「已参与无障碍树的可见窗口」。窗口一旦隐藏 / 缩到
托盘（微信登录后窗口标题还会变成登录昵称，如 ``Hugo_ever``），调用方在
``ui_window_list`` 里就完全看不到它，于是误判「应用未运行」。本模块用
``EnumWindows`` 直接拿**全部**顶层窗口（含隐藏 / 最小化 / 托盘），并为每个窗口补齐：

* 真实可见性：``visible`` / ``iconic`` / ``cloaked``（DWM 幽灵窗口）
* 身份信息：``hwnd`` / ``process_name`` / ``class_name`` / ``app_alias`` / ``app_id``
* 窗口性质：``is_app_window``（像应用主窗口）/ ``hidden_app``（应用整体隐身的窗口）

同时提供窗口唤醒（``show_window`` / ``force_foreground``）所需的底层能力。

坐标系：``bounds`` 为屏幕绝对物理像素 ``(left, top, width, height)``，与
:mod:`uiagent.base` 保持一致。
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..base import Rect
from ..logging_utils import get_logger

logger = get_logger("win32")

# ------------------------------------------------------------------ 常量
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
#: 置顶窗口（如被固定在最前的微信主窗口）会盖住同区域普通窗口，点击会落到它身上
WS_EX_TOPMOST = 0x00000008

GW_OWNER = 4
GWL_EXSTYLE = -20
GA_ROOT = 2
DWMWA_CLOAKED = 14

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOW = 5
SW_RESTORE = 9

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: 明确不是应用窗口的窗口类（系统 / 辅助设施）
NOISE_CLASSES = frozenset(
    {
        "tooltips_class32",
        "gdi+ hook window class",
        "foregroundstaging",
        "thumbnaildevicehelperwnd",
        "msctfime ui",
        "ime",
        "default ime",
        "progman",
        "workerw",
        "shelldll_defview",
        "shell_traywnd",
    }
)

#: 「看起来像应用主窗口」的窗口类特征（小写子串匹配）
MAIN_WINDOW_CLASS_HINTS = (
    "qwindowicon",  # Qt 主窗口（Qt5/Qt6 各版本前缀，如 Qt51514QWindowIcon）
    "chrome_widgetwin_1",
    "cabinetwclass",
    "hwndwrapper",
    "avalonia-",
    "sunawtframe",
    "notepad",
    "applicationframewindow",
    "windows.ui.core.corewindow",
    "windowsforms10.window.8",
    "mozwindowclass",
    "wps",
    "etmainwindow",
)

#: 辅助窗口的类名特征（小写正则）
_HELPER_CLASS_RE = re.compile(
    r"(messagewindow|trayicon|pbuffer|invisib|dummy|"
    r"hookwindow|^tform|^aboutwindow|^infoform|^iedform|^iedfrm|^xysdk|"
    r"^uxdservice|callbackwindow|monitor$|ime$|^(gdi|csc).*)",
    re.IGNORECASE,
)

#: 辅助窗口的标题特征
_HELPER_TITLE_RE = re.compile(
    r"(messagewindow|trayicon|invisib|dummy|"
    r"^nvogl|^nvsvc|\.dll$|^hidden window$|^progress$|^进度$|notificationwindow|"
    r"^mediacontext|^systemresource|^__wgl|^wgl|^default ime$|^msctfime ui$|"
    r"^uixservice|^statusbar)",
    re.IGNORECASE,
)

#: 进程名 → (中文别名, 稳定 app_id)
PROCESS_ALIAS_TABLE: Dict[str, Tuple[str, str]] = {
    "weixin.exe": ("微信", "wechat"),
    "wechat.exe": ("微信", "wechat"),
    "wechatappex.exe": ("微信", "wechat"),
    "wechatapp.exe": ("微信", "wechat"),
    "wechatocr.exe": ("微信", "wechat"),
    "wetype.exe": ("微信输入法", "wechat-ime"),
    "notepad.exe": ("记事本", "notepad"),
    "mspaint.exe": ("画图", "mspaint"),
    "explorer.exe": ("文件资源管理器", "explorer"),
    "chrome.exe": ("Chrome", "chrome"),
    "msedge.exe": ("Edge", "msedge"),
    "firefox.exe": ("Firefox", "firefox"),
    "code.exe": ("VS Code", "vscode"),
    "winword.exe": ("Word", "word"),
    "excel.exe": ("Excel", "excel"),
    "powerpnt.exe": ("PowerPoint", "powerpoint"),
    "wps.exe": ("WPS 文字", "wps"),
    "et.exe": ("WPS 表格", "wps-et"),
    "wpp.exe": ("WPS 演示", "wps-wpp"),
    "qq.exe": ("QQ", "qq"),
    "tim.exe": ("TIM", "tim"),
    "dingtalk.exe": ("钉钉", "dingtalk"),
    "wework.exe": ("企业微信", "wework"),
    "wxwork.exe": ("企业微信", "wework"),
    "feishu.exe": ("飞书", "feishu"),
    "marvis.exe": ("Marvis", "marvis"),
    "cmd.exe": ("命令提示符", "cmd"),
    "powershell.exe": ("PowerShell", "powershell"),
    "windowsterminal.exe": ("Windows Terminal", "windows-terminal"),
    "python.exe": ("Python", "python"),
    "pythonw.exe": ("Python", "python"),
}

_QT_CLASS_RE = re.compile(r"^qt\d+", re.IGNORECASE)

# ------------------------------------------------------------------ Win32 绑定
_user32: Any = None
_kernel32: Any = None
_dwmapi: Any = None
_EnumWindowsProc = None  # type: ignore[assignment]


def _configure_win32() -> bool:
    """绑定 user32 / kernel32 / dwmapi 原型（幂等）。"""
    global _user32, _kernel32, _dwmapi, _EnumWindowsProc
    if _EnumWindowsProc is not None:
        return True
    if not sys.platform.startswith("win"):
        return False
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        except OSError:  # pragma: no cover - 极老系统
            _dwmapi = None

        _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
        _user32.EnumWindows.restype = wintypes.BOOL
        _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        _user32.GetWindowTextLengthW.restype = ctypes.c_int
        _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32.GetWindowTextW.restype = ctypes.c_int
        _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32.GetClassNameW.restype = ctypes.c_int
        _user32.IsWindow.argtypes = [wintypes.HWND]
        _user32.IsWindow.restype = wintypes.BOOL
        _user32.IsWindowVisible.argtypes = [wintypes.HWND]
        _user32.IsWindowVisible.restype = wintypes.BOOL
        _user32.IsIconic.argtypes = [wintypes.HWND]
        _user32.IsIconic.restype = wintypes.BOOL
        _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.GetWindowLongW.restype = ctypes.c_long
        _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        _user32.GetWindow.restype = wintypes.HWND
        _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        _user32.GetWindowRect.restype = wintypes.BOOL
        _user32.GetForegroundWindow.argtypes = []
        _user32.GetForegroundWindow.restype = wintypes.HWND
        _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        _user32.SetForegroundWindow.restype = wintypes.BOOL
        _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.ShowWindow.restype = wintypes.BOOL
        _user32.BringWindowToTop.argtypes = [wintypes.HWND]
        _user32.BringWindowToTop.restype = wintypes.BOOL
        _user32.WindowFromPoint.argtypes = [wintypes.POINT]
        _user32.WindowFromPoint.restype = wintypes.HWND
        _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        _user32.GetAncestor.restype = wintypes.HWND
        _user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        _user32.SetWindowPos.restype = wintypes.BOOL
        _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        _user32.AttachThreadInput.restype = wintypes.BOOL
        _kernel32.GetCurrentThreadId.argtypes = []
        _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        _kernel32.OpenProcess.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.DWORD]
        _kernel32.OpenProcess.restype = ctypes.c_void_p
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        _kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _kernel32.Process32FirstW.restype = wintypes.BOOL
        _kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _kernel32.Process32NextW.restype = wintypes.BOOL
        if _dwmapi is not None:
            _dwmapi.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        return True
    except Exception:  # pragma: no cover - 非 Windows 或绑定失败
        logger.debug("Win32 API 绑定失败", exc_info=True)
        _user32 = None
        return False


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


# ------------------------------------------------------------------ 数据结构
@dataclass
class Win32Window:
    """一个 Win32 顶层窗口（含隐藏窗口）及其应用身份信息。"""

    hwnd: int
    pid: int
    title: str
    class_name: str
    visible: bool
    iconic: bool
    cloaked: bool
    ex_style: int
    owner_hwnd: int
    bounds: Rect = (0, 0, 0, 0)
    process_name: str = ""
    app_alias: str = ""
    app_id: str = ""
    framework: str = ""
    #: 是否像「应用窗口」（可见主体窗口 or 隐藏的应用主窗口）
    is_app_window: bool = False
    #: 是否属于「该应用当前没有任何可见窗口」的隐藏窗口
    hidden_app: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> int:
        return int(self.bounds[2])

    @property
    def height(self) -> int:
        return int(self.bounds[3])

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> Tuple[int, int]:
        left, top, width, height = self.bounds
        return (left + width // 2, top + height // 2)

    @property
    def topmost(self) -> bool:
        """是否置顶窗口（WS_EX_TOPMOST）。

        置顶窗口会盖住同区域的普通窗口：命中它上面的坐标点击/输入不会到达下层窗口。
        """
        return bool(int(self.ex_style) & WS_EX_TOPMOST)

    def to_dict(self) -> Dict[str, Any]:
        # process_name 统一输出可执行文件名（Weixin.exe），完整路径放 process_path：
        # 同一字段在不同枚举路径下语义必须一致，否则调用方会把长路径当进程名匹配。
        raw_proc = str(self.process_name or "")
        proc_base = raw_proc.replace("/", "\\").rsplit("\\", 1)[-1] or raw_proc
        return {
            "hwnd": int(self.hwnd),
            "title": self.title,
            "name": self.title,
            "class_name": self.class_name,
            "process_id": int(self.pid),
            "process_name": proc_base,
            "process_path": raw_proc,
            "app_alias": self.app_alias,
            "app_id": self.app_id,
            "framework": self.framework,
            "visible": bool(self.visible),
            "iconic": bool(self.iconic),
            "cloaked": bool(self.cloaked),
            "topmost": self.topmost,
            "bounds": list(self.bounds),
            "center": list(self.center),
            "is_app_window": bool(self.is_app_window),
            "hidden_app": bool(self.hidden_app),
        }


# ------------------------------------------------------------------ 进程信息
_PROC_CACHE: Dict[int, Tuple[str, float]] = {}
_PROC_CACHE_TTL = 30.0
_PROC_LIST_CACHE: Dict[str, Any] = {"at": 0.0, "items": []}


def query_process_name(pid: int) -> str:
    """通过 PID 查询进程可执行文件全路径（失败返回空串，带 30s 缓存）。"""
    if not pid:
        return ""
    cached = _PROC_CACHE.get(pid)
    now = time.time()
    if cached and now - cached[1] < _PROC_CACHE_TTL:
        return cached[0]
    path = ""
    try:
        if _configure_win32():
            handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if _kernel32.QueryFullProcessImageNameW(
                        ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)
                    ):
                        path = buf.value
                finally:
                    _kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:  # pragma: no cover - 权限 / 句柄异常
        logger.debug("查询进程名失败 pid=%s", pid, exc_info=True)
    if len(_PROC_CACHE) > 512:
        _PROC_CACHE.clear()
    _PROC_CACHE[pid] = (path, now)
    return path


def list_processes(refresh: bool = False) -> List[Dict[str, Any]]:
    """枚举当前所有进程（PID + 可执行文件名），结果缓存 3 秒。"""
    now = time.time()
    if not refresh and now - float(_PROC_LIST_CACHE["at"]) < 3.0:
        return list(_PROC_LIST_CACHE["items"])
    items: List[Dict[str, Any]] = []
    try:
        if _configure_win32():
            snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot and snapshot != INVALID_HANDLE_VALUE:
                try:
                    entry = _PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                    if _kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                        while True:
                            items.append(
                                {
                                    "pid": int(entry.th32ProcessID),
                                    "name": str(entry.szExeFile or ""),
                                    "alias": app_alias_for(str(entry.szExeFile or ""), "")[0],
                                }
                            )
                            if not _kernel32.Process32NextW(
                                ctypes.c_void_p(snapshot), ctypes.byref(entry)
                            ):
                                break
                finally:
                    _kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    except Exception:  # pragma: no cover
        logger.debug("枚举进程失败", exc_info=True)
    _PROC_LIST_CACHE["at"] = now
    _PROC_LIST_CACHE["items"] = items
    return list(items)


def find_processes(needle: str, limit: int = 8) -> List[Dict[str, Any]]:
    """按进程名 / 别名模糊查找进程（用于未命中时的排查提示）。"""
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    hit: List[Dict[str, Any]] = []
    for item in list_processes():
        name = (item.get("name") or "").lower()
        alias = str(item.get("alias") or "").lower()
        if needle in name or needle in alias or (alias and alias in needle):
            hit.append(item)
            if len(hit) >= limit:
                break
    return hit


# ------------------------------------------------------------------ 应用身份
def app_alias_for(process_name: str, class_name: str = "") -> Tuple[str, str, str]:
    """返回 ``(中文别名, app_id, 框架)``。

    * 进程名命中别名表 → 用表内别名（``weixin.exe`` → 微信/wechat）
    * 未命中 → 用可执行文件名（去扩展名）兜底，保证任何窗口都有可读身份
    * 框架：``Qt`` / ``Chromium`` / ``WinForms`` / ``Electron`` 等（可选参考）
    """
    base = os.path.basename((process_name or "").strip()).lower()
    cls = (class_name or "").strip()
    cls_lower = cls.lower()

    if base in PROCESS_ALIAS_TABLE:
        alias, app_id = PROCESS_ALIAS_TABLE[base]
    elif base:
        stem = os.path.splitext(os.path.basename(process_name or ""))[0]
        alias, app_id = stem, stem.lower()
    else:
        alias, app_id = "", ""

    if _QT_CLASS_RE.match(cls_lower) and "qwindowicon" in cls_lower:
        framework = "Qt"
    elif "chrome_widgetwin" in cls_lower:
        framework = "Chromium"
    elif cls_lower.startswith("windowsforms10"):
        framework = "WinForms"
    elif cls_lower.startswith("hwndwrapper"):
        framework = "WPF"
    elif cls_lower.startswith("avalonia-"):
        framework = "Avalonia"
    else:
        framework = ""
    return alias, app_id, framework


def _has_main_class_hint(class_name: str) -> bool:
    cls = (class_name or "").lower()
    return any(hint in cls for hint in MAIN_WINDOW_CLASS_HINTS)


def is_helper_window(class_name: str = "", title: str = "") -> bool:
    """辅助 / 消息 / 托盘窗口判定（这类窗口永远不该被激活）。"""
    cls_lower = (class_name or "").lower()
    if cls_lower in NOISE_CLASSES:
        return True
    if cls_lower and _HELPER_CLASS_RE.search(cls_lower):
        return True
    if title and _HELPER_TITLE_RE.search(title):
        return True
    return False


def looks_like_app_window(win: Win32Window, process_has_visible: bool = False) -> bool:
    """判断某窗口是否「像应用主窗口」，用于决定它值不值得出现在窗口列表里。"""
    if win.cloaked:
        return False
    if (win.class_name or "").lower() in NOISE_CLASSES:
        return False
    if win.ex_style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    if is_helper_window(win.class_name, win.title):
        return False
    if not win.title:
        return False
    if win.width < 160 or win.height < 120:
        return False
    if win.visible:
        return True
    # 隐藏窗口：要么窗口类像应用主窗口；要么「该应用当前整体隐身」（没有任何可见窗口）
    if _has_main_class_hint(win.class_name):
        return True
    return not process_has_visible


# ------------------------------------------------------------------ 窗口查询
def is_window(hwnd: int) -> bool:
    if not hwnd or not _configure_win32():
        return False
    try:
        return bool(_user32.IsWindow(wintypes.HWND(int(hwnd))))
    except Exception:  # pragma: no cover
        return False


def is_window_visible(hwnd: int) -> Optional[bool]:
    """窗口真实可见性；``hwnd`` 无效时返回 ``None``。"""
    if not hwnd or not _configure_win32():
        return None
    try:
        handle = wintypes.HWND(int(hwnd))
        if not _user32.IsWindow(handle):
            return None
        return bool(_user32.IsWindowVisible(handle))
    except Exception:  # pragma: no cover
        return None


def is_iconic(hwnd: int) -> bool:
    if not hwnd or not _configure_win32():
        return False
    try:
        return bool(_user32.IsIconic(wintypes.HWND(int(hwnd))))
    except Exception:  # pragma: no cover
        return False


def get_window_rect(hwnd: int) -> Rect:
    if not hwnd or not _configure_win32():
        return (0, 0, 0, 0)
    try:
        rect = wintypes.RECT()
        if _user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
            return (
                int(rect.left),
                int(rect.top),
                max(0, int(rect.right) - int(rect.left)),
                max(0, int(rect.bottom) - int(rect.top)),
            )
    except Exception:  # pragma: no cover
        pass
    return (0, 0, 0, 0)


def get_foreground_hwnd() -> int:
    if not _configure_win32():
        return 0
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:  # pragma: no cover
        return 0


def get_window_title(hwnd: int) -> str:
    if not hwnd or not _configure_win32():
        return ""
    try:
        handle = wintypes.HWND(int(hwnd))
        length = int(_user32.GetWindowTextLengthW(handle))
        buf = ctypes.create_unicode_buffer(length + 2)
        _user32.GetWindowTextW(handle, buf, length + 2)
        return buf.value
    except Exception:  # pragma: no cover
        return ""


def get_class_name(hwnd: int) -> str:
    if not hwnd or not _configure_win32():
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetClassNameW(wintypes.HWND(int(hwnd)), buf, 512)
        return buf.value
    except Exception:  # pragma: no cover
        return ""


def _is_cloaked(hwnd: int) -> bool:
    if _dwmapi is None:
        return False
    try:
        value = ctypes.c_int(0)
        hr = _dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
        return hr == 0 and value.value != 0
    except Exception:  # pragma: no cover
        return False


def get_pid_of_hwnd(hwnd: int) -> int:
    if not hwnd or not _configure_win32():
        return 0
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # pragma: no cover
        return 0


def window_from_hwnd(hwnd: int, process_name: str = "") -> Optional[Win32Window]:
    """按 HWND 构造窗口对象（未命中返回 ``None``）。"""
    if not is_window(hwnd):
        return None
    pid = get_pid_of_hwnd(hwnd)
    name = process_name or query_process_name(pid)
    cls = get_class_name(hwnd)
    alias, app_id, framework = app_alias_for(name, cls)
    try:
        ex_style = int(_user32.GetWindowLongW(wintypes.HWND(int(hwnd)), -20)) & 0xFFFFFFFF
        owner = int(_user32.GetWindow(wintypes.HWND(int(hwnd)), GW_OWNER) or 0)
    except Exception:  # pragma: no cover
        ex_style, owner = 0, 0
    raw_visible = bool(is_window_visible(hwnd))
    return Win32Window(
        hwnd=int(hwnd),
        pid=pid,
        title=get_window_title(hwnd),
        class_name=cls,
        # 前台窗口视为显示中（部分 Qt 主窗口 WS_VISIBLE 与显示状态不同步）
        visible=bool(raw_visible or int(hwnd) == get_foreground_hwnd()),
        iconic=is_iconic(hwnd),
        cloaked=_is_cloaked(hwnd),
        ex_style=ex_style,
        owner_hwnd=owner,
        bounds=get_window_rect(hwnd),
        process_name=name,
        app_alias=alias,
        app_id=app_id,
        framework=framework,
        extra={} if raw_visible else {"win32_visible": raw_visible},
    )


def enum_top_level_windows(limit: int = 0) -> List[Win32Window]:
    """枚举全部顶层窗口（含隐藏 / 托盘 / 无标题辅助窗口）。"""
    if not _configure_win32():
        return []
    collected: List[Win32Window] = []

    def _callback(hwnd: int, lparam: int) -> bool:
        pid = wintypes.DWORD(0)
        try:
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = int(_user32.GetWindowTextLengthW(hwnd))
            buf = ctypes.create_unicode_buffer(length + 2)
            _user32.GetWindowTextW(hwnd, buf, length + 2)
            cls_buf = ctypes.create_unicode_buffer(512)
            _user32.GetClassNameW(hwnd, cls_buf, 512)
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            ex_style = int(_user32.GetWindowLongW(hwnd, -20)) & 0xFFFFFFFF
            owner = int(_user32.GetWindow(hwnd, GW_OWNER) or 0)
            collected.append(
                Win32Window(
                    hwnd=int(hwnd),
                    pid=int(pid.value),
                    title=buf.value,
                    class_name=cls_buf.value,
                    visible=bool(_user32.IsWindowVisible(hwnd)),
                    iconic=bool(_user32.IsIconic(hwnd)),
                    cloaked=_is_cloaked(int(hwnd)),
                    ex_style=ex_style,
                    owner_hwnd=owner,
                    bounds=(
                        int(rect.left),
                        int(rect.top),
                        max(0, int(rect.right) - int(rect.left)),
                        max(0, int(rect.bottom) - int(rect.top)),
                    ),
                )
            )
        except Exception:  # pragma: no cover - 单个窗口异常不影响整体枚举
            logger.debug("枚举窗口 hwnd=%s 失败", hwnd, exc_info=True)
        return True

    try:
        _user32.EnumWindows(_EnumWindowsProc(_callback), 0)
    except Exception:  # pragma: no cover
        logger.debug("EnumWindows 失败", exc_info=True)
        return []
    annotate_windows(collected)
    if limit and limit > 0:
        return collected[:limit]
    return collected


def annotate_windows(windows: Sequence[Win32Window]) -> List[Win32Window]:
    """为窗口补齐进程名 / 应用别名 / 是否应用窗口（就地修改并返回）。"""
    seen_pid: Dict[int, str] = {}
    for win in windows:
        if win.pid not in seen_pid:
            seen_pid[win.pid] = query_process_name(win.pid)
        if not win.process_name:
            win.process_name = seen_pid[win.pid]
        if not win.app_alias:
            alias, app_id, framework = app_alias_for(win.process_name, win.class_name)
            win.app_alias = alias
            win.app_id = app_id
            win.framework = win.framework or framework
    foreground_hwnd = get_foreground_hwnd()
    for win in windows:
        # 前台窗口一定在显示：部分 Qt 应用主窗口的 WS_VISIBLE 与真实显示状态不同步，
        # 不修正会把「当前正在用的窗口」误判成隐藏窗口（原始值存 extra 备查）。
        if not win.visible and int(win.hwnd) == foreground_hwnd:
            win.extra["win32_visible"] = False
            win.visible = True
    visible_pids = {win.pid for win in windows if win.visible}
    for win in windows:
        process_has_visible = win.pid in visible_pids
        win.hidden_app = (not win.visible) and (not process_has_visible)
        win.is_app_window = looks_like_app_window(win, process_has_visible)
    return list(windows)


def app_windows(windows: Optional[Sequence[Win32Window]] = None) -> List[Win32Window]:
    """筛选「像应用窗口」的顶层窗口（含隐藏的应用主窗口）。"""
    pool = annotate_windows(windows) if windows is not None else enum_top_level_windows()
    return [win for win in pool if win.is_app_window]


def window_rank(win: Win32Window) -> Tuple[int, int]:
    """激活/展示优先级：可见未最小化 → 可见已最小化 → 隐藏；同档取面积大者。"""
    if win.visible and not win.iconic:
        tier = 0
    elif win.visible:
        tier = 1
    else:
        tier = 2
    return (tier, -win.area)


def select_best_window(windows: Sequence[Win32Window]) -> Optional[Win32Window]:
    """从候选窗口中挑最值得激活的一个（排除托盘 / 消息窗口）。"""
    pool = [win for win in windows if not is_helper_window(win.class_name, win.title)]
    if not pool:
        pool = list(windows)
    if not pool:
        return None
    return sorted(pool, key=window_rank)[0]


# ------------------------------------------------------------------ 窗口唤醒
def show_window(hwnd: int) -> Dict[str, Any]:
    """把隐藏 / 最小化的窗口显示出来（``SW_SHOW`` / ``SW_RESTORE``）。

    :return: ``{"ok", "hwnd", "was_visible", "was_iconic", "shown", "visible_after", "iconic_after"}``
    """
    state: Dict[str, Any] = {
        "ok": False,
        "hwnd": int(hwnd or 0),
        "was_visible": None,
        "was_iconic": None,
        "shown": False,
        "visible_after": None,
        "iconic_after": None,
    }
    if not is_window(hwnd):
        state["error"] = f"hwnd={hwnd} 不是有效窗口（可能已关闭）"
        return state

    handle = wintypes.HWND(int(hwnd))
    state["was_visible"] = bool(_user32.IsWindowVisible(handle))
    state["was_iconic"] = bool(_user32.IsIconic(handle))

    if state["was_iconic"]:
        _user32.ShowWindow(handle, SW_RESTORE)
        state["shown"] = True
    if not state["was_visible"]:
        # 隐藏窗口：托盘隐藏的 Qt/微信窗口需要 SW_SHOW 才会重新参与显示
        _user32.ShowWindow(handle, SW_SHOW)
        state["shown"] = True
    time.sleep(0.25)

    state["visible_after"] = bool(_user32.IsWindowVisible(handle))
    state["iconic_after"] = bool(_user32.IsIconic(handle))
    state["ok"] = True
    return state


def force_foreground(hwnd: int, restore: bool = True, attempts: int = 3) -> bool:
    """把指定 HWND 置为前台窗口。

    Windows 对 ``SetForegroundWindow`` 有前台锁定限制：非前台进程直接调用可能只让
    任务栏图标闪烁。这里采用「AttachThreadInput + BringWindowToTop + 重试」的标准
    绕过手法，并在每次尝试后回读 ``GetForegroundWindow`` 校验是否真正生效。
    """
    if not _configure_win32() or not hwnd:
        return False
    handle = wintypes.HWND(int(hwnd))
    if not _user32.IsWindow(handle):
        return False

    if restore and _user32.IsIconic(handle):
        _user32.ShowWindow(handle, SW_RESTORE)
        time.sleep(0.15)

    current_tid = int(_kernel32.GetCurrentThreadId())
    for _ in range(max(1, int(attempts))):
        foreground = int(_user32.GetForegroundWindow() or 0)
        if foreground == int(hwnd):
            _user32.BringWindowToTop(handle)
            return True

        target_tid = int(_user32.GetWindowThreadProcessId(handle, None) or 0)
        fg_tid = (
            int(_user32.GetWindowThreadProcessId(wintypes.HWND(foreground), None) or 0)
            if foreground
            else 0
        )
        attached: List[int] = []
        for tid in {target_tid, fg_tid}:
            if tid and tid != current_tid:
                if _user32.AttachThreadInput(
                    wintypes.DWORD(current_tid), wintypes.DWORD(tid), True
                ):
                    attached.append(tid)
        try:
            _user32.BringWindowToTop(handle)
            _user32.SetForegroundWindow(handle)
        except Exception:  # pragma: no cover - 极端环境下 API 失败
            logger.debug("SetForegroundWindow 失败", exc_info=True)
        finally:
            for tid in attached:
                _user32.AttachThreadInput(
                    wintypes.DWORD(current_tid), wintypes.DWORD(tid), False
                )
        time.sleep(0.12)
        if int(_user32.GetForegroundWindow() or 0) == int(hwnd):
            return True

    return int(_user32.GetForegroundWindow() or 0) == int(hwnd)


# ------------------------------------------------------------------ 遮挡诊断 / 可点性
def is_topmost(hwnd: int) -> bool:
    """指定窗口是否置顶（WS_EX_TOPMOST）。"""
    if not _configure_win32() or not hwnd:
        return False
    try:
        style = int(_user32.GetWindowLongW(wintypes.HWND(int(hwnd)), GWL_EXSTYLE) or 0)
    except Exception:  # pragma: no cover - 窗口销毁等
        return False
    return bool(style & WS_EX_TOPMOST)


def window_at_point(x: int, y: int) -> int:
    """屏幕坐标 (x, y) 处最顶层的**顶层窗口** hwnd；该点无窗口时返回 0。

    用于判断"激活/置前后，这个点究竟属于哪个窗口"——被置顶窗口（如固定在最前的
    微信主窗口）盖住的坐标，点击/输入不会到达下层窗口。
    """
    if not _configure_win32():
        return 0
    try:
        point = wintypes.POINT(int(x), int(y))
        child = int(_user32.WindowFromPoint(point) or 0)
        if not child:
            return 0
        root = int(_user32.GetAncestor(wintypes.HWND(child), GA_ROOT) or 0)
        return root or child
    except Exception:  # pragma: no cover - 极端环境
        logger.debug("WindowFromPoint 失败", exc_info=True)
        return 0


def top_window_at_point(x: int, y: int) -> Optional[Win32Window]:
    """坐标处最顶层窗口的完整信息（含应用别名 / 置顶标记），无窗口返回 None。"""
    hwnd = window_at_point(x, y)
    if not hwnd:
        return None
    return window_from_hwnd(hwnd)


def probe_window_cover(hwnd: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
    """检查窗口某点（默认中心点）是否被其它窗口遮挡。

    返回 ``{"checked", "hwnd", "point", "reachable", "covered_by"}``：
    ``reachable=False`` 表示该点的真实归属窗口不是自己，坐标点击/输入不会到达本窗口。
    """
    state: Dict[str, Any] = {
        "checked": False,
        "hwnd": int(hwnd or 0),
        "point": None,
        "reachable": True,
        "covered_by": None,
    }
    if not hwnd or not _configure_win32():
        return state
    if x is None or y is None:
        left, top, width, height = get_window_rect(hwnd)
        x, y = left + width // 2, top + height // 2
    state["point"] = [int(x), int(y)]
    state["checked"] = True
    top = window_at_point(int(x), int(y))
    if top and int(top) != int(hwnd):
        cover = window_from_hwnd(int(top))
        state["reachable"] = False
        state["covered_by"] = cover.to_dict() if cover is not None else {"hwnd": int(top)}
    return state


def ensure_reachable(hwnd: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
    """保证窗口在该点「真实可点」：被上层/置顶窗口遮挡时临时把窗口抬到最前。

    手段：``SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE|SWP_NOMOVE|SWP_NOSIZE)`` 把窗口插入
    置顶带最前（不改尺寸、不抢焦点），使坐标点击真正落到本窗口；点击结束后调用
    :func:`restore_after_boost` 恢复原置顶状态（原本不是置顶的窗口退回非置顶带）。

    返回状态 dict（``boost`` / ``was_topmost`` / ``covered_by_before`` / ``reachable_after``），
    未发生遮挡时不改变任何 Z 序。
    """
    state: Dict[str, Any] = {
        "checked": False,
        "boosted": False,
        "was_topmost": False,
        "covered_by_before": None,
        "reachable_after": True,
    }
    if not hwnd or not _configure_win32():
        return state

    before = probe_window_cover(hwnd, x, y)
    state["checked"] = before["checked"]
    if before["reachable"]:
        return state

    state["covered_by_before"] = before["covered_by"]
    state["was_topmost"] = is_topmost(hwnd)
    handle = wintypes.HWND(int(hwnd))
    try:
        _user32.SetWindowPos(
            handle,
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        time.sleep(0.08)
    except Exception:  # pragma: no cover - 极端环境
        logger.debug("SetWindowPos 抬升失败", exc_info=True)
        return state

    after = probe_window_cover(hwnd, x, y)
    # 注意：这里表示「已施加临时置顶」，供调用方点击结束后无条件复原；
    # 不能用 after["reachable"] 判定，某些窗口（如被系统策略锁定）抬升后仍不可达，
    # 但 Z 序已经被改动，必须复原。
    state["boosted"] = True
    state["reachable_after"] = after["reachable"]
    return state


def restore_after_boost(hwnd: int, state: Optional[Dict[str, Any]] = None) -> None:
    """撤销 :func:`ensure_reachable` 的临时置顶（原本就置顶的窗口保持置顶）。"""
    if not hwnd or not _configure_win32():
        return
    if not state or not state.get("boosted"):
        return
    if state.get("was_topmost"):
        return
    try:
        _user32.SetWindowPos(
            wintypes.HWND(int(hwnd)),
            wintypes.HWND(HWND_NOTOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        time.sleep(0.05)
    except Exception:  # pragma: no cover - 极端环境
        logger.debug("恢复置顶状态失败", exc_info=True)
