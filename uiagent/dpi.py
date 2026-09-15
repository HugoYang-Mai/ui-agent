"""DPI 感知与屏幕坐标系工具。

问题背景
--------
Windows 默认对未声明 DPI 感知的进程做坐标虚拟化：在 125% 缩放的 2560x1440 屏幕上，
不感知 DPI 的进程看到的屏幕尺寸是 2048x1152（= 物理像素 / 1.25）。而 UI Automation
返回的 BoundingRectangle 是物理像素。两者混用会让"找到元素"与"点击坐标"整体偏移，
且缩放比例因显示器而异。

解决方案
--------
在任何 GDI / 窗口 / 截图调用之前，把进程切到 ``PROCESS_PER_MONITOR_DPI_AWARE``
（数值 2），此后：

* UI Automation 边界矩形
* PIL / pyautogui 截图像素
* pyautogui 鼠标坐标
* GetSystemMetrics 屏幕尺寸

四者统一为**物理像素**，构成唯一坐标系。

注意：``SetProcessDpiAwareness`` 只能在进程生命周期内成功设置一次。若进程清单已声明
DPI 感知（或被调用过），重复调用返回 ``E_ACCESSDENIED``，本模块会将其视为"已生效"。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Tuple

# ---- SetProcessDpiAwareness 的 level 取值 ----
DPI_AWARENESS_UNAWARE = 0            # 不感知，坐标被虚拟化（默认，最差）
DPI_AWARENESS_SYSTEM_AWARE = 1       # 系统级感知，多显示器不同缩放时会错位
DPI_AWARENESS_PER_MONITOR_AWARE = 2  # 每显示器感知（本项目的统一坐标系基线）

# ---- GetProcessDpiAwareness 的返回值 ----
PROCESS_DPI_AWARENESS_INVALID = -1
PROCESS_DPI_UNAWARE = 0
PROCESS_SYSTEM_DPI_AWARE = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2

# ---- GetSystemMetrics 索引 ----
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CMONITORS = 80

_E_ACCESSDENIED = -2147024891  # 0x80070005
_S_OK = 0

_AWARENESS_NAME = {
    PROCESS_DPI_UNAWARE: "Unaware(0)",
    PROCESS_SYSTEM_DPI_AWARE: "SystemAware(1)",
    PROCESS_PER_MONITOR_DPI_AWARE: "PerMonitorAware(2)",
    PROCESS_DPI_AWARENESS_INVALID: "Unknown(-1)",
}


def enable_dpi_awareness(level: int = DPI_AWARENESS_PER_MONITOR_AWARE) -> int:
    """启用进程 DPI 感知，统一坐标系为物理像素。

    必须在导入 / 调用 pyautogui、PIL.ImageGrab、uiautomation 等任何会读取屏幕
    尺寸或坐标的库**之前**调用。

    :param level: 感知级别，默认 2（per-monitor）。
    :return: HRESULT，``0`` 表示本次调用成功；``-2147024891`` 表示此前已设置过
        （视为已生效）；其它非 0 值表示设置失败。
    """
    if sys.platform != "win32":
        return _S_OK

    try:
        shcore = ctypes.windll.shcore
    except (AttributeError, OSError):
        # Windows 7 无 shcore，退回旧 API（等价 system aware）
        ctypes.windll.user32.SetProcessDPIAware()
        return _S_OK

    shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
    shcore.SetProcessDpiAwareness.restype = ctypes.c_long
    hr = shcore.SetProcessDpiAwareness(int(level))

    if hr == _S_OK:
        return _S_OK
    if hr == _E_ACCESSDENIED:
        # 已被清单/先前调用设置，无需再处理
        return hr

    # 其它错误：退回旧 API 兜底
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # pragma: no cover - 极端环境
        pass
    return hr


def get_dpi_awareness() -> int:
    """查询当前进程 DPI 感知级别；失败返回 -1。"""
    if sys.platform != "win32":
        return PROCESS_DPI_AWARENESS_INVALID
    value = ctypes.c_int(PROCESS_DPI_AWARENESS_INVALID)
    try:
        shcore = ctypes.windll.shcore
        shcore.GetProcessDpiAwareness.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        hr = shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        if hr == _S_OK:
            return int(value.value)
    except (AttributeError, OSError):
        pass
    return PROCESS_DPI_AWARENESS_INVALID


def get_dpi_awareness_name(level: int | None = None) -> str:
    """返回 DPI 感知级别的可读名称。"""
    if level is None:
        level = get_dpi_awareness()
    return _AWARENESS_NAME.get(level, f"Unknown({level})")


def is_dpi_aware() -> bool:
    """判断当前进程是否已具备物理像素坐标系（per-monitor 或 system 级）。"""
    return get_dpi_awareness() in (PROCESS_SYSTEM_DPI_AWARE, PROCESS_PER_MONITOR_DPI_AWARE)


def get_screen_size() -> Tuple[int, int]:
    """主显示器尺寸，单位物理像素。"""
    if sys.platform != "win32":
        return (0, 0)
    user32 = ctypes.windll.user32
    return (int(user32.GetSystemMetrics(_SM_CXSCREEN)), int(user32.GetSystemMetrics(_SM_CYSCREEN)))


def get_virtual_screen_rect() -> Tuple[int, int, int, int]:
    """多显示器合并后的虚拟桌面矩形 ``(x, y, width, height)``，单位物理像素。

    单显示器且为主屏时通常为 ``(0, 0, w, h)``；主屏不在最左 / 最上时 ``x``/``y`` 可能为负。
    """
    if sys.platform != "win32":
        return (0, 0, 0, 0)
    user32 = ctypes.windll.user32
    return (
        int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)),
    )


def get_monitor_count() -> int:
    """显示器数量。"""
    if sys.platform != "win32":
        return 0
    return int(ctypes.windll.user32.GetSystemMetrics(_SM_CMONITORS))


def get_scale_factor() -> float:
    """系统缩放比例（1.0 / 1.25 / 1.5 ...）。

    优先使用 ``GetDpiForSystem``（Win10 1607+，随进程 DPI 感知状态返回系统 DPI），
    失败时退回 ``GetDeviceCaps(LOGPIXELSX)``。
    """
    if sys.platform != "win32":
        return 1.0

    try:
        user32 = ctypes.windll.user32
        get_dpi_for_system = getattr(user32, "GetDpiForSystem", None)
        if get_dpi_for_system is not None:
            get_dpi_for_system.restype = ctypes.c_uint
            dpi = int(get_dpi_for_system())
            if dpi:
                return round(dpi / 96.0, 4)
    except Exception:  # pragma: no cover
        pass

    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hdc = user32.GetDC(None)
        if not hdc:
            return 1.0
        try:
            LOGPIXELSX = 88
            dpi = int(gdi32.GetDeviceCaps(hdc, LOGPIXELSX))
        finally:
            user32.ReleaseDC(None, hdc)
        return round(dpi / 96.0, 4) if dpi else 1.0
    except Exception:  # pragma: no cover
        return 1.0


def describe() -> dict:
    """汇总当前 DPI / 屏幕坐标系状态，供自检与日志使用。"""
    vx, vy, vw, vh = get_virtual_screen_rect()
    sw, sh = get_screen_size()
    level = get_dpi_awareness()
    return {
        "platform": sys.platform,
        "dpi_awareness": level,
        "dpi_awareness_name": get_dpi_awareness_name(level),
        "dpi_aware": is_dpi_aware(),
        "scale_factor": get_scale_factor(),
        "screen_width": sw,
        "screen_height": sh,
        "virtual_screen": {"x": vx, "y": vy, "width": vw, "height": vh},
        "monitor_count": get_monitor_count(),
    }
