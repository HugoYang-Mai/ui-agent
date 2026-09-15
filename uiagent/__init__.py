"""ui-agent：基于 Windows UI Automation + OCR 的鼠标定位与界面操控内核。

设计约束
--------
1. **统一坐标系**：进程在使用任何 GDI / 窗口 / 截图 API 之前，必须先调用
   :func:`uiagent.dpi.enable_dpi_awareness`（内部即 ``SetProcessDpiAwareness(2)``）。
   否则 UIA 边界矩形（物理像素）与截图 / pyautogui 坐标（逻辑像素）会因系统缩放
   （例如 2560x1440 @125% 被报告成 2048x1152）而不一致，导致点击偏移。
2. **零副作用导入**：本 ``__init__`` 不导入 uiautomation / pyautogui / PIL 等重量级包，
   以便调用方在导入它们之前先完成 DPI 感知设置。

典型用法::

    from uiagent import dpi
    dpi.enable_dpi_awareness()          # 必须最先执行

    from uiagent.controller import UniversalController
    ctrl = UniversalController()
    ctrl.list_windows()
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
