"""只读探测：摸清本机记事本（Windows 11 新版）启动后的真实状态。

背景
----
端到端实测首轮失败：记事本启动后弹出了“系统找不到指定的路径”错误对话框，
窗口标题为「脚本.txt - Notepad」（恢复了上次会话的文档），导致点击与 Ctrl+V
全部落在对话框上，编辑区始终为空。

本脚本**只读观测 + 安全关闭**，用于确认：
1. 启动后是否存在恢复会话的标签页 / 错误对话框；
2. 编辑区元素的可辨识特征（role / class / focused）；
3. 无输入情况下 Alt+F4 能否干净关闭（若出现保存提示，一律点「取消」，
   绝不丢弃任何既有文档）。

运行::

    .venv\\Scripts\\python.exe -X utf8 selfcheck\\probe_notepad.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

_SELFCHECK_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SELFCHECK_DIR)
for path in (_PROJECT_ROOT, _SELFCHECK_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from uiagent import dpi  # noqa: E402

dpi.enable_dpi_awareness(dpi.DPI_AWARENESS_PER_MONITOR_AWARE)

from e2e_notepad import (  # noqa: E402
    _ensure_foreground,
    _notepad_windows,
    _pick_editor,
    _terminate,
    _wait_new_window,
    _wait_window_gone,
)
from uiagent.controller import UniversalController  # noqa: E402

OUT_DIR = os.path.join(_PROJECT_ROOT, "out")


def _elements(ctrl: UniversalController, **kwargs) -> List[Dict[str, Any]]:
    try:
        found = ctrl.find_all_elements(include_invisible=False, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    return [
        {
            "name": e.name,
            "role": e.role,
            "class_name": e.class_name,
            "automation_id": e.automation_id,
            "bounds": list(e.bounds),
            "center": list(e.center),
            "focused": e.focused,
            "enabled": e.enabled,
            "process_id": e.process_id,
        }
        for e in found
    ]


def _snapshot_state(ctrl: UniversalController, window) -> Dict[str, Any]:
    pid = window.process_id
    return {
        "windows_of_pid": [
            {"name": w.name, "class_name": w.class_name, "bounds": list(w.bounds),
             "process_id": w.process_id}
            for w in _notepad_windows(ctrl)
            if w.process_id == pid
        ],
        "tab_items": _elements(ctrl, role="tabitem", window=window, max_depth=8, limit=20, timeout=5.0),
        "documents": _elements(ctrl, role="document", window=window, max_depth=10, limit=10, timeout=5.0),
        "edits": _elements(ctrl, role="edit", window=window, max_depth=10, limit=10, timeout=5.0),
        "buttons": _elements(ctrl, role="button", window=window, max_depth=8, limit=30, timeout=5.0),
        "message_buttons": _elements(ctrl, role="button", limit=5, max_depth=6, timeout=5.0),
        "focused_element": (
            lambda f: None if f is None else {
                "name": f.name, "role": f.role, "class_name": f.class_name,
                "process_id": f.process_id, "bounds": list(f.bounds),
            }
        )(ctrl.get_focused_element()),
    }


def _dismiss_dialogs(ctrl: UniversalController, pid: int, limit: int = 3) -> List[Dict[str, Any]]:
    handled: List[Dict[str, Any]] = []
    for _ in range(limit):
        target = None
        for label in ("确定", "OK", "关闭"):
            for btn in ctrl.find_all_elements(name=label, role="button", limit=5, max_depth=6, timeout=3.0):
                if btn.process_id == pid:
                    target = btn
                    break
            if target is not None:
                break
        if target is None:
            break
        ctrl.click(x=target.center[0], y=target.center[1])
        handled.append({"button": target.name, "center": list(target.center)})
        time.sleep(0.8)
    return handled


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    ctrl = UniversalController(search_timeout=2.0)
    screen = ctrl.screen_rect()
    center = (screen[0] + screen[2] // 2, screen[1] + screen[3] // 2)

    report: Dict[str, Any] = {"screen": list(screen), "steps": {}}
    proc: Optional[subprocess.Popen] = None
    window = None

    try:
        before_pids = {w.process_id for w in _notepad_windows(ctrl)}
        report["steps"]["before"] = {"notepad_pids": sorted(before_pids)}
        print(f"[ OK ] 启动前既有记事本进程：{sorted(before_pids)}", flush=True)

        proc = subprocess.Popen(["notepad.exe"], close_fds=True)
        window = _wait_new_window(ctrl, before_pids, timeout=25.0)
        if window is None:
            print("[FAIL] 未捕获到记事本窗口", flush=True)
            report["steps"]["launch"] = {"error": "no window"}
            return 1
        pid = window.process_id
        report["steps"]["launch"] = {
            "window": {"name": window.name, "class_name": window.class_name,
                       "bounds": list(window.bounds), "pid": pid,
                       "process_name": window.process_name},
        }
        print(f"[ OK ] 记事本窗口：{window.name!r} pid={pid} bounds={tuple(window.bounds)}", flush=True)

        time.sleep(2.5)
        report["steps"]["state_after_launch"] = _snapshot_state(ctrl, window)
        print(f"[INFO] 启动后标签页 {len(report['steps']['state_after_launch']['tab_items'])} 个，"
              f"文档区 {len(report['steps']['state_after_launch']['documents'])} 个", flush=True)

        handled = _dismiss_dialogs(ctrl, pid)
        report["steps"]["dismissed_dialogs"] = handled
        print(f"[INFO] 已关闭错误对话框 {len(handled)} 个：{handled}", flush=True)

        time.sleep(1.0)
        report["steps"]["state_after_dismiss"] = _snapshot_state(ctrl, window)
        editor = _pick_editor(ctrl, window)
        report["steps"]["editor"] = None if editor is None else {
            "name": editor.name, "role": editor.role, "class_name": editor.class_name,
            "automation_id": editor.automation_id, "bounds": list(editor.bounds),
            "center": list(editor.center),
        }
        print(f"[INFO] 编辑区：{report['steps']['editor']}", flush=True)

        shot = os.path.join(OUT_DIR, f"probe_notepad_{stamp}.png")
        ctrl.save_screenshot(shot)
        report["screenshot"] = shot
        print(f"[ OK ] 截图：{shot}", flush=True)

        # ---------------- 安全关闭（无输入，正常应直接关闭；若提示保存则取消）
        started = time.perf_counter()
        close_log: List[Dict[str, Any]] = []
        if editor is not None:
            _ensure_foreground(ctrl, pid, tuple(editor.center))
        ctrl.hotkey("alt", "f4")
        time.sleep(1.5)
        for _ in range(3):
            remaining = [w for w in _notepad_windows(ctrl) if w.process_id == pid]
            if not remaining:
                close_log.append({"event": "closed"})
                break
            buttons = [
                b for b in ctrl.find_all_elements(role="button", limit=20, max_depth=8, timeout=5.0)
                if b.process_id == pid and b.name.strip() in ("保存", "不保存", "取消", "Save", "Don't Save", "Cancel")
            ]
            close_log.append({
                "event": "still_open",
                "windows": [{"name": w.name, "bounds": list(w.bounds)} for w in remaining],
                "buttons": [{"name": b.name, "center": list(b.center)} for b in buttons],
            })
            cancel = next((b for b in buttons if b.name.strip() in ("取消", "Cancel")), None)
            if cancel is None:
                break
            ctrl.click(x=cancel.center[0], y=cancel.center[1])
            close_log.append({"event": "clicked_cancel"})
            time.sleep(0.8)
            break

        gone = _wait_window_gone(pid, ctrl, timeout=8.0)
        if not gone:
            killed = _terminate(pid)
            gone = _wait_window_gone(pid, ctrl, timeout=8.0) or killed
            close_log.append({"event": "taskkill_fallback", "killed": killed})
        report["steps"]["close"] = {
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "log": close_log,
            "window_gone": gone,
        }
        print(f"[{' OK ' if gone else 'WARN'}] 关闭结果：gone={gone} log={close_log}", flush=True)

    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[FAIL] 异常：{report['error']}", flush=True)
    finally:
        try:
            ctrl.move_to(center[0], center[1])
            report["mouse_restored"] = list(ctrl.mouse.position())
        except Exception as exc:  # noqa: BLE001
            report["mouse_restored"] = str(exc)
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    json_path = os.path.join(OUT_DIR, f"probe_notepad_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"[ OK ] 报告：{json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
