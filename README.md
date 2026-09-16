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

定位策略为三级降级：**UIA 精确定位 → OCR 文字定位兜底 → 返回失败**。内核已通过 **MCP Server（stdio 传输）** 对外暴露 13 个 `ui_*` 工具，可供 CowAgent / Marvis 等 MCP 客户端直接接入；接入配置、工具参数与协议自测结论见 [`docs/mcp-server.md`](docs/mcp-server.md)；审计日志（调用链 / 定位方式 / 写操作明细）设计见 [`docs/audit-log-design.md`](docs/audit-log-design.md)。

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
│   ├── mcp_server.py                    # MCP Server：13 个 ui_* 工具 + COM 专用线程调度 + tool_call/tool_result/ensure/launch 埋点
│   ├── launcher.py                      # P2 启动链路：resolve / launch / wait_window（白名单 + dry_run + 别名表）
│   ├── apps.yaml                        # P2 别名表（别名 → 可执行路径 / AUMID / 分级），可选、可删
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
ctrl.ensure_app(app="微信")               # P1 统一入口：状态判定(S1~S4) → 唤醒 → 就绪确认 → 置前
ctrl.app_status(["微信", "记事本"])        # P1 只读：批量状态查询（visible/minimized/hidden/not_running）

# ---- 启动能力（P2；写操作，受 UIAGENT_LAUNCH_ENABLED / UIAGENT_LAUNCH_ALLOWLIST 约束：
#      白名单默认启用，未配置 env 时套用内置默认白名单，不含 cmd / powershell / 终端）----
ctrl.resolve_app("计算器")                 # 只读：只解析入口（alias_table → 注册表 App Paths → PATH → 开始菜单 → UWP）
ctrl.launch_app("计算器", dry_run=True)    # 预演：只解析、不产生任何进程
ctrl.launch_app("记事本", timeout=15.0)    # 启动 + 等窗口就绪 → pid / hwnd / resolved_by / launch_ms / wait_ms
ctrl.wait_app_window("记事本", timeout=5.0) # 只等窗口就绪（pid / app 双线索，含打包应用别名兜底）

from uiagent.controller import window_miss_report
window_miss_report(title="微信")           # 未命中时返回 hints + candidates + hidden_apps 诊断

# ---- 写操作（有副作用，谨慎调用）----
ctrl.click("确定")                        # locate 后点击（被置顶窗口遮挡时自动临时抬升目标窗口）
ctrl.type_chinese("你好")                 # 剪贴板粘贴，规避输入法问题
ctrl.hotkey("ctrl", "s")
```

审计日志（`uiagent/audit.py`）：默认开启，JSONL 落盘 `logs\`；输入文本默认只落长度 + 摘要 + 掩码预览。

启动链路（P2，改动点 #16）环境开关：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `UIAGENT_LAUNCH_ENABLED` | `1` | 启动能力总开关；设 `0` 时 `resolve_app`（只读解析）仍可用，但 `launch_app` / `ui_launch_app` 一律拒绝（`degraded_reason=launch_disabled`，含 `dry_run`）；`ui_wait_window` 属只读等待，**不受**该开关限制 |
| `UIAGENT_LAUNCH_ALLOWLIST` | 未设置 → **内置默认白名单** | 允许启动的白名单（逗号分隔，别名 / 目标文件名，大小写不敏感）。①**未设置 / 空白** → 套用内置默认白名单 `DEFAULT_LAUNCH_ALLOWLIST`（记事本、计算器、画图、资源管理器、Edge、微信等常用应用，**不含** `cmd` / `powershell` / 终端等命令解释器）；②**显式配置** → 以配置为准，整体替换默认白名单；③**显式设为 `*` / `all`** → 不校验（显式解除限制）。不在生效白名单内 → `degraded_reason=not_allowlisted`（`hint` 标注来源与放开方式）并落审计告警 |

> 两者**均为进程启动前置校验**：拒绝路径已用 `tasklist` 前后快照验证零进程产生（见第 7 节）。`resolve_app` 是只读解析，不受开关限制，可随时用于预演（等价于 `dry_run=true`）。

关闭链路（关闭可靠性根治）环境开关：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `UIAGENT_CLOSE_FALLBACK` | `alt_f4` | `ui_close` 的**键盘回退开关**：`alt_f4` → 程序化 `WM_CLOSE` 直投后若句柄仍在、且**无模态框**、**前台核验通过**（`GetForegroundWindow()==hwnd`）时补一次 `alt+f4`；`off` → **键盘通道硬开关**：不发任何关闭按键，显式 `method="alt_f4"` 亦降级回 `WM_CLOSE` 并标注 `degraded_reason=close_fallback_disabled`（绝不空转）；单次调用可用 `ui_close(fallback=…)` 覆盖 |

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
| 四状态唤起（P1） | `ensure_app(app=…)` / `ui_app_ensure` 按状态分层：S1 `visible` 直接置前（零等待）、S2 `minimized` 走 `SW_RESTORE` + bounds 稳定等待、S3 `hidden` 走 `SW_SHOW` + bounds 稳定等待；S4 未运行如实上报并返回 `candidates`，且可交由 P2 启动链路自动接管；`UIAGENT_ENSURE_V2=0` 可回退 `find_window` + `activate_window` 旧链路（`fallback="legacy"`：命中窗口 `state="unknown"`、未命中不带 `candidates`），已用同一窗口实测开关差分 |
| 启动未运行应用（P2） | `resolve_app` / `launch_app` / `wait_app_window`（MCP：`ui_launch_app` / `ui_wait_window`）：解析链 `apps.yaml` 别名表 → 注册表 `App Paths` → `PATH` → 开始菜单 `.lnk` → UWP AUMID（Win 键搜索兜底本版本未实现）；`dry_run=True` 只预演不产生进程；`UIAGENT_LAUNCH_ENABLED=0` 一键禁用；`UIAGENT_LAUNCH_ALLOWLIST` **默认启用安全白名单**（未配置时套用内置默认白名单：常用应用，不含 `cmd` / `powershell` / 终端等命令解释器；显式配置以配置为准；显式设为 `*` / `all` 不校验）；解析出多候选**不自动启动**（返回 `candidates` 由上层指定） |
| 遮挡诊断 | `probe_window_cover` 判断目标点是否被置顶窗口盖住（返回 `covered` / `covered_by` / `hint`）；坐标点击前自动**临时抬升**目标窗口 Z 序（`ensure_reachable`），点击后立即 `restore_after_boost` 复原，并在结果中返回 `auto_raise` / `hit_window` |
| 性能 | 枚举阶段纯本地构造、不触碰 UIA（避免跨进程 COM 阻塞），仅在激活 / 定位具体窗口时按 `hwnd` 懒加载原生控件 |

本机实测（2026-09-15）：`list_windows()` 返回 19 个窗口（7 可见 / 12 隐藏），单次耗时约 10 ms；隐藏窗口带 `visible=false` 与 `app_alias`，可按 `hwnd` 激活。P1 `ensure_app` 四状态实测（记事本，每状态 8 轮采样）：S1 `visible` P50 10.3 ms / P95 14.2 ms、S2 `minimized` P50 195.6 ms / P95 208.7 ms、S3 `hidden` P50 181.4 ms / P95 183.3 ms，均在 150 / 650 / 900 ms 预算内；S4 未运行 P50 43.0 ms（返回 8 个 `candidates`，不启动进程）。

P2 冷启动实测（2026-09-15，均为"先关闭 → 内核发起启动 → 等窗口就绪"完整链路，`cold_ms = launch_ms + wait_ms`）：

| 应用 | 解析档位 | `launch_ms` | `wait_ms` | `cold_ms` | 窗口匹配 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 记事本 `notepad.exe` | `alias_table` | 54.6 ms | 973.0 ms | **1027.6 ms**（wall 1029.6） | `app_fallback` | `state=visible`，`hwnd=2034674`，`ui_app_ensure` 命中同一 `hwnd`（10.3 ms） |
| 计算器 `计算器` → `calc.exe`+AUMID | `alias_table` | 23.4 ms | 1021.7 ms | **≈1045 ms** | `app_fallback` | `state=visible`，`hwnd=393928`，`ui_wait_window(app)` 187 ms |
| 画图 `画图` → `mspaint.exe` | `alias_table` | 90.1 ms | 494.6 ms | **≈585 ms** | `pid` | `state=visible`，`hwnd=264218`，`ui_wait_window(app)` 163 ms |

三个轻量应用均落入方案 4.2 轻量级 SLA（≤1.5 s），`launch_ms` 均 ≤300 ms（S4 发起段预算）。注意：计算器 / 画图（Store 打包应用）的可见窗口多数归 `ApplicationFrameHost.exe`，仅按 `pid` 匹配不到，由别名表兜底键命中（`matched_by=app_fallback`、`fallback=true`，`degraded_reason=pid_window_missing`），这也是 `ui_wait_window` 支持 `app` 线索的原因。

解析链分段实测（同为 2026-09-15）：`apps.yaml` 别名表首次调用 11.8 ms（承担表加载）、缓存命中 0.009~0.029 ms；开始菜单 128 项 `.lnk` 首次冷索引 2.1 ms、TTL 300 s 内缓存命中 0.002 ms；其他来源 `regedit`（`app_paths`）0.58 ms、`任务管理器`（`uwp`）2.79 ms、`powershell.exe`（`alias_table`）0.02 ms；未安装应用（`chrome` / `winword`）走完全链返回 `ok=false` + `hint`（`chrome` 388 ms），均无残留进程。

启动安全闸验证（逐项比对 `tasklist` 前后快照，断言**零进程产生**）：

| 用例 | 环境 | 结果 |
| --- | --- | --- |
| 开关禁用 + 真实启动请求 | `UIAGENT_LAUNCH_ENABLED=0` | `ok=false` / `pid=0` / `degraded_reason=launch_disabled` / 无进程 |
| 开关禁用 + `dry_run` | 同上 | 同上（`dry_run` 也不放行） |
| 白名单不匹配 + 真实启动请求 | `UIAGENT_LAUNCH_ALLOWLIST=notepad.exe`，请求 `计算器` | `ok=false` / `degraded_reason=not_allowlisted` / 无进程 |
| 白名单匹配 → 放行 | `ALLOWLIST=notepad.exe`，请求 `记事本` | `ok=true` / `state=dry_run`（预演） |
| 白名单大小写不敏感 | `ALLOWLIST=NOTEPAD`，请求 `notepad.exe` | `ok=true` / `state=dry_run`（误拒为 0） |
| 默认白名单生效（未配置 env） | 不设 `UIAGENT_LAUNCH_ALLOWLIST`，请求 `记事本` | `ok=true` / `state=dry_run`（`allowlist_source=default`） |
| 默认白名单拦截命令解释器 | 不设 `UIAGENT_LAUNCH_ALLOWLIST`，请求 `cmd` | `ok=false` / `not_allowlisted` / 无进程（`hint` 给出放开方式） |
| 显式配置覆盖默认白名单 | `ALLOWLIST=weixin.exe,微信`，请求 `记事本` | `ok=false` / `not_allowlisted`（默认条目不再放行） |
| 显式解除限制入口 | `ALLOWLIST=*`，请求 `cmd`（`dry_run=true`） | `ok=true` / `state=dry_run`（不校验，仅解析） |
| 别名 / 空格 / 大小写归一 | `ALLOWLIST=" 记事本 , NOTEPAD "`，请求 `notepad.exe` | `ok=true` / `state=dry_run`（条目去空格 + 忽略大小写） |

---

## 8. 审计日志（audit log）

设计文档：[`docs/audit-log-design.md`](docs/audit-log-design.md)。用途：记录"谁在何时调用了什么、用了哪种定位方式、点了哪里、命中了哪个窗口"，便于调试与回归分析。**不改变任何工具签名与返回结构**，不写 stdout。

- 载体：`uiagent/audit.py`，JSONL 落盘 `logs\audit-YYYYMMDD-<pid>.jsonl`（`logs\` 已 gitignore）；单文件超 10 MB 轮转为 `<同名>.1`，启动时清理超过保留天数的旧文件。
- 八类事件：`tool_call` / `tool_result`（MCP 层，覆盖全部 13 个工具）、`locate`（定位明细：`source=uia|ocr|coords`、`fallback`、`uia_ms` / `ocr_ms`、候选数，以及 P0 的 `scope` / `scope_hwnd` / `cached`）、`ensure`（P1 应用唤起：`app` / `state` / `hwnd` / `activated` / `cached` / `ensure_ms` / `timing` / `degraded` / `degraded_reason`）、`launch`（P2 启动链路，已落盘：`app` / `resolved_by` / `target_path` / `pid` / `launch_ms` / `wait_ms` / `dry_run` / `degraded_reason`；被开关或白名单拒绝时同样落盘 `degraded_reason`）、`action`（写操作：点击坐标、`hit_window`、`auto_raise`、输入摘要）、`run_start` / `run_end`（按静默间隔切分的任务回放单元）。
- 脱敏（默认 `mask`）：只落 `input_len` + `input_sha256[:8]` + 首字符掩码预览（如 `老***`），**不落明文**；`hash` 只留长度与摘要，`full` 仅本地深度调试时使用。
- 埋点：`mcp_server.py` 的 `_guard`、`controller.py` 的 `locate()` / `locate_many()`、`controller.py` 的 `ensure_app()`（P1 新增 `ensure` 事件）、`controller.py` 的 `launch_app()` / `wait_app_window()`（P2 新增 `launch` 事件）、`controller.py` 的写操作方法（`click` / `type_text` / `type_chinese` / `hotkey` / `activate_window`）。

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

验收：`docs/audit-log-design.md` §9 六项标准全部通过（六类事件字段齐全、中文输入无明文、按 8 秒间隔切分 `run_id`、CLI 三档可用、`UI_AGENT_AUDIT=0` 时零文件产生）。P1 后事件类别扩为八类（新增 `ensure`）；P2 后 `launch` 事件由"仅定义契约"转为**真实落盘**（`launch_app` / `wait_app_window`，含被开关 / 白名单拒绝的路径）；MCP 自测 **29/29**（只读 12/12 + 含写 17 项，含 P2 冷启动用例）通过。本机最近一次 `audit --stats` 实测：69 条记录 / 4 个 session / 6 个 run，六类事件齐全（`tool_call` 19、`tool_result` 19、`locate` 13、`action` 6、`run_start` 6、`run_end` 6），失败 0 次，平均耗时 727 ms。

---

## 9. 已知约束（MCP 接入前必读）

- **仅 Windows**：UIA 实现绑定 `uiautomation`，无跨平台分支。
- **OCR 性能**：全屏 2560×1440 单次识别约 3.0 s（首次含模型加载约 0.4 s）；建议接入时对区域裁剪后识别，或先 UIA 命中以避开 OCR。
- **执行层有副作用**：`executor/` 下的鼠标 / 键盘 / 剪贴板为真实操作，自检流程**不会**实例化该层。
- **UIA 不可用场景**（游戏、远程桌面、Canvas 自绘等）只能依赖 OCR，见方案文档降级说明。
- **窗口标题不可靠**：微信 / QQ 等应用窗口标题是登录昵称，判断归属请用 `app_alias` / `process_name`；定位优先用 `hwnd` / `class_name`。
- **隐藏窗口不是"未运行"**：只看 `visible=true` 会误判；隐藏 / 托盘窗口在列表中 `visible=false`，可用 `hwnd` 唤醒激活。
- **置顶窗口遮挡**：置顶窗口会盖住同区域普通窗口；`activate_window` 返回 `covered` / `covered_by` / `hint`，`click` 会自动临时抬升目标窗口并返回 `auto_raise` / `hit_window`。
- **输入法会拦截"纯 ASCII 串"的键盘输入**：`ui_type` 的 `auto` 分流是**整串级**——含中文走剪贴板，**纯 ASCII 串（如 `a1b2c3`）走键盘注入**，中文输入法激活时会被组字 / 候选吞掉或串码（实测 `a1b2c3` → `啊靶场`）。需要精确落字时显式 `method="clipboard"`，或先切英文输入法；剪贴板模式会**覆盖系统剪贴板**（内核不备份 / 恢复），输入后应回读校验（`chars` 只是发出字符数）。
- **键盘类写操作前必须先取前台焦点**：`ui_type` / `ui_hotkey` 把按键发给**当前前台窗口的焦点控件**，二者都不负责置焦；`ctrl+s` / `alt+f4` 前先用 `ui_activate_window(hwnd=…)` 或 `ui_app_ensure(require_foreground=true)`，并复核 `activated` / `state`（`degraded_reason=foreground_locked` 表示窗口已唤醒但未占前台，此时发键可能落到别的窗口）。
- **保存 / 关闭不能用 `ok=true` 判定**：`ctrl+s` 后回读磁盘（文件内容 / `mtime`）确认落盘（无路径的新文档会弹「另存为」）；关闭一律用 **`ui_close`**（程序化 `WM_CLOSE` 直投 + 轮询收敛，不依赖按键送达），成功以 **`hwnd` 消失**为准（`user32.IsWindow(hwnd)==false`，**不要求进程退出**）；遇未保存确认框等模态框时返回 `blocked=true` + `dialogs[]`，交上层决策、不盲重试。`alt+f4` 仅作条件化回退（`UIAGENT_CLOSE_FALLBACK` 可硬关）。仅给 `app` 时按**应用**解析目标窗口（别名 / 进程名，不再把 `app` 当标题）；应用当前无窗口（未运行或已全部关闭）返回 `ok=false` + `hints`，**属预期语义而非关闭失败**。
- **`ui_app_status` 的 `state` 是"应用最优窗口"口径**：`select_best_window` 取「可见未最小化 > 可见已最小化 > 隐藏、同档面积最大」的窗口，多窗口应用隐藏其中一个后应用级 `state` 仍可能为 `visible`（**口径使然，不是缓存陈旧**）；判断特定窗口请按 `hwnd` 查 `candidates[].state`，或用 `ui_window_list(include_invisible=true)`。
- **`AppContext` 缓存（`UIAGENT_CTX_TTL`，默认 30 s）只缓存"窗口线索"，不缓存状态**：`ui_app_status` 的 `state` 每次调用由实时枚举推导（`EnumWindows` + `IsWindowVisible` / `IsIconic` / cloaked），因此该变量**无法改善状态新鲜度**；`cached` / `cached_hwnd` / `cached_age_ms` 属历史线索，不得用于状态 / 关闭裁决。设 `UIAGENT_CTX_TTL=0` 只会关闭"窗口搜索域复用"（`ui_find` / `ui_click` 退化为桌面级定位、`ui_app_ensure` 每次重新枚举），属性能回退；个别调用想不读不写缓存可用 `reuse_ttl=0`。完整调用约定见 [`docs/mcp-server.md`](docs/mcp-server.md) §7.1。
- **启动能力（P2）会真实拉起进程**：`ui_launch_app` / `ui_wait_window` 默认启用（`UIAGENT_LAUNCH_ENABLED=1`）；`UIAGENT_LAUNCH_ENABLED=0` 时整条链路直接拒绝（`degraded_reason=launch_disabled`，含 `dry_run`）；`UIAGENT_LAUNCH_ALLOWLIST` **默认启用安全白名单**：未设置 / 空白时套用内置默认白名单（只含记事本、计算器、画图、资源管理器、Edge、微信等常用应用，**不含** `cmd` / `powershell` / 终端等命令解释器），显式配置时以配置为准（可放开默认之外的入口），显式设为 `*` / `all` 表示不校验（显式解除限制）；不在生效白名单内 → `degraded_reason=not_allowlisted`，返回体 `hint` 标注拒绝来源与放开方式。建议先用 `dry_run=true` 预演解析结果再真实启动。`ensure_app` / `ui_app_ensure` 自身不启动进程，`launch_if_missing=true` 才交由启动链路接管；`degraded=true` 表示未达最优就绪状态而非失败。
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
| `ui_window_list` | 只读 | 枚举顶层窗口 + 环境快照；按应用聚合 `apps[]`（带 `state` / `states`），并给出 `hidden_windows[]` / `topmost_windows[]`；每项带 `hwnd` / `visible` / `iconic` / `state` / `app_alias` / `class_name` / `process_name` |
| `ui_activate_window` | 写 | 按 `hwnd` / 标题（含应用别名）/ `process_name` 定位并置前，可还原最小化、唤醒隐藏 / 托盘窗口（`wait_ready` 控制唤醒后是否等 bounds 稳定）；未命中返回 `hints` + `candidates`，命中后附遮挡诊断 `covered` / `covered_by` + `state` / `timing` |
| `ui_app_ensure` | 写 | 统一应用唤起入口（P1）：S1~S4 状态判定 → 唤醒 / 就绪等待 → 置前，返回 `state` / `state_before` / `ready` / `timing` / `reachable`；S4 返回 `candidates`，`launch_if_missing=true` 时由 P2 启动链路自动冷启动 |
| `ui_app_status` | 只读 | 批量查询应用状态（`visible` / `minimized` / `hidden` / `cloaked` / `not_running`）+ 候选窗口与 `AppContext` 缓存信息 |
| `ui_launch_app` | 写 | 启动应用并等窗口就绪（P2，**高风险：真实创建进程**）：解析链 `apps.yaml` 别名表 → 注册表 `App Paths` → `PATH` → 开始菜单 `.lnk` → UWP；返回 `state` / `hwnd` / `pid` / `resolved_by` / `target_path` / `launch_ms` / `wait_ms`；`dry_run=true` 只预演；`launch_method=auto\|exec\|process\|uwp`；超时返回 `state="launching"`（非失败） |
| `ui_wait_window` | 只读 | 等待窗口出现并完成布局（P2）：三选一定位 `hwnd` > `pid` > `app`（`app` 支持别名 / 进程名，含打包应用兜底键），返回 `state` / `waited_ms` / `stable`；替代上层"睡眠 + 反复轮询" |
| `ui_find` | 只读 | 定位元素或界面文字（UIA → OCR 兜底） |
| `ui_click` | 写 | 定位式点击或坐标点击；目标点被置顶窗口遮挡时自动临时抬升目标窗口，返回 `auto_raise` / `hit_window` |
| `ui_type` | 写 | 输入文本（含中文自动走剪贴板；**纯 ASCII 串走键盘注入，中文输入法激活时会被 IME 吞字 / 串码**，需精确落字时显式 `method="clipboard"`） |
| `ui_hotkey` | 写 | 发送组合快捷键（发给**当前前台窗口**的焦点控件，关闭窗口请改用 `ui_close`） |
| `ui_close` | 写 | 关闭窗口（关闭可靠性根治新增）：把 `WM_CLOSE` **直接投递到目标窗口**并轮询等句柄消失（`method="wm_close"` 默认；`alt_f4` 为条件化回退）；成功以 `closed`（`hwnd` 消失）为准，**不要求进程退出**；遇未保存确认框等模态框返回 `blocked=true` + `dialogs[]` 交上层决策；目标解析优先级 `hwnd` > `target`（标题）> `app`（**应用解析**：别名 / 进程名，与 `ui_app_status` 同源，不再把 `app` 误当标题），应用当前无窗口时返回 `ok=false` + `hints`（属预期语义，非关闭失败） |
| `ui_screenshot` | 只读 | 截图落盘（全屏 / 区域） |
| `ui_ocr` | 只读 | 屏幕 OCR（blocks / keyword / text） |

实现要点：所有调用经**专用 COM 单线程**串行执行（`uiagent/mcp_server.py`），stdout 仅承载 JSON-RPC，日志走 stderr；`_guard` 在出口把 `tool_call` / `tool_result` 写入 `logs\audit-*.jsonl`（详见第 8 节），不影响工具签名与返回结构。

协议自测：

```powershell
.\.venv\Scripts\python.exe -X utf8 selfcheck\mcp_selfcheck.py                 # 只读链路
.\.venv\Scripts\python.exe -X utf8 selfcheck\mcp_selfcheck.py --include-write # 追加记事本端到端
```

最近实测（2026-09-16 04:10，含 C0 目标解析修复）：`initialize` 握手 `2025-11-25`、`tools/list` 返回全部 13 个工具、`tools/call` 覆盖只读与写链路（含 P1 四状态用例与 P2 冷启动用例），**29/29 检查通过（passed=true，18.49 s）**；只读链路单独跑 **12/12（passed=true，7.05 s，2026-09-15 采集）**。P2 冷启动段实测（MCP 真实链路，记事本）：`ui_launch_app` `launch_ms=42.96` / `wait_ms=986.77`（`cold_ms=1029.7`，轻量级 SLA ≤1.5 s 通过）、`state=visible` / `hwnd=12453098` / `resolved_by=alias_table`；随后 `ui_app_ensure` 命中同一 `hwnd`（14.9 ms），`ui_wait_window(pid+app)` 678.2 ms 命中；**P2 前置关闭用例（`ui_close` 关闭记事本后 `ui_app_status` 复核 `state=not_running`）PASS**（修复前因 `app` 被误当标题匹配而连轮 `ok=false`、用例被 SKIP）；`ui_launch_app(dry_run=true)`（`pid=0`）与 `ui_wait_window` 超时（返回 `state=launching`）均在只读链路断言通过。P1 `ui_app_ensure` 四状态实测（记事本，8 轮采样，2026-09-15 采集）：S1 `visible` P50 10.3 ms / P95 14.2 ms、S2 `minimized` P50 195.6 ms / P95 208.7 ms、S3 `hidden` P50 181.4 ms / P95 183.3 ms，均在 150 / 650 / 900 ms 预算内；S4 未启动 P50 43.0 ms，返回 8 个 `candidates` 与 `degraded_reason=app_not_running`。

关闭可靠性回归（真机，2026-09-16）：新链路 `ui_close`（`WM_CLOSE` 直投 + 句柄消失轮询）**50/50 首轮关闭成功**（记事本 25 + 计算器 25，0 补发、0 `blocked`）；C0 修复后复验 **30/30**（记事本 15 + 计算器 15）；改造前同环境 `alt+f4` 拼装链路 **0/20** 首轮成功（问题在送达 / 判定窗，非保存失败）。

完整接入配置、逐工具参数表与故障排查见 [`docs/mcp-server.md`](docs/mcp-server.md)。

---

## 11. 待开发项

> **维护约定**：本清单只登记**当前未完成**的事项；**每完成一项即从清单中删除**，保持清单与实际状态一致（完成记录请落到对应提交信息与 `docs/` 下的设计文档，不在此留痕）；新增项须写清分档理由与来源。
>
> 分档口径：**必做** = 影响结果正确性或数据安全；**建议做** = 影响覆盖面与性能、且有可校验的目标值；**可做** = 样本补充、长稳灰度，或已明确为设计取舍的项。

### 必做（影响正确性 / 数据安全）

| # | 待开发项 | 一句话说明 | 来源 |
| --- | --- | --- | --- |
| — | 暂无 | 「保存 / 关闭可靠性根治」已于 2026-09-16 落地（新增 `ui_close`：程序化 `WM_CLOSE` 直投 + 句柄消失判定 + 模态框阻断上报 + `app` 应用级目标解析（C0，修复自测中 `app` 被误当标题导致的关闭 miss）；真机 50/50 首轮关闭成功、C0 修复后复验 30/30，改造前同环境 0/20） | — |

### 建议做（覆盖面与性能）

| # | 待开发项 | 一句话说明 | 来源 |
| --- | --- | --- | --- |
| 2 | Win 键搜索兜底（RF8） | 解析链未覆盖「开始菜单搜索」，未经索引的应用（如 `chrome` / `winword`）无法解析启动，需补 Win 搜索兜底 | P2 启动能力实测：解析链为 `apps.yaml` → 注册表 `App Paths` → `PATH` → 开始菜单 `.lnk` → UWP |
| 3 | 重量级应用冷启动实测 | 当前只标定了轻量级 SLA（≤1.5 s），微信 / Office / IDE 的 `heavy ≤ 20 s` 目标尚未用实测校准 | P2 就绪等待预算（heavy 档为占位值） |
| 4 | OCR 校验性能优化 | 记事本链路 OCR 校验单次 1903 ms、占该链路总耗时 58.4%，是剩余的大头；方向为区域裁剪、UIA 优先命中或降采样 | P0~P2 端到端提速验证：单步耗时定位 |
| 5 | 记事本 unicode 直落残留换位 | 记事本档 unicode 通道实测 78/80（97.5%；另有 79/80 一次），2 例失败为相邻 `1` 与 `-` 换位（`bench-11-ok`→`bench1-1-ok`、`abc-123`→`abc1-23`），字符集合无损、仅顺序微错；30 轮双通道真值探针未能复现，疑为间歇性焦点 / 合成时序竞态，根因未定位。分档理由：有可校验目标值（unicode 档 100%） | 输入法吞字根治落地：M3 矩阵（记事本 20 轮 × 4 单元格 × 4 通道） |
| 14 | 关闭能力在重量级 / 多窗口应用的样本覆盖 | 关闭回归目前只覆盖轻量单窗口应用（记事本 25 + 计算器 25，均 50/50）；微信（多窗口 + 托盘）、Qt / 浏览器多窗口、IDE 等「关一个窗口 ≠ 退出应用」的形态尚未实测，`select_best_window` 与 `hwnd` 生命周期的交互需补样本 | 关闭可靠性根治：真机回归样本盘点 |
| 15 | `alt+f4` 回退通道的送达回执 | 程序化 `WM_CLOSE` 已是默认通道，但 `method="alt_f4"` 回退仍只做「发键前前台核验」；「核验通过但键落到别处」这一残余风险无回执可证，可考虑投递前后各记一次 `GetForegroundWindow()` 与目标 `hwnd` 一并返回 | 关闭可靠性根治：只读排查结论（原 `2/20` 卡点归因） |

### 可做（样本、灰度与设计取舍）

| # | 待开发项 | 一句话说明 | 来源 |
| --- | --- | --- | --- |
| 6 | UWP `cloaked` 状态补样本 | `cloaked`（UWP 挂起）状态缺真实样本，判定路径未受压测 | P0~P2 提速验证样本盘点 |
| 7 | 3 天长稳灰度 | 目前只有单次端到端与自测数据，缺连续运行的长稳观测 | P1 / P2 上线后的稳定性验证计划 |
| 8 | `args` / `working_dir` 注入压测 | 二者目前无拦截；**将来若放开启动白名单，必须先补这项压测** | 启动白名单安全默认化改造的只读核查结论 |
| 9 | 四状态样本量由 8 扩到 20 | 每状态 8 轮采样的分位数（P50 / P95）波动区间偏宽，扩到 20 轮才能稳定支撑预算判定 | P1 `ui_app_ensure` 四状态实测 |
| 10 | 打包应用 `pid` 线索失效 | 打包（UWP / MSIX）应用拿到的 `pid` 无法用于 `ui_wait_window` 定位，**已有 `app_fallback` 兜底**，属设计取舍，暂不改 | P2 启动链路实测 |
| 11 | 多候选不自动启动（RF5） | 命中多个候选时不自动选一个启动，避免误启无关应用，**属刻意设计，暂不处理** | P1 `ui_app_ensure` 设计决策 |
| 12 | 冷启动就绪等待 980 ms 优化 | 就绪等待实测约 980 ms，绝大部分是**外部应用自身初始化耗时**，优化空间有限，暂不动 | P2 冷启动实测（`wait_ms≈966`） |
| 13 | unicode 通道应用覆盖缺口 | VS Code（窗口可捕获但未定位到编辑区可读节点）、Windows Terminal（未定位文本元素）、微信（未识别输入框）三者的 unicode 通道未实测；Chrome 未安装，最终矩阵仅覆盖记事本与 Edge。分档理由：属样本 / 覆盖补充，不影响已实测链路正确性 | 输入法吞字根治落地：M3 侦察与最终矩阵 |
| 16 | 模态框阻断后由内核代处置 | 遇未保存确认框时 `ui_close` 只做**只读诊断 + 阻断上报**（`blocked=true` + `dialogs[]`），不代点按钮；**属刻意设计**（不替用户决定「保存 / 丢弃」），保留上层决策权 | 关闭可靠性根治：C3 阻断语义设计 |

*（内容由AI生成，仅供参考）*
