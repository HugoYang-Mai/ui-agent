"""ui-agent MCP Server 的独立启动入口（stdio 传输）。

供 MCP 客户端（CowAgent / Marvis / Claude Desktop 等）以子进程方式拉起：

.. code-block:: json

    {
      "mcpServers": {
        "ui-agent": {
          "command": "D:\\\\Projects\\\\ui-agent\\\\.venv\\\\Scripts\\\\python.exe",
          "args": ["D:\\\\Projects\\\\ui-agent\\\\serve_mcp.py"]
        }
      }
    }

等价启动方式：``.venv\\Scripts\\python.exe main.py mcp``。

约定
----
* stdout 只承载 JSON-RPC 报文，内核日志一律走 stderr。
* 进程启动第一步即 ``SetProcessDpiAwareness(2)``，统一为物理像素坐标系。
* 所有 UIA / OCR 调用都在同一个已初始化 COM 的工作线程内串行执行。
"""

from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def main() -> int:
    from uiagent.mcp_server import run_stdio

    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
