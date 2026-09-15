"""应用启动入口解析与启动（P2 改动点 #13）。

补齐《应用唤起与定位优化方案》S4（未启动）路径：把「应用别名 / exe 名」解析为可执行入口，
按 PID 启动并等待窗口就绪。全部为**新增**能力，不改变既有 ``ui_window_list`` /
``ui_activate_window`` / ``ui_app_ensure`` 在 S1~S3 的语义。

解析链（方案 3.3）
------------------
``alias_table`` → ``app_paths`` → ``start_menu`` → ``uwp`` → ``search_fallback``

* ``alias_table``：``uiagent/apps.yaml`` 显式别名映射（改动点 #14），缺失时仅用内置表；
* ``app_paths``：注册表 ``App Paths``（HKCU / HKLM / WOW6432Node），并兜底 ``PATH`` 查找；
* ``start_menu``：开始菜单 ``.lnk`` 索引（ProgramData + 用户 Start Menu），直接以快捷方式启动；
* ``uwp``：``Get-StartApps`` 的 ``AppID``，经 ``shell:AppsFolder`` 启动；
* ``search_fallback``：**未实现**（方案 RF8 已标注其受输入法/焦点影响不稳定）。
  解析不到时返回 ``resolved_by="none"`` 与 ``hint``，由上层改用显式路径，不做键盘模拟。

安全约束（方案 RF5 / RF6）
--------------------------
1. ``UIAGENT_LAUNCH_ENABLED=0`` 时 ``launch()`` 直接拒绝，回到「仅能操作已运行应用」；
2. ``UIAGENT_LAUNCH_ALLOWLIST`` 非空时严格校验（别名 / 进程名 / 目标可执行文件名任一命中）；
3. 解析出多个候选时**不自动选择**，返回 ``candidates`` 与 ``degraded_reason="ambiguous_candidates"``；
4. ``dry_run=True`` 只解析不启动，不产生任何进程；
5. ``wait_window()`` 超时返回 ``state="launching"``（非异常）。

审计：每次启动落一条 ``launch`` 事件（``app`` / ``resolved_by`` / ``target_path`` / ``pid`` /
``launch_ms`` / ``wait_ms`` / ``dry_run``），见 :meth:`AppLauncher.launch`。
"""

from __future__ import annotations

import csv
import ctypes
import os
import shlex
import shutil
import subprocess
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .accessibility import win32_windows as w32
from .logging_utils import get_logger

logger = get_logger("launcher")

#: 别名表文件（改动点 #14）；不存在时仅使用内置别名
DEFAULT_APPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.yaml")

#: 环境变量（改动点 #16）
LAUNCH_ENABLED_ENV = "UIAGENT_LAUNCH_ENABLED"
LAUNCH_ALLOWLIST_ENV = "UIAGENT_LAUNCH_ALLOWLIST"

#: 冷启动分级默认超时（方案 4.2（2）：轻量 ≤1.5 s / 常规 ≤8 s / 重量 ≤20 s 为目标值，
#: 这里的 timeout 是「放弃等待」的宽松上限，超时返回 state=launching 而非失败）
TIER_TIMEOUTS: Dict[str, float] = {"light": 8.0, "normal": 15.0, "heavy": 25.0}

#: 应用分级（用于选择默认 timeout 与文档中的 SLA 归组）
APP_TIERS: Dict[str, Tuple[str, ...]] = {
    "light": ("notepad", "calculator", "calc", "mspaint", "绘图", "画图", "记事本", "计算器"),
    "normal": ("chrome", "msedge", "explorer", "cmd", "powershell", "pwsh", "wt", "windowsterminal", "firefox"),
    "heavy": ("weixin", "wechat", "qq", "word", "excel", "powerpnt", "outlook", "code", "devenv", "photoshop"),
}

#: 内置别名兜底表（``apps.yaml`` 缺失或被用户删改时的最小可用集，不启动任何未安装应用）
_BUILTIN_APPS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "notepad",
        "aliases": ("notepad", "notepad.exe", "记事本"),
        "path": "notepad.exe",
        "tier": "light",
    },
    {
        "id": "calc",
        "aliases": ("calc", "calc.exe", "calculator", "计算器"),
        "path": "calc.exe",
        "tier": "light",
        "appid": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
    },
    {
        "id": "mspaint",
        "aliases": ("mspaint", "mspaint.exe", "paint", "画图"),
        "path": "mspaint.exe",
        "tier": "light",
    },
    {
        "id": "explorer",
        "aliases": ("explorer", "explorer.exe", "资源管理器", "文件资源管理器"),
        "path": "explorer.exe",
        "tier": "normal",
    },
)

_START_MENU_DIRS = (
    os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
    os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
    ),
)

#: ``ShellExecuteExW`` 标志
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_FLAG_NO_UI = 0x00000400
_SEE_MASK_NOASYNC = 0x00000100
_SW_SHOWNORMAL = 1

#: ``subprocess`` 子进程分离标志（GUI 应用不随本进程退出而退出）
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_PS_TIMEOUT = 8.0


class _SHELLEXECUTEINFOW(ctypes.Structure):
    """``SHELLEXECUTEINFOW``（仅用到前段字段，``hIcon``/``hMonitor`` 为同一 union 槽）。"""

    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    )


# ===================================================================== 环境开关
def _flag(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        raw = default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "n", "f")


def launch_enabled() -> bool:
    """启动能力总开关（``UIAGENT_LAUNCH_ENABLED``，默认 ``1``）。"""
    return _flag(LAUNCH_ENABLED_ENV, "1")


def launch_allowlist() -> Tuple[str, ...]:
    """启动白名单（``UIAGENT_LAUNCH_ALLOWLIST``，逗号分隔）。空 = 不校验。"""
    raw = str(os.environ.get(LAUNCH_ALLOWLIST_ENV, "") or "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _normalize(value: Any) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    return text.lower()


def _stem(value: Any) -> str:
    text = _normalize(value)
    for suffix in (".exe", ".lnk", ".com", ".bat"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _basename(value: Any) -> str:
    return str(value or "").replace("/", "\\").rsplit("\\", 1)[-1].lower()


def _match_keys(app: Any) -> List[str]:
    """把 ``app`` 归一化为**按序尝试**的窗口线索键列表（支持多别名，供打包应用兜底命中）。

    ``app`` 可以是单个字符串，也可以是键列表；每个键同时给出原形与去后缀（``.exe``）形式。
    """
    raw: List[Any] = list(app) if isinstance(app, (list, tuple, set)) else [app]
    keys: List[str] = []
    seen: set = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        for variant in (text, _stem(text)):
            low = variant.lower()
            if variant and low not in seen:
                seen.add(low)
                keys.append(variant)
    return keys


def app_match_keys(app: Any) -> List[str]:
    """把应用名扩展为多档窗口线索键：原形 / 去后缀 / **别名表命中的别名**（P2 打包应用兜底）。

    供 ``controller.ensure_app`` / ``app_status`` / ``ui_wait_window`` 复用：Win11 打包应用
    （记事本、计算器、画图）的窗口常挂在宿主进程上，只有别名（多为中文名，与窗口标题一致）
    才能匹配到，单靠 exe 名会误判为 ``not_running``。
    """
    keys = _match_keys(app)
    extra: List[str] = []
    text = str(app or "").strip()
    if text:
        normalized, stem = _normalize(text), _stem(text)
        try:
            for entry in get_launcher().alias_table():
                if normalized in entry["keys"] or stem in entry["keys"] or normalized in _match_keys(entry.get("id")):
                    extra.extend(list(entry.get("aliases") or ()))
                    if entry.get("path"):
                        extra.append(str(entry["path"]))
        except Exception as exc:  # noqa: BLE001 - 别名表不可用时退回原键
            logger.debug("别名表扩展失败，仅用原键：%s", exc)
    return _match_keys([*keys, *extra])


def _wait_keys(app: Any, candidate: Optional[Dict[str, Any]], target: str = "", appid: str = "") -> List[str]:
    """构造 ``wait_window`` 的应用线索键（别名表命中时带上全部别名）。

    打包应用（Win11 记事本 / 计算器 / 画图）的窗口常挂在 ``ApplicationFrameHost`` 等宿主进程上，
    仅靠 exe 名匹配不到，因此把别名（含中文名，通常与窗口标题一致）一并传入按序尝试。
    """
    raw: List[Any] = [app, target]
    raw.extend(list((candidate or {}).get("aliases") or ()))
    if appid:
        stem = str(appid).split("!", 1)[0]
        raw.extend([stem, stem.split("_", 1)[0]])
    return _match_keys(raw)


# ===================================================================== 别名表（#14）
def load_alias_table(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取 ``apps.yaml`` 别名表；返回归一化条目列表（失败时静默降级为内置表）。"""
    entries: List[Dict[str, Any]] = []
    target = os.path.abspath(path or DEFAULT_APPS_FILE)
    raw_apps: Any = None
    if os.path.isfile(target):
        try:
            import yaml  # 局部导入：无 PyYAML 时仍可用内置表

            with open(target, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            raw_apps = data.get("apps") if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 - 配置错误不阻断启动能力
            logger.warning("读取别名表 %s 失败，改用内置别名：%s", target, exc)
    if isinstance(raw_apps, dict):
        for key, value in raw_apps.items():
            item = dict(value) if isinstance(value, dict) else {"path": value}
            aliases = item.get("aliases") or [key]
            if isinstance(aliases, str):
                aliases = [aliases]
            entry = {
                "id": str(item.get("id") or key),
                "keys": tuple({_normalize(key), *(_normalize(a) for a in aliases), *(_stem(a) for a in aliases)}),
                "aliases": tuple(str(a) for a in aliases),
                "path": str(item.get("path") or ""),
                "args": str(item.get("args") or ""),
                "appid": str(item.get("appid") or ""),
                "tier": str(item.get("tier") or ""),
                "source": "alias_table",
                "config": target,
            }
            if entry["path"] or entry["appid"]:
                entries.append(entry)
    for builtin in _BUILTIN_APPS:
        aliases = tuple(builtin.get("aliases") or ())
        if any(_normalize(a) in {k for e in entries for k in e["keys"]} for a in aliases):
            continue  # 配置文件已显式定义该别名，以内置表为准
        entries.append(
            {
                "id": builtin["id"],
                "keys": tuple({_normalize(builtin["id"]), *(_normalize(a) for a in aliases), *(_stem(a) for a in aliases)}),
                "aliases": tuple(str(a) for a in aliases),
                "path": builtin.get("path", ""),
                "args": "",
                "appid": builtin.get("appid", ""),
                "tier": builtin.get("tier", ""),
                "source": "alias_table",
                "config": "builtin",
            }
        )
    return entries


# ===================================================================== 分级
def classify_tier(app: Any, candidate: Optional[Dict[str, Any]] = None) -> str:
    """把应用归入 ``light`` / ``normal`` / ``heavy``（用于选择默认 timeout）。"""
    explicit = _normalize((candidate or {}).get("tier"))
    if explicit in TIER_TIMEOUTS:
        return explicit
    tokens = {
        _stem(app),
        _normalize(app),
        _stem((candidate or {}).get("target")),
        _stem((candidate or {}).get("name")),
    }
    for tier, names in APP_TIERS.items():
        if tokens & {_normalize(n) for n in names}:
            return tier
    return "normal"


def default_timeout(app: Any, candidate: Optional[Dict[str, Any]] = None) -> float:
    return TIER_TIMEOUTS[classify_tier(app, candidate)]


# ===================================================================== 解析器
class AppLauncher:
    """启动入口解析 / 启动 / 就绪等待（P2 改动点 #13）。"""

    def __init__(self, apps_file: Optional[str] = None, index_ttl: float = 300.0) -> None:
        self.apps_file = os.path.abspath(apps_file or DEFAULT_APPS_FILE)
        self.index_ttl = float(index_ttl)
        self._alias_cache: Optional[List[Dict[str, Any]]] = None
        self._alias_mtime: float = -1.0
        self._start_menu_cache: Optional[List[Tuple[str, str]]] = None
        self._start_menu_at: float = 0.0
        self._start_apps_cache: Optional[List[Dict[str, str]]] = None
        self._start_apps_at: float = 0.0

    # ------------------------------------------------------------ 别名表
    def alias_table(self, refresh: bool = False) -> List[Dict[str, Any]]:
        mtime = os.path.getmtime(self.apps_file) if os.path.isfile(self.apps_file) else 0.0
        if refresh or self._alias_cache is None or mtime != self._alias_mtime:
            self._alias_cache = load_alias_table(self.apps_file)
            self._alias_mtime = mtime
        return self._alias_cache

    def _resolve_alias(self, key: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for entry in self.alias_table():
            if key in entry["keys"] or _stem(key) in entry["keys"]:
                found.append(
                    {
                        "source": "alias_table",
                        "name": entry["id"],
                        "label": key,
                        "target": entry["path"] or entry["appid"],
                        "args": entry["args"],
                        "appid": entry["appid"],
                        "tier": entry["tier"],
                        "aliases": tuple(entry.get("aliases") or ()),
                        "config": entry["config"],
                    }
                )
        return found

    # ------------------------------------------------------------ App Paths（含 PATH 兜底）
    def _resolve_app_paths(self, key: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        stem = _stem(key)
        names = {key, stem, f"{stem}.exe"}
        try:
            import winreg  # type: ignore
        except Exception:  # pragma: no cover - 非 Windows
            winreg = None  # type: ignore
        if winreg is not None:
            roots = (
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
            )
            for hive, sub in roots:
                try:
                    with winreg.OpenKey(hive, sub) as root:
                        count = winreg.QueryInfoKey(root)[0]
                        for index in range(count):
                            try:
                                name = winreg.EnumKey(root, index)
                            except OSError:
                                break
                            if _normalize(name) not in names and _stem(name) != stem:
                                continue
                            try:
                                with winreg.OpenKey(root, name) as item:
                                    path = str(winreg.QueryValue(item, "") or "")
                            except OSError:
                                path = ""
                            if path:
                                found.append(
                                    {
                                        "source": "app_paths",
                                        "name": _basename(path) or name,
                                        "label": name,
                                        "target": path,
                                        "args": "",
                                        "appid": "",
                                        "tier": "",
                                        "registry": sub,
                                    }
                                )
                except OSError:
                    continue
        if not found:
            for probe in (key, f"{stem}.exe"):
                which = shutil.which(probe)
                if which:
                    found.append(
                        {
                            "source": "app_paths",
                            "name": _basename(which),
                            "label": probe,
                            "target": which,
                            "args": "",
                            "appid": "",
                            "tier": "",
                            "registry": "PATH",
                        }
                    )
                    break
        return found

    # ------------------------------------------------------------ 开始菜单
    def start_menu_index(self, refresh: bool = False) -> List[Tuple[str, str]]:
        now = time.time()
        if (
            not refresh
            and self._start_menu_cache is not None
            and (now - self._start_menu_at) < self.index_ttl
        ):
            return self._start_menu_cache
        items: List[Tuple[str, str]] = []
        for directory in _START_MENU_DIRS:
            if not directory or not os.path.isdir(directory):
                continue
            for root, _dirs, files in os.walk(directory):
                for name in files:
                    if not name.lower().endswith(".lnk"):
                        continue
                    items.append((_stem(name), os.path.join(root, name)))
                    if len(items) >= 4000:
                        break
                if len(items) >= 4000:
                    break
        self._start_menu_cache = items
        self._start_menu_at = now
        return items

    def _resolve_start_menu(self, key: str) -> List[Dict[str, Any]]:
        stem = _stem(key)
        index = self.start_menu_index()
        exact = [item for item in index if item[0] == stem]
        fuzzy = [] if exact else [item for item in index if stem and stem in item[0]]
        return [
            {
                "source": "start_menu",
                "name": stem_name,
                "label": stem_name,
                "target": path,
                "args": "",
                "appid": "",
                "tier": "",
            }
            for stem_name, path in (exact or fuzzy)[:5]
        ]

    # ------------------------------------------------------------ UWP / 商店应用
    def start_apps_index(self, refresh: bool = False) -> List[Dict[str, str]]:
        now = time.time()
        if (
            not refresh
            and self._start_apps_cache is not None
            and (now - self._start_apps_at) < self.index_ttl
        ):
            return self._start_apps_cache
        items: List[Dict[str, str]] = []
        script = "Get-StartApps | Select-Object Name,AppID | ConvertTo-Csv -NoTypeInformation"
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=_PS_TIMEOUT,
                check=False,
            )
            if completed.returncode == 0:
                text = completed.stdout.decode("utf-8", errors="replace")
                for row in csv.reader(text.splitlines()):
                    if len(row) >= 2 and row[0].strip() and row[0].strip().lower() != "name":
                        items.append({"name": row[0].strip(), "appid": row[1].strip()})
        except Exception as exc:  # noqa: BLE001 - UWP 索引失败只影响该解析档位
            logger.debug("Get-StartApps 索引失败：%s", exc)
        self._start_apps_cache = items
        self._start_apps_at = now
        return items

    def _resolve_uwp(self, key: str) -> List[Dict[str, Any]]:
        stem = _stem(key)
        found: List[Dict[str, Any]] = []
        for item in self.start_apps_index():
            if not item["appid"]:
                continue
            name = _normalize(item["name"])
            if name == stem or stem in name or name in stem:
                found.append(
                    {
                        "source": "uwp",
                        "name": item["name"],
                        "label": item["name"],
                        "target": f"shell:AppsFolder\\{item['appid']}",
                        "args": "",
                        "appid": item["appid"],
                        "tier": "",
                    }
                )
        return found[:5]

    # ------------------------------------------------------------ 解析入口
    def resolve(self, app: str) -> Dict[str, Any]:
        """解析应用启动入口（**只读**：不启动任何进程）。

        :return: ``{"ok", "app", "resolved_by", "target_path", "entry_type", "args",
            "appid", "tier", "ambiguous", "candidates", "elapsed_ms", "hint"}``
        """
        started = time.perf_counter()
        key = _normalize(app)
        payload: Dict[str, Any] = {
            "ok": False,
            "app": str(app or ""),
            "resolved_by": "none",
            "target_path": "",
            "entry_type": "",
            "args": "",
            "appid": "",
            "tier": "",
            "ambiguous": False,
            "candidates": [],
            "elapsed_ms": 0.0,
            "hint": "",
        }
        if not key:
            payload["error"] = "app 不能为空"
            return payload

        candidates: List[Dict[str, Any]] = []
        # 0) 显式路径（绝对路径 / 含分隔符）
        explicit = str(app or "").strip().strip('"')
        if ("\\" in explicit or "/" in explicit) and os.path.exists(explicit):
            candidates.append(
                {
                    # 补充取值（方案解析链之外的最高优先级直通档）：用户直接给出可执行文件路径
                    "source": "explicit_path",
                    "name": _basename(explicit),
                    "label": explicit,
                    "target": os.path.abspath(explicit),
                    "args": "",
                    "appid": "",
                    "tier": "",
                    "config": "explicit_path",
                }
            )
        if not candidates:
            candidates = self._resolve_alias(key)
        if not candidates:
            candidates = self._resolve_app_paths(key)
        if not candidates:
            candidates = self._resolve_start_menu(key)
        if not candidates:
            candidates = self._resolve_uwp(key)

        # 去重（同一目标只保留首个档位）
        deduped: List[Dict[str, Any]] = []
        seen: set = set()
        for item in candidates:
            signature = (str(item.get("target") or item.get("appid") or "")).lower()
            if not signature or signature in seen:
                continue
            seen.add(signature)
            deduped.append(item)

        payload["candidates"] = deduped
        if deduped:
            payload["resolved_by"] = str(deduped[0]["source"])
            if len(deduped) == 1:
                best = deduped[0]
                payload.update(
                    {
                        "ok": True,
                        "target_path": str(best.get("target") or ""),
                        "entry_type": (
                            "shortcut"
                            if str(best.get("target") or "").lower().endswith(".lnk")
                            else ("uwp" if best.get("appid") else "executable")
                        ),
                        "args": str(best.get("args") or ""),
                        "appid": str(best.get("appid") or ""),
                        "tier": classify_tier(app, best),
                    }
                )
            else:
                payload["ambiguous"] = True
                payload["tier"] = classify_tier(app, deduped[0])
                payload["hint"] = (
                    f"解析出 {len(deduped)} 个候选，不自动选择（方案 RF5）；"
                    "请指定确切别名/绝对路径，或改用 search_fallback 之外的显式入口"
                )
        else:
            payload["hint"] = (
                "别名表 / 注册表 App Paths / PATH / 开始菜单 / UWP 均未解析到入口。"
                "可改用绝对路径重试；Win 键搜索兜底（search_fallback）本版本未实现（方案 RF8）"
            )
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return payload

    # ------------------------------------------------------------ 白名单校验
    def allowlist_check(self, app: Any, resolved: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """校验白名单（``UIAGENT_LAUNCH_ALLOWLIST`` 非空时严格匹配）。"""
        allow = launch_allowlist()
        if not allow:
            return {"ok": True, "checked": False, "allowlist": []}
        tokens = {
            _normalize(app),
            _stem(app),
            _normalize((resolved or {}).get("name")),
            _stem((resolved or {}).get("name")),
            _normalize((resolved or {}).get("target")),
            _basename((resolved or {}).get("target")),
            _stem(_basename((resolved or {}).get("target"))),
        }
        tokens = {t for t in tokens if t}
        allow_set = {item for item in allow} | {_stem(item) for item in allow}
        return {
            "ok": bool(tokens & allow_set),
            "checked": True,
            "allowlist": list(allow),
            "tokens": sorted(tokens),
        }

    # ------------------------------------------------------------ 启动
    def launch(
        self,
        app: str,
        args: str = "",
        working_dir: str = "",
        method: str = "auto",
        dry_run: bool = False,
        timeout: Optional[float] = None,
        wait: bool = True,
        stable_ms: float = w32.BOUNDS_STABLE_MS,
    ) -> Dict[str, Any]:
        """解析并启动应用，随后等待窗口就绪（写操作，需白名单/开关授权）。

        :param dry_run: 仅解析启动入口并返回，**不产生任何进程**（方案 3.4 / 验收 ③）
        :param method: ``auto`` / ``exec``（ShellExecuteEx）/ ``process``（CreateProcess）/ ``uwp``
        :param timeout: 等待窗口就绪的超时（缺省按应用分级，见 :data:`TIER_TIMEOUTS`）
        :return: ``{"ok", "app", "resolved_by", "target_path", "pid", "hwnd", "launch_ms",
            "wait_ms", "state", "degraded", "degraded_reason", "candidates", "dry_run", "hint"}``
        """
        from .audit import get_audit_logger

        audit = get_audit_logger()
        started = time.perf_counter()
        resolved = self.resolve(app)
        tier = str(resolved.get("tier") or classify_tier(app, None))
        budget = float(timeout) if timeout and float(timeout) > 0 else TIER_TIMEOUTS.get(tier, 15.0)

        payload: Dict[str, Any] = {
            "ok": False,
            "app": str(app or ""),
            "resolved_by": str(resolved.get("resolved_by") or "none"),
            "target_path": str(resolved.get("target_path") or ""),
            "pid": 0,
            "hwnd": 0,
            "launch_ms": 0.0,
            "wait_ms": 0.0,
            "state": "not_running",
            "tier": tier,
            "timeout": budget,
            "dry_run": bool(dry_run),
            "degraded": False,
            "degraded_reason": "",
            "candidates": resolved.get("candidates") or [],
            "elapsed_ms": 0.0,
            "hint": "",
        }

        def _finish(ok: bool, state: str, reason: str = "", error: str = "") -> Dict[str, Any]:
            payload["ok"] = bool(ok)
            payload["state"] = state
            payload["degraded"] = bool(reason)
            payload["degraded_reason"] = reason
            if error:
                payload["error"] = error
            if not payload["hint"] and resolved.get("hint"):
                payload["hint"] = str(resolved["hint"])
            payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            if audit is not None:
                audit.log_launch(
                    app=payload["app"],
                    resolved_by=payload["resolved_by"],
                    target_path=payload["target_path"],
                    pid=payload["pid"],
                    hwnd=payload["hwnd"],
                    launch_ms=payload["launch_ms"],
                    wait_ms=payload["wait_ms"],
                    state=payload["state"],
                    dry_run=payload["dry_run"],
                    degraded=payload["degraded"],
                    degraded_reason=payload["degraded_reason"],
                    candidates_count=len(payload["candidates"]),
                    enabled=launch_enabled(),
                )
            return payload

        # ① 开关（UIAGENT_LAUNCH_ENABLED）
        if not launch_enabled():
            return _finish(
                False,
                "not_running",
                "launch_disabled",
                f"启动能力已禁用（{LAUNCH_ENABLED_ENV}=0）：回到「仅能操作已运行应用」",
            )
        # ② 解析结果
        if resolved.get("ambiguous"):
            return _finish(
                False,
                "not_running",
                "ambiguous_candidates",
                f"解析出 {len(payload['candidates'])} 个候选，不自动启动（方案 RF5）",
            )
        if not resolved.get("ok"):
            return _finish(False, "not_running", "window_not_found", str(resolved.get("hint") or "未解析到启动入口"))
        # ③ 白名单（RF6）
        allowed = self.allowlist_check(app, resolved.get("candidates", [None])[0] if resolved.get("candidates") else None)
        if not allowed["ok"]:
            return _finish(
                False,
                "not_running",
                "not_allowlisted",
                f"app={app!r} 不在启动白名单内（{LAUNCH_ALLOWLIST_ENV}）：{allowed['allowlist']}",
            )
        target = str(resolved.get("target_path") or "")
        appid = str(resolved.get("appid") or "")
        merged_args = str(args or resolved.get("args") or "")

        # ④ dry_run：只解析不启动
        if dry_run:
            payload["hint"] = "dry_run=True：仅解析入口，未产生任何进程"
            return _finish(True, "dry_run")

        # ⑤ 真实启动
        mode = str(method or "auto").lower()
        if mode == "auto":
            mode = "uwp" if (appid and not target) else "exec"
        try:
            if mode == "uwp":
                pid, detail = self._launch_uwp(appid or target)
            elif mode == "process":
                pid, detail = self._launch_process(target, merged_args, working_dir)
            else:
                pid, detail = self._launch_exec(target, merged_args, working_dir)
        except Exception as exc:  # noqa: BLE001 - 启动失败按降级返回
            return _finish(False, "not_running", "launch_failed", f"{type(exc).__name__}: {exc}")
        payload["launch_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        payload["pid"] = int(pid or 0)
        payload["launch_detail"] = detail
        if not pid and not detail.get("started"):
            return _finish(False, "not_running", "launch_failed", str(detail.get("error") or "启动调用未成功"))

        # ⑥ 等待窗口就绪（优先 PID；同时给多档应用线索，供「启动桩」/打包应用兜底）
        if wait:
            candidate = (resolved.get("candidates") or [None])[0]
            wait_keys = _wait_keys(app, candidate, target, appid)
            waited = self.wait_window(
                pid=payload["pid"],
                app=wait_keys,
                timeout=budget,
                stable_ms=stable_ms,
            )
            payload["wait_ms"] = float(waited.get("waited_ms") or 0.0)
            payload["hwnd"] = int(waited.get("hwnd") or 0)
            payload["wait_detail"] = waited
            if waited.get("ok"):
                payload["state"] = str(waited.get("state") or "visible")
                reason = str(waited.get("degraded_reason") or "")
                return _finish(True, payload["state"], reason)
            return _finish(
                False,
                "launching",
                str(waited.get("degraded_reason") or "launch_timeout"),
                f"启动已发起，但 {budget:.1f}s 内未等到可定位窗口（pid={payload['pid']}）",
            )
        return _finish(True, "launching")

    # ------------------------------------------------------------ 启动实现
    def _launch_exec(self, target: str, args: str, working_dir: str) -> Tuple[int, Dict[str, Any]]:
        """``ShellExecuteExW`` 启动（支持 .exe / .lnk / ``shell:AppsFolder`` 入口）。"""
        directory = self._check_working_dir(working_dir)
        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_FLAG_NO_UI | _SEE_MASK_NOASYNC
        info.lpVerb = "open"
        info.lpFile = target
        info.lpParameters = args or None
        info.lpDirectory = directory or None
        info.nShow = _SW_SHOWNORMAL
        try:
            ok = bool(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)))
        except Exception:  # pragma: no cover - 非 Windows
            ok = False
        detail: Dict[str, Any] = {"method": "exec", "started": ok, "target": target, "args": args}
        pid = 0
        if not ok:
            detail["error"] = ctypes.windll.kernel32.GetLastError() if hasattr(ctypes, "windll") else "ShellExecuteExW failed"
            logger.warning("ShellExecuteExW 启动 %s 失败：%s，回退 CreateProcess", target, detail["error"])
            pid, fallback = self._launch_process(target, args, working_dir)
            detail.update({"method": "process_fallback", "fallback": fallback})
            return pid, detail
        handle = info.hProcess
        if handle:
            try:
                pid = int(ctypes.windll.kernel32.GetProcessId(wintypes.HANDLE(handle)) or 0)
            except Exception:  # pragma: no cover
                pid = 0
            finally:
                try:
                    ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
                except Exception:  # pragma: no cover
                    pass
        return pid, detail

    def _launch_process(self, target: str, args: str, working_dir: str) -> Tuple[int, Dict[str, Any]]:
        """``subprocess``（CreateProcess）启动，进程与原进程分离。"""
        directory = self._check_working_dir(working_dir)
        argv = [target] + (shlex.split(args, posix=False) if args else [])
        proc = subprocess.Popen(
            argv,
            cwd=directory or None,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(proc.pid), {"method": "process", "started": True, "target": target, "args": args}

    def _launch_uwp(self, appid: str) -> Tuple[int, Dict[str, Any]]:
        """UWP / 商店应用：``explorer.exe shell:AppsFolder\\<AppID>``。"""
        target = appid if str(appid or "").startswith("shell:") else f"shell:AppsFolder\\{appid}"
        proc = subprocess.Popen(
            ["explorer.exe", target],
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # explorer 的 pid 与真实应用无关，置 0 让 wait_window 按应用名匹配
        return 0, {"method": "uwp", "started": True, "target": target, "explorer_pid": int(proc.pid)}

    def _check_working_dir(self, working_dir: str) -> str:
        if not working_dir:
            return ""
        directory = os.path.abspath(str(working_dir))
        if not os.path.isdir(directory):
            raise FileNotFoundError(f"working_dir 不存在或不是目录：{directory}")
        return directory

    # ------------------------------------------------------------ 就绪等待
    def wait_window(
        self,
        pid: int = 0,
        hwnd: int = 0,
        app: Any = "",
        timeout: Optional[float] = None,
        stable_ms: float = w32.BOUNDS_STABLE_MS,
        interval_ms: float = 40.0,
    ) -> Dict[str, Any]:
        """等待目标窗口出现并完成布局（只读）。

        三选一定位：``hwnd`` > ``pid`` > ``app``。``app`` 可为单个线索或线索列表（按序尝试）。
        超时返回 ``ok=false`` / ``state="launching"`` / ``degraded_reason="launch_timeout"``
        （方案 RF7，非异常）。

        :return: ``{"ok", "state", "hwnd", "waited_ms", "stable", "pid", "matched_by",
            "matched_key", "bounds", "title", "process_name", "degraded_reason"?}``
        """
        started = time.perf_counter()
        # 方案 3.3（4）：ui_wait_window 默认 timeout=10.0；未显式给出时按应用分级放宽
        keys = _match_keys(app)
        primary = keys[0] if keys else ""
        budget = float(timeout) if timeout and float(timeout) > 0 else (default_timeout(primary, None) if primary else 10.0)
        deadline = started + max(0.05, budget)
        interval = max(0.01, float(interval_ms) / 1000.0)
        matched_by = "hwnd" if hwnd else ("pid" if pid else "app")
        # 打包应用（Win11 记事本 / 计算器 / 画图）的启动进程可能只是「启动桩」：
        # 窗口归属真实应用进程，按 PID 永远等不到。仅在**同时给出 pid 与线索**时启用
        # 应用名兜底（显式传 pid 视为强约束，不做兜底），并标记 matched_by=app_fallback。
        fallback_keys = keys if (pid and keys and not hwnd) else []
        fallback_at = started + max(0.3, min(0.8, budget * 0.1)) if fallback_keys else deadline + 1.0
        used_fallback = False
        candidate: Optional[w32.Win32Window] = None
        matched_key = ""
        polls = 0

        while True:
            polls += 1
            candidate = None
            matched_key = ""
            if hwnd and w32.is_window(int(hwnd)):
                candidate = w32.window_from_hwnd(int(hwnd))
                matched_key = "hwnd"
            else:
                if pid and not used_fallback:
                    candidate = self._window_by_pid(int(pid))
                    if candidate is not None:
                        matched_key = "pid"
                    elif fallback_keys and time.perf_counter() >= fallback_at:
                        used_fallback = True
                        matched_by = "app_fallback"
                if candidate is None and (used_fallback or not pid):
                    candidate, matched_key = self._window_by_keys(keys)
                    if candidate is not None and not used_fallback:
                        matched_by = "app"
            if candidate is not None:
                break
            if time.perf_counter() >= deadline:
                return {
                    "ok": False,
                    "state": "launching",
                    "hwnd": 0,
                    "waited_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "stable": False,
                    "pid": int(pid or 0),
                    "matched_by": matched_by,
                    "matched_key": matched_key,
                    "polls": polls,
                    "bounds": None,
                    "title": "",
                    "process_name": "",
                    "degraded_reason": "launch_timeout",
                    "error": f"{budget:.1f}s 内未等到可定位窗口（matched_by={matched_by}）",
                }
            time.sleep(interval)

        handle = int(getattr(candidate, "hwnd", 0) or 0)
        remaining = max(0.0, deadline - time.perf_counter())
        stable = w32.wait_bounds_stable(
            handle,
            stable_ms=stable_ms,
            timeout=max(0.2, min(float(w32.BOUNDS_STABLE_TIMEOUT), remaining)),
        )
        state = str(stable.get("state") or w32.window_state(hwnd=handle) or "unknown")
        reason = str(stable.get("degraded_reason") or "")
        if used_fallback and not reason:
            reason = "pid_window_missing"
        return {
            "ok": bool(stable.get("ok")),
            "state": state if stable.get("ok") else ("launching" if state == "not_found" else state),
            "hwnd": handle,
            "waited_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "stable": bool(stable.get("stable")),
            "stable_ms": float(stable.get("elapsed_ms") or 0.0),
            "pid": int(getattr(candidate, "pid", 0) or 0) or int(pid or 0),
            "matched_by": matched_by,
            "matched_key": matched_key,
            "fallback": used_fallback,
            "polls": polls,
            "bounds": stable.get("bounds") or list(getattr(candidate, "bounds", ()) or ()),
            "title": str(getattr(candidate, "title", "") or ""),
            "process_name": str(getattr(candidate, "process_name", "") or ""),
            "degraded_reason": reason,
        }

    def _window_by_keys(self, keys: List[str]) -> Tuple[Optional[w32.Win32Window], str]:
        """按线索键列表依次查找窗口，返回 ``(窗口, 命中的键)``。"""
        for key in keys:
            pool = w32.list_app_windows(key)
            if not pool:
                continue
            best = w32.select_best_window(pool)
            if best is not None:
                return best, key
        return None, ""

    def _window_by_pid(self, pid: int) -> Optional[w32.Win32Window]:
        """按 PID 找该进程最值得作为「就绪窗口」的顶层窗口。"""
        pool = [win for win in w32.enum_top_level_windows() if int(win.pid) == int(pid)]
        pool = [win for win in pool if win.is_app_window]
        if not pool:
            return None
        return w32.select_best_window(pool)


# ===================================================================== 模块级单例
_launcher: Optional[AppLauncher] = None


def get_launcher() -> AppLauncher:
    """进程级 :class:`AppLauncher` 单例（解析索引缓存复用）。"""
    global _launcher
    if _launcher is None:
        _launcher = AppLauncher()
    return _launcher
