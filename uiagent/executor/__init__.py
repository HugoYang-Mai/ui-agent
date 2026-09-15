"""执行层：鼠标 / 键盘 / 剪贴板。

⚠️ 本包内的类会**真实操作**鼠标键盘，属于有副作用的写操作。
只读自检（selfcheck/readonly_selfcheck.py）不会实例化这些类。
"""

from .clipboard import ClipboardExecutor
from .keyboard import KeyboardExecutor
from .mouse import MouseExecutor

__all__ = ["MouseExecutor", "KeyboardExecutor", "ClipboardExecutor"]
