"""
阶段1 Task2：LangGraph 工具调用循环测试。

不发真实网络请求：用 Fake 流式客户端按“轮”（每次 chat.completions.create
对应一轮）返回预置的 chunk 列表，覆盖：
  直答 / 单工具分片累积 / 并行多工具 / 工具 error 恢复 / 非法 arguments /
  循环上限的收敛与失败两条路径 / TOOLS_ENABLED=false / reasoning / 上游异常。
"""
from __future__ import annotations

import types

import pytest

from agent import iter_agent_events
from workspace import Workspace


# ---------- Fake OpenAI 流式客户端 ----------

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, reasoning=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.model_extra = {"reasoning_content": reasoning} if reasoning else {}


class _Choice:
    def __init__(self, delta):
        self.delta = delta
        self.index = 0


class _Chunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]


def text_chunk(text):
    return _Chunk(_Delta(content=text))


def reasoning_chunk(text):
    return _Chunk(_Delta(reasoning=text))


def tc_chunk(index, id=None, name=None, arguments=None):
    """arguments=None 模拟首片仅带 id/name（SDK 中 arguments 缺省为 None）。"""
    return _Chunk(_Delta(tool_calls=[_ToolCall(index, id, name, arguments)]))


class FakeCompletions:
    def __init__(self, rounds, raise_exc=None):
        self.rounds = rounds
        self.raise_exc = raise_exc
        self.create_calls: list[dict] = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        idx = len(self.create_calls) - 1
        chunks = self.rounds[min(idx, len(self.rounds) - 1)]
        return iter(list(chunks))


class FakeClient:
    def __init__(self, rounds, raise_exc=None):
        self.chat = types.SimpleNamespace(
            completions=FakeCompletions(rounds, raise_exc))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(str(root))


def run_agent(rounds, ws, *, max_iterations=8, tools_enabled=True,
              raise_exc=None, messages=None):
    client = FakeClient(rounds, raise_exc)
    events = list(iter_agent_events(
        client,
        messages if messages is not None else [{"role": "user", "content": "hi"}],
        base_kwargs={"model": "fake-model"},
        workspace=ws,
        max_iterations=max_iterations,
        tools_enabled=tools_enabled,
    ))
    return client, events


def by_type(events, t):
    return [e for e in events if e["type"] == t]


def final_messages(events):
    finals = by_type(events, "_final_state")
    assert finals, "缺少 _final_state 事件"
    return finals[-1]["messages"]


# ---------- 用例 ----------

def test_plain_answer_no_tools(ws):
    client, events = run_agent(
        [[text_chunk("你"), text_chunk("好")]], ws)
    assert [e["type"] for e in events] == ["token", "token", "_final_state"]
    assert final_messages(events)[-1] == {"role": "assistant", "content": "你好"}
    # 工具开启时请求必带 tools 声明
    assert client.chat.completions.create_calls[0]["tools"]
    assert client.chat.completions.create_calls[0]["stream"] is True


def test_reasoning_delta_passthrough(ws):
    _, events = run_agent(
        [[reasoning_chunk("思考中"), text_chunk("答案")]], ws)
    assert by_type(events, "reasoning")[0]["delta"] == "思考中"
    assert by_type(events, "token")[0]["delta"] == "答案"


def test_reasoning_interleaved_with_tool_call_shards(ws):
    """同一轮 reasoning 分片与 tool_call 分片交错：推理分片实时透传不丢失，
    tool_call 在流末整体成帧，随后的工具执行与终答不受影响（问题 10.6）。"""
    (ws.root / "a.txt").write_text("INTERLEAVE-OK", encoding="utf-8")
    rounds = [
        [reasoning_chunk("先想想"),
         tc_chunk(0, id="i1", name="read_file"),
         reasoning_chunk("再决定"),
         tc_chunk(0, arguments='{"path":"a.txt"}')],
        [text_chunk("结论")],
    ]
    _, events = run_agent(rounds, ws)

    assert [e["delta"] for e in by_type(events, "reasoning")] == ["先想想", "再决定"]
    call = by_type(events, "tool_call")
    assert len(call) == 1 and call[0]["id"] == "i1"
    assert call[0]["args"] == {"path": "a.txt"}
    result = by_type(events, "tool_result")[0]
    assert result["status"] == "success" and "INTERLEAVE-OK" in result["output"]
    assert by_type(events, "token")[-1]["delta"] == "结论"


def test_single_tool_sharded_then_answer(ws):
    (ws.root / "a.txt").write_text("hello file", encoding="utf-8")
    rounds = [
        [tc_chunk(0, id="c1", name="read_file"),
         tc_chunk(0, arguments='{"path":'),
         tc_chunk(0, arguments='"a.txt"}')],
        [text_chunk("读完了")],
    ]
    client, events = run_agent(rounds, ws)

    calls = by_type(events, "tool_call")
    results = by_type(events, "tool_result")
    assert len(calls) == 1
    assert calls[0]["id"] == "c1"
    assert calls[0]["name"] == "read_file"
    assert calls[0]["args"] == {"path": "a.txt"}
    assert "raw_args" not in calls[0]
    assert len(results) == 1
    assert results[0]["id"] == "c1"
    assert results[0]["status"] == "success"
    assert "hello file" in results[0]["output"]

    msgs = final_messages(events)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == '{"path":"a.txt"}'
    assert "_parsed_args" not in msgs[1]  # 私有旁路不得污染发往模型的消息
    assert msgs[2] == {"role": "tool", "tool_call_id": "c1",
                       "content": msgs[2]["content"]}
    # 第二轮请求带回了 tool 结果消息
    assert client.chat.completions.create_calls[1]["messages"][-1]["role"] == "tool"


def test_parallel_tool_calls_pair_by_id(ws):
    (ws.root / "a.txt").write_text("X", encoding="utf-8")
    rounds = [
        [tc_chunk(0, id="p1", name="read_file"),
         tc_chunk(1, id="p2", name="list_dir"),
         tc_chunk(0, arguments='{"path":"a.txt"}'),
         tc_chunk(1, arguments='{"path":"."}')],
        [text_chunk("都读完了")],
    ]
    _, events = run_agent(rounds, ws)

    calls = by_type(events, "tool_call")
    results = by_type(events, "tool_result")
    assert [c["id"] for c in calls] == ["p1", "p2"]  # 按 index 排序
    assert [r["id"] for r in results] == ["p1", "p2"]
    assert all(r["status"] == "success" for r in results)

    msgs = final_messages(events)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["p1", "p2"]


def test_tool_error_then_model_recovers(ws):
    # read_file 不存在的文件 -> 工具 error，模型下一轮正常作答（循环不中断）
    rounds = [
        [tc_chunk(0, id="e1", name="read_file",
                  arguments='{"path":"missing.txt"}')],
        [text_chunk("文件不存在")],
    ]
    _, events = run_agent(rounds, ws)

    result = by_type(events, "tool_result")[0]
    assert result["status"] == "error"
    assert not by_type(events, "fatal_error")
    assert by_type(events, "token")[-1]["delta"] == "文件不存在"
    # 错误内容回填给模型
    msgs = final_messages(events)
    assert "missing.txt" in msgs[2]["content"]


def test_invalid_arguments_emits_null_args_and_error(ws):
    rounds = [
        [tc_chunk(0, id="b1", name="read_file", arguments="not-json{")],
        [text_chunk("我重新调用")],
    ]
    _, events = run_agent(rounds, ws)

    call = by_type(events, "tool_call")[0]
    assert call["args"] is None
    assert call["raw_args"] == "not-json{"
    result = by_type(events, "tool_result")[0]
    assert result["status"] == "error"
    assert "合法 JSON" in result["output"]
    # 原始非法串仍原样回填，便于模型自我纠正
    msgs = final_messages(events)
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == "not-json{"


def test_iteration_limit_gives_final_chance_and_converges(ws):
    (ws.root / "a.txt").write_text("Y", encoding="utf-8")
    rounds = [
        [tc_chunk(0, id="t1", name="read_file", arguments='{"path":"a.txt"}')],
        [tc_chunk(0, id="t2", name="list_dir", arguments='{"path":"."}')],
        [text_chunk("最终回答")],
    ]
    client, events = run_agent(rounds, ws, max_iterations=2)

    assert not by_type(events, "fatal_error")
    assert [c["id"] for c in by_type(events, "tool_call")] == ["t1", "t2"]
    assert len(client.chat.completions.create_calls) == 3
    msgs = final_messages(events)
    hints = [m for m in msgs if m["role"] == "user"
             and "工具调用次数已达上限" in m["content"]]
    assert len(hints) == 1
    # 第三轮请求里提示消息紧贴在最前（tool 结果之后）
    third_msgs = client.chat.completions.create_calls[2]["messages"]
    assert third_msgs[-1]["content"].startswith("【系统提示】")
    # 收敛轮物理上不传 tools：模型只能文字回答，永不硬中断
    assert "tools" not in client.chat.completions.create_calls[2]


def test_iteration_limit_still_calling_fails_without_orphan_call(ws):
    rounds = [
        [tc_chunk(0, id="t1", name="list_dir", arguments='{"path":"."}')],
        [tc_chunk(0, id="t2", name="list_dir", arguments='{"path":"."}')],
    ]
    client, events = run_agent(rounds, ws, max_iterations=1)

    fatal = by_type(events, "fatal_error")
    assert len(fatal) == 1 and "上限" in fatal[0]["message"]
    # 第二轮的 tool_call 帧绝不允许先发（否则无配对 tool_result）
    assert [c["id"] for c in by_type(events, "tool_call")] == ["t1"]
    assert [r["id"] for r in by_type(events, "tool_result")] == ["t1"]
    assert len(client.chat.completions.create_calls) == 2
    # 收敛轮请求物理上不带 tools（兜底仅防御异常 API 行为）
    assert "tools" not in client.chat.completions.create_calls[1]
    # 失败后没有 _final_state
    assert not by_type(events, "_final_state")


def test_tools_disabled_is_plain_path(ws):
    client, events = run_agent(
        [[text_chunk("直连回答")]], ws, tools_enabled=False)
    assert not by_type(events, "tool_call")
    assert not by_type(events, "tool_result")
    kwargs = client.chat.completions.create_calls[0]
    assert "tools" not in kwargs
    assert kwargs["stream"] is True
    assert final_messages(events)[-1]["content"] == "直连回答"


def test_upstream_exception_in_graph_becomes_fatal_error(ws):
    client, events = run_agent(
        [], ws, raise_exc=RuntimeError("upstream boom"))
    fatal = by_type(events, "fatal_error")
    assert len(fatal) == 1
    assert "upstream boom" in fatal[0]["message"]
    assert not by_type(events, "_final_state")


def test_upstream_exception_in_plain_path_becomes_fatal_error(ws):
    _, events = run_agent(
        [], ws, tools_enabled=False, raise_exc=RuntimeError("plain boom"))
    fatal = by_type(events, "fatal_error")
    assert len(fatal) == 1 and "plain boom" in fatal[0]["message"]


def test_every_tool_call_has_matching_result(ws):
    """契约回归：任意场景下 tool_call 与 tool_result 必须按 id 一一配对。"""
    (ws.root / "a.txt").write_text("Z", encoding="utf-8")
    rounds = [
        [tc_chunk(0, id="m1", name="read_file", arguments='{"path":"a.txt"}'),
         tc_chunk(1, id="m2", name="read_file", arguments="bad"),
         tc_chunk(2, id="m3", name="glob", arguments='{"pattern":"*.x"}')],
        [text_chunk("ok")],
    ]
    _, events = run_agent(rounds, ws)
    call_ids = [c["id"] for c in by_type(events, "tool_call")]
    result_ids = [r["id"] for r in by_type(events, "tool_result")]
    assert call_ids == result_ids == ["m1", "m2", "m3"]
