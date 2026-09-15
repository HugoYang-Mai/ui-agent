"""剪贴板执行层（pyperclip）。

用途：中文 / 富文本输入的中转，以及读取界面文本作为 UIA 取值失败时的兜底。
"""

from __future__ import annotations

import time
from typing import Optional

from ..logging_utils import get_logger

logger = get_logger("clipboard")

_SETTLE = 0.08


class ClipboardExecutor:
    """剪贴板读写封装。"""

    def __init__(self) -> None:
        import pyperclip  # 延迟导入

        self._clip = pyperclip

    def get_text(self) -> str:
        """读取剪贴板文本（非文本内容返回空串）。"""
        try:
            data = self._clip.paste()
            return data if isinstance(data, str) else ""
        except Exception:
            logger.debug("读取剪贴板失败", exc_info=True)
            return ""

    def set_text(self, text: str) -> None:
        """写入剪贴板文本。"""
        self._clip.copy(text if text is not None else "")
        time.sleep(_SETTLE)

    def clear(self) -> None:
        """清空剪贴板。"""
        self._clip.copy("")

    def copy_and_paste(self, text: str, keyboard) -> None:
        """借助键盘执行器完成"写入剪贴板 + 粘贴"。"""
        self.set_text(text)
        keyboard.paste()

    def get_text_or_none(self) -> Optional[str]:
        """读取剪贴板，空内容返回 None。"""
        text = self.get_text()
        return text or None
