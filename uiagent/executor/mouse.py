"""鼠标执行层（pyautogui）。

坐标一律为**屏幕绝对物理像素**（前提：进程已启用 per-monitor DPI 感知）。

安全说明
--------
本类会真实移动 / 点击鼠标。``FAILSAFE=True`` 时把鼠标甩到屏幕左上角可紧急中断。
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from ..base import Point
from ..logging_utils import get_logger

logger = get_logger("mouse")


class MouseExecutor:
    """鼠标操作封装。"""

    def __init__(
        self,
        pause: float = 0.05,
        failsafe: bool = True,
        duration: float = 0.2,
    ) -> None:
        import pyautogui  # 延迟导入：确保调用方已完成 DPI 感知设置

        self._gui = pyautogui
        pyautogui.FAILSAFE = failsafe
        pyautogui.PAUSE = pause
        self.failsafe = failsafe
        self.duration = duration

    # ------------------------------------------------------------ 查询
    def position(self) -> Point:
        """当前鼠标位置。"""
        x, y = self._gui.position()
        return (int(x), int(y))

    def screen_size(self) -> Tuple[int, int]:
        """主屏尺寸（物理像素）。"""
        width, height = self._gui.size()
        return (int(width), int(height))

    def on_screen(self, x: int, y: int) -> bool:
        """判断坐标是否落在主屏范围内。"""
        width, height = self.screen_size()
        return 0 <= int(x) < width and 0 <= int(y) < height

    # ------------------------------------------------------------ 移动
    def move_to(self, x: int, y: int, duration: Optional[float] = None) -> None:
        self._gui.moveTo(int(x), int(y), duration=self.duration if duration is None else duration)

    def move_rel(self, dx: int, dy: int, duration: Optional[float] = None) -> None:
        self._gui.moveRel(int(dx), int(dy), duration=self.duration if duration is None else duration)

    # ------------------------------------------------------------ 点击
    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.08,
        duration: Optional[float] = None,
    ) -> Point:
        """单击 / 连击；``x``/``y`` 为 None 时在当前鼠标位置点击。"""
        if x is None or y is None:
            x, y = self.position()
        logger.info("click (%s, %s) button=%s clicks=%s", x, y, button, clicks)
        self._gui.click(
            int(x),
            int(y),
            clicks=clicks,
            interval=interval,
            button=button,
            duration=self.duration if duration is None else duration,
        )
        return (int(x), int(y))

    def double_click(self, x: int, y: int, button: str = "left", interval: float = 0.08) -> Point:
        """双击。"""
        logger.info("double_click (%s, %s)", x, y)
        self._gui.doubleClick(int(x), int(y), interval=interval, button=button)
        return (int(x), int(y))

    def right_click(self, x: int, y: int) -> Point:
        """右键单击。"""
        logger.info("right_click (%s, %s)", x, y)
        self._gui.rightClick(int(x), int(y))
        return (int(x), int(y))

    def mouse_down(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left") -> None:
        if x is not None and y is not None:
            self.move_to(x, y)
        self._gui.mouseDown(button=button)

    def mouse_up(self, button: str = "left") -> None:
        self._gui.mouseUp(button=button)

    # ------------------------------------------------------------ 拖拽
    def drag_to(
        self,
        start: Point,
        end: Point,
        duration: float = 0.5,
        button: str = "left",
    ) -> None:
        """从 start 拖动到 end（绝对坐标）。"""
        logger.info("drag %s -> %s", start, end)
        self._gui.moveTo(int(start[0]), int(start[1]))
        self._gui.dragTo(int(end[0]), int(end[1]), duration=duration, button=button)

    def drag_rel(self, dx: int, dy: int, duration: float = 0.5, button: str = "left") -> None:
        self._gui.drag(int(dx), int(dy), duration=duration, button=button)

    # ------------------------------------------------------------ 滚轮
    def scroll(self, clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        """滚动滚轮；正数向上，负数向下。"""
        if x is not None and y is not None:
            self.move_to(x, y)
        self._gui.scroll(int(clicks))

    def hscroll(self, clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        """水平滚动。"""
        if x is not None and y is not None:
            self.move_to(x, y)
        self._gui.hscroll(int(clicks))

    # ------------------------------------------------------------ 便捷
    def click_element(self, element, button: str = "left", clicks: int = 1) -> Point:
        """点击 UIElement 的中心点。"""
        if element is None:
            raise ValueError("element 不能为 None")
        x, y = element.center
        return self.click(x, y, button=button, clicks=clicks)

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)
