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

把 `D:\Projects\ui-agent` 的 UIA + OCR 操控内核包装为 **基于 stdio 的本地 MCP Server**，向 MCP 客户端（CowAgent / Marvis / Claude Desktop 等）暴露 12 个 `ui_*` 工具，实现窗口枚举、应用唤起与状态查询、**未运行应用的启动与窗口就绪等待**、元素定位、鼠标点击、键盘输入、截屏与 OCR 能力。

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
uiagent/mcp_server.py  ── MCPServer（12 个 ui_* 工具，async）
        │  asyncio.to_thread
        ▼
COM 专用工作线程 _UiThread（单线程 STA，串行执行）
        │
        ▼
UniversalController（内核门面）
   ├── WindowsAccessibility（UIA 精确查找）
   ├── RapidOCREngine（OCR 文字定位兜底）
   ├── AppLauncher（P2：应用解析 resolve / 进程发起 launch / 窗口等待 wait_window）
   │     └── apps.yaml（别名 → 可执行路径 / tier 显式映射）
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
| `UIAGENT_ENSURE_V2` | `1` | 应用唤起回退开关（P1）：`1` 走「状态判定 S1~S4 → 唤醒 / 置前 → 就绪确认」新链路；`0` 时 `ui_app_ensure` 退化为 `find_window` + `activate_window` 旧链路（命中窗口时 `state="unknown"`、不做 bounds 稳定等待，`timing` 仅 `snapshot_ms` / `total_ms`；未命中同样返回 `state="not_running"` 但不带 `candidates`），用于线上快速回滚 |
| `UIAGENT_LAUNCH_ENABLED` | `1` | **P2 启动能力总开关**：`0` 时 `ui_launch_app` 整条链路直接拒绝（含 `dry_run`），返回 `ok=false` + `degraded_reason="launch_disabled"`，且不产生任何进程；用于线上快速关闭"启动未运行应用"这一高风险能力 |
| `UIAGENT_LAUNCH_ALLOWLIST` | 未设置 → **内置默认白名单**（安全默认） | **P2 启动白名单**（逗号分隔，匹配应用别名 / exe 名 / 目标文件名，大小写不敏感）。三种取值语义：①**未设置 / 空白** → 套用内置默认白名单 `DEFAULT_LAUNCH_ALLOWLIST`（常用应用：记事本、计算器、画图、资源管理器、Edge、微信；**刻意不含** `cmd` / `powershell` / `pwsh` / 终端 / `taskmgr` / `regedit` 等命令解释器与系统管理工具）；②**显式配置**（非空）→ **以配置为准**，整体替换默认白名单（可放开默认之外的入口，如 `weixin.exe,微信,msedge.exe,notepad.exe`）；③**显式设为 `*` 或 `all`** → 不校验（显式解除限制入口，需人为设置）。校验不通过时返回 `ok=false` + `degraded_reason="not_allowlisted"`（`error`/`hint` 会标注是「内置默认白名单」还是「env 显式配置」拒绝，并给出放开方式），并落审计告警 |

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
| `ui_app_ensure` | 写 | 统一应用唤起入口（P1）：按 `hwnd` / `app` / 标题 / 进程名定位后，按 S1~S4 状态唤醒 → 等待就绪 → 置前，返回 `state` / `timing` / `reachable`；S4 返回 `candidates`，`launch_if_missing=true` 时交由 P2 启动链路自动冷启动 |
| `ui_app_status` | 只读 | 批量查询应用状态（`visible` / `minimized` / `hidden` / `cloaked` / `not_running`）+ 候选窗口与缓存信息 |
| `ui_launch_app` | 写 | **P2 新增**：启动未运行的应用并等窗口就绪（**高风险：真实创建进程**）；解析链 `alias → App Paths → PATH → 开始菜单 .lnk → UWP`，返回 `state` / `hwnd` / `pid` / `resolved_by` / `target_path` / `launch_ms` / `wait_ms`；`dry_run` 只解析不启动 |
| `ui_wait_window` | 只读 | **P2 新增**：等待目标窗口出现并完成布局（`hwnd` > `pid` > `app` 三选一定位），替代上层"睡眠 + 反复查询"的轮询往返；超时返回 `state="launching"`（非错误） |
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
| `state` | 统一状态口径（P1 新增）：`visible` / `minimized` / `hidden` / `cloaked`，由 `visible` + `iconic` + Win32 cloaked 位推导，调用方可直接按状态分派而无需自行判断 |

应用身份识别要点：微信等 Qt 应用的窗口标题是**登录昵称**（如 `Hugo_ever`），无法从标题判断应用；此时用 `app_alias` / `process_name` 对应应用，再用 `hwnd` 精确激活。同一应用多实例时 `apps[]` 会聚合出 `hwnds[]` 与 `has_visible`，可判断"应用在运行但只是窗口没显示"；P1 起 `apps[]` 另带 `state`（该应用最优窗口状态）与 `states`（各状态窗口计数）。

### 4.2 `ui_app_ensure`

统一应用唤起入口（P1）：**一次调用**完成「状态判定（S1~S4）→ 唤醒 / 就绪等待 → 置前」，替代「`ui_window_list` 找 `hwnd` → `ui_activate_window` → 再定位」的多步往返。**写操作**（可能还原 / 显示窗口并改变 Z 序）。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `app` | str | — | 应用别名 / 进程名 / exe 名，如 `微信` / `weixin.exe` / `记事本` |
| `hwnd` | int | — | 窗口句柄，优先级最高（最稳；可由 `ui_window_list` / `ui_app_status` 取得） |
| `title` | str | — | 窗口标题（子串匹配，兼容既有定位方式） |
| `process_name` | str | — | 进程名片段，如 `weixin` / `notepad.exe` |
| `launch_if_missing` | bool | `false` | 应用未运行时是否允许启动（P2 起为真实能力）：`true` 时转交 `ui_launch_app` 冷启动并回填 `timing.launch_ms`（失败或开关禁用时 `degraded_reason` 为 `launch_disabled` / `not_allowlisted` / `launch_failed`）；`false` 时仅如实上报 `state="not_running"` + `candidates` |
| `require_foreground` | bool | `true` | 是否要求最终位于前台；`false` 时仅唤醒、不强制置前 |
| `wait_ready` | bool | `true` | 从最小化 / 隐藏唤醒后是否等待 bounds 稳定（连续采样尺寸差 ≤2 px、稳定判据 150 ms） |
| `timeout` | float | — | 整体耗时预算（秒）；超预算即跳过就绪等待并降级返回（不抛异常） |

`app` / `hwnd` / `title` / `process_name` 至少给一个，否则返回 `ok=false` + `error`。

四状态语义（返回体 `state`；`state_before` 为调用前状态；每个应用只唤起「最优候选窗口」）：

| 状态 | 含义 | 处理路径 | 耗时预算（P95） |
| --- | --- | --- | --- |
| `visible`（S1） | 已可见窗口 | 直接置前，零等待（不再固定 sleep 250 ms） | ≤ 150 ms |
| `minimized`（S2） | 任务栏最小化 | `SW_RESTORE` → bounds 稳定等待 → 置前 | ≤ 650 ms |
| `hidden`（S3） | 托盘驻留 / 隐藏 | `SW_SHOW` → bounds 稳定等待 → 置前 | ≤ 900 ms |
| `not_running`（S4） | 无任何候选窗口 | 如实上报 + 返回 `candidates` / `hints`；`launch_if_missing=false` 时不启动进程，`=true` 时转交 P2 启动链路（`ui_launch_app`） | — |

> `cloaked`（UWP 挂起 / 位于其它虚拟桌面的窗口）不计入 S1~S3，单独以 `state=cloaked` 返回并置 `degraded_reason=app_window_cloaked`。

返回：

| 字段 | 说明 |
| --- | --- |
| `ok` / `found` | S1~S3 命中为 `true`；S4 为 `false`（MCP 层补 `error`，并附 `candidates` / `hints`） |
| `app` / `hwnd` / `window` | 应用名、目标 `hwnd`、窗口字典（与 `ui_window_list` 的 `windows[]` 同构） |
| `state` / `state_before` | 唤起后 / 调用前的状态（取值见上表与 `cloaked`） |
| `ready` | 是否已「可操作」（`state ∈ {visible, minimized}` 且已置前，或不要求前台） |
| `activated` / `cached` | 是否真正置前 / 是否命中 P0 的 `AppContext` 缓存（命中可省一次窗口枚举） |
| `timing` | `{snapshot_ms, show_ms, settle_ms, foreground_ms, launch_ms, total_ms}`（P1 的 `launch_ms` 恒为 `null`；P2 起 `launch_if_missing=true` 且真正冷启动时，`launch_ms` 为窗口就绪等待耗时 `wait_ms`） |
| `reachable` / `covered_by` / `hint` | 中心点是否可达 / 遮挡者信息 / 可执行建议（同 `ui_activate_window` 的遮挡诊断） |
| `degraded` / `degraded_reason` | 降级标记与原因：`foreground_locked`（置前被系统前台锁定策略拦截，窗口已唤醒）、`window_unstable`（bounds 未在超时内稳定）、`app_window_cloaked`、`timeout`、`app_not_running`（未请求启动时）；P2 起启动链路原因原样透传：`launch_disabled` / `not_allowlisted` / `launch_failed` / `launch_timeout`，以及 `launch_unavailable`（`launch_if_missing=true` 但缺 `app` 等前置条件、未实际执行启动） |
| `launch` | P2 新增：`launch_if_missing=true` 或 `dry_run=true` 时为 launcher 的完整返回体（`resolved_by` / `target_path` / `pid` / `launch_ms` / `wait_ms` / `state`），便于上层追溯启动决策；否则为 `null` |
| `candidates` / `multi_instance` | 疑似候选窗口（含 `hwnd` / `title` / `state` / `selected`）/ 是否多实例（上层可据此复核或指定 `hwnd`） |
| `detail` | 人类可读说明 |

回退开关：`UIAGENT_ENSURE_V2=0` 时退化为 `find_window` + `activate_window` 旧链路（返回体多带 `fallback="legacy"`，命中窗口时 `state="unknown"` 且不做 bounds 稳定等待、`timing` 仅保留 `snapshot_ms` / `total_ms`；未命中时同样返回 `state="not_running"` 但**不带** `candidates` / `hints`），用于线上快速回滚。

开关实测（2026-09-15，同一窗口 `hwnd=66322`，`require_foreground=false` / `wait_ready=false`）：

| 场景 | 新链路（`=1`） | 旧链路（`=0`） |
| --- | --- | --- |
| 命中可见窗口 | `state="visible"`、`state_before="visible"`、`timing` 六键齐全、`total≈159 ms` | `state="unknown"`、`fallback="legacy"`、无 `state_before` / `ready` / `multi_instance`、`total≈1.4 ms` |
| 应用未运行 | `state="not_running"` + `degraded_reason="app_not_running"` + `candidates`（8 个，含 `state`） | `state="not_running"` + `degraded_reason="app_not_running"`，返回体**无** `candidates` / `hints` 字段 |

> 上表命中窗口样本为中心点探测耗时较高的特殊窗口（NVIDIA Overlay），`total=159 ms` 略超 S1 预算，仅用于验证开关差分，不计入下方预算统计。

### 4.3 `ui_app_status`

批量查询应用状态（**只读**：不改 Z 序、不启动进程、不唤醒窗口），用于动作前做一次廉价决策。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `apps` | str[] | — | 待查询应用列表（别名 / 进程名），如 `["微信", "记事本"]`；留空则实时枚举并返回当前全部「像应用主窗口」的聚合 |
| `include_not_running` | bool | `true` | 是否返回未启动应用的记录（`state="not_running"`） |

返回：`ok / count / apps[] / elapsed_ms / context`。

| 字段 | 说明 |
| --- | --- |
| `app` | 应用名（别名 / 进程名 / 标题 fallback） |
| `state` | `visible` / `minimized` / `hidden` / `cloaked` / `not_running` |
| `hwnd` / `window` | 最优候选窗口句柄与字典（`not_running` 时 `hwnd=0`、`window=null`） |
| `foreground` | 该窗口是否当前前台 |
| `cached` / `cached_hwnd` / `cached_age_ms` | 是否命中 `AppContext` 缓存、缓存窗口与缓存年龄（ms） |
| `multi_instance` / `candidates[]` | 是否多实例 / 各候选窗口（`hwnd` / `title` / `process_name` / `state` / `selected`） |

本机实测（2026-09-15）：一次查询 11 个应用约 8 ms；不存在的应用返回 `state="not_running"`、`hwnd=0`。

### 4.4 `ui_launch_app`（P2 新增）

启动应用并等待窗口就绪（**写操作：真实创建进程，高风险能力**），用于「应用当前未运行」场景，替代人工模拟「Win 键 + 搜索 + 回车」。受 `UIAGENT_LAUNCH_ENABLED`（总开关）与 `UIAGENT_LAUNCH_ALLOWLIST`（白名单）双重约束。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `app` | str | 必填 | 应用别名 / exe 名 / 完整路径，如 `记事本` / `notepad.exe` / `C:/Windows/System32/calc.exe` |
| `dry_run` | bool | `false` | 只解析启动入口并返回，**不产生任何进程**（`state="dry_run"`、`pid=0`、`hwnd=0`） |
| `timeout` | float | 按分级 | 等待窗口就绪的超时（秒）；留空取分级默认：`light` 8 s / `normal` 15 s / `heavy` 25 s（`launcher.TIER_TIMEOUTS`）。注意这**不是** SLA 目标值，而是「放弃等待」的宽松上限 |
| `launch_method` | str | `auto` | `auto` / `exec`（`ShellExecuteEx`）/ `process`（`CreateProcess`）/ `uwp`（`explorer.exe shell:AppsFolder\<AppID>`） |
| `args` / `working_dir` | str | 空 | 附加命令行参数 / 工作目录，默认空（`args` 仅在应用确需时使用） |
| `wait` | bool | `true` | 是否等待窗口就绪；`false` 时只发起启动并立即返回 `pid` |

**解析链**（`resolved_by`）：`alias_table`（`apps.yaml` 别名表，含 Store 打包应用的 `AppID`）→ `app_paths`（注册表 `App Paths`）→ `path`（`PATH` 查找）→ `start_menu`（开始菜单 `.lnk`）→ `uwp`（`Get-StartApps` 的 `AppID`）；全部未命中返回 `ok=false` + `hint`（Win 键搜索兜底本版本未实现）。解析出多候选时**不自动启动**，返回 `candidates[]` + `resolved_by` 由上层决策（RF5）。

返回：`ok / app / state / hwnd / pid / resolved_by / target_path / launch_ms / wait_ms / tier / timeout / dry_run / degraded / degraded_reason / candidates[] / hint`（等待成功时另带 `wait_detail`）。

| 字段 | 说明 |
| --- | --- |
| `state` | `launching`（已发起但超时未就绪，**非失败**，RF7）/ `visible` / `minimized` / `hidden` / `not_running` / `dry_run` |
| `launch_ms` / `wait_ms` | 启动发起耗时 / 窗口就绪等待耗时；`cold_ms ≈ launch_ms + wait_ms` |
| `wait_detail` | 就绪等待明细：`matched_by`（`hwnd` / `pid` / `app` / `app_fallback`）、`matched_key`、`fallback`、`polls`、`stable`、`state`、`title`、`process_name` |
| `degraded_reason` | `launch_disabled`（开关关闭；含 `dry_run` 路径）/ `not_allowlisted`（白名单拒绝）/ `launch_failed`（发起失败）/ `launch_timeout`（等待超时）/ `pid_window_missing`（`pid` 未匹配到窗口，已回退别名键命中，`wait_detail.fallback=true`） |

安全语义（RF6）：`UIAGENT_LAUNCH_ENABLED=0` 时**任何**调用（含 `dry_run`）直接拒绝、不产生进程，并落审计 `launch` 事件；`UIAGENT_LAUNCH_ALLOWLIST` **默认启用安全白名单**——未设置 / 空白时套用内置默认白名单（常用应用，不含 `cmd` / `powershell` / 终端等命令解释器），显式配置时以配置为准，显式设为 `*` / `all` 表示不校验；校验按别名 / 目标文件名匹配（大小写不敏感），非白名单启动尝试 = 审计告警；解析歧义（多候选）与打包应用窗口（多数归 `ApplicationFrameHost.exe`，仅按 `pid` 匹配不到）分别由「不自动选择」与 `app_fallback` 兜底键处理。

### 4.5 `ui_wait_window`（P2 新增）

等待目标窗口出现并完成布局（**只读**：不启动进程、不改 Z 序），替代上层「睡眠 + 反复查询」的轮询往返。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `hwnd` | int | — | 目标窗口句柄（优先级最高） |
| `pid` | int | — | 目标进程 ID（可直接用 `ui_launch_app` 的返回） |
| `app` | str | — | 应用别名 / 进程名（含打包应用兜底键：如 `计算器` 可命中 `ApplicationFrameHost.exe` 承载的窗口） |
| `timeout` | float | `10.0` | 最长等待时间（秒） |
| `stable_ms` | float | `150` | bounds 需保持稳定的时长（毫秒），就绪判据与 `ui_app_ensure` 一致 |

定位优先级：`hwnd` > `pid` > `app`。返回：`ok / state / hwnd / pid / waited_ms / stable / polls / bounds / title / process_name / matched_by / matched_key / degraded_reason`。

- `state="ready"` 视为就绪（`ok=true`）；超时返回 `state="launching"` + `degraded_reason="launch_timeout"`，**不是错误**（RF7）；
- `matched_by=app_fallback` 表示 `pid` 线索未命中窗口、已回退到别名键（Store 打包应用的常态，见 4.4 注）。

### 4.6 `ui_activate_window`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `hwnd` | int | — | 窗口句柄，优先级最高，可激活隐藏 / 托盘窗口 |
| `title` | str | — | 窗口标题（默认子串匹配；也可传应用别名如 `微信`） |
| `process_name` | str | — | 进程名或路径片段，如 `notepad.exe` / `weixin` |
| `exact` | bool | `false` | 标题是否精确匹配 |
| `restore` | bool | `true` | 最小化时先还原 |
| `wait_ready` | bool | `true` | 唤醒（`SW_RESTORE` / `SW_SHOW`）后是否等待 bounds 稳定再置前，避免后续点击落到仍在布局的窗口上（P1 新增；仅 S2 / S3 状态迁移时生效） |

返回：`ok / found / activated / hwnd / window / state / stable / timing{show_ms, settle_ms, foreground_ms, total_ms} / detail`，并附带遮挡诊断：

| 字段 | 说明 |
| --- | --- |
| `covered` | 窗口中心点是否被其它窗口遮挡（`true` 时坐标点击 / 键盘输入可能落到遮挡窗口上） |
| `covered_by` | 遮挡窗口信息（`hwnd` / `app_alias` / `title` / `topmost`） |
| `hint` | 可执行的处置建议 |
| `state` | 激活后的窗口状态（`visible` / `minimized` / `hidden` / `cloaked`），P1 新增 |
| `stable` | `wait_ready=true` 时 bounds 是否在稳定判据内（连续采样尺寸差 ≤2 px） |
| `timing` | 分阶段耗时：`show_ms` / `settle_ms` / `foreground_ms` / `total_ms` |

未命中时返回 `found=false` 与 `hints`（排查建议）、`candidates`（疑似候选窗口，含别名 / 进程名 / 类名），不再让调用方"盲目重试"。

### 4.7 `ui_find`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `target` | str | 必填 | 元素名称或界面文字，如 `保存` |
| `role` | str | — | 限定角色，如 `Button` / `Edit` / `Window` |
| `window_title` | str | — | 把搜索限制在指定窗口内 |
| `use_ocr_fallback` | bool | `true` | UIA 未命中时启用 OCR 兜底 |
| `fuzzy` | bool | `true` | OCR 模糊匹配 |
| `limit` | int | `1` | `>1` 时附带 `matches[]` 候选列表 |

返回：`ok / found / source(uia|ocr) / name / role / bounds / center / x / y / matches[]`。

### 4.8 `ui_click`

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

### 4.9 `ui_type`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | str | 必填 | 待输入文本（支持中文与换行） |
| `interval` | float | `0.02` | 逐字符间隔（仅键盘模式） |
| `method` | str | `auto` | `auto` / `keystroke` / `clipboard` |

返回：`ok / chars / method / elapsed_seconds`。**调用前需确保目标输入框已获焦**（可先 `ui_click`）。

### 4.10 `ui_hotkey`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `keys` | str | 必填 | 如 `ctrl+s`、`ctrl+shift+esc`；支持别名 `control/win/escape/return/del` |
| `delay` | float | `0` | 发送前等待秒数 |

返回：`ok / keys[] / sent_at`。

### 4.11 `ui_screenshot`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | str | — | 保存路径（`.png/.jpg/.bmp/.webp`），缺省写入 `UI_AGENT_OUTPUT_DIR` 下的时间戳文件名 |
| `region` | int[4] | — | `[left, top, width, height]`，缺省整个虚拟桌面 |

返回：`ok / path / width / height / region`。禁止写入系统核心目录（`C:\Windows`、`Program Files*`、`C:\ProgramData`）。

### 4.12 `ui_ocr`

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
- 只读工具可任意调用；写工具（`ui_activate_window` / `ui_app_ensure` / `ui_launch_app` / `ui_click` / `ui_type` / `ui_hotkey`）会真实操控桌面，**调用方 Agent 需自行做安全确认**。其中 `ui_launch_app` 属**高风险**（真实创建进程），启用前建议先跑 `dry_run=true` 预演解析结果。
- **降级 ≠ 失败**：`ui_app_ensure` 的 `degraded=true` 只表示未达到最优就绪状态（`degraded_reason` ∈ `foreground_locked` / `window_unstable` / `app_window_cloaked` / `timeout`），返回体仍带可用的 `hwnd` / `state` / `window`，调用方可继续按 `hwnd` 操作。
- **启动链路的三态而非二态**：`ui_launch_app` 的 `state="launching"`（等待窗口超时，`degraded_reason="launch_timeout"`）与 `ui_wait_window` 的超时同义——进程已发起、窗口只是还没就绪，**不是错误**，上层可继续 `ui_wait_window` 轮询或稍后 `ui_app_status` 复核（RF7）。
- **启动被拒也是结构化返回**：`UIAGENT_LAUNCH_ENABLED=0` → `ok=false` + `degraded_reason="launch_disabled"`（含 `dry_run`）；白名单不匹配（默认白名单或 env 显式配置一视同仁，`hint` 标注来源与放开方式）→ `degraded_reason="not_allowlisted"`；两者均不产生任何进程，且同样落审计 `launch` 事件（RF6）。
- 应用未运行时 `ui_app_ensure` 返回 `ok=false` + `state="not_running"` + `candidates`：`launch_if_missing=false`（默认）**不启动进程**（`degraded_reason="app_not_running"`）；`launch_if_missing=true` 时转交 P2 启动链路自动冷启动，成功则回填 `timing.launch_ms`（为窗口就绪等待耗时 `wait_ms`），失败或开关 / 白名单拒绝时 `degraded_reason` 为 `launch_disabled` / `not_allowlisted` / `launch_failed`。

---

## 6. 协议自测

```powershell
# 只读链路（握手 + tools/list + ui_window_list / ui_app_status / ui_launch_app(dry_run) / ui_wait_window / ui_find / ui_screenshot / ui_ocr）
.\.venv\Scripts\python.exe selfcheck\mcp_selfcheck.py

# 追加记事本端到端写链路（自建临时文档，无用户数据风险）
.\.venv\Scripts\python.exe selfcheck\mcp_selfcheck.py --include-write
```

自测以真实 MCP 客户端身份拉起 `serve_mcp.py` 子进程，覆盖：

| # | 检查项 | 说明 |
| --- | --- | --- |
| 1 | `initialize` 握手 | 校验协议版本与 `serverInfo.name == ui-agent` |
| 2 | `tools/list` | 校验 **12 个**预期工具齐备、无多余工具，并导出 `inputSchema` 摘要 |
| 3–12 | 只读 `tools/call` | `ui_window_list`（含 `state` 字段校验）/ `ui_app_status`（批量查询 + `not_running`）/ **`ui_launch_app`（`dry_run=true` 预演，校验不产生进程）** / **`ui_wait_window`（超时返回 `launching`，校验非异常）** / `ui_find` / `ui_screenshot`（校验文件落盘）/ `ui_ocr`（blocks + text） |
| 13–29 | 写链路 `tools/call`（`--include-write`） | 启动记事本打开自建临时文档 → `ui_activate_window` 置前 → **P1 四状态用例（S1 可见 / S2 最小化 / S3 隐藏 / S4 未启动 + 耗时预算断言）** → `ui_click` 坐标点击置焦 → `ui_type` 输入中文 → `ui_hotkey(ctrl+s)` → 校验磁盘文件内容 → 截图 → `ui_ocr` 关键词核验 → **P2 冷启动用例（退出记事本构造 S4 → `ui_launch_app` 冷启动 + 轻量 SLA ≤1.5 s 断言 + 发起段 ≤300 ms 断言 + 启动后 `hwnd` 可定位 + `ui_wait_window(pid+app)` 命中）** → `alt+f4` 关闭并回收进程 |

最近一次实测结果：

| 项 | 值 |
| --- | --- |
| 结论 | `passed = true`，**29 / 29 检查通过**（只读 12 项 + 写链路与 P1 / P2 用例 17 项） |
| 协议版本 | `2025-11-25` |
| 工具数 | 12（`tools/list` 无缺失、无多余） |
| 总耗时 | 21.97 s（含服务启动预热、2 次全屏 OCR 与一次真实冷启动） |
| 报告 | `out\mcp_selfcheck_report_20260915_132757.json` |
| 只读链路单跑 | `passed = true`，**12 / 12**，5.48 s，报告 `out\mcp_selfcheck_report_20260915_131524.json` |
| P1 四状态（写链路内单次实测） | S1 `visible` 10.80 ms / S2 `minimized` 189.63 ms / S3 `hidden` 183.29 ms（三者 `degraded=false`）/ S4 `not_running`（`degraded_reason=app_not_running`，8 个候选，未启动进程） |
| P2 冷启动（写链路内单次实测） | `state=visible`、`launch_ms=59.26` / `wait_ms=1004.08`（`cold_ms=1063.3`，轻量 SLA ≤1.5 s 通过、发起段 ≤300 ms 通过）；随后 `ui_app_ensure` 命中同一 `hwnd`（10.84 ms），`ui_wait_window(pid+app)` 690.34 ms 命中 |
| P2 预演与超时（只读链路断言） | `ui_launch_app(dry_run=true)`：`state=dry_run`、`pid=0`、`hwnd=0`（无进程）；`ui_wait_window(timeout=1.0)`：`state=launching`、`degraded_reason=launch_timeout`、`waited_ms≈1014.9`（非异常） |
| 截图证据 | `out\mcp_selftest_screen_20260915_132757.png`、`out\mcp_selftest_notepad_20260915_132757.png` |
| 残留进程 | 无（记事本由 `alt+f4` 正常退出；冷启动实例由用例末尾清理） |
| 复核复跑（2026-09-15 13:52，同代码快照） | `passed = true`，**29 / 29**，18.90 s，报告 `out\mcp_selfcheck_report_20260915_135252.json`；只读链路单跑 **12 / 12**，7.05 s，报告 `out\mcp_selfcheck_report_20260915_135224.json`；P2 冷启动 `launch_ms=52.2` / `wait_ms=966.4`（`cold_ms=1018.6`，wall 口径 1020.6 ms）、`resolved_by=alias_table`、`matched_by=app_fallback`；启动后 `ui_app_ensure` 命中同一 `hwnd`（10.11 ms），`ui_wait_window(pid+app)` 698.10 ms 命中；P1 四状态 S1 8.97 / S2 199.08 / S3 181.77 ms |

P1 `ui_app_ensure` 四状态多轮采样（被测应用：记事本；每状态 8 轮；经 MCP stdio 真实链路；采样脚本为本地验证用中间产物，未入库）：

| 状态 | P50 | P95 | 预算（P95） | 结论 |
| --- | --- | --- | --- | --- |
| `visible`（S1） | 10.26 ms | 14.21 ms | ≤ 150 ms | 通过 |
| `minimized`（S2） | 195.56 ms | 208.69 ms | ≤ 650 ms | 通过 |
| `hidden`（S3） | 181.41 ms | 183.33 ms | ≤ 900 ms | 通过 |
| `not_running`（S4） | 42.99 ms | 49.51 ms | —（如实上报，不计失败） | 通过 |

S2 / S3 的耗时主要来自 `timing.settle_ms`（约 156 ms，即 bounds 稳定判据 150 ms），属预期行为；S1 已去掉旧链路的固定 250 ms 等待。

P2 分段耗时实测（被测应用：记事本 / 计算器 / 画图；本机 Windows 11 + 系统记事本与计算器；采样脚本为本地验证用中间产物，未入库）：

| 分段 | 样本 | 实测 |
| --- | --- | --- |
| `resolve()` 别名表（含 `apps.yaml` 加载） | 记事本 / 计算器 / 画图 | 首次 11.8 ms（仅首个调用承担加载）/ 0.028 ms / 0.014 ms；均 `resolved_by=alias_table`、`ambiguous=false` |
| `resolve()` 缓存命中 | 同上，各 3 轮取第 2~3 轮 | 0.009~0.029 ms（预算：命中 0 ms 级） |
| `resolve()` 开始菜单索引 | 128 项 `.lnk` | 首次冷索引 2.1 ms；TTL 300 s 内缓存命中 0.002 ms |
| `resolve()` 其他来源 | `regedit` / `任务管理器` / `powershell.exe` | `app_paths` 0.58 ms / `uwp` 2.79 ms / `alias_table` 0.02 ms（均命中，`candidates=1`） |
| `resolve()` 未安装应用 | `chrome` / `winword` | `ok=false`、`candidates=0`、返回 `hint`；走完全链后失败（`chrome` 388 ms） |
| `launch(dry_run=true)` | 记事本 | `state=dry_run`、`pid=0`、`hwnd=0`、`launch_ms=0`，进程快照前后一致（**零进程**） |
| `launch()` 冷启动 | 记事本 | `launch_ms=59.3` + `wait_ms=1004.1` = `cold_ms=1063.3`（wall 1065.5），`state=visible`、`hwnd=1311382`、`matched_by=app_fallback` |
| 同上（独立脚本复测，各 1 轮） | 记事本 / 计算器 | 记事本 `49.2 / 984.6 / 1033.8`、计算器 `6.5 / 992.4 / 998.9`，均 `state=visible` |
| `wait_window()` | 记事本（`pid` + `app` 双线索） | 690.3 ms、`stable=true`、`matched_by=app_fallback`；复测 984.6 / 992.4 ms（`polls` 17~18） |
| 启动后定位 | 记事本 | `ui_app_ensure` 命中同一 `hwnd`（10.8 ms），「启动 → 定位」闭环成立 |

> 说明：两款被测应用均为 Store 打包版（窗口归 `ApplicationFrameHost.exe` 承载），`pid` 线索匹配不到窗口，故 `matched_by=app_fallback`（别名键兜底命中），属已知设计路径而非失败。`wait_ms` 占 `cold_ms` 的 93% 以上，说明瓶颈在应用自身首窗口创建，非内核段。

P2 启动安全闸验证（零进程断言，逐项比对 `tasklist` 前后快照）：

| 用例 | 环境 | 结果 |
| --- | --- | --- |
| 开关禁用 + 真实启动请求 | `UIAGENT_LAUNCH_ENABLED=0` | `ok=false`、`state=not_running`、`pid=0`、`degraded_reason=launch_disabled`、**无进程**（12.5 ms） |
| 开关禁用 + `dry_run` | `UIAGENT_LAUNCH_ENABLED=0` | 同上（1.6 ms，含 `dry_run` 也不放行） |
| 白名单不匹配 + 真实启动请求 | `UIAGENT_LAUNCH_ALLOWLIST=notepad.exe`，请求 `计算器` | `ok=false`、`degraded_reason=not_allowlisted`、**无进程**（1.7 ms） |
| 白名单匹配 → 放行 | `ALLOWLIST=notepad.exe`，请求 `记事本` | `ok=true`、`state=dry_run`（预演） |
| 白名单大小写不敏感 | `ALLOWLIST=NOTEPAD`，请求 `notepad.exe` | `ok=true`、`state=dry_run`（误拒为 0） |
| 默认白名单生效（未配置 env） | 不设 `UIAGENT_LAUNCH_ALLOWLIST`，请求 `记事本` | `ok=true`、`state=dry_run`（`allowlist_source=default`） |
| 默认白名单拦截命令解释器 | 不设 `UIAGENT_LAUNCH_ALLOWLIST`，请求 `cmd` | `ok=false`、`degraded_reason=not_allowlisted`、**无进程**（`hint` 给出放开方式） |
| 显式配置覆盖默认白名单 | `ALLOWLIST=weixin.exe,微信`，请求 `记事本` | `ok=false`、`degraded_reason=not_allowlisted`（默认条目不再放行） |
| 显式解除限制入口 | `ALLOWLIST=*`，请求 `cmd`（`dry_run=true`） | `ok=true`、`state=dry_run`（不校验，仅解析） |

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
| 唤起状态口径（P1） | `state` 由 `visible` + `iconic` + Win32 cloaked 位推导为 `visible` / `minimized` / `hidden` / `cloaked`；`cloaked` 不计入 S1~S3（UWP 挂起或位于其它虚拟桌面，此时坐标操作不可靠） |
| 启动能力（P2 已具备） | `ui_launch_app` 可真实拉起未运行的应用并等待窗口就绪；`ui_wait_window` 可等已发起的进程窗口（`hwnd` / `pid` / `app` 三线索）。`ui_app_ensure(launch_if_missing=true)` 会转交该链路（等价于「启动 + 四状态唤起」一次调用）。**无独立启动能力时**（`launch_if_missing=false`）仍返回 `state="not_running"` + `candidates`（`degraded_reason="app_not_running"`） |
| 启动安全闸（RF6） | ①`UIAGENT_LAUNCH_ENABLED=0` → 一切启动调用（含 `dry_run`）拒绝，`degraded_reason="launch_disabled"`；②`UIAGENT_LAUNCH_ALLOWLIST` **默认启用安全白名单**（未设置 / 空白 → 内置默认白名单：常用应用，不含 `cmd` / `powershell` / 终端等命令解释器；显式配置 → 以配置为准；显式设为 `*` / `all` → 不校验）→ 仅放行生效白名单（别名 / 目标文件名，大小写不敏感），其余 `not_allowlisted` + 审计告警；③多候选（`ambiguous`）**不自动选择**，返回 `candidates[]` 交上层决策（RF5）。三条拒绝路径均已验证**零进程产生** |
| 启动解析边界 | 解析链为别名表 → `App Paths` → `PATH` → 开始菜单 → UWP；Win 键搜索兜底**未实现**（`search_fallback` 不返回），未命中即有 `hint` 提示改用绝对路径（RF8 剩余项） |
| 打包应用窗口归属 | Store 应用（记事本 / 计算器）窗口多由 `ApplicationFrameHost.exe` 承载，按 `pid` 匹配不到窗口，需靠 `app` / 别名键兜底——此时 `matched_by="app_fallback"`、`wait_detail.fallback=true`，是**正常路径**而非降级失败 |
| 启动超时语义（RF7） | `ui_launch_app` / `ui_wait_window` 超时返回 `state="launching"`（非错误）；`timeout` 缺省按分级 `light` 8 s / `normal` 15 s / `heavy` 25 s，是「放弃等待」上限，**不是 SLA 目标值** |
| 回退开关（P1） | `UIAGENT_ENSURE_V2=0` 时 `ui_app_ensure` 退回 `find_window` + `activate_window` 旧链路（`fallback="legacy"`）：命中窗口返回 `state="unknown"` 且不判四状态；未命中返回 `state="not_running"` 且无 `candidates`；`timing` 仅 `snapshot_ms` / `total_ms` |
| 回退开关（P2） | `UIAGENT_LAUNCH_ENABLED=0` 一键禁用启动能力，S4 回到改造前「空白态」（`not_running` + 提示人工启动），其余工具不受影响；`UIAGENT_LAUNCH_ALLOWLIST` 显式配置即覆盖内置默认白名单（需放开默认之外的入口时使用），显式设为 `*` / `all` 即解除白名单限制（不校验） |

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
| `ui_app_ensure` 报 `not_running` 但应用明明开着 | 看返回的 `candidates` / `hints`：应用可能只有 `cloaked` 窗口（UWP 挂起或位于其它虚拟桌面）或无主窗口；把 `candidates[].hwnd` 直接传给 `ui_app_ensure(hwnd=…)` / `ui_window_list(include_invisible=true)` 复核 |
| `ui_app_ensure` 返回 `degraded_reason=foreground_locked` | 系统前台锁定策略（`SetForegroundWindow` 受限）导致未置前——窗口已被唤醒，可继续按 `hwnd` 操作，**不是失败** |
| `ui_launch_app` 返回 `ok=false` + `launch_disabled` | `UIAGENT_LAUNCH_ENABLED=0` 已禁用启动能力（含 `dry_run`）；需启动能力时设为 `1` 并重启 MCP 服务 |
| `ui_launch_app` 返回 `ok=false` + `not_allowlisted` | 目标不在生效白名单内：未配置 `UIAGENT_LAUNCH_ALLOWLIST` 时命中的是**内置默认白名单**（常用应用，不含 `cmd` / `powershell` / 终端等命令解释器），如确实需要启动该目标，显式配置该变量把别名或 exe 名（逗号分隔）加入；确需完全不校验时显式设为 `*` / `all`；返回体 `hint` 已标注拒绝来源与放开方式 |
| `ui_launch_app` 返回 `state="launching"` | 等待窗口超时（`degraded_reason=launch_timeout`），进程已发起：可再调 `ui_wait_window(pid=<返回的 pid>, app=…)` 继续等，或稍后 `ui_app_status` 复核；**不要**当作失败重试启动 |
| `ui_launch_app` / `ui_wait_window` 一直等不到窗口 | 若 `ok=false` 且带 `hint`，说明解析链未命中（Win 键搜索兜底未实现）——改用绝对路径（如 `C:/Program Files/.../app.exe`）或把应用加入 `apps.yaml` 别名表；若 `ok=true` 但 `state` 非预期，用 `ui_window_list(include_invisible=true)` 按 `pid` 查真实窗口 |
| `ui_launch_app` 返回多个 `candidates` | 解析歧义（如同名快捷方式），按 RF5 **不会自动启动**；请用 `candidates[].target_path` 指定绝对路径，或先冻结该别名 |
*（内容由AI生成，仅供参考）*
