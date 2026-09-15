---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 8ba5b382cadd141b612ac8623b46555a_48e8fa24b0a211f1b128525400f8a581
    ReservedCode1: O69sNEM8YM+F8qp6DWNZfjtuOmb3E8Mee5gy+pe/rUFh+QwjVEf+YLFtJEFwThuo6sNTU8K5Y/tQqU8i5+PQNB47GtJ20xV4Y9N5N9BCNmrhpl2+UmSSaF2z98gzM2gZnUMs5xK1QCOkQKfYr5EuNSHMd1GmAGUzp2eAPWyuWVYhgJlIHJvUJe9Utso=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 8ba5b382cadd141b612ac8623b46555a_48e8fa24b0a211f1b128525400f8a581
    ReservedCode2: O69sNEM8YM+F8qp6DWNZfjtuOmb3E8Mee5gy+pe/rUFh+QwjVEf+YLFtJEFwThuo6sNTU8K5Y/tQqU8i5+PQNB47GtJ20xV4Y9N5N9BCNmrhpl2+UmSSaF2z98gzM2gZnUMs5xK1QCOkQKfYr5EuNSHMd1GmAGUzp2eAPWyuWVYhgJlIHJvUJe9Utso=
---

# ui-agent 审计日志设计（audit log）

> 目的：记录"何时被调用、每次任务用了什么定位方式、点了什么按钮、命中哪个窗口"，便于开发调试与回归分析。
> 原则：**不改变任何工具签名与返回结构**；不写 stdout（stdout 只承载 MCP JSON-RPC）；不改变现有 `logging_utils.py`（stderr）的行为。

---

## 1. 分层

| 层 | 载体 | 用途 | 去向 |
| --- | --- | --- | --- |
| 运行日志（已有） | `uiagent/logging_utils.py` | 异常与排错 | stderr（宿主管控，当前 WARNING） |
| 审计日志（本设计） | 新增 `uiagent/audit.py` | 调用链、定位方式、写操作明细 | JSONL 文件 |

## 2. 文件与切分

- 目录：`D:\Projects\ui-agent\logs\`（可由 `UI_AGENT_AUDIT_DIR` 覆盖）
- 文件名：`audit-YYYYMMDD-<pid>.jsonl`（MCP stdio 每个宿主连接即一个进程，按 PID 分文件避免多进程争写）
- 单文件超过 10 MB 轮转为 `<同名>.1`
- 启动时清理超过 `UI_AGENT_AUDIT_KEEP_DAYS`（默认 14 天）的旧文件
- 写入：UTF-8、每行一个完整 JSON 对象、`threading.Lock` 串行化、`flush()` 每条落盘（单条 < 1 ms，可接受）
- `logs/` 加入 `.gitignore`

## 3. 事件模型

公共字段：`ts`（ISO8601 带毫秒与时区）、`session_id`（进程级，格式 `YYYYMMDD-HHMMSS-<pid>`）、`event`。

### 3.1 `tool_call` / `tool_result`（MCP 层，覆盖全部 8 个工具）

| 字段 | 说明 |
| --- | --- |
| `tool` | `ui_click` / `ui_type` / ... |
| `args` | 入参摘要（`ui_type` 的 `text` 按脱敏策略处理） |
| `duration_ms` | 调用耗时 |
| `ok` / `error` | 结果与异常文本（`tool_result` 事件） |

### 3.2 `locate`（定位明细，核心）

| 字段 | 说明 |
| --- | --- |
| `target` / `role` / `window_title` | 查找目标 |
| `source` | `uia` / `ocr` / `coords` |
| `found` / `x` / `y` / `confidence` | 定位结果 |
| `element` | UIA 命中时：`name` / `role` / `automation_id` / `class_name` / `bounds` |
| `ocr_block` | OCR 命中时：`text` / `confidence` / `bounds` |
| `fallback` | 是否发生 UIA 未命中 → OCR 兜底 |
| `uia_ms` / `ocr_ms` | 分段耗时 |
| `candidates_count` | 候选数量 |

### 3.3 `action`（写操作）

| 字段 | 说明 |
| --- | --- |
| `action` | `click` / `type` / `hotkey` / `activate` |
| `x` / `y` / `button` / `clicks` | 点击位置与方式 |
| `hit_window` | 该坐标实际命中的顶层窗口（`hwnd` / `title` / `app_alias` / `process_name`） |
| `auto_raise` | 是否临时抬升 Z 序 |
| `verified` | `verify=true` 时的回读结果 |
| `input_len` / `input_sha` / `preview` | 输入文本的长度、`sha256[:8]`、掩码预览（不落明文） |
| `keys` | 快捷键组合 |
| `hwnd` / `title` / `process_name` | 激活类操作的目标与结果 |

### 3.4 `run_start` / `run_end`（Phase 2，任务归组）

- 规则：相邻两次工具调用间隔 > `UI_AGENT_AUDIT_RUN_GAP`（默认 8 秒）则切分为新的 `run_id`（格式 `<session_id>-r<N>`）
- `run_start` 记录起始时间与首个工具；`run_end` 记录结束时间、该 run 内工具序列与总数

## 4. 埋点位置（仅 3 处）

1. **MCP 层**：`uiagent/mcp_server.py` 的 `_guard`（统一异常边界，全部工具经此出口）
   - 增加工具名参数（8 个调用点各传一个字面量，如 `"ui_click"`）
   - `finally` 中落 `tool_call` / `tool_result` 两个事件
2. **定位层**：`uiagent/controller.py` 的 `locate()` 与 `locate_many()` 返回前落 `locate` 事件
   - `LocateResult` 已含 `source` / `confidence` / `element` / `ocr_block` / `hit_window` / `auto_raise` / `detail`，直接序列化，无需扩结构
3. **写操作层**：`controller.click()` / `type_text()` / `type_chinese()` / `hotkey()` / `activate_window()` 落 `action` 事件

CLI（`main.py`）复用同一 `AuditLogger`，通过 `setup_logging` 之外的独立初始化即可。

## 5. 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `UI_AGENT_AUDIT` | `1` | 总开关，`0` 完全关闭 |
| `UI_AGENT_AUDIT_DIR` | `D:\Projects\ui-agent\logs` | 日志目录 |
| `UI_AGENT_AUDIT_TEXT` | `mask` | 输入文本策略：`mask` / `hash` / `full` |
| `UI_AGENT_AUDIT_KEEP_DAYS` | `14` | 保留天数 |
| `UI_AGENT_AUDIT_RUN_GAP` | `8` | run 归组的静默间隔（秒） |

## 6. 输入文本脱敏策略

- `mask`（默认）：`input_len` + `input_sha256[:8]` + `preview`
  - 长度 ≥ 3 时 `preview` = 首字符 + `***`（如 `老***`）
  - 长度 < 3 时 `preview` = `***`
- `hash`：仅 `input_len` + `input_sha256[:8]`，无 `preview`
- `full`：记录原文（仅本地深度调试时临时启用）

`input_sha256[:8]` 的用途：不看原文也能判断"两次输入是否相同"，便于比对复现。

## 7. Phase 2 增强（本次一并实现）

- `run_id` 归组（见 3.4），便于整段回放一次自动化任务
- `python main.py audit --stats [--date YYYY-MM-DD]`：输出工具调用次数、`uia`/`ocr` 命中占比、兜底率、失败率、平均耗时
- `python main.py audit --tail`：实时跟读最新审计文件
- `python main.py audit --run <run_id>`：按 run 回放该次任务的完整调用序列

## 8. 非目标

- 不引入第三方依赖（仅标准库 + 已有依赖）
- 不采集屏幕内容、窗口文本等隐私数据（截图路径只记路径不记内容）
- 不改变工具契约：MCP 工具数量仍为 8，参数与返回字段保持兼容

## 9. 验收标准

1. 跑 `selfcheck` 与 MCP 自检，`logs\audit-*.jsonl` 中能看到 `tool_call` / `tool_result` / `locate` / `action` / `run_start` / `run_end` 六类事件，字段齐全
2. 输入含中文时 `input_len` / `input_sha` / `preview` 正确，且不含明文
3. 连续多轮调用能按 8 秒间隔切分出多个 `run_id`
4. `main.py audit --stats` / `--tail` / `--run` 可用
5. MCP selfcheck 只读用例 7/7 通过，工具返回结构与改动前一致
6. `UI_AGENT_AUDIT=0` 时零文件产生
*（内容由AI生成，仅供参考）*
