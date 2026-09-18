"""
阶段4B：Multi-Agent 子任务委派测试（Fake 非流式客户端，零网络零子进程）。

覆盖：两轮委派 happy path（摘要格式/计数/系统提示/工具消息回填）/
工具集过滤（不含写工具与 delegate_task，RAG/MCP 组合）/ allowed 越权硬拦截
（零磁盘写入）/ 轮次上限（收敛轮物理不带 tools → 未完全收敛部分结果）/
收敛轮文本作答 / 模型调用异常收敛 error / 空任务 / 摘要截断 /
register_delegate_tool 幂等与 None 兜底 / 未注册"未知工具"降级 /
_slim_messages 子循环生效 / SUB_SYSTEM_PROMPT 结构化契约 /
/health 外显与子进程装配隔离（MULTI_AGENT_ENABLED=false 零加载）。

运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_sub_agent.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import sub_agent  # noqa: E402
import tools  # noqa: E402
from workspace import Workspace  # noqa: E402

from test_agent_mcp import FakeManager  # noqa: E402

AI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_KW = {"model": "fake-model", "temperature": 0.3}


# ---------------- Fake 非流式客户端 ----------------

class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.model_extra = None  # reasoning_content 路径：忽略不透出


class _TC:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _Resp:
    def __init__(self, msg):
        self.choices = [SimpleNamespace(message=msg)]


def text_resp(text):
    return _Resp(_Msg(content=text))


def tool_resp(*calls):
    """calls: [(call_id, tool_name, raw_arguments_json)]"""
    return _Resp(_Msg(tool_calls=[_TC(*c) for c in calls]))


class FakeNonStreamClient:
    """按调用次序返回预设响应；记录每次 create kwargs（断言 tools 键/消息）。"""

    def __init__(self, rounds, fail_at=None):
        self._rounds = list(rounds)
        self.create_calls: list[dict] = []
        self._i = 0
        self._fail_at = fail_at
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.create_calls.append(kwargs)
        self._i += 1
        if self._fail_at is not None and self._i >= self._fail_at:
            raise RuntimeError("model down")
        return self._rounds.pop(0)


# ---------------- fixtures ----------------

@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello TODO world\n", encoding="utf-8")
    return Workspace(str(root))


@pytest.fixture
def registry():
    """注册前后就地快照恢复（防污染其他用例，照抄 4A 手法）。"""
    snap = (list(tools.TOOL_SCHEMAS), dict(tools._DISPATCH),
            dict(tools._TOOL_TIMEOUTS))
    yield
    tools.TOOL_SCHEMAS[:] = snap[0]
    tools._DISPATCH.clear(); tools._DISPATCH.update(snap[1])
    tools._TOOL_TIMEOUTS.clear(); tools._TOOL_TIMEOUTS.update(snap[2])


@pytest.fixture
def mcp_tools():
    snap_meta = dict(tools._MCP_TOOL_META)
    mgr = FakeManager()
    tools.register_mcp_tools(mgr)
    yield mgr
    tools._MCP_TOOL_META.clear(); tools._MCP_TOOL_META.update(snap_meta)


# ---------------- happy path ----------------

def test_happy_path_two_rounds(ws):
    rounds = [
        tool_resp(("c1", "read_file", '{"path": "a.txt"}')),
        text_resp("结论：文件含 TODO\n依据：read_file(a.txt)"),
    ]
    client = FakeNonStreamClient(rounds)
    out = sub_agent.run_sub_agent(client, ws, "检查 a.txt 是否含 TODO",
                                  base_kwargs=BASE_KW)

    assert out.name == "delegate_task" and out.status == "success"
    assert "子任务完成（2 轮" in out.output
    assert "read_file×1" in out.output
    assert "任务：检查 a.txt 是否含 TODO" in out.output
    assert "结论：" in out.output and "文件含 TODO" in out.output
    # 子 agent 消息窗口独立：首轮 system=SUB_SYSTEM_PROMPT，user=task
    first = client.create_calls[0]
    assert first["messages"][0] == {"role": "system",
                                    "content": sub_agent.SUB_SYSTEM_PROMPT}
    assert first["messages"][1]["role"] == "user"
    assert "检查 a.txt 是否含 TODO" in first["messages"][1]["content"]
    assert first["stream"] is False and "tools" in first
    # 第二轮携带 tool 消息（真实工具结果回填），不再重复发 system
    second_msgs = client.create_calls[1]["messages"]
    tool_msgs = [m for m in second_msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and "hello TODO world" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "c1"


def test_empty_task_error(ws):
    out = sub_agent.run_sub_agent(FakeNonStreamClient([]), ws, "   ",
                                  base_kwargs=BASE_KW)
    assert out.status == "error" and "不能为空" in out.output


def test_context_appended_to_user_message(ws):
    rounds = [text_resp("ok")]
    client = FakeNonStreamClient(rounds)
    sub_agent.run_sub_agent(client, ws, "任务X", base_kwargs=BASE_KW,
                            context="相关文件：b.txt")
    user_content = client.create_calls[0]["messages"][1]["content"]
    assert "任务X" in user_content and "相关文件：b.txt" in user_content


# ---------------- 工具集过滤与越权拦截 ----------------

def test_sub_toolset_excludes_write_and_delegate(registry):
    tools.register_delegate_tool(None)  # 注册 delegate 后仍必须被排除
    schemas, allowed = tools.sub_toolset()
    names = {s["function"]["name"] for s in schemas}
    assert names == allowed
    assert "read_file" in names and "list_dir" in names
    assert "write_file" not in names and "run_command" not in names
    assert "delegate_task" not in names          # 两层封顶的硬保证
    if tools.RAG_ENABLED:
        assert "search_code" in names


def test_sub_toolset_with_mcp_mixed_readonly(registry, mcp_tools):
    schemas, allowed = tools.sub_toolset()
    names = {s["function"]["name"] for s in schemas}
    assert "mcp_demo_now" in names               # 声明只读 → 进子集
    assert "mcp_demo_append_log" not in names    # 未声明只读 → 排除


def test_privilege_escalation_blocked_zero_disk(ws):
    """幻觉越权：子循环内请求 write_file → allowed 拦截 + 零磁盘写入。"""
    rounds = [
        tool_resp(("c1", "write_file",
                   '{"path": "evil.txt", "content": "pwned"}')),
        text_resp("好的"),
    ]
    client = FakeNonStreamClient(rounds)
    out = sub_agent.run_sub_agent(client, ws, "试试写文件", base_kwargs=BASE_KW)
    assert out.status == "success"               # 子任务本身收敛，不杀主流
    assert not (ws.root / "evil.txt").exists()   # 零磁盘写入（硬防线）
    # 工具消息回填的是拦截错误，模型可见
    tool_msgs = [m for m in client.create_calls[1]["messages"]
                 if m["role"] == "tool"]
    assert "不在子任务可用范围内" in tool_msgs[0]["content"]


def test_execute_tool_allowed_check_before_dispatch(ws):
    out = tools.execute_tool(ws, "read_file", {"path": "a.txt"},
                             allowed=frozenset({"list_dir"}))
    assert out.status == "error" and "不在子任务可用范围内" in out.output
    out2 = tools.execute_tool(ws, "read_file", {"path": "a.txt"})  # None 零影响
    assert out2.status == "success"


# ---------------- 轮次预算与收敛 ----------------

def test_round_limit_converge_round_no_tools_then_incomplete(ws):
    rounds = [
        tool_resp(("c1", "read_file", '{"path": "a.txt"}')),
        tool_resp(("c2", "read_file", '{"path": "a.txt"}')),  # 收敛轮仍要工具
    ]
    client = FakeNonStreamClient(rounds)
    out = sub_agent.run_sub_agent(client, ws, "一直调工具的任务",
                                  base_kwargs=BASE_KW, max_sub_iterations=1)
    assert out.status == "success"
    assert "未完全收敛" in out.output and "达 1 轮上限" in out.output
    # 收敛轮请求物理不带 tools 键
    assert "tools" in client.create_calls[0]
    assert "tools" not in client.create_calls[1]
    # 收敛提示已注入
    assert client.create_calls[1]["messages"][-1]["content"] == \
        sub_agent.SUB_LIMIT_HINT


def test_converge_round_text_answer_is_normal_success(ws):
    rounds = [
        tool_resp(("c1", "list_dir", "{}")),
        text_resp("收敛轮作答"),
    ]
    client = FakeNonStreamClient(rounds)
    out = sub_agent.run_sub_agent(client, ws, "任务", base_kwargs=BASE_KW,
                                  max_sub_iterations=1)
    assert out.status == "success"
    assert "子任务完成" in out.output and "收敛轮作答" in out.output


# ---------------- 失败收敛与截断 ----------------

def test_model_error_converges_to_error_tooloutput(ws):
    client = FakeNonStreamClient([], fail_at=1)
    out = sub_agent.run_sub_agent(client, ws, "任务", base_kwargs=BASE_KW)
    assert out.status == "error"
    assert "子任务执行失败" in out.output and "model down" in out.output


def test_output_truncated_at_sub_output_max_chars(ws, monkeypatch):
    monkeypatch.setattr(sub_agent, "SUB_OUTPUT_MAX_CHARS", 50)
    client = FakeNonStreamClient([text_resp("长" * 200)])
    out = sub_agent.run_sub_agent(client, ws, "任务", base_kwargs=BASE_KW)
    assert out.status == "success"
    assert "（结论超 50 字符截断）" in out.output


# ---------------- 注册健壮性 ----------------

def test_register_delegate_idempotent(registry):
    def _ok(ws, task, context=None):
        return tools.ToolOutput(name="delegate_task", status="success",
                                output="ok")
    tools.register_delegate_tool(_ok)
    n1 = sum(1 for s in tools.TOOL_SCHEMAS
             if s["function"]["name"] == "delegate_task")
    tools.register_delegate_tool(_ok)
    n2 = sum(1 for s in tools.TOOL_SCHEMAS
             if s["function"]["name"] == "delegate_task")
    assert n1 == 1 and n2 == 1
    assert tools.tool_available("delegate_task")
    assert tools.requires_confirm("delegate_task") is False  # 只读零 confirm
    out = tools.execute_tool(ws, "delegate_task", {"task": "x"})
    assert out.status == "success"
    # per-tool 超时覆盖已注册（SUB_TASK_TIMEOUT_SECONDS + 10 缓冲）
    assert tools._TOOL_TIMEOUTS["delegate_task"] == \
        tools.SUB_TASK_TIMEOUT_SECONDS + 10.0


def test_register_delegate_none_executor_stub(registry):
    tools.register_delegate_tool(None)
    out = tools.execute_tool(ws, "delegate_task", {"task": "x"})
    assert out.status == "error" and "委派执行器未装配" in out.output


def test_delegate_unregistered_unknown_tool_degradation(ws):
    """MULTI_AGENT_ENABLED=false 语义：未注册 → 未知工具 error，流不断。"""
    out = tools.execute_tool(ws, "delegate_task", {"task": "x"})
    assert out.status == "error" and "未知工具" in out.output


# ---------------- token 治理与防编造契约 ----------------

def test_slim_messages_applied_in_sub_loop(ws, monkeypatch):
    monkeypatch.setattr(agent, "SLIM_BUDGET_CHARS", 10)
    rounds = [
        tool_resp(("c1", "read_file", '{"path": "a.txt"}')),
        tool_resp(("c2", "read_file", '{"path": "a.txt"}')),
        text_resp("done"),
    ]
    client = FakeNonStreamClient(rounds)
    sub_agent.run_sub_agent(client, ws, "任务", base_kwargs=BASE_KW)
    # 第 3 轮请求：c1 结果（非最近轮）被占位化；c2 结果（轮级豁免）全文保留
    third = client.create_calls[2]["messages"]
    c1 = next(m for m in third if m.get("tool_call_id") == "c1")
    c2 = next(m for m in third if m.get("tool_call_id") == "c2")
    assert "历史工具输出已省略" in c1["content"]
    assert "hello TODO world" in c2["content"]


def test_sub_system_prompt_structured_contract():
    """AC-12：防编造结构化契约内容断言。"""
    p = sub_agent.SUB_SYSTEM_PROMPT
    assert "真实工具调用" in p          # 结果必须来自真实工具调用
    assert "禁止编造" in p
    assert "只读" in p                  # 只读定位
    assert "依据" in p                  # 结论+依据清单格式


# ---------------- /health 外显与子进程装配隔离 ----------------

def test_health_shows_multi_agent_disabled_by_default(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    # 阶段4C 起 /health 读 _features_state：直接置字段并还原（setitem 自动恢复）
    monkeypatch.setitem(main._features_state, "multi_agent_enabled", False)
    monkeypatch.setitem(main._features_state, "mcp_enabled", False)
    monkeypatch.setattr(main, "_mcp_manager", None)
    c = TestClient(main.app)
    r = c.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["multi_agent_enabled"] is False


def test_multi_agent_disabled_zero_module_load_subprocess():
    """子进程隔离（照抄 RAG/4A 模式）：MULTI_AGENT_ENABLED=false 时 import main
    零 sub_agent 模块加载、TOOL_SCHEMAS 不含 delegate_task。"""
    code = ("import sys; sys.path.insert(0, '.'); import main;"
            "print('sub_agent' in sys.modules,"
            "any(s['function']['name'] == 'delegate_task' "
            "for s in __import__('tools').TOOL_SCHEMAS))")
    e = os.environ.copy()
    e["MULTI_AGENT_ENABLED"] = "false"
    r = subprocess.run([sys.executable, "-c", code], cwd=AI_ROOT, env=e,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "False False" in r.stdout


def test_multi_agent_enabled_registers_delegate_subprocess():
    """子进程装配：MULTI_AGENT_ENABLED=true（无真实模型调用）时经 lifespan
    注册 delegate_task，requires_confirm 恒 False。"""
    code = "\n".join([
        "import sys, os, tempfile; sys.path.insert(0, '.')",
        # 隔离 features.json：文件优先于 env，不指走的话开发者本地 E2E
        # 产生的真实 config/features.json 会覆盖本测试的 MULTI_AGENT_ENABLED
        "import feature_config",
        "_d = tempfile.mkdtemp()",
        "feature_config.CONFIG_DIR = _d",
        "feature_config.CONFIG_PATH = os.path.join(_d, 'features.json')",
        "import main",
        "from fastapi.testclient import TestClient",
        "import tools",
        "with TestClient(main.app):",
        "    print(tools.tool_available('delegate_task'),",
        "          tools.requires_confirm('delegate_task'),",
        "          'sub_agent' in sys.modules)",
    ])
    e = os.environ.copy()
    e["MULTI_AGENT_ENABLED"] = "true"
    e["DEEPSEEK_API_KEY"] = "sk-test"
    r = subprocess.run([sys.executable, "-c", code], cwd=AI_ROOT, env=e,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "True False True" in r.stdout
