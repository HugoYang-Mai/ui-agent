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

定位策略为三级降级：**UIA 精确定位 → OCR 文字定位兜底 → 返回失败**。内核已通过 **MCP Server（stdio 传输）** 对外暴露 8 个 `ui_*` 工具，可供 CowAgent / Marvis 等 MCP 客户端直接接入；接入配置、工具参数与协议自测结论见 [`docs/mcp-server.md`](docs/mcp-server.md)；审计日志（调用链 / 定位方式 / 写操作明细）设计见 [`docs/audit-log-design.md`](docs/audit-log-design.md)。

窗口识别采用 **Win32 `EnumWindows` + UIA 双层**：隐藏 / 最小化到托盘的窗口同样可枚举，并可按 `hwnd` / 进程名 / 窗口类名 / 标题定位与激活，未命中时返回可执行的排查建议（详见第 7 节）；全部调用与写操作自动落审计日志（详见第 8 节）。

---

## 1. 环境

| 项 | 值 |
| --- | --- |
| 解释器 | `Python 3.12.10`（独立 venv：`D:\Projects\ui-agent\.venv`） |
| 创建方式 | `py -3.12 -m venv .venv`（**严禁**使用 `python` 命令，本机 `python` 指向 Marvis 内置运行时） |
| 安装依赖 | `.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| OCR | `rapidocr-onnxruntime 1.4.4` + `onnxruntime 1.30.0`（纯 CPU 推理，模型随包内置，无需下载） |
| 屏幕环境 | 单显示器，物理分辨率 2560×1440，系统缩放 125%（`scale_factor=1.25`，逻辑分辨率 2048×1152） |

依赖锁定见 [`requirements.txt`](requirements.txt)（审计日志仅用标准库 + 现有依赖，未新增包）。

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
├── main.py                              # CLI 入口（第 1 步即启用 DPI 感知；stdout 固定 UTF-8）
├── serve_mcp.py                         # MCP Server 独立入口（stdio，供客户端子进程拉起）
├── requirements.txt                     # 锁定版本依赖
├── .gitignore                           # 忽略 logs\ / out\ / .venv\ / __pycache__
├── docs\
│   ├── mcp-server.md                    # MCP 接入文档：配置、工具参数、协议自测结论
│   └── audit-log-design.md              # 审计日志设计：事件模型、埋点位置、脱敏策略、验收标准
├── uiagent\
│   ├── __init__.py                      # 版本号，零副作用
│   ├── dpi.py                           # SetProcessDpiAwareness(2)、DPI/缩放查询、Rect/Point 坐标系工具
│   ├── base.py                          # UIElement / OCRText 数据类（含 hwnd / app_alias / iconic / topmost）、抽象基类、normalize_role
│   ├── screenshot.py                    # PIL.ImageGrab 物理像素截屏
│   ├── logging_utils.py                 # 运行日志（全部走 stderr，stdout 只承载结构化结果）
│   ├── audit.py                         # 审计日志：JSONL 落盘、输入文本脱敏、run 归组、统计 / 跟读 / 回放
│   ├── controller.py                    # UniversalController：统一门面（定位 / 点击 / 输入 / 窗口 / 审计埋点）
│   ├── mcp_server.py                    # MCP Server：8 个 ui_* 工具 + COM 专用线程调度 + tool_call/tool_result 埋点
│   ├── accessibility\
│   │   ├── win32_windows.py             # Win32 ctypes 层：EnumWindows 全量枚举、进程名 / 应用别名标注、置前与遮挡探测
│   │   └── windows_uia.py               # WindowsAccessibility：UIA 查询 + 窗口查找 / 激活（委托 Win32 层）
│   ├── ocr\
│   │   └── rapidocr_engine.py           # RapidOCREngine：CPU 推理，输出屏幕绝对坐标
│   └── executor\
│       ├── mouse.py                     # MouseExecutor（写操作）
│       ├── keyboard.py                  # KeyboardExecutor（写操作）
│       └── clipboard.py                 # ClipboardExecutor（写操作，中文输入用）
├── selfcheck\
│   ├── readonly_selfcheck.py            # 只读自检（6 项），产出 JSON + MD 报告
│   ├── probe_notepad.py                 # 记事本编辑器探测
│   ├── e2e_notepad.py                   # 记事本端到端实测（真实鼠标键盘全链路）
│   └── mcp_selfcheck.py                 # MCP 协议自测（握手 / tools/list / tools/call）
├── logs\                                # 审计日志 audit-YYYYMMDD-<pid>.jsonl（本地运行产物，不进版本库）
└── out\                                 # 自检报告与截图（本地运行产物，不进版本库）
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
| `selfcheck [--ocr-limit N] [--top-windows N] [--out 目录] [--json-only]` | 运行 6 项只读自检并落盘报告 | 只读 |
| `windows [--all] [--non-window] [--limit N]` | 枚举顶层窗口（含隐藏 / 托盘窗口，带 `hwnd` / `visible` / `app_alias`）；`--non-window` 列出全部顶层控件（含任务栏等） | 只读 |
| `tree [--depth N] [--active]` | 打印前台窗口元素树 | 只读 |
| `find [--match 名称] [--role Button] [--exact] [--depth N] [--limit N]` | 元素查找（UIA） | 只读 |
| `text [--region x,y,w,h] [--limit N]` | 全屏或区域 OCR，返回文字及坐标 | 只读 |
| `locate --match 目标 [--role R] [--no-ocr]` | 统一门面定位（UIA 优先，OCR 兜底） | 只读 |
| `click --match 目标 --yes [--button B] [--clicks N] [--verify]` | 点击（UIA → OCR 降级；返回命中窗口与自动抬升标记） | **写操作，需 `--yes`** |
| `type --text 文本 --yes` | 输入文本（中文自动走剪贴板） | **写操作，需 `--yes`** |
| `audit [--stats] [--date YYYY-MM-DD] [--tail] [--run run_id] [--lines N] [--dir 目录]` | 审计日志：统计汇总（默认）/ 实时跟读 / 按 run 回放 | 只读 |
| `mcp` | 以 stdio 方式启动 MCP Server（等价于 `serve_mcp.py`） | 常驻服务 |

写操作未加 `--yes` 时直接拒绝执行（退出码 2）。审计日志默认写入 `D:\Projects\ui-agent\logs\audit-YYYYMMDD-<pid>.jsonl`，`UI_AGENT_AUDIT=0` 可整体关闭。

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

# ---- 窗口识别与激活（Win32 枚举 + UIA 双层）----
ctrl.find_window(title="记事本")           # 也支持 process_name / class_name / hwnd
ctrl.window_from_hwnd(hwnd)               # 按句柄构造窗口元素（懒加载 UIA 原生控件）
ctrl.activate_window(hwnd=hwnd, show_hidden=True)   # 置前，可唤醒隐藏 / 托盘窗口

from uiagent.controller import window_miss_report
window_miss_report(title="微信")           # 未命中时返回 hints + candidates + hidden_apps 诊断

# ---- 写操作（有副作用，谨慎调用）----
ctrl.click("确定")                        # locate 后点击（被置顶窗口遮挡时自动临时抬升目标窗口）
ctrl.type_chinese("你好")                 # 剪贴板粘贴，规避输入法问题
ctrl.hotkey("ctrl", "s")
```

审计日志（`uiagent/audit.py`）：默认开启，JSONL 落盘 `logs\`；输入文本默认只落长度 + 摘要 + 掩码预览。

```python
from uiagent import audit
audit.audit_enabled()        # 总开关（UI_AGENT_AUDIT）
audit.audit_dir()            # 日志目录（UI_AGENT_AUDIT_DIR）
audit.record_text_policy()   # 文本策略：mask / hash / full
```

`UIElement` 关键字段：`name / role / bounds(x,y,w,h) / center(x,y) / class_name / automation_id / process_id / process_name / process_path / hwnd / app_alias / app_id / framework / enabled / focused / visible / iconic / topmost / children`。
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
| 2 | 枚举顶层窗口 | Win32 `EnumWindows` 全量枚举（含隐藏 / 托盘窗口）后按 `ControlType=Window` 过滤，输出名称 / 类名 / 进程名 / bounds / center |
| 3 | 读取前台窗口与焦点元素 | 前台窗口与焦点元素可读 |
| 4 | 元素查找（UIA） | 优先任务栏窗口按 button/menuitem/edit/hyperlink 探测，返回名称与中心坐标 |
| 5 | 元素树采样 | 指定深度子树遍历、角色分布统计 |
| 6 | 截屏 + OCR | 全屏截图落盘 + RapidOCR 返回文本块及物理像素坐标 |

输出：`out\selfcheck_report_<ts>.json`、`out\selfcheck_report_<ts>.md`、`out\selfcheck_screenshot_<ts>.png`。

---

## 7. 窗口识别与激活（Win32 枚举 + UIA 双层）

解决的问题：UIA 只能看到"可见窗口"，微信 / QQ 等应用主窗口最小化到托盘或隐藏后整体从枚举结果中消失，调用方据此误判"应用未运行"；且这类窗口标题是登录昵称（如 `Hugo_ever`），也无法从标题判断归属应用。

实现：`uiagent/accessibility/win32_windows.py` 用 `ctypes` 直接调 `EnumWindows` 做**权威枚举**（含隐藏 / 最小化 / 托盘 / 不可见辅助窗口），`windows_uia.py` 委托该层；UIA 仅保留可见窗口的元素操作能力（按 `hwnd` 懒加载原生控件）。

| 能力 | 说明 |
| --- | --- |
| 全量枚举 | 每项带 `hwnd` / `visible` / `iconic` / `process_name` / `process_path` / `class_name` / `app_alias` / `app_id` / `framework` / `topmost` / bounds / center |
| 隐藏与托盘窗口 | `list_windows(include_hidden=True)`（默认）始终返回应用主窗口的隐藏态（`visible=false`）；`include_invisible=True` 才额外暴露助手 / 消息类噪声窗口 |
| 应用识别 | 进程名 + 窗口类名别名表（如 Qt 窗口类 `Qt51514QWindowIcon` → 别名「微信」），不依赖标题 |
| 定位方式 | `find_window(title=…) / process_name=… / class_name=… / hwnd=…`；标题支持子串匹配与别名匹配 |
| 未命中诊断 | 返回 `hints`（可执行排查建议）与 `candidates`（疑似候选窗口，含别名 / 进程名 / 类名）；Python 侧可用 `window_miss_report(...)` 单独获取 |
| 激活 | `activate_window(hwnd=…, restore=True, show_hidden=True)`：最小化先还原、隐藏 / 托盘先 `ShowWindow` 唤醒，再用 `AttachThreadInput` 绕过前台锁定 |
| 遮挡诊断 | `probe_window_cover` 判断目标点是否被置顶窗口盖住（返回 `covered` / `covered_by` / `hint`）；坐标点击前自动**临时抬升**目标窗口 Z 序（`ensure_reachable`），点击后立即 `restore_after_boost` 复原，并在结果中返回 `auto_raise` / `hit_window` |
| 性能 | 枚举阶段纯本地构造、不触碰 UIA（避免跨进程 COM 阻塞），仅在激活 / 定位具体窗口时按 `hwnd` 懒加载原生控件 |

本机实测（2026-09-15）：`list_windows()` 返回 19 个窗口（7 可见 / 12 隐藏），单次耗时约 10 ms；隐藏窗口带 `visible=false` 与 `app_alias`，可按 `hwnd` 激活。

---

## 8. 审计日志（audit log）

设计文档：[`docs/audit-log-design.md`](docs/audit-log-design.md)。用途：记录"谁在何时调用了什么、用了哪种定位方式、点了哪里、命中了哪个窗口"，便于调试与回归分析。**不改变任何工具签名与返回结构**，不写 stdout。

- 载体：`uiagent/audit.py`，JSONL 落盘 `logs\audit-YYYYMMDD-<pid>.jsonl`（`logs\` 已 gitignore）；单文件超 10 MB 轮转为 `<同名>.1`，启动时清理超过保留天数的旧文件。
- 六类事件：`tool_call` / `tool_result`（MCP 层，覆盖全部 8 个工具）、`locate`（定位明细：`source=uia|ocr|coords`、`fallback`、`uia_ms` / `ocr_ms`、候选数）、`action`（写操作：点击坐标、`hit_window`、`auto_raise`、输入摘要）、`run_start` / `run_end`（按静默间隔切分的任务回放单元）。
- 脱敏（默认 `mask`）：只落 `input_len` + `input_sha256[:8]` + 首字符掩码预览（如 `老***`），**不落明文**；`hash` 只留长度与摘要，`full` 仅本地深度调试时使用。
- 埋点仅 3 处：`mcp_server.py` 的 `_guard`、`controller.py` 的 `locate()` / `locate_many()`、`controller.py` 的写操作方法（`click` / `type_text` / `type_chinese` / `hotkey` / `activate_window`）。

环境变量：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `UI_AGENT_AUDIT` | `1` | 总开关；`0` 完全关闭（不建目录、不落文件） |
| `UI_AGENT_AUDIT_DIR` | `D:\Projects\ui-agent\logs` | 审计日志目录 |
| `UI_AGENT_AUDIT_TEXT` | `mask` | 输入文本策略：`mask` / `hash` / `full` |
| `UI_AGENT_AUDIT_KEEP_DAYS` | `14` | 日志保留天数 |
| `UI_AGENT_AUDIT_RUN_GAP` | `8` | run 归组的静默间隔（秒） |

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py audit --stats                        # 统计汇总（默认）
.\.venv\Scripts\python.exe -X utf8 main.py audit --stats --date 2026-09-15      # 仅统计指定日期
.\.venv\Scripts\python.exe -X utf8 main.py audit --tail                         # 实时跟读最新审计文件
.\.venv\Scripts\python.exe -X utf8 main.py audit --run 20260915-090847-152884-r1 # 按 run_id 回放调用序列
```

验收：`docs/audit-log-design.md` §9 六项标准全部通过（六类事件字段齐全、中文输入无明文、按 8 秒间隔切分 `run_id`、CLI 三档可用、MCP 自测只读 7/7 与含写 14/14 通过、`UI_AGENT_AUDIT=0` 时零文件产生）。本机最近一次 `audit --stats` 实测：69 条记录 / 4 个 session / 6 个 run，六类事件齐全（`tool_call` 19、`tool_result` 19、`locate` 13、`action` 6、`run_start` 6、`run_end` 6），失败 0 次，平均耗时 727 ms。

---

## 9. 已知约束（MCP 接入前必读）

- **仅 Windows**：UIA 实现绑定 `uiautomation`，无跨平台分支。
- **OCR 性能**：全屏 2560×1440 单次识别约 3.0 s（首次含模型加载约 0.4 s）；建议接入时对区域裁剪后识别，或先 UIA 命中以避开 OCR。
- **执行层有副作用**：`executor/` 下的鼠标 / 键盘 / 剪贴板为真实操作，自检流程**不会**实例化该层。
- **UIA 不可用场景**（游戏、远程桌面、Canvas 自绘等）只能依赖 OCR，见方案文档降级说明。
- **窗口标题不可靠**：微信 / QQ 等应用窗口标题是登录昵称，判断归属请用 `app_alias` / `process_name`；定位优先用 `hwnd` / `class_name`。
- **隐藏窗口不是"未运行"**：只看 `visible=true` 会误判；隐藏 / 托盘窗口在列表中 `visible=false`，可用 `hwnd` 唤醒激活。
- **置顶窗口遮挡**：置顶窗口会盖住同区域普通窗口；`activate_window` 返回 `covered` / `covered_by` / `hint`，`click` 会自动临时抬升目标窗口并返回 `auto_raise` / `hit_window`。
- **审计日志本地落盘**：`logs\audit-*.jsonl` 含调用参数（已脱敏）与窗口信息，敏感环境用 `UI_AGENT_AUDIT=0` 关闭。
- **CLI stdout 为结构化 JSON**，日志走 stderr，便于 MCP Server 直接以子进程方式包裹。

---

## 10. MCP Server（stdio）

```powershell
.\.venv\Scripts\python.exe serve_mcp.py      # 或 .\.venv\Scripts\python.exe main.py mcp
```

服务标识：`name=ui-agent`、`version=0.2.0`（`uiagent/mcp_server.py`）；Python 包版本 `uiagent.__version__=0.1.0`（`main.py --version` 输出）。

| 工具 | 类型 | 作用 |
| --- | --- | --- |
| `ui_window_list` | 只读 | 枚举顶层窗口 + 环境快照；按应用聚合 `apps[]`，并给出 `hidden_windows[]` / `topmost_windows[]`；每项带 `hwnd` / `visible` / `iconic` / `app_alias` / `class_name` / `process_name` |
| `ui_activate_window` | 写 | 按 `hwnd` / 标题（含应用别名）/ `process_name` 定位并置前，可还原最小化、唤醒隐藏 / 托盘窗口；未命中返回 `hints` + `candidates`，命中后附遮挡诊断 `covered` / `covered_by` |
| `ui_find` | 只读 | 定位元素或界面文字（UIA → OCR 兜底） |
| `ui_click` | 写 | 定位式点击或坐标点击；目标点被置顶窗口遮挡时自动临时抬升目标窗口，返回 `auto_raise` / `hit_window` |
| `ui_type` | 写 | 输入文本（中文自动走剪贴板） |
| `ui_hotkey` | 写 | 发送组合快捷键 |
| `ui_screenshot` | 只读 | 截图落盘（全屏 / 区域） |
| `ui_ocr` | 只读 | 屏幕 OCR（blocks / keyword / text） |

实现要点：所有调用经**专用 COM 单线程**串行执行（`uiagent/mcp_server.py`），stdout 仅承载 JSON-RPC，日志走 stderr；`_guard` 在出口把 `tool_call` / `tool_result` 写入 `logs\audit-*.jsonl`（详见第 8 节），不影响工具签名与返回结构。

协议自测：

```powershell
.\.venv\Scripts\python.exe -X utf8 selfcheck\mcp_selfcheck.py                 # 只读链路
.\.venv\Scripts\python.exe -X utf8 selfcheck\mcp_selfcheck.py --include-write # 追加记事本端到端
```

最近实测：`initialize` 握手 `2025-11-25`、`tools/list` 返回全部 8 个工具、`tools/call` 覆盖只读与写链路，**14/14 检查通过（passed=true，16.08 s）**。

完整接入配置、逐工具参数表与故障排查见 [`docs/mcp-server.md`](docs/mcp-server.md)。
*（内容由AI生成，仅供参考）*
