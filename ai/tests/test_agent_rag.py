"""
阶段2 Task4：LangGraph 工具循环集成 search_code 与两级回退测试。

TR-4.1 Fake 驱动用例：单/两轮 search_code、与 read_file 并行、超限收敛与失败、
       tool_call/tool_result 按 id 配对、轨迹事件序列；
TR-4.2 两级回退：RAG_ENABLED=false 装配态下模型请求 search_code 得到
       "未知工具" error（循环不断）；TOOLS_ENABLED=false 走直连零工具帧。

零真实网络、零真实模型：Fake 流式客户端（复用 test_agent.py 的桩）+
FakeProvider 确定性向量；索引桩直接 monkeypatch indexer.get_workspace_index，
不触全局注册表、不写真实 ai/index_store/。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_agent_rag.py -q
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import indexer  # noqa: E402
import tools  # noqa: E402
from agent import iter_agent_events  # noqa: E402
from embedding import EmbeddingProvider  # noqa: E402
from indexer import Chunk, Hit, WorkspaceIndex  # noqa: E402
from test_agent import (FakeClient, by_type, final_messages, tc_chunk,  # noqa: E402
                        text_chunk)
from workspace import Workspace  # noqa: E402


# ---------------- Fake embedding（确定性 2 维标记向量） ----------------

class FakeProvider(EmbeddingProvider):
    model_name = "fake-embed-agent"

    @property
    def dim(self) -> int:
        return 2

    @staticmethod
    def _vec(text: str) -> list[float]:
        v = [1.0 if "alpha" in text else 0.0,
             1.0 if "beta" in text else 0.0]
        v[0] += 0.05  # 基线分量，避免全零向量
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class ScriptedIndex:
    """ensure_ready 按脚本返回的索引桩（building → ready），验证"稍后重试"流。"""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.provider = FakeProvider()
        self.last_error = None
        self.snapshot = None  # search_code 读 capped 标记用
        self.ensure_calls = 0

    def ensure_ready(self, wait_seconds=None):
        self.ensure_calls += 1
        return self._statuses.pop(0) if self._statuses else "ready"

    def search(self, query_vec, top_k=None, path_prefix=""):
        text = "【文件: src/auth.py · L1-5】\n" + "alpha login check\n" * 5
        return [Hit(Chunk("src/auth.py", 1, 5, text), 0.99)]


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text("alpha login check\n" * 5,
                                          encoding="utf-8")
    return Workspace(root)


@pytest.fixture
def rag_env(ws, tmp_path, monkeypatch):
    """真实 WorkspaceIndex + FakeProvider + 隔离 index_root。"""
    idx = WorkspaceIndex(ws, provider=FakeProvider(),
                         index_root=tmp_path / "index_store")
    monkeypatch.setattr(indexer, "get_workspace_index", lambda w: idx)
    return ws, idx


def run_agent(rounds, ws, *, max_iterations=8, tools_enabled=True):
    client = FakeClient(rounds)
    events = list(iter_agent_events(
        client,
        [{"role": "user", "content": "登录校验逻辑在哪"}],
        base_kwargs={"model": "fake-model"},
        workspace=ws,
        max_iterations=max_iterations,
        tools_enabled=tools_enabled,
    ))
    return client, events


def schema_names(kwargs):
    return [s["function"]["name"] for s in kwargs["tools"]]


# ---------------- TR-4.1 循环集成 ----------------

def test_search_code_single_round_then_answer(rag_env):
    ws, idx = rag_env
    idx.refresh_once()  # 预建索引，首轮即 ready
    rounds = [
        [tc_chunk(0, id="s1", name="search_code",
                  arguments='{"query":"alpha"}')],
        [text_chunk("在 src/auth.py")],
    ]
    client, events = run_agent(rounds, ws)

    # 请求声明里含 search_code schema（自动进循环的走查证据）
    assert "search_code" in schema_names(client.chat.completions.create_calls[0])

    call = by_type(events, "tool_call")
    assert len(call) == 1
    assert call[0] == {"type": "tool_call", "id": "s1",
                       "name": "search_code", "args": {"query": "alpha"}}
    result = by_type(events, "tool_result")
    assert len(result) == 1
    assert result[0]["id"] == "s1" and result[0]["name"] == "search_code"
    assert result[0]["status"] == "success"
    assert "src/auth.py L1-5" in result[0]["output"]
    assert not result[0]["truncated"]
    assert by_type(events, "token")[-1]["delta"] == "在 src/auth.py"

    msgs = final_messages(events)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert "src/auth.py" in msgs[2]["content"]


def test_search_code_two_rounds_building_then_ready(ws, monkeypatch):
    idx = ScriptedIndex(["building", "ready"])
    monkeypatch.setattr(indexer, "get_workspace_index", lambda w: idx)
    rounds = [
        [tc_chunk(0, id="b1", name="search_code",
                  arguments='{"query":"alpha"}')],
        [tc_chunk(0, id="b2", name="search_code",
                  arguments='{"query":"alpha"}')],
        [text_chunk("找到了")],
    ]
    _, events = run_agent(rounds, ws)

    results = by_type(events, "tool_result")
    assert [r["id"] for r in results] == ["b1", "b2"]
    assert all(r["status"] == "success" for r in results)
    assert "索引正在后台构建" in results[0]["output"]
    assert "src/auth.py L1-5" in results[1]["output"]
    assert idx.ensure_calls == 2  # building 提示后模型重试
    assert by_type(events, "token")[-1]["delta"] == "找到了"


def test_search_code_parallel_with_read_file(rag_env):
    ws, idx = rag_env
    idx.refresh_once()
    rounds = [
        [tc_chunk(0, id="p1", name="search_code",
                  arguments='{"query":"alpha"}'),
         tc_chunk(1, id="p2", name="read_file",
                  arguments='{"path":"src/auth.py"}')],
        [text_chunk("都完成了")],
    ]
    _, events = run_agent(rounds, ws)

    calls = by_type(events, "tool_call")
    results = by_type(events, "tool_result")
    assert [c["id"] for c in calls] == ["p1", "p2"]
    assert [r["id"] for r in results] == ["p1", "p2"]
    assert all(r["status"] == "success" for r in results)
    assert "src/auth.py L1-5" in results[0]["output"]
    assert "alpha login check" in results[1]["output"]


def test_search_code_counts_toward_limit_and_converges(rag_env):
    ws, idx = rag_env
    idx.refresh_once()
    rounds = [
        [tc_chunk(0, id="t1", name="search_code",
                  arguments='{"query":"alpha"}')],
        [text_chunk("收敛回答")],
    ]
    client, events = run_agent(rounds, ws, max_iterations=1)

    assert not by_type(events, "fatal_error")
    assert len(client.chat.completions.create_calls) == 2
    # 收敛提示紧跟在 search_code 结果之后
    second_msgs = client.chat.completions.create_calls[1]["messages"]
    assert second_msgs[-2]["role"] == "tool"
    assert second_msgs[-1]["content"].startswith("【系统提示】")
    assert by_type(events, "token")[-1]["delta"] == "收敛回答"


def test_search_code_limit_exceeded_no_orphan_call(rag_env):
    ws, idx = rag_env
    idx.refresh_once()
    rounds = [
        [tc_chunk(0, id="t1", name="search_code",
                  arguments='{"query":"alpha"}')],
        [tc_chunk(0, id="t2", name="search_code",
                  arguments='{"query":"alpha"}')],
    ]
    _, events = run_agent(rounds, ws, max_iterations=1)

    fatal = by_type(events, "fatal_error")
    assert len(fatal) == 1 and "上限" in fatal[0]["message"]
    # 第二轮 tool_call 在发帧前失败：无孤儿帧
    assert [c["id"] for c in by_type(events, "tool_call")] == ["t1"]
    assert [r["id"] for r in by_type(events, "tool_result")] == ["t1"]
    assert not by_type(events, "_final_state")


def test_search_code_invalid_json_args_flow(rag_env):
    ws, idx = rag_env
    rounds = [
        [tc_chunk(0, id="j1", name="search_code", arguments="no-json{")],
        [text_chunk("重试后作答")],
    ]
    _, events = run_agent(rounds, ws)

    call = by_type(events, "tool_call")[0]
    assert call["args"] is None and call["raw_args"] == "no-json{"
    result = by_type(events, "tool_result")[0]
    assert result["status"] == "error" and "合法 JSON" in result["output"]
    assert not by_type(events, "fatal_error")


# ---------------- TR-4.2 两级回退 ----------------

def test_rag_disabled_search_code_becomes_unknown_tool(ws, monkeypatch):
    """一级回退：RAG_ENABLED=false 的装配态（schema 与 dispatch 均无 search_code）
    下模型仍请求它 → 工具层"未知工具"error，循环不断，模型可改用 grep。"""
    base = [s for s in tools.TOOL_SCHEMAS
            if s["function"]["name"] != "search_code"]
    monkeypatch.setattr(agent, "TOOL_SCHEMAS", base)
    monkeypatch.delitem(tools._DISPATCH, "search_code")

    rounds = [
        [tc_chunk(0, id="r1", name="search_code",
                  arguments='{"query":"alpha"}')],
        [text_chunk("改用 grep")],
    ]
    client, events = run_agent(rounds, ws)

    # 请求声明里确实没有 search_code
    assert "search_code" not in schema_names(
        client.chat.completions.create_calls[0])
    result = by_type(events, "tool_result")[0]
    assert result["status"] == "error"
    assert "未知工具" in result["output"] and "search_code" in result["output"]
    assert not by_type(events, "fatal_error")
    assert by_type(events, "token")[-1]["delta"] == "改用 grep"


def test_tools_disabled_plain_stream_zero_tool_frames(ws):
    """二级回退：TOOLS_ENABLED=false 直连路径，请求不带 tools 键、零工具帧。"""
    rounds = [[text_chunk("直连回答")]]
    client, events = run_agent(rounds, ws, tools_enabled=False)

    assert not by_type(events, "tool_call")
    assert not by_type(events, "tool_result")
    assert "tools" not in client.chat.completions.create_calls[0]
    assert by_type(events, "token")[-1]["delta"] == "直连回答"
    assert final_messages(events)[-1]["content"] == "直连回答"
