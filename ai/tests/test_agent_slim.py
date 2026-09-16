"""历史工具输出瘦身（token 治理）单测。

覆盖：预算内全保留、超预算尾部保留+早期占位、占位含工具名与原字符数、
非 tool 消息与 assistant.tool_calls 结构不动、权威消息不被修改、
单条超大输出自身占位、真实工具循环集成（read_file 大文件后旧结果占位）。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_agent_slim.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
from test_agent import run_agent, tc_chunk, ws  # noqa: E402,F401


def _tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assistant_with_calls(calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": args}}
        for cid, name, args in calls]}


# ---------------- 纯函数行为 ----------------

def test_slim_within_budget_returns_equivalent():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        _assistant_with_calls([("c1", "list_dir", '{"path":"."}')]),
        _tool_msg("c1", "a\nb\nc"),
    ]
    out = agent._slim_messages(msgs, budget=1000)
    assert out == msgs          # 等价
    assert out[3] is msgs[3]    # 未占位的消息引用共享（零拷贝）


def test_slim_keeps_tail_and_replaces_old(monkeypatch=None):
    big1 = "x" * 500
    big2 = "y" * 500
    small = "z" * 10
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant_with_calls([("c1", "read_file", '{"path":"a.txt"}')]),
        _tool_msg("c1", big1),
        _assistant_with_calls([("c2", "read_file", '{"path":"b.txt"}')]),
        _tool_msg("c2", big2),
        _assistant_with_calls([("c3", "list_dir", '{"path":"."}')]),
        _tool_msg("c3", small),
    ]
    out = agent._slim_messages(msgs, budget=600)
    # c3、c2 在尾部 600 字符预算内（10+500=510），c1 被占位
    assert out[5]["tool_calls"] == msgs[5]["tool_calls"]  # assistant 结构不动
    assert out[6]["content"] == small                     # 尾部保留全文
    assert out[4]["content"] == big2
    ph = out[2]
    assert ph["role"] == "tool" and ph["tool_call_id"] == "c1"
    assert "已省略" in ph["content"] and "read_file" in ph["content"]
    assert f"原 {len(big1)} 字符" in ph["content"]
    assert "重新调用" in ph["content"]
    # 权威消息不被修改
    assert msgs[2]["content"] == big1
    # 请求副本里的 tool_call_id 保留（OpenAI 配对协议要求）
    assert ph["tool_call_id"] == "c1"


def test_slim_single_oversized_message_replaced():
    huge = "q" * 2000
    msgs = [
        _assistant_with_calls([("c1", "read_file", '{"path":"huge.txt"}')]),
        _tool_msg("c1", huge),
        {"role": "user", "content": "继续"},
    ]
    out = agent._slim_messages(msgs, budget=1000)
    assert "已省略" in out[1]["content"]
    assert out[2]["content"] == "继续"  # 非 tool 消息不动


def test_slim_no_tool_messages_noop():
    msgs = [{"role": "user", "content": "hi"}]
    assert agent._slim_messages(msgs, budget=10) == msgs


def test_slim_placeholder_without_meta_falls_back():
    # tool 消息找不到对应 assistant.tool_calls（异常历史）也不崩
    msgs = [_tool_msg("ghost", "w" * 500)]
    out = agent._slim_messages(msgs, budget=100)
    assert "未知工具" in out[0]["content"]


# ---------------- 工具循环集成 ----------------

def test_agent_loop_sends_slimmed_history(ws, monkeypatch):
    (ws.root / "big.txt").write_text("line " * 12000, encoding="utf-8")  # 60KB
    monkeypatch.setattr(agent, "SLIM_BUDGET_CHARS", 1000)

    rounds = [
        [tc_chunk(0, id="t1", name="read_file",
                  arguments='{"path":"big.txt"}')],
        [tc_chunk(0, id="t2", name="list_dir", arguments='{"path":"."}')],
        [],  # 收敛轮
    ]
    client, _events = run_agent(rounds, ws)

    third = client.chat.completions.create_calls[2]["messages"]
    tools_msgs = [m for m in third if m["role"] == "tool"]
    assert len(tools_msgs) == 2  # 配对完整：占位仍是合法 tool 消息
    by_id = {m["tool_call_id"]: m for m in tools_msgs}
    # 旧的 read_file 大输出（60KB > 1KB 预算）→ 占位；新的 list_dir → 全文保留
    assert "已省略" in by_id["t1"]["content"] and "read_file" in by_id["t1"]["content"]
    assert f"原 60027 字符" in by_id["t1"]["content"]
    assert "已省略" not in by_id["t2"]["content"]
    # 第二轮请求中 t1 同样因单条超预算而占位（read_file 输出 60027 > 预算 1000）
    second = client.chat.completions.create_calls[1]["messages"]
    second_t1 = [m for m in second if m.get("tool_call_id") == "t1"][0]
    assert "已省略" in second_t1["content"]
