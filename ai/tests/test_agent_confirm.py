"""
阶段3 Task3：confirm 人机确认机制测试。

不发真实网络请求：Fake 客户端按轮返回预置 chunk；confirm 等待发生在
agent 工具节点（后台线程驱动事件流），主线程模拟 POST /ai/confirm 决策。

覆盖：确认继续流 / 拒绝流 / 超时自动拒绝 / 断连批量拒绝 / 配对契约（无孤儿帧）/
写工具计入 MAX_TOOL_ITERATIONS / 只读工具零 confirm / SANDBOX_ENABLED=false 降级
无 confirm / POST /ai/confirm API（404/422/决策/二次 404）。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_agent_confirm.py -q
"""
from __future__ import annotations

import threading
import time

import pytest

import agent
import tools
from workspace import Workspace

from test_agent import FakeClient, by_type, tc_chunk, text_chunk
from agent import iter_agent_events


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(str(root))


WRITE_ROUNDS = [
    [tc_chunk(0, id="w1", name="write_file"),
     tc_chunk(0, arguments='{"path": "out.txt", "content": "hello confirm"}')],
    [text_chunk("文件写好了")],
]


def run_stream_with_confirm(rounds, ws, *, decide=None, max_iterations=8,
                            stream_id="s1"):
    """后台线程驱动 agent 事件流；主线程每遇到新 confirm 帧即调 decide() 决策。

    decide: Callable[[confirm事件dict], None] | None（None 表示不决策，
    用于超时/无 confirm 场景）。返回 (client, events)。
    """
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
    assert finished.is_set(), "agent 流未在限定时间内结束（confirm 可能挂死）"
    return client, events


def assert_paired(events):
    """配对契约：每个 tool_call 必有同 id 的 tool_result（顺序一致、无孤儿）。"""
    call_ids = [e["id"] for e in by_type(events, "tool_call")]
    result_ids = [e["id"] for e in by_type(events, "tool_result")]
    assert call_ids == result_ids


# ---------------- 确认 / 拒绝 / 超时 ----------------

def test_confirm_approved_executes_and_paired(ws):
    def approve(evt):
        assert evt["tool"] == "write_file"
        assert "新建" in evt["summary"] and "out.txt" in evt["summary"]
        assert agent.resolve_confirm(evt["id"], True) is not None

    client, events = run_stream_with_confirm(WRITE_ROUNDS, ws, decide=approve)

    assert_paired(events)
    assert len(by_type(events, "confirm")) == 1
    tr = by_type(events, "tool_result")[0]
    assert tr["id"] == "w1" and tr["status"] == "success"
    assert "已写入" in tr["output"]
    assert (ws.root / "out.txt").read_text(encoding="utf-8") == "hello confirm"
    # 帧顺序：tool_call → confirm → tool_result
    types = [e["type"] for e in events]
    assert types.index("tool_call") < types.index("confirm") < types.index("tool_result")


def test_confirm_rejected_zero_side_effect(ws):
    def reject(evt):
        assert agent.resolve_confirm(evt["id"], False) is not None

    client, events = run_stream_with_confirm(WRITE_ROUNDS, ws, decide=reject)

    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "拒绝" in tr["output"]
    assert not (ws.root / "out.txt").exists()          # 磁盘零写入
    assert by_type(events, "token")                    # 模型继续收敛作答
    assert by_type(events, "_final_state")             # 流正常 done，非 fatal


def test_confirm_timeout_auto_rejects(ws, monkeypatch):
    monkeypatch.setattr(agent, "CONFIRM_TIMEOUT_SECONDS", 0.3)
    client, events = run_stream_with_confirm(WRITE_ROUNDS, ws, decide=None)

    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "超时" in tr["output"]
    assert not (ws.root / "out.txt").exists()
    # 超时清表：迟到的 POST 决策 → 404 语义
    confirm_evt = by_type(events, "confirm")[0]
    assert agent.resolve_confirm(confirm_evt["id"], True) is None


def test_confirm_run_command_executes(ws):
    rounds = [
        [tc_chunk(0, id="c1", name="run_command"),
         tc_chunk(0, arguments='{"command": "echo hello-confirm"}')],
        [text_chunk("done")],
    ]

    def approve(evt):
        assert evt["summary"] == "在工作区根执行命令：echo hello-confirm"
        assert agent.resolve_confirm(evt["id"], True) is not None

    client, events = run_stream_with_confirm(rounds, ws, decide=approve)

    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "success" and "hello-confirm" in tr["output"]


# ---------------- 断连自动拒绝 ----------------

def test_disconnect_rejects_pending_by_stream(ws):
    entry = agent.create_confirm("write_file", "summary", stream_id="sd")
    assert agent.reject_pending_confirms("sd") == 1
    assert entry.approved is False and entry.event.is_set()
    # 已清表：重复拒绝 0 条、迟到决策 404 语义
    assert agent.reject_pending_confirms("sd") == 0
    assert agent.resolve_confirm(entry.id, True) is None
    # 其他流的未决确认不受影响
    other = agent.create_confirm("run_command", "cmd", stream_id="other")
    assert agent.reject_pending_confirms("sd") == 0
    assert other.approved is None
    agent.reject_pending_confirms("other")  # 清理


def test_disconnect_during_stream_auto_rejects(ws):
    def disconnect(evt):
        assert agent.reject_pending_confirms("sd-stream") == 1

    client, events = run_stream_with_confirm(WRITE_ROUNDS, ws,
                                             decide=disconnect,
                                             stream_id="sd-stream")
    assert_paired(events)
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "拒绝" in tr["output"]
    assert not (ws.root / "out.txt").exists()
    assert by_type(events, "_final_state")  # 断连不杀流


# ---------------- 迭代上限与门控 ----------------

def test_write_tool_counts_toward_max_iterations(ws):
    rounds = [
        WRITE_ROUNDS[0],
        # 收敛提示轮（max=1 已用掉）仍请求写工具 → AgentLimitError，不再有 confirm
        [tc_chunk(0, id="w2", name="write_file"),
         tc_chunk(0, arguments='{"path": "b.txt", "content": "y"}')],
    ]

    def approve(evt):
        agent.resolve_confirm(evt["id"], True)

    client, events = run_stream_with_confirm(rounds, ws, decide=approve,
                                             max_iterations=1)
    # 第一轮写请求正常确认执行；第二轮在上限检查处终止（无 tool_call/confirm 帧）
    assert len(by_type(events, "confirm")) == 1
    assert (ws.root / "out.txt").read_text(encoding="utf-8") == "hello confirm"
    fe = by_type(events, "fatal_error")
    assert fe and "上限" in fe[0]["message"]


def test_readonly_tools_no_confirm(ws):
    (ws.root / "a.txt").write_text("内容", encoding="utf-8")
    rounds = [
        [tc_chunk(0, id="r1", name="read_file"),
         tc_chunk(0, arguments='{"path": "a.txt"}')],
        [text_chunk("读到了")],
    ]
    client, events = run_stream_with_confirm(rounds, ws, decide=None)
    assert by_type(events, "confirm") == []       # 只读工具零 confirm
    assert_paired(events)
    assert by_type(events, "tool_result")[0]["status"] == "success"


def test_sandbox_disabled_no_confirm_degradation(ws, monkeypatch):
    """SANDBOX_ENABLED=false 语义：dispatch 无写工具 → 无 confirm 帧，未知工具降级。"""
    monkeypatch.delitem(tools._DISPATCH, "write_file")
    client, events = run_stream_with_confirm(WRITE_ROUNDS, ws, decide=None)
    assert by_type(events, "confirm") == []
    tr = by_type(events, "tool_result")[0]
    assert tr["status"] == "error" and "未知工具" in tr["output"]
    assert not (ws.root / "out.txt").exists()


# ---------------- confirm 摘要 ----------------

def test_confirm_summary_preview_and_overwrite(ws):
    (ws.root / "exists.txt").write_text("old", encoding="utf-8")
    content = "\n".join(f"line{i}" for i in range(1, 16))

    s = agent._confirm_summary(
        "write_file", {"path": "exists.txt", "content": content}, ws)
    assert s.startswith("覆盖文件 exists.txt")
    assert "line1" in s and "line10" in s and "line11" not in s
    assert "共 15 行" in s

    s2 = agent._confirm_summary(
        "write_file", {"path": "new.txt", "content": "hi"}, ws)
    assert s2.startswith("新建文件 new.txt")

    s3 = agent._confirm_summary(
        "write_file", {"path": "e.txt", "content": ""}, ws)
    assert "空内容" in s3 and "共 0 行" in s3

    s4 = agent._confirm_summary(
        "write_file", {"path": "../esc.txt", "content": "x"}, ws)
    assert "越出工作区" in s4


# ---------------- POST /ai/confirm API ----------------

def test_confirm_api_decision_and_404():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)

    # 未知 id → 404
    r = c.post("/ai/confirm", json={"confirm_id": "no-such-id", "approved": True})
    assert r.status_code == 404

    # approved 非布尔 → pydantic 422
    entry = agent.create_confirm("write_file", "s", stream_id=None)
    r = c.post("/ai/confirm", json={"confirm_id": entry.id, "approved": "yes"})
    assert r.status_code == 422
    agent.reject_pending_confirms(None)  # 清理

    # 正常决策 → 200，条目唤醒
    entry2 = agent.create_confirm("run_command", "cmd", stream_id=None)
    r = c.post("/ai/confirm", json={"confirm_id": entry2.id, "approved": True})
    assert r.status_code == 200
    assert r.json()["success"] is True and r.json()["tool"] == "run_command"
    assert entry2.approved is True and entry2.event.is_set()

    # 二次决策同一 id → 404（已失效）
    r = c.post("/ai/confirm", json={"confirm_id": entry2.id, "approved": False})
    assert r.status_code == 404
