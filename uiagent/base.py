"""数据模型与抽象基类。

坐标系约定：所有 ``bounds`` / ``center`` 均为**物理像素**，且是**屏幕绝对坐标**
（多显示器时为虚拟桌面坐标），原点为主显示器左上角。前提是进程已调用
:func:`uiagent.dpi.enable_dpi_awareness`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: (left, top, width, height)，单位物理像素
Rect = Tuple[int, int, int, int]
#: (x, y)，单位物理像素
Point = Tuple[int, int]


def normalize_role(role: Optional[str]) -> str:
    """角色名归一化：``ButtonControl`` / ``AXButton`` / ``button`` → ``button``。

    去掉非字母数字字符与尾部 ``control`` 后缀，便于跨后端 / 跨平台做模糊匹配。
    """
    if not role:
        return ""
    s = "".join(ch for ch in str(role).lower() if ch.isalnum())
    if s.startswith("ax") and len(s) > 4:
        s = s[2:]
    if s.endswith("control"):
        s = s[: -len("control")]
    return s


@dataclass
class UIElement:
    """无障碍树中的一个 UI 元素。"""

    name: str = ""
    role: str = "Unknown"
    bounds: Rect = (0, 0, 0, 0)
    enabled: bool = True
    focused: bool = False
    automation_id: str = ""
    class_name: str = ""
    control_type: int = 0
    process_id: int = 0
    process_name: str = ""
    #: 进程可执行文件完整路径（如 ``C:\Program Files\Tencent\Weixin\Weixin.exe``）
    process_path: str = ""
    visible: bool = True
    depth: int = 0
    children: List["UIElement"] = field(default_factory=list)
    #: 顶层窗口句柄 HWND（非窗口元素为 0）；窗口项可据此直接激活
    hwnd: int = 0
    #: 应用别名（如「微信」/「记事本」），用于把窗口标题（登录昵称）对应回具体应用
    app_alias: str = ""
    #: 稳定应用标识（如 ``wechat``），跨界面语言可比对
    app_id: str = ""
    #: UI 框架（``Qt`` / ``Chromium`` / ``WinForms`` / ``WPF`` 等，未知为空串）
    framework: str = ""
    #: 窗口是否处于最小化状态
    iconic: bool = False
    #: 窗口是否置顶（WS_EX_TOPMOST）；置顶窗口会盖住同区域普通窗口，坐标点击会落到它身上
    topmost: bool = False
    #: 后端原生控件引用（uiautomation.Control 等），不参与序列化与比较
    native: Any = field(default=None, repr=False, compare=False)

    @property
    def center(self) -> Point:
        """元素中心点（物理像素）。"""
        left, top, width, height = self.bounds
        return (left + width // 2, top + height // 2)

    @property
    def is_clickable(self) -> bool:
        """是否有可点击的实际区域。"""
        return self.bounds[2] > 0 and self.bounds[3] > 0

    def to_dict(self, include_children: bool = False, max_children: int = 30) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "role": self.role,
            "bounds": list(self.bounds),
            "center": list(self.center),
            "enabled": self.enabled,
            "focused": self.focused,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "process_id": self.process_id,
            "process_name": self.process_name,
            "process_path": self.process_path,
            "visible": self.visible,
            "iconic": self.iconic,
            "topmost": self.topmost,
            "hwnd": int(self.hwnd or 0),
            "app_alias": self.app_alias,
            "app_id": self.app_id,
            "framework": self.framework,
            "depth": self.depth,
        }
        if include_children:
            data["children"] = [
                c.to_dict(True, max_children) for c in self.children[:max_children]
            ]
        return data

    def __str__(self) -> str:  # pragma: no cover - 调试友好
        return (
            f"<UIElement role={self.role!r} name={self.name!r} "
            f"bounds={self.bounds} center={self.center} enabled={self.enabled}>"
        )


@dataclass
class OCRText:
    """一段 OCR 识别结果（含屏幕绝对坐标）。"""

    text: str
    confidence: float
    bounds: Rect        # (left, top, width, height) 屏幕绝对坐标
    center: Point       # (x, y) 屏幕绝对坐标

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(float(self.confidence), 4),
            "bounds": list(self.bounds),
            "center": list(self.center),
        }

    def __str__(self) -> str:  # pragma: no cover
        return f"<OCRText {self.text!r} conf={self.confidence:.3f} center={self.center}>"


class AccessibilityBase(ABC):
    """无障碍接口后端基类（Windows 上由 UI Automation 实现）。"""

    @abstractmethod
    def get_active_window(self) -> Optional[UIElement]:
        """返回当前前台窗口。"""

    @abstractmethod
    def list_windows(self, include_invisible: bool = False) -> List[UIElement]:
        """枚举顶层窗口。"""

    @abstractmethod
    def find_element(
        self,
        name: Optional[str] = None,
        role: Optional[str] = None,
        window: Optional[UIElement] = None,
        **kwargs: Any,
    ) -> Optional[UIElement]:
        """查找首个匹配元素。"""

    @abstractmethod
    def find_all_elements(
        self,
        name: Optional[str] = None,
        role: Optional[str] = None,
        window: Optional[UIElement] = None,
        **kwargs: Any,
    ) -> List[UIElement]:
        """查找全部匹配元素。"""

    @abstractmethod
    def get_element_tree(
        self, root: Optional[UIElement] = None, depth: int = 3
    ) -> Optional[UIElement]:
        """获取元素树（返回带 children 的根节点）。"""

    def get_value(self, target: Any) -> Optional[str]:
        """读取控件文本值（只读）。默认实现返回 ``None``（后端不支持时）。"""
        return None


class OCRBase(ABC):
    """OCR 引擎基类。"""

    @abstractmethod
    def recognize(self, image: Any = None, region: Optional[Rect] = None) -> List[OCRText]:
        """识别图像 / 屏幕区域，返回全部文本块及屏幕坐标。"""

    @abstractmethod
    def find_text(
        self, text: str, image: Any = None, region: Optional[Rect] = None
    ) -> List[OCRText]:
        """在图像 / 屏幕区域中查找指定文字，返回全部命中。"""

    @abstractmethod
    def recognize_region(self, region: Optional[Rect] = None) -> str:
        """识别区域并将所有文本拼接为字符串。"""
