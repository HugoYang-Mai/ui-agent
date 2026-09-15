"""剪贴板执行层（pyperclip）。

用途：中文 / 富文本输入的中转，以及读取界面文本作为 UIA 取值失败时的兜底。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

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

    # ------------------------------------------------------------ 保存 / 还原
    def snapshot(self) -> Dict[str, Any]:
        """保存剪贴板快照（决策①：输入前保存，输入后还原）。

        :return: ``{"has_content": bool, "is_text": bool, "text": str}``。
            非文本内容（图片 / 文件等）``is_text=False``——``pyperclip`` 只能读写
            文本，必须显式标记为"不可还原"，禁止假装成功（方案 4.2）。
            读取异常时 ``is_text=False`` 且携带 ``reason="clipboard_error"``。
        """
        try:
            data = self._clip.paste()
        except Exception:
            logger.debug("读取剪贴板快照失败", exc_info=True)
            return {
                "has_content": False,
                "is_text": False,
                "text": "",
                "reason": "clipboard_error",
            }
        if isinstance(data, str):
            return {"has_content": bool(data), "is_text": True, "text": data}
        if data is None or data == "":
            return {"has_content": False, "is_text": True, "text": ""}
        return {"has_content": True, "is_text": False, "text": ""}

    def restore(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        """还原剪贴板快照；**不抛异常**，失败返回 ``False``（方案 4.2 硬约束一）。

        分支：原本为空 → :meth:`clear`；原本为文本 → :meth:`set_text`；
        非文本 / 快照非法 / 写回异常 → ``False``（留痕由调用方负责）。
        """
        if not isinstance(snapshot, dict) or not snapshot.get("is_text", False):
            return False
        try:
            if snapshot.get("has_content", False):
                self.set_text(str(snapshot.get("text") or ""))
            else:
                self.clear()
            return True
        except Exception:
            logger.debug("还原剪贴板失败", exc_info=True)
            return False

    def verify_restore(self, snapshot: Optional[Dict[str, Any]]) -> Optional[bool]:
        """复核剪贴板是否已回到快照内容。

        :return: ``True`` 一致；``False`` 不一致；``None`` 不可核验（非文本快照 /
            读取失败）——不可核验时不判定失败，避免误报（方案 4.2 硬约束二）。

        背景：个别应用的 OLE 粘贴会在我方还原后短暂抢回剪贴板，
        ``restore()`` 返回成功并不等价于"若干毫秒后内容仍是快照"，故给出显式复核口。
        """
        if not isinstance(snapshot, dict) or not snapshot.get("is_text", False):
            return None
        current = self.get_text()
        expected = str(snapshot.get("text") or "") if snapshot.get("has_content", False) else ""
        return current == expected
