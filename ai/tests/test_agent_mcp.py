"""
阶段4A：agent 工具循环接入 MCP 外部工具测试（Fake 流式客户端 + Fake 注册，
零真实子进程、零网络）。

覆盖：只读声明工具零 confirm 直通 / 未声明工具 confirm 帧出现（摘要含
server/tool/参数）→ 确认执行 / 拒绝 error 回填零副作用 / 未注册名"未知工具"
降级（MCP_ENABLED=false 语义）/ 计入 MAX_TOOL_ITERATIONS / 坏 JSON 参数 /
工具 schema 对模型可见 / /health 外显（mcp_enabled/mcp_servers）/
子进程装配隔离（MCP_ENABLED=false 时零 mcp 模块加载）。

运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_agent_mcp.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import tools  # noqa: E402
from workspace import Workspace  # noqa: E402

from test_agent import FakeClient, by_type, tc_chunk, text_chunk  # noqa: E402
from agent import iter_agent_events  # noqa: E402

AI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeManager:
    """鸭子类型 manager：提供两个演示工具（与 E2E 演示 server 同构）。"""

    tool_timeout = 60.0

    def __init__(self):
        self.calls: list[tuple] = []

    def iter_connected_tools(self):
        yield "demo", {"name": "now", "description": "返回当前时间",
                       "input_schema": {"type": "object", "properties": {}},
                       "read_only_hint": True}
        yield "demo", {"name": "append_log", "description": "追加日志",
                       "input_schema": {"type": "object", "properties": {
                           "text": {"type": "string"}}},
                       "read_only_hint": False}

    def call_tool(self, server, tool, args):
        self.calls.append((server, tool, args))
        if tool == "now":
            return False, "2026-09-17T10:00:00"
        return False, "已追加（fake）"


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(str(root))


@pytest.fixture
def mcp_tools():
    """注册 Fake MCP 工具并在用例结束后就地快照恢复（防污染其他用例）。"""
    snap = (list(tools.TOOL_SCHEMAS), dict(tools._DISPATCH),
            dict(tools._TOOL_TIMEOUTS), dict(tools._MCP_TOOL_META))
    mgr = FakeManager()
    registered = tools.register_mcp_tools(mgr)
    yield mgr, registered
    tools.TOOL_SCHEMAS[:] = snap[0]
    tools._DISPATCH.clear(); tools._DISPATCH.update(snap[1])
    tools._TOOL_TIMEOUTS.clear(); tools._TOOL_TIMEOUTS.update(snap[2])
    tools._MCP_TOOL_META.clear(); tools._MCP_TOOL_META.update(snap[3])


def run_stream(rounds, ws, *, decide=None, max_iterations=8, stream_id="s1"):
    """后台线程驱动 agent 事件流；主线程按需决策 confirm（照抄阶段3 手法）。"""
    import threading
    import time

    client = FakeClient(rounds)
    events: list[dict] = []
    finished = threading.Event()

    def drive():
        try:
            for evt in iter_agent_events(
                    client, [{"role": "user", "content": "hi"}],
                    base_kwargs={"model": "fake-model"},
                    workspace=ws, max_iterations=max_iterations,
                    tools_enabled=True, stream_id=stream_id):
                events.append(evt)
        finally:
            finished.set()

    t = threading.Thread(target=drive, daemon=True)
    t.start()

    if decide is not None:
        deadline = time.monotonic() + 5
        decided = 0
        while time.monotonic() < deadline:
            confirms = [e for e in events if e["type"] == "confirm"]
            while decided < len(confirms):
                decide(confirms[decided])
                decided += 1
            if finished.is_set():
                break
            time.sleep(0.005)
    t.join(timeout=15)
    assert finished.is_set(), "agent 流未在限定时间内结束"
    return client, events


def assert_paired(events):
    call_ids = [e["id"] for e in by_type(events, "tool_call")]
    result_ids = [e["id"] for e in by_type(events, "tool_result")]
    assert call_ids == result_ids


# ---------------- 只读声明工具：零 confirm 直通 ----------------

def test_mcp_readonly_tool_direct_no_confirm(ws, mcp_tools):
    mgr, registered = mcp_tools
    rounds = [
        [tc_chunk(0, id="m1", name="mcp_demo_now"), tc_chunk(0, arguments="{}")],
        [text_chunk("现在是 " + "2026")],
    ]
    client, events = run_stream(rounds, ws)

    assert "mcp_demo_now" in registered
    assert by_type(events, "confirm") == []          # 声明只读 → 零 confirm
    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "success" and "2026-09-17T10:00:00" in tr["output"]
    assert mgr.calls == [("demo", "now", {})]
    # 工具 schema 对模型可见（描述前缀注明来源）
    sent_tools = client.chat.completions.create_calls[0]["tools"]
    schema = next(s for s in sent_tools
                  if s["function"]["name"] == "mcp_demo_now")
    assert "（来自外部 MCP server demo）" in schema["function"]["description"]
    assert by_type(events, "_final_state")


# ---------------- 未声明只读：confirm 全链路 ----------------

def test_mcp_undeclared_tool_confirm_then_execute(ws, mcp_tools):
    rounds = [
        [tc_chunk(0, id="m2", name="mcp_demo_append_log"),
         tc_chunk(0, arguments='{"text": "hello e2e"}')],
        [text_chunk("已记录")],
    ]

    def approve(evt):
        assert evt["tool"] == "mcp_demo_append_log"
        assert "demo" in evt["summary"] and "append_log" in evt["summary"]
        assert '"text"' in evt["summary"] and "hello e2e" in evt["summary"]
        assert agent.resolve_confirm(evt["id"], True) is not None

    client, events = run_stream(rounds, ws, decide=approve)

    assert_paired(events)
    assert len(by_type(events, "confirm")) == 1
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "success" and "已追加" in tr["output"]
    assert by_type(events, "token")   # 确认后模型继续收敛
    # 帧顺序：tool_call → confirm → tool_result
    types_seq = [e["type"] for e in events]
    assert types_seq.index("tool_call") < types_seq.index("confirm") \
        < types_seq.index("tool_result")


def test_mcp_undeclared_tool_rejected_zero_side_effect(ws, mcp_tools):
    mgr, _ = mcp_tools
    rounds = [
        [tc_chunk(0, id="m3", name="mcp_demo_append_log"),
         tc_chunk(0, arguments='{"text": "nope"}')],
        [text_chunk("好的，不写了")],
    ]

    def reject(evt):
        assert agent.resolve_confirm(evt["id"], False) is not None

    client, events = run_stream(rounds, ws, decide=reject)

    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "拒绝" in tr["output"]
    assert mgr.calls == []                 # 零副作用：外部工具未被调用
    assert by_type(events, "_final_state")  # 流正常 done，模型继续收敛


def test_mcp_confirm_timeout_auto_rejects(ws, mcp_tools, monkeypatch):
    monkeypatch.setattr(agent, "CONFIRM_TIMEOUT_SECONDS", 0.3)
    rounds = [
        [tc_chunk(0, id="m4", name="mcp_demo_append_log"),
         tc_chunk(0, arguments='{"text": "late"}')],
        [text_chunk("超时就算了")],
    ]
    client, events = run_stream(rounds, ws, decide=None)

    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "超时" in tr["output"]
    assert by_type(events, "_final_state")


# ---------------- 降级 / 上限 / 坏参数 ----------------

def test_mcp_unregistered_name_unknown_tool_degradation(ws):
    """MCP_ENABLED=false 语义：未注册的 mcp_* 名 → 未知工具 error，无 confirm。"""
    rounds = [
        [tc_chunk(0, id="m5", name="mcp_ghost_tool"),
         tc_chunk(0, arguments='{"x": 1}')],
        [text_chunk("好的")],
    ]
    client, events = run_stream(rounds, ws, decide=None)

    assert_paired(events)
    assert by_type(events, "confirm") == []
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "未知工具" in tr["output"]


def test_mcp_tool_counts_toward_max_iterations(ws, mcp_tools):
    rounds = [
        [tc_chunk(0, id="m6", name="mcp_demo_now"), tc_chunk(0, arguments="{}")],
        # 收敛提示轮（max=1 已用掉）仍请求工具 → AgentLimitError，无 confirm
        [tc_chunk(0, id="m7", name="mcp_demo_now"), tc_chunk(0, arguments="{}")],
    ]

    client, events = run_stream(rounds, ws, decide=None, max_iterations=1)

    assert len(by_type(events, "confirm")) == 0     # 只读工具本就无 confirm
    assert len(by_type(events, "tool_call")) == 1   # 第二轮在发帧前被拦
    fe = by_type(events, "fatal_error")
    assert fe and "上限" in fe[0]["message"]


def test_mcp_bad_json_args_error_before_confirm(ws, mcp_tools):
    rounds = [
        [tc_chunk(0, id="m8", name="mcp_demo_append_log"),
         tc_chunk(0, arguments='{"text": broken json')],
        [text_chunk("明白了")],
    ]
    client, events = run_stream(rounds, ws, decide=None)

    assert_paired(events)
    assert by_type(events, "confirm") == []   # 参数非法在 confirm 门控前处理
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "不是合法 JSON" in tr["output"]


def test_mcp_false_convergence_round_has_no_tools_key(ws, mcp_tools):
    """收敛轮（max+1）请求物理不带 tools 键，MCP 工具同样不可再被请求。"""
    rounds = [
        [tc_chunk(0, id="m9", name="mcp_demo_now"), tc_chunk(0, arguments="{}")],
        [text_chunk("直接回答")],
    ]
    client, events = run_stream(rounds, ws, max_iterations=1)
    calls = client.chat.completions.create_calls
    assert "tools" in calls[0]
    assert "tools" not in calls[1]
    assert_paired(events)


# ---------------- /health 外显与子进程装配隔离 ----------------

def test_health_shows_mcp_disabled_by_default(monkeypatch):
    """MCP_ENABLED=false（默认）：/health 如实外显，未注册任何 mcp_* 工具。"""
    from fastapi.testclient import TestClient
    import main

    # 防本地 .env 临时开启 MCP（E2E 期）影响断言
    monkeypatch.setattr(main, "MCP_ENABLED", False)
    monkeypatch.setattr(main, "_mcp_manager", None)
    c = TestClient(main.app)
    r = c.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["mcp_enabled"] is False
    assert data["mcp_servers"] == []
    assert not any(s["function"]["name"].startswith("mcp_")
                   for s in tools.TOOL_SCHEMAS)


def test_mcp_disabled_zero_module_load_subprocess():
    """子进程隔离（照抄 RAG 模式）：MCP_ENABLED=false 时 import main
    零 mcp_client / mcp 模块加载（AC-6）。"""
    code = ("import sys; sys.path.insert(0, '.'); import main;"
            "print('mcp_client' in sys.modules, 'mcp' in sys.modules,"
            "any(s['function']['name'].startswith('mcp_') for s in __import__('tools').TOOL_SCHEMAS))")
    e = os.environ.copy()
    e["MCP_ENABLED"] = "false"
    r = subprocess.run([sys.executable, "-c", code], cwd=AI_ROOT, env=e,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "False False False" in r.stdout
