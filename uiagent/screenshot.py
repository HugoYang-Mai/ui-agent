"""截屏工具（物理像素坐标系）。

前提：进程已调用 :func:`uiagent.dpi.enable_dpi_awareness`，否则
``ImageGrab`` 会返回被缩放后的图像，与 UIA 坐标不一致。

坐标语义
--------
``region=(left, top, width, height)`` 为**屏幕绝对坐标**（虚拟桌面坐标）。
``all_screens=True``（默认）时 PIL 会把 bbox 平移到虚拟桌面原点，负坐标亦可正确处理。
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from PIL import Image, ImageGrab

from .base import Rect
from .dpi import get_virtual_screen_rect
from .logging_utils import get_logger

logger = get_logger("screenshot")


class Screener:
    """屏幕截图器。"""

    def __init__(self, all_screens: bool = True, include_layered_windows: bool = True) -> None:
        self.all_screens = all_screens
        self.include_layered_windows = include_layered_windows

    # ------------------------------------------------------------------ 基础
    def virtual_rect(self) -> Rect:
        """虚拟桌面矩形 ``(x, y, width, height)``。"""
        return get_virtual_screen_rect()

    def grab(self, region: Optional[Rect] = None) -> Image.Image:
        """截取屏幕区域，返回 PIL Image（RGB）。

        :param region: ``(left, top, width, height)``，``None`` 表示整个虚拟桌面。
        """
        left, top, width, height = self._resolve_region(region)
        bbox = (left, top, left + width, top + height)
        image = ImageGrab.grab(
            bbox=bbox,
            include_layered_windows=self.include_layered_windows,
            all_screens=self.all_screens,
        )
        if image.mode != "RGB":
            image = image.convert("RGB")
        logger.debug("grab region=%s -> size=%s", bbox, image.size)
        return image

    def grab_numpy(self, region: Optional[Rect] = None):
        """截取屏幕区域，返回 BGR 顺序的 numpy 数组（OpenCV 约定）。"""
        import cv2
        import numpy as np

        image = self.grab(region)
        arr = np.array(image)                 # RGB
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    def save(self, path: str, region: Optional[Rect] = None) -> str:
        """截图并保存到指定路径。"""
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.grab(region).save(path)
        logger.info("screenshot saved: %s", path)
        return path

    def screen_size(self) -> Tuple[int, int]:
        """主显示器尺寸（物理像素）。"""
        image = self.grab(None)
        return image.size

    # ------------------------------------------------------------------ 内部
    def _resolve_region(self, region: Optional[Rect]) -> Rect:
        if region is None:
            vx, vy, vw, vh = self.virtual_rect()
            if vw <= 0 or vh <= 0:  # 极端兜底：无虚拟屏幕信息
                image = ImageGrab.grab(all_screens=self.all_screens)
                return (0, 0, image.size[0], image.size[1])
            return (vx, vy, vw, vh)
        if len(region) != 4:
            raise ValueError(f"region 必须是 (left, top, width, height)，收到 {region!r}")
        left, top, width, height = (int(v) for v in region)
        if width <= 0 or height <= 0:
            raise ValueError(f"region 宽高必须为正数，收到 {region!r}")
        return (left, top, width, height)
