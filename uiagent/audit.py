"""审计日志（audit log）：JSONL 落盘，记录调用链 / 定位方式 / 写操作明细。

设计见 ``docs/audit-log-design.md``。要点：

* 目录 ``<项目根>/logs``（``UI_AGENT_AUDIT_DIR`` 可覆盖），
  文件名 ``audit-YYYYMMDD-<pid>.jsonl``（MCP stdio 每个宿主连接即一个进程）。
* 单文件超过 10 MB 轮转为 ``<同名>.1``；启动时清理超过保留天数的旧文件。
* UTF-8、每行一个完整 JSON 对象、``threading.Lock`` 串行化、每条 ``flush()``。
* **只写文件**：绝不写 stdout（stdout 只承载 MCP JSON-RPC），也不改动
  ``logging_utils`` 的 stderr 行为。
* ``UI_AGENT_AUDIT=0`` 时完全关闭：不建目录、不落文件。

事件类型：``tool_call`` / ``tool_result``（MCP 层）、``locate``（定位层）、
``action``（写操作层）、``run_start`` / ``run_end``（任务归组）。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

_LOG_PREFIX = "audit-"
_LOG_SUFFIX = ".jsonl"
_MAX_BYTES = 10 * 1024 * 1024
_FALSE_VALUES = {"0", "false", "no", "off", "n", "f"}
_TEXT_POLICIES = ("mask", "hash", "full")
_DEFAULT_KEEP_DAYS = 14
_DEFAULT_RUN_GAP = 8.0


# ===================================================================== 配置
def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else str(value)


def audit_enabled() -> bool:
    """审计总开关（``UI_AGENT_AUDIT``，默认开启）。"""
    return _env("UI_AGENT_AUDIT", "1").strip().lower() not in _FALSE_VALUES


def audit_dir() -> str:
    """审计日志目录（``UI_AGENT_AUDIT_DIR``，默认项目下 ``logs``）。"""
    configured = _env("UI_AGENT_AUDIT_DIR", os.path.join(_project_root(), "logs"))
    return os.path.abspath(os.path.expanduser(configured))


def keep_days() -> int:
    """日志保留天数（``UI_AGENT_AUDIT_KEEP_DAYS``，默认 14）。"""
    try:
        value = int(float(_env("UI_AGENT_AUDIT_KEEP_DAYS", str(_DEFAULT_KEEP_DAYS))))
    except (TypeError, ValueError):
        return _DEFAULT_KEEP_DAYS
    return max(0, value)


def run_gap_seconds() -> float:
    """run 归组的静默间隔（``UI_AGENT_AUDIT_RUN_GAP``，默认 8 秒）。"""
    try:
        value = float(_env("UI_AGENT_AUDIT_RUN_GAP", str(_DEFAULT_RUN_GAP)))
    except (TypeError, ValueError):
        return _DEFAULT_RUN_GAP
    return value if value > 0 else _DEFAULT_RUN_GAP


def record_text_policy() -> str:
    """输入文本策略（``UI_AGENT_AUDIT_TEXT``：mask / hash / full，默认 mask）。"""
    policy = _env("UI_AGENT_AUDIT_TEXT", "mask").strip().lower()
    return policy if policy in _TEXT_POLICIES else "mask"


# ===================================================================== 脱敏
def describe_text(text: Any, policy: Optional[str] = None) -> Dict[str, Any]:
    """按策略描述一段输入文本，返回可直接落盘的字段。

    * ``mask``（默认）：``input_len`` + ``input_sha`` + ``preview``（首字符 + ``***``）
    * ``hash``：``input_len`` + ``input_sha``（无预览）
    * ``full``：额外附带原文 ``text``（仅本地深度调试）
    """
    raw = "" if text is None else str(text)
    mode = (policy or record_text_policy()).strip().lower()
    data: Dict[str, Any] = {
        "input_len": len(raw),
        "input_sha": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8],
    }
    if mode == "full":
        data["text"] = raw
    elif mode == "hash":
        pass
    else:
        data["preview"] = (raw[0] + "***") if len(raw) >= 3 else "***"
    return data


def mask_args(args: Optional[Dict[str, Any]], policy: Optional[str] = None) -> Dict[str, Any]:
    """入参摘要：``text`` 字段按脱敏策略替换（避免明文落盘）。"""
    if not isinstance(args, dict):
        return {}
    data: Dict[str, Any] = {}
    for key, value in args.items():
        if key == "text" and isinstance(value, str):
            data[key] = describe_text(value, policy)
        else:
            data[key] = value
    return data


def iso_time(ts: Optional[float] = None) -> str:
    """ISO8601（带毫秒与时区）。"""
    moment = datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone()
    return moment.isoformat(timespec="milliseconds")


# ===================================================================== 写入器
class AuditLogger:
    """线程安全的 JSONL 审计写入器（进程内单例，见 :func:`get_audit_logger`）。"""

    def __init__(
        self,
        directory: Optional[str] = None,
        keep_days_override: Optional[int] = None,
        run_gap: Optional[float] = None,
        text_policy: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.enabled = audit_enabled() if enabled is None else bool(enabled)
        self.directory = os.path.abspath(directory or audit_dir())
        self.keep_days = keep_days() if keep_days_override is None else int(keep_days_override)
        self.run_gap = run_gap_seconds() if run_gap is None else float(run_gap)
        self.text_policy = text_policy or record_text_policy()
        self.session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"

        self._lock = threading.RLock()
        self._handle: Optional[Any] = None
        self._path = ""
        self._written = 0

        # run 归组状态
        self._run_index = 0
        self._run_id: Optional[str] = None
        self._run_started = 0.0
        self._run_started_at = ""
        self._run_tools: List[str] = []
        self._run_events = 0
        self._run_errors = 0
        self._last_ts = 0.0

        if self.enabled:
            with self._lock:
                self._purge_old_locked()

    # ------------------------------------------------------------ 文件管理
    def _purge_old_locked(self) -> None:
        if self.keep_days <= 0:
            return
        cutoff = time.time() - self.keep_days * 86400
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            if not name.startswith(_LOG_PREFIX):
                continue
            path = os.path.join(self.directory, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:  # pragma: no cover - 文件被占用时跳过
                continue

    def _open_locked(self) -> None:
        os.makedirs(self.directory, exist_ok=True)
        self._path = os.path.join(
            self.directory, f"{_LOG_PREFIX}{time.strftime('%Y%m%d')}-{os.getpid()}{_LOG_SUFFIX}"
        )
        try:
            self._written = os.path.getsize(self._path)
        except OSError:
            self._written = 0
        self._handle = open(self._path, "a", encoding="utf-8")

    def _rotate_locked(self) -> None:
        if self._handle is None:
            self._open_locked()
            return
        self._handle.close()
        try:
            os.replace(self._path, self._path + ".1")
        except OSError:  # pragma: no cover - 轮转失败时直接续写原文件
            self._handle = open(self._path, "a", encoding="utf-8")
            return
        self._written = 0
        self._handle = open(self._path, "a", encoding="utf-8")

    def _write_locked(self, event: str, payload: Dict[str, Any], run_id: Optional[str]) -> Dict[str, Any]:
        """写一条记录（调用方需持有锁）。"""
        if self._handle is None:
            self._open_locked()
        if self._written >= _MAX_BYTES:
            self._rotate_locked()

        record: Dict[str, Any] = {
            "ts": iso_time(),
            "session_id": self.session_id,
            "event": event,
        }
        if run_id:
            record["run_id"] = run_id
        record.update(payload)

        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        assert self._handle is not None
        self._handle.write(line)
        self._handle.flush()
        self._written += len(line.encode("utf-8"))
        return record

    # ------------------------------------------------------------ run 归组
    def _close_run_locked(self, now: Optional[float] = None) -> None:
        if self._run_id is None:
            return
        moment = now if now is not None else time.time()
        self._write_locked(
            "run_end",
            {
                "started_at": self._run_started_at,
                "ended_at": iso_time(moment),
                "duration_ms": round((moment - self._run_started) * 1000.0, 3),
                "tools": list(self._run_tools),
                "tool_count": len(self._run_tools),
                "event_count": self._run_events,
                "error_count": self._run_errors,
            },
            self._run_id,
        )
        self._run_id = None

    def _ensure_run_locked(self, now: float, tool: Optional[str]) -> None:
        """相邻活动间隔超过 ``run_gap`` 时切分新的 run_id。"""
        if self._run_id is not None and (now - self._last_ts) <= self.run_gap:
            return
        if self._run_id is not None:
            self._close_run_locked(now)
        self._run_index += 1
        self._run_id = f"{self.session_id}-r{self._run_index}"
        self._run_started = now
        self._run_started_at = iso_time(now)
        self._run_tools = []
        self._run_events = 0
        self._run_errors = 0
        self._write_locked(
            "run_start",
            {"started_at": self._run_started_at, "first_tool": tool or ""},
            self._run_id,
        )

    # ------------------------------------------------------------ 公开接口
    @property
    def path(self) -> str:
        """当前日志文件路径（尚未写过的为空串）。"""
        return self._path

    @property
    def run_id(self) -> Optional[str]:
        """当前 run_id（尚未开始时为 ``None``）。"""
        return self._run_id

    def log(self, event: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """落一条事件；任何异常都不影响主流程（审计失败静默）。"""
        if not self.enabled:
            return None
        try:
            with self._lock:
                now = time.time()
                tool = fields.get("tool")
                self._ensure_run_locked(now, str(tool) if tool else None)
                self._last_ts = now
                if event == "tool_call" and tool:
                    self._run_tools.append(str(tool))
                self._run_events += 1
                if event == "tool_result" and fields.get("ok") is False:
                    self._run_errors += 1
                return self._write_locked(event, fields, self._run_id)
        except Exception:  # pragma: no cover - 审计失败绝不影响内核
            return None

    def log_action(self, action: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """落一条写操作事件。"""
        payload: Dict[str, Any] = {"action": action}
        payload.update(fields)
        return self.log("action", **payload)

    def close(self) -> None:
        """收尾：补一条 ``run_end`` 并关闭文件句柄。"""
        if not self.enabled:
            return
        with self._lock:
            try:
                self._close_run_locked()
            except Exception:  # pragma: no cover
                pass
            if self._handle is not None:
                try:
                    self._handle.flush()
                    self._handle.close()
                except Exception:  # pragma: no cover
                    pass
                self._handle = None


# ===================================================================== 单例
_logger: Optional[AuditLogger] = None
_logger_lock = threading.Lock()


def get_audit_logger() -> Optional[AuditLogger]:
    """取进程级审计写入器；``UI_AGENT_AUDIT=0`` 时返回 ``None``。"""
    global _logger
    if not audit_enabled():
        return None
    if _logger is None:
        with _logger_lock:
            if _logger is None:
                try:
                    _logger = AuditLogger()
                    atexit.register(_logger.close)
                except Exception:  # pragma: no cover - 目录不可写等
                    return None
    return _logger


def reset_audit_logger() -> None:
    """关闭并丢弃单例（测试用）。"""
    global _logger
    with _logger_lock:
        if _logger is not None:
            _logger.close()
            _logger = None


# ===================================================================== 读取
def _log_files(directory: Optional[str] = None, include_rotated: bool = False) -> List[str]:
    base = os.path.abspath(directory or audit_dir())
    try:
        names = os.listdir(base)
    except OSError:
        return []
    files: List[str] = []
    for name in names:
        if not name.startswith(_LOG_PREFIX):
            continue
        if name.endswith(_LOG_SUFFIX) or (include_rotated and name.endswith(_LOG_SUFFIX + ".1")):
            files.append(os.path.join(base, name))
    files.sort(key=lambda path: (os.path.getmtime(path), path))
    return files


def list_log_files(directory: Optional[str] = None, include_rotated: bool = False) -> List[str]:
    """列出审计文件（按修改时间升序）。"""
    return _log_files(directory, include_rotated=include_rotated)


def latest_log_file(directory: Optional[str] = None) -> str:
    """最新的审计文件（``--tail`` 用）。"""
    files = _log_files(directory)
    return files[-1] if files else ""


def iter_records(
    directory: Optional[str] = None,
    date: Optional[str] = None,
    run_id: Optional[str] = None,
    include_rotated: bool = False,
) -> Iterator[Dict[str, Any]]:
    """按时间顺序迭代审计记录。

    :param date: ``YYYY-MM-DD``，按文件名中的日期过滤
    :param run_id: 精确匹配或前缀匹配（如只给 session 前缀）
    """
    needle = ""
    if date:
        needle = f"{_LOG_PREFIX}{str(date).replace('-', '')}-"
    for path in _log_files(directory, include_rotated=include_rotated):
        if needle and needle not in os.path.basename(path):
            continue
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                if run_id:
                    record_run = str(record.get("run_id") or "")
                    if record_run != run_id and not record_run.startswith(run_id):
                        continue
                yield record


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总统计：调用次数 / 命中来源占比 / 兜底率 / 失败率 / 平均耗时。"""
    events: Dict[str, int] = {}
    tool_calls: Dict[str, int] = {}
    sessions = set()
    runs = set()
    durations: List[float] = []
    failures = 0
    results = 0
    locate_total = locate_found = locate_uia = locate_ocr = locate_fallback = 0
    first_ts = last_ts = ""

    for record in records:
        event = str(record.get("event") or "")
        events[event] = events.get(event, 0) + 1
        if record.get("session_id"):
            sessions.add(str(record["session_id"]))
        if record.get("run_id"):
            runs.add(str(record["run_id"]))
        ts = str(record.get("ts") or "")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        if event == "tool_call":
            tool = str(record.get("tool") or "")
            if tool:
                tool_calls[tool] = tool_calls.get(tool, 0) + 1
        elif event == "tool_result":
            results += 1
            if record.get("ok") is False:
                failures += 1
            duration = record.get("duration_ms")
            if isinstance(duration, (int, float)):
                durations.append(float(duration))
        elif event == "locate":
            locate_total += 1
            source = str(record.get("source") or "")
            if record.get("found"):
                locate_found += 1
            if source == "uia":
                locate_uia += 1
            elif source == "ocr":
                locate_ocr += 1
                locate_fallback += 1

    def _ratio(part: int, whole: int) -> float:
        return round(part / whole, 4) if whole else 0.0

    return {
        "records": len(records),
        "events": events,
        "sessions": len(sessions),
        "runs": len(runs),
        "period": {"start": first_ts, "end": last_ts},
        "tool_calls": tool_calls,
        "tool_call_total": sum(tool_calls.values()),
        "tool_result_total": results,
        "failure_count": failures,
        "failure_rate": _ratio(failures, results),
        "avg_duration_ms": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "max_duration_ms": round(max(durations), 3) if durations else 0.0,
        "locate": {
            "total": locate_total,
            "found": locate_found,
            "uia": locate_uia,
            "ocr": locate_ocr,
            "uia_ratio": _ratio(locate_uia, locate_total),
            "ocr_ratio": _ratio(locate_ocr, locate_total),
            "fallback_count": locate_fallback,
            "fallback_rate": _ratio(locate_fallback, locate_total),
        },
    }
