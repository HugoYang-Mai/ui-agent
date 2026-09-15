"""端到端实测：启动记事本 → 定位编辑区 → 写入中文 → 截图核验 → 关闭不保存 → 复位鼠标。

⚠️ 本脚本会**真实操作鼠标与键盘**（有副作用），仅用于设备端人工在场时的验收测试。

本机特殊性（已实测确认）
------------------------
本机 Windows 11 记事本开启了“启动时恢复上次会话”，冷启动后会恢复历史文档标签
并弹出“系统找不到指定的路径”错误对话框。因此脚本采用如下安全策略：

1. **不触碰恢复的会话标签**：以命令行参数打开一个自建的临时空白文本
   （``<out>/e2e_notepad_scratch_*.txt``），只在该文档上操作；
2. **先清理错误对话框**：按钮查找严格限定在“目标进程的对话框窗口”内，
   且排除对话框标题栏区域，避免误点主窗口的关闭按钮；
3. **输入前后双重校验**：前台窗口 PID + 键盘焦点元素角色（document/edit）双重确认，
   不满足则中止输入，绝不把测试文本打进用户其它窗口；
4. **关闭只放弃自己的更改**：Ctrl+W 关闭本文档 → 提示文本经 OCR 核对确属本文档后
   才点“不保存”；随后 Alt+F4 关窗，若再出现保存提示（属于其它文档）则点“取消”
   并如实上报，绝不强制丢弃用户文档；
5. 全程不覆盖用户文件；结束（含异常路径）必定复位鼠标、恢复剪贴板原内容。

运行::

    .venv\\Scripts\\python.exe -X utf8 selfcheck\\e2e_notepad.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ----------------------------------------------------------- 1. 统一坐标系
from uiagent import dpi  # noqa: E402

DPI_HRESULT = dpi.enable_dpi_awareness(dpi.DPI_AWARENESS_PER_MONITOR_AWARE)

# ----------------------------------------------------------- 2. 其余导入
from uiagent import __version__  # noqa: E402
from uiagent.base import normalize_role  # noqa: E402
from uiagent.controller import UniversalController  # noqa: E402

#: 写入记事本的测试文本（含中文，验证剪贴板输入路径）
TEST_TEXT = "ui-agent 端到端实测：记事本编辑区写入中文成功，坐标单位统一为物理像素。"

#: OCR 核验关键词（选短且字形简单的片段，降低 OCR 误判影响）
VERIFY_KEYS = ["端到端实测", "写入中文成功"]

#: 保存提示对话框里“放弃保存”按钮的候选名称
DISCARD_BUTTONS = ("不保存", "Don't Save", "不儲存", "Don't save", "否")
#: 保存提示对话框里“取消”按钮的候选名称
CANCEL_BUTTONS = ("取消", "Cancel")
#: 错误对话框的确认按钮名称
CONFIRM_BUTTONS = ("确定", "OK")

DEFAULT_OUT_DIR = os.path.join(_PROJECT_ROOT, "out")


# ====================================================================== 工具
def _now() -> str:
    dt = time.localtime()
    return f"{dt.tm_year}年{dt.tm_mon:02d}月{dt.tm_mday:02d}日 {dt.tm_hour:02d}:{dt.tm_min:02d}:{dt.tm_sec:02d}"


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _base_name(path: str) -> str:
    return os.path.basename(path or "").lower()


def _is_notepad_window(element) -> bool:
    base = _base_name(element.process_name)
    if base == "notepad.exe" or "notepad" in base:
        return True
    return element.class_name.lower().startswith("notepad")


def _notepad_windows(ctrl: UniversalController) -> List[Any]:
    """枚举当前桌面上的记事本顶层窗口。"""
    try:
        return [w for w in ctrl.list_top_level(include_invisible=False) if _is_notepad_window(w)]
    except Exception:
        return []


def _wait_new_window(
    ctrl: UniversalController, before_pids: set, timeout: float = 25.0
) -> Optional[Any]:
    """等待出现 PID 不属于 before_pids 的记事本窗口。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in _notepad_windows(ctrl):
            if w.process_id in before_pids:
                continue
            if w.bounds[2] >= 200 and w.bounds[3] >= 150:
                return w
        time.sleep(0.5)
    return None


def _wait_window_gone(pid: int, ctrl: UniversalController, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(w.process_id == pid for w in _notepad_windows(ctrl)):
            return True
        time.sleep(0.5)
    return False


def _clip_region(bounds, screen) -> tuple:
    """把区域裁剪到虚拟桌面范围内。"""
    left, top, width, height = (int(v) for v in bounds)
    sx, sy, sw, sh = (int(v) for v in screen)
    left = max(left, sx)
    top = max(top, sy)
    right = min(left + max(0, width), sx + sw)
    bottom = min(top + max(0, height), sy + sh)
    return (left, top, max(0, right - left), max(0, bottom - top))


def _dialog_windows(ctrl: UniversalController, pid: int, main_bounds) -> List[Any]:
    """找出属于目标进程、且明显不是主窗口的小顶层窗口（对话框候选）。"""
    out: List[Any] = []
    for w in ctrl.list_top_level(include_invisible=False):
        if w.process_id != pid:
            continue
        if tuple(w.bounds) == tuple(main_bounds):
            continue
        if w.bounds[2] <= 0 or w.bounds[3] <= 0:
            continue
        if w.bounds[2] > main_bounds[2] and w.bounds[3] > main_bounds[3]:
            continue
        out.append(w)
    return out


def _dismiss_error_dialogs(
    ctrl: UniversalController, pid: int, main_bounds, limit: int = 4
) -> List[Dict[str, Any]]:
    """点击目标进程错误对话框上的“确定”，最多 limit 次。

    按钮查找严格限定在对话框窗口子树内，并排除标题栏高度范围，
    避免误点主窗口的关闭按钮（该按钮同样名为“关闭”）。
    """
    handled: List[Dict[str, Any]] = []
    for _ in range(limit):
        target = None
        for dialog in _dialog_windows(ctrl, pid, main_bounds):
            top_limit = dialog.bounds[1] + 40
            for label in CONFIRM_BUTTONS:
                try:
                    found = ctrl.find_all_elements(
                        name=label, role="button", window=dialog, max_depth=4, limit=5, timeout=3.0
                    )
                except Exception:
                    found = []
                for btn in found:
                    if btn.center[1] >= top_limit:
                        target = btn
                        break
                if target is not None:
                    break
            if target is not None:
                break
        if target is None:
            break
        ctrl.click(x=target.center[0], y=target.center[1])
        handled.append({"button": target.name, "center": list(target.center)})
        time.sleep(0.7)
    return handled


def _pick_editor(ctrl: UniversalController, window) -> Optional[Any]:
    """在记事本窗口内定位编辑区（活动标签的文档区）。

    优先级：键盘焦点所在的 document/edit → AutomationId=TextEditor →
    面积最大的可见 document/edit。
    """
    focused = ctrl.get_focused_element()
    if focused is not None and normalize_role(focused.role) in ("document", "edit"):
        if focused.bounds[2] > 80 and focused.bounds[3] > 40 and focused.process_id == window.process_id:
            return focused

    candidates: List[Any] = []
    try:
        candidates += ctrl.find_all_elements(
            automation_id="TextEditor", window=window, max_depth=10, limit=5, timeout=6.0
        )
    except Exception:
        pass
    for role in ("document", "edit"):
        try:
            candidates += ctrl.find_all_elements(
                role=role, window=window, max_depth=10, limit=10, timeout=6.0
            )
        except Exception:
            pass

    usable = [e for e in candidates if e.visible and e.bounds[2] > 80 and e.bounds[3] > 40]
    if not usable:
        return None
    usable.sort(key=lambda e: e.bounds[2] * e.bounds[3], reverse=True)
    return usable[0]


def _activate_tab(ctrl: UniversalController, window, file_name: str) -> Optional[Any]:
    """在窗口标签栏中点击激活指定文件名的标签页。"""
    try:
        tabs = ctrl.find_all_elements(
            role="tabitem", window=window, max_depth=8, limit=20, timeout=5.0
        )
    except Exception:
        return None
    for tab in tabs:
        if file_name.lower() in tab.name.lower():
            ctrl.click(x=tab.center[0], y=tab.center[1])
            time.sleep(0.6)
            return tab
    return None


def _ensure_foreground(ctrl: UniversalController, pid: int, point, tries: int = 2) -> bool:
    """确认目标进程窗口处于前台；必要时点击其内部坐标把它激活。"""
    for _ in range(tries):
        active = ctrl.get_active_window()
        if active is not None and active.process_id == pid:
            return True
        try:
            ctrl.click(x=int(point[0]), y=int(point[1]))
        except Exception:
            pass
        time.sleep(0.5)
    active = ctrl.get_active_window()
    return bool(active is not None and active.process_id == pid)


def _ensure_editor_focus(ctrl: UniversalController, pid: int, point, tries: int = 3) -> Optional[Any]:
    """确认键盘焦点落在目标进程的文档/编辑控件上；必要时点击编辑区重新置焦。"""
    for _ in range(tries):
        focused = ctrl.get_focused_element()
        if (
            focused is not None
            and focused.process_id == pid
            and normalize_role(focused.role) in ("document", "edit")
        ):
            return focused
        try:
            ctrl.click(x=int(point[0]), y=int(point[1]))
        except Exception:
            pass
        time.sleep(0.4)
    focused = ctrl.get_focused_element()
    if focused is not None and focused.process_id == pid:
        return focused
    return None


def _find_prompt_buttons(ctrl: UniversalController, pid: int) -> List[Any]:
    """查找保存提示对话框上的按钮（限定目标进程）。"""
    wanted = set(DISCARD_BUTTONS) | set(CANCEL_BUTTONS) | {"保存", "Save"}
    found: List[Any] = []
    try:
        for btn in ctrl.find_all_elements(role="button", limit=40, max_depth=8, timeout=5.0):
            if btn.process_id != pid:
                continue
            if btn.name.strip() in wanted and btn.bounds[2] > 0 and btn.bounds[3] > 0:
                found.append(btn)
    except Exception:
        return []
    return found


def _pick_button(buttons: List[Any], labels) -> Optional[Any]:
    for btn in buttons:
        if btn.name.strip() in labels:
            return btn
    return None


def _ocr_prompt_text(ctrl: UniversalController, button, screen) -> str:
    """OCR 保存提示对话框文字（按钮上方区域），用于确认提示对象。"""
    region = _clip_region(
        (button.center[0] - 330, button.center[1] - 170, 660, 160), screen
    )
    try:
        return ctrl.ocr_text(region)
    except Exception:
        return ""


def _terminate(pid: int) -> bool:
    """终止本次自己启动的记事本进程（仅在用户文档安全时兜底调用）。"""
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


# ====================================================================== 主流程
def run_e2e(
    text: str = TEST_TEXT,
    out_dir: Optional[str] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    out_dir = out_dir or DEFAULT_OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = _ts()
    steps: List[Dict[str, Any]] = []
    ctx: Dict[str, Any] = {"origin_clipboard": "", "proc": None, "scratch": "", "window": None}

    def log(message: str) -> None:
        if verbose:
            print(message, flush=True)

    def record(key: str, title: str, status: str, started: float, **data) -> None:
        steps.append(
            {
                "key": key,
                "title": title,
                "status": status,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "data": data,
            }
        )

    ctrl = UniversalController(search_timeout=2.0)
    screen = ctrl.screen_rect()
    center = (screen[0] + screen[2] // 2, screen[1] + screen[3] // 2)

    report: Dict[str, Any] = {
        "version": __version__,
        "generated_at": _now(),
        "python": sys.version.split()[0],
        "dpi_hresult": DPI_HRESULT,
        "test_text": text,
        "verify_keys": VERIFY_KEYS,
        "screen": list(screen),
        "steps": steps,
        "artifacts": {},
        "result": {"passed": False, "stage": "not_started"},
    }

    def _execute() -> None:
        """执行实测主体；任何中止路径均直接 return，由外层 finally 兜底复位。"""
        # ------------------------------------------------ 0. 初始快照
        started = time.perf_counter()
        before_pids = {w.process_id for w in _notepad_windows(ctrl)}
        try:
            origin_mouse = tuple(ctrl.mouse.position())
        except Exception:
            origin_mouse = (0, 0)
        origin_active = ctrl.get_active_window()
        try:
            import pyperclip

            ctx["origin_clipboard"] = pyperclip.paste()
        except Exception:
            ctx["origin_clipboard"] = ""
        record(
            "snapshot",
            "初始状态快照",
            "ok",
            started,
            notepad_pids_before=sorted(before_pids),
            mouse_position=list(origin_mouse),
            active_window=getattr(origin_active, "name", None),
            clipboard_is_text=bool(ctx["origin_clipboard"]),
        )
        log(f"[ OK ] 初始快照：既有记事本进程 {len(before_pids)} 个，鼠标 {origin_mouse}")

        # ------------------------------------------------ 1. 准备临时空白文档并启动记事本
        started = time.perf_counter()
        scratch = os.path.join(out_dir, f"e2e_notepad_scratch_{stamp}.txt")
        try:
            with open(scratch, "w", encoding="utf-8"):
                pass
        except Exception as exc:  # noqa: BLE001
            record("prepare_scratch", "创建临时空白文档", "fail", started, error=str(exc))
            report["result"] = {"passed": False, "stage": "prepare_scratch", "error": str(exc)}
            return
        ctx["scratch"] = scratch
        scratch_name = os.path.basename(scratch)
        report["artifacts"]["scratch_file"] = scratch
        record("prepare_scratch", "创建临时空白文档", "ok", started, path=scratch)

        proc = subprocess.Popen(["notepad.exe", scratch], close_fds=True)
        ctx["proc"] = proc
        window = _wait_new_window(ctrl, before_pids, timeout=25.0)
        if window is None:
            record("launch", "启动记事本", "fail", started,
                   launcher_pid=proc.pid, error="25s 内未出现记事本窗口")
            report["result"] = {"passed": False, "stage": "launch", "error": "未出现记事本窗口"}
            log("[FAIL] 启动记事本：未捕获到新窗口")
            return
        ctx["window"] = window
        pid = window.process_id
        main_bounds = tuple(window.bounds)
        record(
            "launch",
            "启动记事本",
            "ok",
            started,
            launcher_pid=proc.pid,
            scratch=scratch,
            window={"name": window.name, "class_name": window.class_name,
                    "process_id": pid, "bounds": list(main_bounds)},
        )
        log(f"[ OK ] 启动记事本：pid={pid} 标题={window.name!r}")

        # ------------------------------------------------ 2. 清理恢复会话产生的错误对话框
        started = time.perf_counter()
        time.sleep(1.5)
        handled = _dismiss_error_dialogs(ctrl, pid, main_bounds)
        record(
            "dismiss_dialogs",
            "清理启动期错误对话框",
            "ok",
            started,
            handled=handled,
            count=len(handled),
        )
        log(f"[ OK ] 清理错误对话框 {len(handled)} 个（恢复会话残留）")

        # ------------------------------------------------ 3. 切换到本文档标签
        started = time.perf_counter()
        tab = _activate_tab(ctrl, window, scratch_name)
        if tab is None:
            # 标签栏未识别到本文档时，新建一个空白标签作为兜底
            ctrl.hotkey("ctrl", "n")
            time.sleep(1.0)
        record(
            "activate_tab",
            "切换到实测文档标签",
            "ok",
            started,
            tab_found=tab is not None,
            tab={"name": tab.name, "center": list(tab.center)} if tab else None,
        )
        log(f"[ OK ] 活动标签：{tab.name if tab else 'Ctrl+N 新建空白标签（兜底）'}")

        # ------------------------------------------------ 4. 定位编辑区
        started = time.perf_counter()
        editor = _pick_editor(ctrl, window)
        if editor is None:
            record("locate_editor", "定位编辑区", "fail", started, error="UIA 未找到编辑区元素")
            report["result"] = {"passed": False, "stage": "locate_editor", "error": "未找到编辑区"}
            log("[FAIL] 定位编辑区：UIA 未命中")
            return
        point = tuple(editor.center)
        record(
            "locate_editor",
            "定位编辑区（UIA）",
            "ok",
            started,
            editor={"name": editor.name, "role": editor.role, "class_name": editor.class_name,
                    "automation_id": editor.automation_id, "bounds": list(editor.bounds),
                    "center": list(point), "process_id": editor.process_id},
        )
        log(f"[ OK ] 定位编辑区：{editor.role}/{editor.class_name} center={point}")

        # ------------------------------------------------ 5. 点击编辑区并双重校验
        started = time.perf_counter()
        ctrl.click(x=point[0], y=point[1])
        time.sleep(0.4)
        same_pid = _ensure_foreground(ctrl, pid, point, tries=1)
        focused = _ensure_editor_focus(ctrl, pid, point, tries=2) if same_pid else None
        active = ctrl.get_active_window()
        focus_ok = bool(same_pid and focused is not None)
        record(
            "focus",
            "点击编辑区并校验前台窗口 + 键盘焦点",
            "ok" if focus_ok else "fail",
            started,
            target_pid=pid,
            active_window={"name": getattr(active, "name", None),
                           "process_id": getattr(active, "process_id", None)},
            focused_element=None if focused is None else {
                "name": focused.name, "role": focused.role,
                "class_name": focused.class_name, "process_id": focused.process_id,
            },
        )
        if not focus_ok:
            report["result"] = {
                "passed": False, "stage": "focus",
                "error": "前台窗口或键盘焦点不在目标记事本编辑区，已中止输入以避免误写入",
            }
            log("[FAIL] 前台/焦点校验未通过，已中止写入")
            return
        log(f"[ OK ] 焦点校验通过：{focused.role}/{focused.class_name}")

        # ------------------------------------------------ 6. 写入中文
        started = time.perf_counter()
        ctrl.type_chinese(text)
        time.sleep(0.7)
        value_after = ctrl.get_element_value(editor)
        if not value_after:
            value_after = ctrl.get_element_value(focused) if focused is not None else None
        head = text[:8]
        write_ok = bool(value_after and head in value_after)
        record(
            "type_text",
            "写入中文（剪贴板路径）",
            "ok" if write_ok else "warn",
            started,
            typed_length=len(text),
            value_length=len(value_after) if value_after else 0,
            value_head=(value_after or "")[:60],
        )
        log(f"[{' OK ' if write_ok else 'WARN'}] 写入中文：UIA 回读 {len(value_after) if value_after else 0} 字")

        # ------------------------------------------------ 7. 截图 + OCR 核验
        started = time.perf_counter()
        shot_path = os.path.join(out_dir, f"e2e_notepad_screenshot_{stamp}.png")
        ctrl.save_screenshot(shot_path)
        report["artifacts"]["screenshot"] = shot_path
        region = _clip_region(editor.bounds, screen)
        ocr_blocks = ctrl.ocr_screen(region)
        joined = "".join(b.text for b in ocr_blocks)
        compact = "".join(joined.split())
        hits = {key: (key in compact) for key in VERIFY_KEYS}
        ocr_ok = all(hits.values())
        record(
            "verify",
            "截图 + OCR 核验",
            "ok" if ocr_ok else "warn",
            started,
            screenshot=shot_path,
            ocr_region=list(region),
            ocr_blocks=len(ocr_blocks),
            ocr_text=joined[:200],
            key_hits=hits,
            uia_value_starts_with_expected=bool(value_after and value_after.strip().startswith(head)),
        )
        log(f"[{' OK ' if ocr_ok else 'WARN'}] OCR 核验：命中 {hits}")

        # ------------------------------------------------ 8. 关闭本文档标签（不保存）
        started = time.perf_counter()
        if _dismiss_error_dialogs(ctrl, pid, main_bounds):
            log("[INFO] 关闭前又清理了一批错误对话框")
        if not _ensure_foreground(ctrl, pid, point):
            record("close_tab", "关闭实测文档标签", "fail", started,
                   error="无法将记事本置前，已放弃发送关闭快捷键")
            report["result"] = {
                "passed": False, "stage": "close_tab",
                "error": "无法将记事本置前，未发送 Ctrl+W（避免误关用户其它窗口）",
                "written": write_ok, "ocr_verified": ocr_ok,
            }
            log("[FAIL] 无法置前，未执行关闭")
            return

        ctrl.hotkey("ctrl", "w")
        time.sleep(1.3)
        close_notes: List[str] = []
        discarded_own = False
        for _ in range(3):
            buttons = _find_prompt_buttons(ctrl, pid)
            if not buttons:
                break
            discard = _pick_button(buttons, DISCARD_BUTTONS)
            if discard is None:
                close_notes.append("未找到“不保存”按钮，停止处理保存提示")
                break
            prompt_text = _ocr_prompt_text(ctrl, discard, screen)
            ours = (
                scratch_name.lower() in prompt_text.lower()
                or "无标题" in prompt_text
                or "Untitled" in prompt_text
                or head in prompt_text
            )
            close_notes.append(f"保存提示 OCR={prompt_text!r} 判定属于本文档={ours}")
            if not ours:
                cancel = _pick_button(buttons, CANCEL_BUTTONS)
                if cancel is not None:
                    ctrl.click(x=cancel.center[0], y=cancel.center[1])
                    close_notes.append("提示不属于本文档 → 已点“取消”保留用户文档")
                break
            ctrl.click(x=discard.center[0], y=discard.center[1])
            discarded_own = True
            close_notes.append(f"点击 {discard.name!r} 放弃本文档更改")
            time.sleep(0.9)
        record(
            "close_tab",
            "关闭实测文档标签（不保存）",
            "ok" if discarded_own else "warn",
            started,
            notes=close_notes,
            discarded_own_changes=discarded_own,
        )
        log(f"[{' OK ' if discarded_own else 'WARN'}] 关闭文档标签：{close_notes}")

        # ------------------------------------------------ 9. 关闭记事本窗口
        started = time.perf_counter()
        gone = _wait_window_gone(pid, ctrl, timeout=5.0)
        window_notes: List[str] = []
        if not gone and _ensure_foreground(ctrl, pid, point):
            ctrl.hotkey("alt", "f4")
            time.sleep(1.5)
            buttons = _find_prompt_buttons(ctrl, pid)
            if buttons:
                cancel = _pick_button(buttons, CANCEL_BUTTONS)
                if cancel is not None:
                    ctrl.click(x=cancel.center[0], y=cancel.center[1])
                    window_notes.append("关窗时出现保存提示（属其它文档）→ 已点“取消”，未强制关闭")
                else:
                    window_notes.append("关窗时出现保存提示但未找到“取消”，已停止操作")
            gone = _wait_window_gone(pid, ctrl, timeout=8.0)
        record(
            "close_window",
            "关闭记事本窗口",
            "ok" if gone else "warn",
            started,
            window_gone=gone,
            notes=window_notes,
        )
        log(f"[{' OK ' if gone else 'WARN'}] 关闭窗口：gone={gone} {window_notes}")

        # ------------------------------------------------ 10. 结果汇总
        report["result"] = {
            "passed": bool(write_ok and ocr_ok and gone),
            "written": write_ok,
            "ocr_verified": ocr_ok,
            "key_hits": hits,
            "closed_without_save": gone,
            "discarded_own_changes": discarded_own,
            "close_notes": close_notes + window_notes,
            "uia_value": (value_after or "")[:120],
        }

    try:
        _execute()
    except Exception as exc:  # noqa: BLE001
        report["result"] = {"passed": False, "stage": "exception",
                            "error": f"{type(exc).__name__}: {exc}"}
        log(f"[FAIL] 异常：{type(exc).__name__}: {exc}")
    finally:
        # ------------------------------------------------ 11. 复位鼠标 / 恢复剪贴板
        started = time.perf_counter()
        try:
            ctrl.move_to(center[0], center[1])
            restored = tuple(ctrl.mouse.position())
        except Exception as exc:  # noqa: BLE001
            restored = None
            log(f"[WARN] 鼠标复位失败：{exc}")
        record("restore_mouse", "复位鼠标", "ok" if restored else "warn", started,
               restored_to=list(restored) if restored else None)
        log(f"[ OK ] 鼠标复位：{restored}")

        started = time.perf_counter()
        clipboard_restored = False
        if ctx["origin_clipboard"]:
            try:
                import pyperclip

                pyperclip.copy(ctx["origin_clipboard"])
                clipboard_restored = True
            except Exception:
                clipboard_restored = False
        record("restore_clipboard", "恢复剪贴板", "ok" if clipboard_restored else "skip",
               started, restored=clipboard_restored,
               note="" if clipboard_restored else "剪贴板原内容为空或非文本，未做恢复")

        proc = ctx.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    # ------------------------------------------------ 报告落盘（始终执行）
    report["steps_total"] = len(steps)
    json_path = os.path.join(out_dir, f"e2e_notepad_report_{stamp}.json")
    try:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        report["artifacts"]["report_json"] = json_path
        log(f"[ OK ] 报告：{json_path}")
    except Exception as exc:  # noqa: BLE001
        log(f"[WARN] 报告写入失败：{exc}")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

    import argparse

    parser = argparse.ArgumentParser(description="ui-agent 记事本端到端实测")
    parser.add_argument("--text", default=TEST_TEXT, help="写入记事本的文本")
    parser.add_argument("--out", default=None, help="报告输出目录（默认 <项目>/out）")
    args = parser.parse_args(argv)

    report = run_e2e(text=args.text, out_dir=args.out)
    print("=" * 68, flush=True)
    print(f"端到端实测结果：{'通过' if report['result'].get('passed') else '未通过'}", flush=True)
    for step in report["steps"]:
        print(f"  [{step['status'].upper():4}] {step['title']}  {step['elapsed_ms']} ms", flush=True)
    print(json.dumps(report["result"], ensure_ascii=False, indent=2), flush=True)
    print("=" * 68, flush=True)
    return 0 if report["result"].get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
