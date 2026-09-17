"""
阶段4A：MCP client 连接管理器与工具注册测试（Fake session/manager，零真实
子进程、零网络）。

覆盖：MCP_SERVERS 配置解析（坏 JSON/坏条目跳过）、content 类型占位、
_tool_entry 只读声明提取、call_tool 收敛矩阵（成功/isError/协议错误/异常/
超时/server 死亡标 failed）、同步桥接（真实事件循环线程 + Fake session）、
连接超时/失败不阻断启动（Fake CM 注入）、register_mcp_tools 命名
sanitize/64 截断/重名跳过 WARN、readOnlyHint→requires_confirm 真值表、
dispatch 闭包（输出截断/错误语义/复用 execute_tool）。

运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_mcp_client.py -q
"""
from __future__ import annotations

import asyncio
import threading
import types

import pytest

import mcp_client
import tools
from mcp_client import (MCPManager, _content_to_text, _tool_entry,
                        parse_servers_config)
from workspace import Workspace


# ---------------- 配置解析 ----------------

def test_parse_servers_config_valid():
    raw = ('[{"name":"fs","command":"npx","args":["-y","srv","C:/demo"],'
           '"env":{"K":"V"}}]')
    cfg = parse_servers_config(raw)
    assert cfg == [{"name": "fs", "command": "npx",
                    "args": ["-y", "srv", "C:/demo"], "env": {"K": "V"}}]


def test_parse_servers_config_bad_json_and_entries():
    assert parse_servers_config("not-json") == []
    assert parse_servers_config('{"name":"x"}') == []          # 非数组
    assert parse_servers_config("[]") == []
    # 坏条目跳过，好条目保留
    cfg = parse_servers_config('[{"name":"ok","command":"c"},'
                               '{"name":""},{"command":"c"},"x",null]')
    assert [c["name"] for c in cfg] == ["ok"]
    # args/env 缺省为空，非字符串元素强转
    cfg2 = parse_servers_config('[{"name":"a","command":"c","args":[1,true],'
                                '"env":{"A":1}}]')
    assert cfg2[0]["args"] == ["1", "True"] and cfg2[0]["env"] == {"A": "1"}


def test_manager_from_env(monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", '[{"name":"s","command":"c"}]')
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SECONDS", "21")
    m = MCPManager.from_env()
    assert m.connect_timeout == 3.5 and m.tool_timeout == 21.0
    assert [st.name for st in m._states] == ["s"]


# ---------------- content / 工具条目转换 ----------------

def test_content_to_text_and_unsupported_placeholder():
    blk = lambda t, **kw: types.SimpleNamespace(type=t, **kw)
    text = _content_to_text([blk("text", text="A"), blk("image"),
                             blk("resource"), blk("text", text="B")])
    assert text == "A\n[不支持的内容类型: image]\n[不支持的内容类型: resource]\nB"
    assert _content_to_text(None) == ""


def test_tool_entry_read_only_hint_extraction():
    t = types.SimpleNamespace(name="t1", description=" d ",
                              input_schema={"type": "object"}, annotations=None)
    assert _tool_entry(t)["read_only_hint"] is False
    ann = types.SimpleNamespace(read_only_hint=True)
    t2 = types.SimpleNamespace(name="t2", description=None,
                               input_schema=None, annotations=ann)
    e = _tool_entry(t2)
    assert e["read_only_hint"] is True
    assert e["input_schema"] == {"type": "object", "properties": {}}
    assert e["description"] == ""


# ---------------- call_tool 收敛矩阵 ----------------

class FakeSession:
    """可编程 Fake session：按预设返回/抛异常（模仿 mcp ClientSession）。"""

    def __init__(self, behavior):
        self.behavior = behavior  # ("ok"|"iserror"|"mcperror"|"die"|"slow", 载荷)
        self.calls: list[tuple] = []

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        self.calls.append((name, arguments, read_timeout_seconds))
        kind, payload = self.behavior
        if kind == "ok":
            return types.SimpleNamespace(
                isError=False, content=[types.SimpleNamespace(type="text",
                                                              text=payload)])
        if kind == "iserror":
            return types.SimpleNamespace(
                isError=True, content=[types.SimpleNamespace(type="text",
                                                             text=payload)])
        if kind == "mcperror":
            # SDK 协议错误：MCPError(code, message)
            raise mcp_client.MCPError(-32602, payload)
        if kind == "die":
            raise ConnectionError(payload)
        if kind == "slow":
            # 模仿真实 SDK：read_timeout_seconds 到点抛超时
            await asyncio.wait_for(asyncio.sleep(payload), read_timeout_seconds)
        raise AssertionError(kind)


def make_manager(behavior, tool_timeout=5.0) -> MCPManager:
    m = MCPManager([{"name": "s1", "command": "x", "args": [], "env": {}}],
                   connect_timeout=2.0, tool_timeout=tool_timeout)
    st = m._states[0]
    st.status = "connected"
    st.session = FakeSession(behavior)
    st.tools = [{"name": "t1", "description": "", "input_schema":
                 {"type": "object"}, "read_only_hint": False}]
    return m


def test_call_tool_success_and_iserror():
    m = make_manager(("ok", "结果文本"))
    assert asyncio.run(m._call_one(m._states[0], "t1", {"a": 1})) == \
        (False, "结果文本")
    m2 = make_manager(("iserror", "工具自己报错"))
    is_err, text = asyncio.run(m2._call_one(m2._states[0], "t1", {}))
    assert is_err and "工具自己报错" in text
    # 未知 server 收敛（不触碰事件循环即可判定）
    m3 = make_manager(("ok", "x"))
    is_err2, text2 = m3.call_tool("nope", "t1", {})
    assert is_err2 and "不可用" in text2


def test_call_tool_mcperror_and_death_marks_failed():
    m = make_manager(("mcperror", "unknown tool"))
    is_err, text = asyncio.run(m._call_one(m._states[0], "t1", {}))
    assert is_err and "协议错误" in text
    assert m._states[0].status == "connected"  # 协议错误不断开

    m2 = make_manager(("die", "pipe broken"))
    is_err2, text2 = asyncio.run(m2._call_one(m2._states[0], "t1", {}))
    assert is_err2 and "已断开" in text2
    assert m2._states[0].status == "failed"    # 进程死亡诚实标 failed
    # 死亡后再调用立即 error（不重启；未连接判定在桥接前完成）
    is_err3, text3 = m2.call_tool("s1", "t1", {})
    assert is_err3 and "不可用" in text3


def test_call_tool_timeout_converges():
    m = make_manager(("slow", 5), tool_timeout=0.1)
    is_err, text = asyncio.run(m._call_one(m._states[0], "t1", {}))
    assert is_err and "超时" in text
    assert m._states[0].status == "connected"  # 超时不是断开


def test_call_tool_sync_bridge_real_loop():
    """同步桥接：真实后台事件循环线程 + run_coroutine_threadsafe。"""
    m = make_manager(("ok", "桥接成功"))
    m._loop = asyncio.new_event_loop()
    m._thread = threading.Thread(target=m._loop.run_forever,
                                 name="mcp-test", daemon=True)
    m._thread.start()
    try:
        assert m.call_tool("s1", "t1", {"k": "v"}) == (False, "桥接成功")
        # read_timeout_seconds 透传 = tool_timeout
        name, args, timeout = m._states[0].session.calls[0]
        assert name == "t1" and args == {"k": "v"}
        assert timeout == m.tool_timeout
    finally:
        m.close()
    assert m._loop is None  # close 同步收敛（session 无 stack 时干净退出）


# ---------------- 连接阶段（Fake CM 注入，零真实子进程） ----------------

class FakeAsyncCM:
    """async 上下文管理器替身，enter 返回预设值，追踪关闭。"""

    def __init__(self, result):
        self._result = result
        self.closed = False

    async def __aenter__(self):
        return self._result

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class FakeProtocolSession:
    """模仿握手期 session：initialize/list_tools 可编程。"""

    def __init__(self, tools=None, fail=None, hang=False):
        self.tools = tools or []
        self.fail = fail
        self.hang = hang

    async def initialize(self):
        if self.fail:
            raise self.fail
        if self.hang:
            await asyncio.sleep(30)

    async def list_tools(self):
        return types.SimpleNamespace(tools=self.tools)


def patch_mcp_sdk(monkeypatch, session):
    """把 mcp_client 引用的 SDK 符号替换为 Fake（stdio_client/ClientSession）。"""
    rw = (object(), object())
    stdio_cm = FakeAsyncCM(rw)
    session_cm = FakeAsyncCM(session)
    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: stdio_cm)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda r, w: session_cm)
    return stdio_cm, session_cm


def test_connect_one_success_caches_tools(monkeypatch):
    tool = types.SimpleNamespace(
        name="t1", description="d",
        input_schema={"type": "object"}, annotations=None)
    session = FakeProtocolSession(tools=[tool])
    stdio_cm, session_cm = patch_mcp_sdk(monkeypatch, session)
    m = MCPManager([{"name": "s1", "command": "x", "args": ["a"], "env": {}}])

    asyncio.run(m.connect_all())

    st = m._states[0]
    assert st.status == "connected" and st.tools[0]["name"] == "t1"
    assert [t["name"] for _, t in m.iter_connected_tools()] == ["t1"]
    assert m.health() == [{"name": "s1", "status": "connected", "tools": 1}]
    # 关闭：stack 生命周期归 manager（_stack 替换为真实 AsyncExitStack）
    assert st._stack is not None


def test_connect_timeout_and_error_skip_not_block(monkeypatch):
    # 握手挂起 → connect 预算内超时标 failed
    hang_session = FakeProtocolSession(hang=True)
    patch_mcp_sdk(monkeypatch, hang_session)
    m = MCPManager([{"name": "slow", "command": "x"}], connect_timeout=0.2)
    asyncio.run(m.connect_all())
    st = m._states[0]
    assert st.status == "failed" and "超时" in st.error

    # 握手抛异常 → failed 带类型名，iter_connected_tools 不产出
    bad_session = FakeProtocolSession(fail=RuntimeError("boom"))
    patch_mcp_sdk(monkeypatch, bad_session)
    m2 = MCPManager([{"name": "bad", "command": "x"}], connect_timeout=2)
    asyncio.run(m2.connect_all())
    st2 = m2._states[0]
    assert st2.status == "failed" and "RuntimeError" in st2.error
    assert list(m2.iter_connected_tools()) == []


def test_manager_without_servers(monkeypatch):
    monkeypatch.delenv("MCP_SERVERS", raising=False)
    m = MCPManager.from_env()
    assert m._states == [] and m.health() == []
    asyncio.run(m.connect_all())  # 空配置直接返回不抛


# ---------------- tools.register_mcp_tools / requires_confirm ----------------

@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(str(root))


@pytest.fixture
def clean_mcp_registry():
    """注册前后快照恢复（就地恢复 TOOL_SCHEMAS，agent 持同一对象）。"""
    snap = (list(tools.TOOL_SCHEMAS), dict(tools._DISPATCH),
            dict(tools._TOOL_TIMEOUTS), dict(tools._MCP_TOOL_META))
    yield
    tools.TOOL_SCHEMAS[:] = snap[0]
    tools._DISPATCH.clear(); tools._DISPATCH.update(snap[1])
    tools._TOOL_TIMEOUTS.clear(); tools._TOOL_TIMEOUTS.update(snap[2])
    tools._MCP_TOOL_META.clear(); tools._MCP_TOOL_META.update(snap[3])


class FakeManager:
    """鸭子类型 manager：tools.register_mcp_tools 所需的最小接口。"""

    def __init__(self, entries, call_result=(False, "fake 输出"),
                 tool_timeout=60.0):
        self._entries = entries  # [(server, {name, description, input_schema, read_only_hint})]
        self._call_result = call_result
        self.tool_timeout = tool_timeout
        self.calls: list[tuple] = []

    def iter_connected_tools(self):
        yield from self._entries

    def call_tool(self, server, tool, args):
        self.calls.append((server, tool, args))
        return self._call_result


def _entry(server, name, hint=False, desc="外部工具描述", schema=None):
    return (server, {"name": name, "description": desc,
                     "input_schema": schema or {"type": "object",
                                                "properties": {}},
                     "read_only_hint": hint})


def test_register_naming_sanitize_truncate_and_schema_prefix(clean_mcp_registry):
    entries = [
        _entry("my server!", "do@thing", desc="查东西"),
        _entry("s" * 80, "t" * 80),               # 触发 64 截断
    ]
    registered = tools.register_mcp_tools(FakeManager(entries))

    assert registered == ["mcp_my_server__do_thing",
                          ("mcp_" + "s" * 80 + "_" + "t" * 80)[:64]]
    for reg in registered:
        assert len(reg) <= 64
        assert tools.tool_available(reg)
    fn = next(s for s in tools.TOOL_SCHEMAS
              if s["function"]["name"] == "mcp_my_server__do_thing")
    assert fn["function"]["description"].startswith("（来自外部 MCP server my server!）")
    assert "查东西" in fn["function"]["description"]
    assert fn["function"]["parameters"] == {"type": "object", "properties": {}}


def test_register_duplicate_name_skips_with_warn(clean_mcp_registry, caplog):
    entries = [_entry("srv", "tool"), _entry("srv", "tool")]  # 同名注册两次
    with caplog.at_level("WARNING", logger="ai-assist"):
        registered = tools.register_mcp_tools(FakeManager(entries))
    assert registered == ["mcp_srv_tool"]
    assert any("命名冲突" in r.message for r in caplog.records)


def test_register_timeout_and_read_only_hint_table(clean_mcp_registry):
    tools.register_mcp_tools(FakeManager(
        [_entry("srv", "ro", hint=True), _entry("srv", "rw", hint=False)],
        tool_timeout=30.0))
    # per-tool 超时 = tool_timeout + 5 缓冲
    assert tools._TOOL_TIMEOUTS["mcp_srv_ro"] == 35.0
    # requires_confirm 真值表
    assert tools.requires_confirm("write_file") is True
    assert tools.requires_confirm("run_command") is True
    assert tools.requires_confirm("mcp_srv_ro") is False     # 声明只读 → 免确认
    assert tools.requires_confirm("mcp_srv_rw") is True      # 未声明 → 必须 confirm
    assert tools.requires_confirm("read_file") is False      # 内置只读
    assert tools.requires_confirm("mcp_ghost_none") is False  # 未注册名 → 走未知工具降级
    assert tools.get_mcp_meta("mcp_srv_ro") == {
        "server": "srv", "tool": "ro", "read_only_hint": True}


def test_mcp_dispatch_via_execute_tool_truncation_and_error(ws, clean_mcp_registry):
    big = "x" * (tools.MAX_BYTES + 100)
    tools.register_mcp_tools(FakeManager([_entry("srv", "t")],
                                         call_result=(False, big)))
    out = tools.execute_tool(ws, "mcp_srv_t", {})
    assert out.status == "success" and out.truncated
    assert out.output.endswith("…（输出超限截断）")
    assert len(out.output.encode("utf-8")) <= tools.MAX_BYTES + 32

    tools.register_mcp_tools(FakeManager([_entry("srv", "err")],
                                         call_result=(True, "坏了")))
    out2 = tools.execute_tool(ws, "mcp_srv_err", {})
    assert out2.status == "error" and out2.output == "坏了"

    # 坏参数：非 dict 走既有 execute_tool 校验兜底；多余键则透传给外部
    # server（由其 input_schema 自行校验，MCP 语义与内置工具固定签名不同）
    out3 = tools.execute_tool(ws, "mcp_srv_t", "not-a-dict")
    assert out3.status == "error" and "JSON 对象" in out3.output
    out4 = tools.execute_tool(ws, "mcp_srv_t", {"unexpected": 1})
    assert out4.status == "success"
