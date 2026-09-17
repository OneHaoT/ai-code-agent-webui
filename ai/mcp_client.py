"""
阶段4A：MCP client 连接管理器（stdio 传输，接入外部工具生态）。

职责
----
1. 解析 MCP_SERVERS 配置（JSON 数组；坏条目 WARN 跳过，全部非法等效未启用）；
2. 启动期并行建立 stdio 子进程连接（mcp SDK ClientSession），单 server
   连接+枚举总预算 MCP_CONNECT_TIMEOUT_SECONDS（默认 10s）；失败 WARN
   跳过，不阻断 AI 启动（/health 标 failed）；
3. call_tool(server, tool, args)：经 session 调用，MCP_TOOL_TIMEOUT_SECONDS
   （默认 60s）硬超时；isError / 协议错误 / 异常 / 超时 / server 死亡一律
   收敛为 (is_error=True, 中文说明)，绝不向调用方抛异常杀流；
4. 结果 content blocks 仅支持 text（多块以换行拼接），image/resource 等类型
   占位行标注；字节截断由 tools.py 闭包按 TOOL_MAX_BYTES 处理（本模块返回
   纯文本，不依赖 tools 模块——两者单向依赖：tools → 本模块）；
5. 生命周期：AI 进程正常退出时关闭全部 session 与子进程；server 意外死亡
   → 后续调用立即 error，不自动重启（诚实降级，重启 AI 恢复）。

线程模型
--------
mcp SDK 为 async API，而工具在线程池中同步执行：manager 自持一个后台事件
循环线程，lifespan 启动期调用 start()（阻塞直到连接阶段结束），工具线程的
call_tool 经 run_coroutine_threadsafe 桥接到该循环；close() 同步收敛。

威胁模型（诚实声明）：MCP server 进程以当前用户权限运行，其能力域在
Workspace 沙箱边界之外（本项目的路径沙箱不覆盖外部工具）。防线 =
① server 由管理员在 ai/.env 显式配置，模型不可控；② 未声明只读的工具
一律 confirm 人机把关（tools.requires_confirm）；③ server 自身的权限约定。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError

logger = logging.getLogger("ai-assist")


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("%s 非法，回退 %s", key, default)
        return default


def parse_servers_config(raw: str | None) -> list[dict]:
    """解析 MCP_SERVERS JSON 配置；坏条目 WARN 跳过（不抛异常）。

    条目格式：{"name": "fs", "command": "npx", "args": [...], "env": {...}}。
    name/command 必填；args/env 可选。全部非法时返回 []（等效未启用）。
    """
    try:
        servers = json.loads(raw or "[]")
    except json.JSONDecodeError as e:
        logger.warning("MCP_SERVERS 不是合法 JSON：%s（按无 server 处理）", e)
        return []
    if not isinstance(servers, list):
        logger.warning("MCP_SERVERS 必须是 JSON 数组（按无 server 处理）")
        return []
    valid: list[dict] = []
    for i, item in enumerate(servers):
        if (not isinstance(item, dict) or not str(item.get("name", "")).strip()
                or not str(item.get("command", "")).strip()):
            logger.warning("MCP_SERVERS[%d] 缺 name/command 或不是对象，已跳过", i)
            continue
        entry = {
            "name": str(item["name"]).strip(),
            "command": str(item["command"]).strip(),
            "args": [str(a) for a in item.get("args") or []],
            "env": {str(k): str(v) for k, v in (item.get("env") or {}).items()},
        }
        valid.append(entry)
    return valid


class _ServerState:
    """单个 MCP server 的连接状态（connected | failed）与工具清单缓存。"""

    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.command = cfg["command"]
        self.args = list(cfg.get("args") or [])
        self.env = dict(cfg.get("env") or {})
        self.status = "pending"          # pending -> connected | failed
        self.error: str | None = None
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        # 工具清单缓存：[{name, description, input_schema, read_only_hint}]
        self.tools: list[dict] = []


class MCPManager:
    """MCP 连接管理器（鸭子类型接口供 tools.register_mcp_tools 使用）：

    - iter_connected_tools() -> (server_name, tool_entry) 迭代；
    - call_tool(server, tool, args) -> (is_error, text) 同步调用；
    - tool_timeout: float（tools.py 据此注册 per-tool 超时 = 值 + 5s 缓冲）；
    - health() -> [{name, status, tools}]（/health 数据源）。
    """

    def __init__(self, servers: list[dict],
                 connect_timeout: float = 10.0, tool_timeout: float = 60.0):
        self._states = [_ServerState(cfg) for cfg in servers]
        self.connect_timeout = connect_timeout
        self.tool_timeout = tool_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ---- 配置 ----

    @classmethod
    def from_env(cls) -> "MCPManager":
        servers = parse_servers_config(os.getenv("MCP_SERVERS"))
        return cls(
            servers,
            connect_timeout=_float_env("MCP_CONNECT_TIMEOUT_SECONDS", 10.0),
            tool_timeout=_float_env("MCP_TOOL_TIMEOUT_SECONDS", 60.0),
        )

    # ---- 连接（启动期） ----

    def start(self) -> None:
        """启动后台事件循环线程并并行建立全部连接（阻塞至连接阶段结束）。

        connect_all 内部对每个 server 自兜底（失败 WARN 跳过），本方法不抛。
        """
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-client", daemon=True)
        self._thread.start()
        try:
            asyncio.run_coroutine_threadsafe(
                self.connect_all(), self._loop).result(
                timeout=self.connect_timeout * len(self._states) + 15)
        except Exception as e:  # noqa: BLE001 —— 连接阶段绝不阻断启动
            logger.warning("MCP 连接阶段异常（跳过，AI 正常启动）：%s", e)

    async def connect_all(self) -> None:
        """并行连接全部 server；每个失败 WARN 跳过（不抛异常）。"""
        if not self._states:
            logger.info("MCP: 无有效 server 配置，跳过连接")
            return
        await asyncio.gather(*(self._connect_one(st) for st in self._states))
        ok = [st.name for st in self._states if st.status == "connected"]
        bad = [(st.name, st.error) for st in self._states if st.status == "failed"]
        logger.info("MCP: 连接完成 connected=%s failed=%s", ok, bad)

    async def _connect_one(self, st: _ServerState) -> None:
        """单 server：启动子进程 → initialize 握手 → list_tools 缓存。

        总预算 connect_timeout（含子进程启动）；失败清理半开连接后标 failed。
        """

        async def _open() -> None:
            stack = AsyncExitStack()
            try:
                env = None
                if st.env:
                    # 用户 env 与 SDK 安全默认环境合并（防 Windows 缺 SystemRoot 等）
                    env = {**get_default_environment(), **st.env}
                params = StdioServerParameters(
                    command=st.command, args=st.args, env=env)
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(
                    ClientSession(read, write))
                await session.initialize()
                result = await session.list_tools()
            except BaseException:
                await stack.aclose()  # 半开连接清理（取消路径同样触发）
                raise

            st.session = session
            st._stack = stack
            st.tools = [_tool_entry(t) for t in result.tools]
            st.status = "connected"
            st.error = None
            logger.info("MCP: server=%s connected, tools=%s（%d 个）",
                        st.name, [t["name"] for t in st.tools], len(st.tools))

        try:
            await asyncio.wait_for(_open(), self.connect_timeout)
        except asyncio.TimeoutError:
            st.status = "failed"
            st.error = f"连接超时（>{self.connect_timeout:.0f}s）"
            logger.warning("MCP: server=%s %s", st.name, st.error)
        except Exception as e:  # noqa: BLE001
            st.status = "failed"
            st.error = f"{type(e).__name__}: {e}"
            logger.warning("MCP: server=%s 连接失败，已跳过：%s", st.name, st.error)

    # ---- 工具调用（运行期） ----

    def call_tool(self, server: str, tool: str, args: dict) -> tuple[bool, str]:
        """同步调用入口（tools.py 闭包在线程池调用）。返回 (is_error, text)。

        任何失败（超时/异常/server 死亡/未知 server）都收敛为 is_error=True
        + 中文说明，绝不抛异常杀流。
        """
        st = self._state(server)
        if st is None or st.status != "connected" or st.session is None:
            return True, f"外部工具服务不可用：{server}（未连接或已断开）"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._call_one(st, tool, args), self._loop)
            # 协程内部已有硬超时，此处在 Wrap 上再留缓冲兜底（防桥接层挂死）
            return fut.result(self.tool_timeout + 15)
        except Exception as e:  # noqa: BLE001 —— 桥接层最后防线
            return True, f"外部工具调用失败：{type(e).__name__}: {e}"

    async def _call_one(self, st: _ServerState, tool: str,
                        args: dict) -> tuple[bool, str]:
        try:
            result = await asyncio.wait_for(
                st.session.call_tool(tool, args or {},
                                     read_timeout_seconds=self.tool_timeout),
                self.tool_timeout + 5)
        except asyncio.TimeoutError:
            return True, f"外部工具执行超时（>{self.tool_timeout:.0f}s）"
        except MCPError as e:
            # 协议层错误（如工具名不存在）：session 仍可用，不标 failed
            return True, f"外部工具协议错误：{e}"
        except BaseException as e:  # noqa: BLE001 —— 含 anyio 断流/进程死亡
            # 进程死亡 / 管道破裂：session 不可恢复，标 failed（诚实降级）
            st.status = "failed"
            st.error = f"{type(e).__name__}: {e}"
            return True, f"外部工具服务已断开：{type(e).__name__}: {e}"
        text = _content_to_text(getattr(result, "content", None))
        if getattr(result, "isError", False):
            return True, text or "外部工具返回错误（无内容）"
        return False, text

    # ---- 生命周期 ----

    def close(self) -> None:
        """同步关闭：关闭全部 session 与子进程，再停事件循环线程。"""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._close_all(), self._loop).result(timeout=15)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP 关闭阶段异常：%s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop.close()
        self._loop = None
        self._thread = None
        logger.info("MCP: 全部连接已关闭")

    async def _close_all(self) -> None:
        for st in self._states:
            if st._stack is not None:
                try:
                    await st._stack.aclose()
                except Exception as e:  # noqa: BLE001
                    logger.warning("MCP: 关闭 server=%s 异常：%s", st.name, e)
                st._stack = None
                st.session = None
                st.status = "failed"

    # ---- 观测与注册数据源 ----

    def _state(self, name: str) -> _ServerState | None:
        for st in self._states:
            if st.name == name:
                return st
        return None

    def iter_connected_tools(self):
        """tools.register_mcp_tools 的数据源：(server_name, tool_entry) 迭代。"""
        for st in self._states:
            if st.status == "connected":
                for t in st.tools:
                    yield st.name, t

    def health(self) -> list[dict]:
        """/health 的 mcp_servers 字段：[{name, status(connected|failed), tools}]。"""
        return [{"name": st.name, "status": st.status, "tools": len(st.tools)}
                for st in self._states]


def _tool_entry(t) -> dict:
    """mcp.types.Tool -> 内部工具清单条目（read_only_hint 缺省 False）。"""
    ann = getattr(t, "annotations", None)
    hint = bool(getattr(ann, "read_only_hint", False)) if ann is not None else False
    return {
        "name": t.name,
        "description": (getattr(t, "description", None) or "").strip(),
        "input_schema": getattr(t, "input_schema", None) or {
            "type": "object", "properties": {}},
        "read_only_hint": hint,
    }


def _content_to_text(blocks) -> str:
    """CallToolResult.content blocks -> 文本：仅拼 text，其他类型占位行标注。"""
    parts: list[str] = []
    for b in blocks or []:
        btype = getattr(b, "type", None)
        if btype == "text":
            parts.append(getattr(b, "text", "") or "")
        else:
            parts.append(f"[不支持的内容类型: {btype or 'unknown'}]")
    return "\n".join(parts)
