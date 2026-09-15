---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 8ba5b382cadd141b612ac8623b46555a_26da375cb04b11f18039525400461939
    ReservedCode1: 3v6roGW2e+xjdKZzfamBuonQlKc99PNSB7PU8uwhCIurTSQCvfOnVhmR3qWf1WKoTOhYxYP2UouzX9kU9AKjpm/MVBx6np/C7AnuEcs8LQ6Zg84eDLT9JbSZsq/D1KJQOGG9/q3XtDsOyclKRsdc5Z/nClk06J5p4M1hBEDSPulDZqo8jbzfpgitTcI=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 8ba5b382cadd141b612ac8623b46555a_26da375cb04b11f18039525400461939
    ReservedCode2: 3v6roGW2e+xjdKZzfamBuonQlKc99PNSB7PU8uwhCIurTSQCvfOnVhmR3qWf1WKoTOhYxYP2UouzX9kU9AKjpm/MVBx6np/C7AnuEcs8LQ6Zg84eDLT9JbSZsq/D1KJQOGG9/q3XtDsOyclKRsdc5Z/nClk06J5p4M1hBEDSPulDZqo8jbzfpgitTcI=
---

# ui-agent

基于 **Windows UI Automation（UIA）+ RapidOCR（CPU）** 的鼠标定位与界面操控内核。

定位策略为三级降级：**UIA 精确定位 → OCR 文字定位兜底 → 返回失败**。内核已通过 **MCP Server（stdio 传输）** 对外暴露 8 个 `ui_*` 工具，可供 CowAgent / Marvis 等 MCP 客户端直接接入；接入配置、工具参数与协议自测结论见 [`docs/mcp-server.md`](D:\Projects\ui-agent\docs\mcp-server.md)。

---

## 1. 环境

| 项 | 值 |
| --- | --- |
| 解释器 | `Python 3.12.10`（独立 venv：`D:\Projects\ui-agent\.venv`） |
| 创建方式 | `py -3.12 -m venv .venv`（**严禁**使用 `python` 命令，本机 `python` 指向 Marvis 内置运行时） |
| 安装依赖 | `.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| OCR | `rapidocr-onnxruntime 1.4.4` + `onnxruntime 1.30.0`（纯 CPU 推理，模型随包内置，无需下载） |
| 屏幕环境 | 单显示器，物理分辨率 2560×1440，系统缩放 125%（`scale_factor=1.25`，逻辑分辨率 2048×1152） |

依赖锁定见 [`requirements.txt`](D:\Projects\ui-agent\requirements.txt)。

---

## 2. 坐标系统一（第一优先级约束）

进程必须先调用 `SetProcessDpiAwareness(2)`，**且必须早于** `PIL` / `pyautogui` / `uiautomation` 的导入；否则下列两套坐标会分裂：

- UIA 的 `BoundingRectangle` 返回**物理像素**；
- 未做 DPI 感知时，`PIL.ImageGrab` / `pyautogui.size()` 返回被系统缩放后的**逻辑像素**。

在本机 2560×1440 显示器上，未感知时 `pyautogui.size()` 会错误地报告 `2048×1152`，直接导致点击偏移与 OCR 坐标错位。

内核统一约定：**所有对外坐标（元素 bounds / center、OCR 文本块 bounds / center、鼠标操作入参）一律为物理像素**。

```python
from uiagent import dpi
dpi.enable_dpi_awareness(dpi.DPI_AWARENESS_PER_MONITOR_AWARE)  # 最先执行
from uiagent.controller import UniversalController             # 之后再导入重包
```

`uiagent/__init__.py` 刻意保持零副作用（不导入任何重量级库），以保证调用方能在导入链之前完成 DPI 设置。

---

## 3. 目录结构

```
D:\Projects\ui-agent\
├── main.py                          # CLI 入口（第 1 步即启用 DPI 感知；stdout 固定 UTF-8）
├── serve_mcp.py                     # MCP Server 独立入口（stdio，供客户端子进程拉起）
├── requirements.txt                 # 锁定版本依赖
├── docs\
│   └── mcp-server.md                # MCP 接入文档：配置、工具参数、协议自测结论
├── uiagent\
│   ├── __init__.py                  # 版本号，零副作用
│   ├── dpi.py                       # SetProcessDpiAwareness(2)、DPI/缩放查询、Rect/=坐标系工具
│   ├── base.py                      # UIElement / OCRText 数据类、抽象基类、normalize_role
│   ├── screenshot.py                # PIL.ImageGrab 物理像素截屏
│   ├── logging_utils.py             # 日志（全部走 stderr，stdout 只承载结构化结果）
│   ├── controller.py                # UniversalController：统一门面
│   ├── mcp_server.py                # MCP Server：8 个 ui_* 工具 + COM 专用线程调度
│   ├── accessibility\
│   │   └── windows_uia.py           # WindowsAccessibility：UIA 查询与窗口激活
│   ├── ocr\
│   │   └── rapidocr_engine.py       # RapidOCREngine：CPU 推理，输出屏幕绝对坐标
│   └── executor\
│       ├── mouse.py                 # MouseExecutor（写操作）
│       ├── keyboard.py              # KeyboardExecutor（写操作）
│       └── clipboard.py             # ClipboardExecutor（写操作，中文输入用）
├── selfcheck\
│   ├── readonly_selfcheck.py        # 只读自检（6 项），产出 JSON + MD 报告
│   ├── probe_notepad.py             # 记事本编辑器探测
│   ├── e2e_notepad.py               # 记事本端到端实测（真实鼠标键盘全链路）
│   └── mcp_selfcheck.py             # MCP 协议自测（握手 / tools/list / tools/call）
└── out\                             # 自检报告与截图
```

---

## 4. CLI 用法

所有命令均通过 venv 解释器执行：

```powershell
cd D:\Projects\ui-agent
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe -X utf8 main.py <command>
```

| 命令 | 作用 | 类型 |
| --- | --- | --- |
| `info` | 打印 DPI 感知级别、缩放、屏幕分辨率、虚拟桌面矩形 | 只读 |
| `selfcheck [--ocr-limit N] [--top-windows N]` | 运行 6 项只读自检并落盘报告 | 只读 |
| `windows [--all] [--non-window] [--limit N]` | 枚举顶层窗口；`--non-window` 列出全部顶层控件（含任务栏等） | 只读 |
| `tree [--depth N] [--active]` | 打印前台窗口元素树 | 只读 |
| `find [--match 名称] [--role Button] [--exact] [--depth N]` | 元素查找 | 只读 |
| `text [--region x,y,w,h]` | 全屏或区域 OCR，返回文字及坐标 | 只读 |
| `locate --match 目标 [--no-ocr]` | 统一门面定位（UIA 优先，OCR 兜底） | 只读 |
| `click --match 目标 --yes` | 点击（UIA → OCR 降级） | **写操作，需 `--yes`** |
| `type --text 文本 --yes` | 输入文本（中文自动走剪贴板） | **写操作，需 `--yes`** |
| `mcp` | 以 stdio 方式启动 MCP Server（等价于 `serve_mcp.py`） | 常驻服务 |

写操作未加 `--yes` 时直接拒绝执行（退出码 2）。

---

## 5. Python API

```python
from uiagent import dpi
dpi.enable_dpi_awareness(dpi.DPI_AWARENESS_PER_MONITOR_AWARE)

from uiagent.controller import UniversalController
ctrl = UniversalController(search_timeout=2.0)

# ---- 只读 ----
ctrl.environment()                       # DPI / 屏幕环境
ctrl.list_windows()                      # 顶层窗口列表（UIElement）
ctrl.list_top_level()                    # 桌面全部顶层控件
ctrl.get_active_window()                 # 前台窗口
ctrl.get_focused_element()               # 焦点元素
ctrl.find_all_elements(name="保存", role="Button", window=win, max_depth=8)
ctrl.get_element_tree(window=win, depth=3)
ctrl.element_from_point(x, y)            # 指定坐标点所属元素
ctrl.ocr_screen()                        # 全屏 OCR -> List[OCRText]（含物理像素坐标）
ctrl.find_text_on_screen("确定")          # 文字 -> 坐标
ctrl.locate("确定")                       # LocateResult(source='uia'|'ocr', center=..., ...)

# ---- 写操作（有副作用，谨慎调用）----
ctrl.click("确定")                        # locate 后点击
ctrl.type_chinese("你好")                 # 剪贴板粘贴，规避输入法问题
ctrl.hotkey("ctrl", "s")
```

`UIElement` 关键字段：`name / role / bounds(x,y,w,h) / center(x,y) / class_name / automation_id / process_id / process_name / enabled / focused / visible / children`。
`OCRText` 关键字段：`text / confidence / bounds(x,y,w,h) / center(x,y)`。

---

## 6. 只读自检

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py selfcheck --ocr-limit 20
```

自检项（全部只读，仅落盘报告与截图）：

| # | 自检项 | 验证内容 |
| --- | --- | --- |
| 1 | DPI 感知与统一坐标系 | `SetProcessDpiAwareness(2)` 生效、scale、主屏与虚拟桌面矩形自洽 |
| 2 | 枚举顶层窗口 | 桌面顶层控件枚举 + 按 `ControlType=Window` 过滤 |
| 3 | 读取前台窗口与焦点元素 | 前台窗口与焦点元素可读 |
| 4 | 元素查找（UIA） | 优先任务栏窗口按 button/menuitem/edit/hyperlink 探测，返回名称与中心坐标 |
| 5 | 元素树采样 | 指定深度子树遍历、角色分布统计 |
| 6 | 截屏 + OCR | 全屏截图落盘 + RapidOCR 返回文本块及物理像素坐标 |

输出：`out\selfcheck_report_<ts>.json`、`out\selfcheck_report_<ts>.md`、`out\selfcheck_screenshot_<ts>.png`。

---

## 7. 已知约束（MCP 接入前必读）

- **仅 Windows**：UIA 实现绑定 `uiautomation`，无跨平台分支。
- **OCR 性能**：全屏 2560×1440 单次识别约 3.0 s（首次含模型加载约 0.4 s）；建议接入时对区域裁剪后识别，或先 UIA 命中以避开 OCR。
- **执行层有副作用**：`executor/` 下的鼠标 / 键盘 / 剪贴板为真实操作，自检流程**不会**实例化该层。
- **UIA 不可用场景**（游戏、远程桌面、Canvas 自绘等）只能依赖 OCR，见方案文档降级说明。
- **CLI stdout 为结构化 JSON**，日志走 stderr，便于 MCP Server 直接以子进程方式包裹。

---

## 8. MCP Server（stdio）

```powershell
.\.venv\Scripts\python.exe serve_mcp.py      # 或 .\.venv\Scripts\python.exe main.py mcp
```

| 工具 | 类型 | 作用 |
| --- | --- | --- |
| `ui_window_list` | 只读 | 枚举顶层窗口 + 环境快照 |
| `ui_activate_window` | 写 | 按标题 / 进程名查找并置前 |
| `ui_find` | 只读 | 定位元素或界面文字（UIA → OCR 兜底） |
| `ui_click` | 写 | 定位式点击或坐标点击 |
| `ui_type` | 写 | 输入文本（中文自动走剪贴板） |
| `ui_hotkey` | 写 | 发送组合快捷键 |
| `ui_screenshot` | 只读 | 截图落盘（全屏 / 区域） |
| `ui_ocr` | 只读 | 屏幕 OCR（blocks / keyword / text） |

实现要点：所有调用经**专用 COM 单线程**串行执行（`uiagent/mcp_server.py`），stdout 仅承载 JSON-RPC，日志走 stderr。

协议自测：

```powershell
.\.venv\Scripts\python.exe -X utf8 selfcheck\mcp_selfcheck.py                 # 只读链路
.\.venv\Scripts\python.exe -X utf8 selfcheck\mcp_selfcheck.py --include-write # 追加记事本端到端
```

最近实测：`initialize` 握手 `2025-11-25`、`tools/list` 返回全部 8 个工具、`tools/call` 覆盖只读与写链路，**14/14 检查通过（passed=true，16.08 s）**。

完整接入配置、逐工具参数表与故障排查见 [`docs/mcp-server.md`](D:\Projects\ui-agent\docs\mcp-server.md)。
*（内容由AI生成，仅供参考）*
