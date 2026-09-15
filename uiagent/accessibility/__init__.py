"""无障碍（Accessibility）后端。

Windows 平台由 :mod:`uiagent.accessibility.windows_uia` 提供 UI Automation 实现。
"""

from .windows_uia import WindowsAccessibility, get_accessibility

__all__ = ["WindowsAccessibility", "get_accessibility"]
