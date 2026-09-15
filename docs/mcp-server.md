---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 8ba5b382cadd141b612ac8623b46555a_27ab37edb04b11f18f50525400aeaaa3
    ReservedCode1: By4dUSaWkDHSmex/RagbjWl6M2iof/bpBjUafFJjm49T2sZ/YnTYdni996Lgi2jV0f0gTaGJMNPx+b9LiF5r/jfZx+6XDFDWRI6KwZzYU0JQ9vNg9TrljbVKoJusstFxEBWDAMFsx3Kdtp3MuEeczlfhEa+VKUWalKM0hFpCCxa70QDiv7pOHBU5ebo=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 8ba5b382cadd141b612ac8623b46555a_27ab37edb04b11f18f50525400aeaaa3
    ReservedCode2: By4dUSaWkDHSmex/RagbjWl6M2iof/bpBjUafFJjm49T2sZ/YnTYdni996Lgi2jV0f0gTaGJMNPx+b9LiF5r/jfZx+6XDFDWRI6KwZzYU0JQ9vNg9TrljbVKoJusstFxEBWDAMFsx3Kdtp3MuEeczlfhEa+VKUWalKM0hFpCCxa70QDiv7pOHBU5ebo=
---

# ui-agent MCP Server

把 `D:\Projects\ui-agent` 的 UIA + OCR 操控内核包装为 **基于 stdio 的本地 MCP Server**，向 MCP 客户端（CowAgent / Marvis / Claude Desktop 等）暴露 8 个 `ui_*` 工具，实现窗口枚举、元素定位、鼠标点击、键盘输入、截屏与 OCR 能力。

- 传输方式：`stdio`（JSON-RPC 2.0 over stdin/stdout）
- 协议版本：`2025-11-25`（由 `mcp` 2.x SDK 协商）
- 服务标识：`name=ui-agent`、`version=0.2.0`
- 坐标约定：**所有对外坐标均为屏幕绝对物理像素**（进程启动即 `SetProcessDpiAwareness(2)`）

---

## 1. 架构

```
MCP 客户端（CowAgent / Marvis）
        │  JSON-RPC over stdio
        ▼
uiagent/mcp_server.py  ── MCPServer（8 个 ui_* 工具，async）
        │  asyncio.to_thread
        ▼
COM 专用工作线程 _UiThread（单线程 STA，串行执行）
        │
        ▼
UniversalController（内核门面）
   ├── WindowsAccessibility（UIA 精确查找）
   ├── RapidOCREngine（OCR 文字定位兜底）
   └── executor/{mouse,keyboard,clipboard}（真实鼠标 / 键盘 / 剪贴板）
```

关键工程决策：

| 决策 | 原因 |
| --- | --- |
| 专用 COM 线程（daemon + 阻塞队列） | UIA 依赖 COM 单线程套间，且 `uiautomation` 缓存的元素引用与创建线程绑定；线程与进程同生命周期可保证引用始终有效 |
| 所有工具串行执行 | GUI 是单点资源，并发调用会争抢焦点与 Z 序 |
| 日志固定走 stderr | stdout 被 JSON-RPC 独占，任何杂散输出都会破坏协议 |
| DPI 感知最先执行 | 保证 UIA 边界矩形 / 截图 / 鼠标三套坐标同源（物理像素） |

---

## 2. 启动

```powershell
cd D:\Projects\ui-agent
$env:PYTHONUTF8=1

# 方式一：独立入口
.\.venv\Scripts\python.exe serve_mcp.py

# 方式二：CLI 子命令（等价）
.\.venv\Scripts\python.exe main.py mcp
```

> 两种方式均会阻塞并等待客户端握手。手动直接运行会看到 stdin 等待，属正常现象，按 `Ctrl+C` 结束。

### 环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `UI_AGENT_LOG_LEVEL` | `INFO` | 内核日志级别（输出到 stderr） |
| `UI_AGENT_WARMUP` | `1` | 启动时预热 COM 线程与 UIA 内核（`0` 关闭） |
| `UI_AGENT_CALL_TIMEOUT` | `120` | 单次内核调用超时（秒），超时返回 `ok=false` 错误对象 |
| `UI_AGENT_SEARCH_TIMEOUT` | `2.0` | UIA 元素查找超时（秒） |
| `UI_AGENT_OUTPUT_DIR` | 系统临时目录下 `ui-agent\shots` | `ui_screenshot` 不传 `path` 时的落盘目录 |

---

## 3. 客户端接入配置

```json
{
  "mcpServers": {
    "ui-agent": {
      "command": "D:\\Projects\\ui-agent\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\ui-agent\\serve_mcp.py"],
      "env": {
        "PYTHONUTF8": "1",
        "UI_AGENT_LOG_LEVEL": "WARNING"
      }
    }
  }
}
```

要点：

- `command` 必须是 **venv 解释器**（`python` 命令指向 Marvis 内置运行时，缺依赖）；
- 路径使用双反斜杠转义；`args` 指向 `serve_mcp.py` 绝对路径；
- 客户端需以 UTF-8 读写 stdio 报文。

---

## 4. 工具清单

| 工具 | 类型 | 作用 |
| --- | --- | --- |
| `ui_window_list` | 只读 | 枚举顶层窗口（标题 / 类名 / bounds / center / 进程 / 应用别名 / 可见性 / 置顶 / 是否前台）+ 隐藏窗口与按应用聚合 + DPI 环境快照 |
| `ui_activate_window` | 写 | 按 `hwnd` / 标题 / 进程名查找窗口并置为前台（可还原最小化、可唤醒托盘窗口） |
| `ui_find` | 只读 | 定位元素或界面文字（UIA 优先 → OCR 兜底），返回坐标与来源 |
| `ui_click` | 写 | 点击目标：`target` 定位式点击，或直接给 `x`/`y` 坐标；被置顶窗口遮挡时自动临时抬升目标窗口 |
| `ui_type` | 写 | 向焦点处输入文本（中文自动改走剪贴板） |
| `ui_hotkey` | 写 | 发送组合快捷键，如 `ctrl+s`、`alt+f4` |
| `ui_screenshot` | 只读 | 截屏（全屏或指定区域）并保存为图片文件 |
| `ui_ocr` | 只读 | 屏幕 OCR：文本块清单 / 关键词命中坐标 / 整屏文本 |

### 4.1 `ui_window_list`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `include_invisible` | bool | `false` | 是否连同辅助 / 工具 / 消息类不可见窗口一起返回。**注意：应用主窗口的隐藏 / 托盘态不受此参数影响，始终返回并带 `visible=false`** |
| `only_windows` | bool | `true` | 仅 `ControlType=Window`；`false` 时含任务栏等全部顶层控件 |
| `limit` | int | `50` | 返回数量上限（1–300） |

返回：`ok / count / total / visible_count / hidden_count / foreground_window / environment / apps[] / hidden_windows[] / topmost_count / topmost_windows[] / windows[] / note`。

`windows[]` 每项关键字段：

| 字段 | 说明 |
| --- | --- |
| `hwnd` | 窗口句柄，可直接传给 `ui_activate_window` |
| `visible` / `iconic` | 是否可见 / 是否最小化（托盘隐藏窗口为 `visible=false`） |
| `process_name` / `process_path` | 进程可执行名（如 `Weixin.exe`）与完整路径 |
| `class_name` | Win32 窗口类名（如 `Qt51514QWindowIcon`） |
| `app_alias` / `app_id` / `framework` | 应用别名（如「微信」）/ 应用标识 / UI 框架（如 `Qt`） |
| `topmost` | 是否置顶（`WS_EX_TOPMOST`），置顶窗口会盖住同区域普通窗口 |

应用身份识别要点：微信等 Qt 应用的窗口标题是**登录昵称**（如 `Hugo_ever`），无法从标题判断应用；此时用 `app_alias` / `process_name` 对应应用，再用 `hwnd` 精确激活。同一应用多实例时 `apps[]` 会聚合出 `hwnds[]` 与 `has_visible`，可判断"应用在运行但只是窗口没显示"。

### 4.2 `ui_activate_window`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `hwnd` | int | — | 窗口句柄，优先级最高，可激活隐藏 / 托盘窗口 |
| `title` | str | — | 窗口标题（默认子串匹配；也可传应用别名如 `微信`） |
| `process_name` | str | — | 进程名或路径片段，如 `notepad.exe` / `weixin` |
| `exact` | bool | `false` | 标题是否精确匹配 |
| `restore` | bool | `true` | 最小化时先还原 |

返回：`ok / found / activated / hwnd / window / detail`，并附带遮挡诊断：

| 字段 | 说明 |
| --- | --- |
| `covered` | 窗口中心点是否被其它窗口遮挡（`true` 时坐标点击 / 键盘输入可能落到遮挡窗口上） |
| `covered_by` | 遮挡窗口信息（`hwnd` / `app_alias` / `title` / `topmost`） |
| `hint` | 可执行的处置建议 |

未命中时返回 `found=false` 与 `hints`（排查建议）、`candidates`（疑似候选窗口，含别名 / 进程名 / 类名），不再让调用方"盲目重试"。

### 4.3 `ui_find`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `target` | str | 必填 | 元素名称或界面文字，如 `保存` |
| `role` | str | — | 限定角色，如 `Button` / `Edit` / `Window` |
| `window_title` | str | — | 把搜索限制在指定窗口内 |
| `use_ocr_fallback` | bool | `true` | UIA 未命中时启用 OCR 兜底 |
| `fuzzy` | bool | `true` | OCR 模糊匹配 |
| `limit` | int | `1` | `>1` 时附带 `matches[]` 候选列表 |

返回：`ok / found / source(uia|ocr) / name / role / bounds / center / x / y / matches[]`。

### 4.4 `ui_click`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `target` | str | — | 目标名称或文字（与 `x`/`y` 二选一） |
| `x` / `y` | int | — | 屏幕绝对坐标（物理像素） |
| `button` | str | `left` | `left` / `right` / `middle` |
| `clicks` | int | `1` | `2` 为双击 |
| `verify` | bool | `false` | 点击后回读该点元素信息 |
| `use_ocr_fallback` | bool | `true` | 定位失败时 OCR 兜底 |

返回：`ok / found / source / center / clicked{x,y,button,clicks}`，并附带命中与遮挡信息：

| 字段 | 说明 |
| --- | --- |
| `auto_raise` | 是否因目标点被遮挡而对目标窗口做了临时抬升（点击完成即复原原 Z 序） |
| `hit_window` | 点击瞬间该点真实归属窗口（`hwnd` / `app_alias` / `process_name` / `topmost`） |
| `detail` | 遮挡与命中说明；命中窗口与预期窗口不一致时附警告 |

目标未找到时 `ok=false` 且**不执行点击**。注意：被置顶窗口遮挡的目标，点击会落到遮挡窗口上——`auto_raise=true` 时表示内核已临时抬升目标窗口保证点击落到目标上，`hit_window` 可用于事后核验。

### 4.5 `ui_type`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | str | 必填 | 待输入文本（支持中文与换行） |
| `interval` | float | `0.02` | 逐字符间隔（仅键盘模式） |
| `method` | str | `auto` | `auto` / `keystroke` / `clipboard` |

返回：`ok / chars / method / elapsed_seconds`。**调用前需确保目标输入框已获焦**（可先 `ui_click`）。

### 4.6 `ui_hotkey`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `keys` | str | 必填 | 如 `ctrl+s`、`ctrl+shift+esc`；支持别名 `control/win/escape/return/del` |
| `delay` | float | `0` | 发送前等待秒数 |

返回：`ok / keys[] / sent_at`。

### 4.7 `ui_screenshot`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | str | — | 保存路径（`.png/.jpg/.bmp/.webp`），缺省写入 `UI_AGENT_OUTPUT_DIR` 下的时间戳文件名 |
| `region` | int[4] | — | `[left, top, width, height]`，缺省整个虚拟桌面 |

返回：`ok / path / width / height / region`。禁止写入系统核心目录（`C:\Windows`、`Program Files*`、`C:\ProgramData`）。

### 4.8 `ui_ocr`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `region` | int[4] | — | 识别区域，缺省全屏 |
| `keyword` | str | — | 传入即按关键词模式返回命中坐标 |
| `mode` | str | `blocks` | `blocks` / `keyword` / `text` |
| `limit` | int | `50` | 条目上限（`text` 模式为字符截断长度，`0` 不限） |
| `fuzzy` | bool | `true` | 关键词模糊匹配 |

返回：

- `mode=blocks`：`ok / count / blocks[].{text,confidence,bounds,center}`
- `mode=keyword`：`ok / keyword / count / matches[]`
- `mode=text`：`ok / text / length / truncated`

---

## 5. 返回与错误约定

- 所有工具返回**单个 JSON 字符串**（文本内容，UTF-8，中文不转义），恒含 `ok` 字段。
- 内核异常被统一捕获为 `{"ok": false, "error": "<异常类型>: <描述>"}`，不会中断 MCP 会话。
- 超时（`UI_AGENT_CALL_TIMEOUT`，默认 120s）返回 `ok=false`，`error` 提示目标窗口可能无响应。
- 只读工具可任意调用；写工具（`ui_activate_window` / `ui_click` / `ui_type` / `ui_hotkey`）会真实操控桌面，**调用方 Agent 需自行做安全确认**。

---

## 6. 协议自测

```powershell
# 只读链路（握手 + tools/list + ui_window_list / ui_find / ui_screenshot / ui_ocr）
.\.venv\Scripts\python.exe selfcheck\mcp_selfcheck.py

# 追加记事本端到端写链路（自建临时文档，无用户数据风险）
.\.venv\Scripts\python.exe selfcheck\mcp_selfcheck.py --include-write
```

自测以真实 MCP 客户端身份拉起 `serve_mcp.py` 子进程，覆盖：

| # | 检查项 | 说明 |
| --- | --- | --- |
| 1 | `initialize` 握手 | 校验协议版本与 `serverInfo.name == ui-agent` |
| 2 | `tools/list` | 校验 8 个预期工具齐备、无多余工具，并导出 `inputSchema` 摘要 |
| 3–8 | 只读 `tools/call` | `ui_window_list` / `ui_find` / `ui_screenshot`（校验文件落盘）/ `ui_ocr`（blocks + text） |
| 9–14 | 写链路 `tools/call`（`--include-write`） | 启动记事本打开自建临时文档 → `ui_activate_window` 置前 → `ui_click` 坐标点击置焦 → `ui_type` 输入中文 → `ui_hotkey(ctrl+s)` → 校验磁盘文件内容 → 截图 → `ui_ocr` 关键词核验 → `alt+f4` 关闭并回收进程 |

最近一次实测结果：

| 项 | 值 |
| --- | --- |
| 结论 | `passed = true`，**14 / 14 检查通过** |
| 协议版本 | `2025-11-25` |
| 工具数 | 8（`tools/list` 无缺失、无多余） |
| 总耗时 | 16.08 s（含服务启动预热与 2 次全屏 OCR） |
| 报告 | `out\mcp_selfcheck_report_20260914_213648.json` |
| 截图证据 | `out\mcp_selftest_screen_20260914_213648.png`、`out\mcp_selftest_notepad_20260914_213648.png` |
| 残留进程 | 无（记事本由 `alt+f4` 正常退出） |

退出码：`0` 全部通过，`1` 存在失败检查项。

---

## 7. 已知约束与注意事项

| 约束 | 说明 / 建议 |
| --- | --- |
| 仅 Windows | UIA 实现绑定 `uiautomation`，无跨平台分支 |
| 串行执行 | 所有调用经同一 COM 线程排队；长耗时 OCR 会阻塞后续调用 |
| OCR 性能 | 全屏 2560×1440 单次约 2–3 s；建议传 `region` 裁剪，或先走 `ui_find` 的 UIA 通道 |
| 输入前置条件 | `ui_type` 不负责置焦，需先 `ui_click` 或 `ui_activate_window` |
| 写操作副作用 | 鼠标 / 键盘 / 剪贴板为真实操作，会改变用户桌面状态；`ui_type` 的剪贴板模式会覆盖剪贴板内容 |
| 安全边界 | `ui_screenshot` 拒绝写入 `C:\Windows`、`Program Files*`、`C:\ProgramData` |
| 应用兼容性 | 游戏、远程桌面、Canvas 自绘界面无法 UIA 定位，只能依赖 OCR |
| 窗口标题不可靠 | 微信 / QQ 等应用的窗口标题是登录昵称（如 `Hugo_ever`）；请用 `app_alias` / `process_name` 识别应用，用 `hwnd` 激活，不要靠标题判断应用是否在运行 |
| 隐藏 / 托盘窗口 | 应用主窗口隐藏或最小化到托盘时，`ui_window_list` 仍会返回（`visible=false`），`ui_activate_window` 会先唤醒再置前；若只看 `visible=true` 的窗口会误判"应用未运行" |
| 置顶窗口遮挡 | 置顶窗口（`topmost=true`，如固定最前的微信）会盖住同区域普通窗口：`ui_activate_window` 返回 `covered / covered_by / hint`，`ui_click` 自动临时抬升目标窗口并返回 `auto_raise / hit_window`；也可先用 `ui_window_list` 的 `topmost_windows` 预判遮挡者 |

### 故障排查

| 现象 | 处理 |
| --- | --- |
| 客户端报 `ModuleNotFoundError` | `command` 未指向 venv 解释器 |
| 调用返回 `TimeoutError` | 目标窗口无响应或 UIA 卡死；检查 `UI_AGENT_CALL_TIMEOUT`，并确认目标进程健康 |
| 首次调用明显偏慢 | 冷启动 UIA + OCR 模型加载；保持 `UI_AGENT_WARMUP=1` |
| 中文返回乱码 | 客户端未按 UTF-8 解析报文；设置 `PYTHONUTF8=1` |
| 点击位置偏移 | 客户端进程也需 DPI 感知；本服务内部坐标已是物理像素 |
| `ui_activate_window` 找不到窗口 | 看返回的 `hints` / `candidates`：标题可能不是应用名（改用 `process_name` 或别名），或窗口处于隐藏态（用 `ui_window_list(include_invisible=true)` 取 `hwnd` 后按 `hwnd` 激活） |
| 点击"点到了别的窗口" | 看 `ui_click` 返回的 `hit_window` 与 `detail`：目标点被置顶窗口遮挡，内核已临时抬升目标窗口；持续异常可先关闭 / 最小化遮挡窗口 |
*（内容由AI生成，仅供参考）*
