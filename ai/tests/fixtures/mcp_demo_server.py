"""
阶段4A E2E 演示 MCP server（stdio 传输，仅供浏览器 E2E 使用，不进单测路径）。

包含两个工具，用于验证 confirm 门控与真实调用链路：
- now_iso：返回服务器当前本地时间（声明 readOnlyHint=True，免 confirm 直通）；
- append_log：把一行文本追加到指定目录的 mcp_demo.log 文件（未声明只读，
  一律 confirm 人机把关；E2E 用它验证确认/拒绝与磁盘副作用）。

E2E 用法（写入 ai/.env 后重启 AI 模块，路径按实际环境调整）：
MCP_ENABLED=true
MCP_SERVERS=[{"name":"demo","command":"<ai/.venv/Scripts/python.exe 绝对路径>",
              "args":["-X","utf8","<本项目>/ai/tests/fixtures/mcp_demo_server.py"]}]
"""
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("demo-tools")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def now_iso() -> str:
    """返回服务器当前本地时间（ISO 格式）。"""
    return datetime.now().isoformat(timespec="seconds")


@mcp.tool()
def append_log(target_dir: str, text: str) -> str:
    """把一行文本追加到 target_dir 目录下的 mcp_demo.log 文件（写入操作）。"""
    d = Path(target_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "mcp_demo.log"
    with p.open("a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    return f"已追加到 {p}（当前 {p.stat().st_size} 字节）"


if __name__ == "__main__":
    mcp.run(transport="stdio")
