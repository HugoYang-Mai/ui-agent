"""键盘执行层（pyautogui + 剪贴板）。

中文 / 非 ASCII 文本无法通过 ``pyautogui.typewrite`` 输入（其键位表只覆盖 ASCII），
因此 :meth:`KeyboardExecutor.type_text` 会自动切换到"剪贴板 + Ctrl/Cmd+V"路径。
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from ..logging_utils import get_logger

logger = get_logger("keyboard")

# Windows 上 pyperclip 依赖剪贴板句柄，连续读写之间需要短暂间隔
_CLIPBOARD_SETTLE = 0.08


class KeyboardExecutor:
    """键盘操作封装。"""

    def __init__(self, pause: float = 0.05, failsafe: bool = True) -> None:
        import pyautogui

        self._gui = pyautogui
        pyautogui.FAILSAFE = failsafe
        pyautogui.PAUSE = pause

    # ------------------------------------------------------------ 文本
    @staticmethod
    def _is_ascii(text: str) -> bool:
        try:
            text.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    def type_text(self, text: str, interval: float = 0.02) -> None:
        """输入文本；含非 ASCII 字符时自动走剪贴板。"""
        if not text:
            return
        if self._is_ascii(text):
            logger.info("type_text(ascii) len=%d", len(text))
            self._gui.typewrite(text, interval=interval)
        else:
            self.type_via_clipboard(text)

    def type_via_clipboard(self, text: str) -> None:
        """通过剪贴板粘贴文本（中文 / emoji 等）。"""
        import pyperclip

        logger.info("type_via_clipboard len=%d", len(text))
        pyperclip.copy(text)
        time.sleep(_CLIPBOARD_SETTLE)
        self.hotkey("ctrl", "v")
        time.sleep(_CLIPBOARD_SETTLE)

    def type_chinese(self, text: str) -> None:
        """:meth:`type_via_clipboard` 的别名（兼容方案文档中的命名）。"""
        self.type_via_clipboard(text)

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
