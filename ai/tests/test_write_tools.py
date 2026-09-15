"""
阶段3写/执行工具接入自测：TOOL_SCHEMAS/_DISPATCH 装配、未知工具降级、per-tool 超时。

不调用真实大模型；写/执行核心行为（路径硬边界、白名单、杀树）在
test_sandbox.py，本文件聚焦工具层装配与 execute_tool 编排语义。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_write_tools.py -q
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sandbox  # noqa: E402
import tools  # noqa: E402
from workspace import Workspace  # noqa: E402

AI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


# ---------------- 装配（SANDBOX_ENABLED 默认 true） ----------------

def test_schemas_contain_write_tools_with_required_params():
    names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
    assert "write_file" in names and "run_command" in names
    by_name = {s["function"]["name"]: s["function"] for s in tools.TOOL_SCHEMAS}
    assert by_name["write_file"]["parameters"]["required"] == ["path", "content"]
    assert by_name["run_command"]["parameters"]["required"] == ["command"]
    # 与 CONFIRM_TOOLS 对齐：声明进工具集的写/执行工具必须都走 confirm（Task3 消费）
    assert set(tools.CONFIRM_TOOLS) == {"write_file", "run_command"}


def test_dispatch_registered_and_provider_assembled():
    assert tools._SANDBOX is not None
    assert tools._DISPATCH["write_file"] is tools.write_file
    assert tools._DISPATCH["run_command"] is tools.run_command


# ---------------- execute_tool 编排语义 ----------------

def test_execute_tool_write_file_roundtrip(ws):
    out = tools.execute_tool(ws, "write_file",
                             {"path": "sub/demo.txt", "content": "hello 阶段3"})
    assert out.status == "success"
    assert "已写入" in out.output and "新建" in out.output
    assert (ws.root / "sub" / "demo.txt").read_text(encoding="utf-8") == "hello 阶段3"

    out2 = tools.execute_tool(ws, "write_file",
                              {"path": "sub/demo.txt", "content": "v2"})
    assert out2.status == "success" and "覆盖" in out2.output


def test_execute_tool_write_file_escape_rejected(ws):
    out = tools.execute_tool(ws, "write_file",
                             {"path": "../outside.txt", "content": "x"})
    assert out.status == "error" and "越出工作区边界" in out.output
    assert not (ws.root.parent / "outside.txt").exists()


def test_execute_tool_run_command_success_reject_blacklist(ws):
    ok = tools.execute_tool(ws, "run_command", {"command": "echo hi"})
    assert ok.status == "success"
    assert "退出码 0" in ok.output and "hi" in ok.output

    # 白名单外首词 → 拒绝
    bad = tools.execute_tool(ws, "run_command", {"command": "calc.exe"})
    assert bad.status == "error" and "白名单" in bad.output

    # 白名单内首词但命中黑名单 → 拒绝
    black = tools.execute_tool(ws, "run_command", {"command": "echo format"})
    assert black.status == "error" and "被禁止" in black.output


def test_execute_tool_unknown_tool_degradation_when_disabled(ws, monkeypatch):
    """SANDBOX_ENABLED=false 的核心语义：dispatch 无写工具 → 未知工具降级不杀流。

    子进程装配验证见 test_sandbox_disabled_assembly；此处用 monkeypatch
    等价复现同一条 dispatch 查找路径。
    """
    monkeypatch.delitem(tools._DISPATCH, "write_file")
    out = tools.execute_tool(ws, "write_file", {"path": "x.txt", "content": "hi"})
    assert out.status == "error" and "未知工具" in out.output


# ---------------- per-tool 超时覆盖 ----------------

def test_per_tool_timeout_table_defaults():
    # run_command 注册 EXEC_TIMEOUT_SECONDS+10；既有五工具不加条目走默认
    assert tools._TOOL_TIMEOUTS.get("run_command") == \
        sandbox.EXEC_TIMEOUT_SECONDS + 10.0
    for n in ("read_file", "list_dir", "glob", "grep", "search_code",
              "write_file"):
        assert n not in tools._TOOL_TIMEOUTS


def test_execute_tool_per_tool_timeout_behavior(ws, monkeypatch):
    """注入慢执行器：既有工具走默认超时值失败，run_command 按覆盖值放行。

    run_command 用 stub 替身：本测试只验证 execute_tool 的 per-tool 超时分支
    语义，不测真实子进程（真实行为见 test_execute_tool_run_command_success_reject_blacklist；
    子进程启动受杀软扫描影响延迟波动大，避免全量回归下的偶发超时）。
    """

    class _SlowExecutor:
        def __init__(self, delay):
            self.delay = delay

        def submit(self, fn, *a, **k):
            fut: cf.Future = cf.Future()

            def run():
                time.sleep(self.delay)
                try:
                    result = fn(*a, **k)
                except BaseException as e:  # noqa: BLE001
                    if not fut.cancelled():  # 超时路径已 cancel，回填前须判空
                        fut.set_exception(e)
                    return
                if not fut.cancelled():
                    fut.set_result(result)

            threading.Thread(target=run, daemon=True).start()
            return fut

    def fake_run_command(ws_arg, **k):
        return tools.ToolOutput("run_command", "success", "ok")

    monkeypatch.setitem(tools._DISPATCH, "run_command", fake_run_command)
    monkeypatch.setattr(tools, "_TOOL_EXECUTOR", _SlowExecutor(1.2))
    monkeypatch.setattr(tools, "TIMEOUT_SECONDS", 0.3)
    monkeypatch.setitem(tools._TOOL_TIMEOUTS, "run_command", 5.0)

    # 既有工具仍按默认 TIMEOUT_SECONDS（0.3s）超时
    t0 = time.monotonic()
    out = tools.execute_tool(ws, "list_dir", {})
    assert out.status == "error", out.output
    assert "超时" in out.output
    assert time.monotonic() - t0 < 1.0

    # run_command 走 per-tool 5s → 1.2s 后正常返回结果
    out2 = tools.execute_tool(ws, "run_command", {"command": "echo ok"})
    assert out2.status == "success", out2.output


# ---------------- SANDBOX_ENABLED=false 装配（子进程隔离） ----------------

def test_sandbox_disabled_assembly():
    """env 装配期开关：false 时工具集与 dispatch 均不含写/执行工具，provider 不装配。"""
    code = (
        "import json, sys, tempfile;"
        "sys.path.insert(0, '.');"
        "import tools;"
        "from workspace import Workspace;"
        "ws = Workspace(tempfile.mkdtemp());"
        "r = tools.execute_tool(ws, 'write_file', {'path': 'x.txt', 'content': 'hi'});"
        "print(json.dumps({"
        "'names': [s['function']['name'] for s in tools.TOOL_SCHEMAS],"
        "'dispatch_has_write': 'write_file' in tools._DISPATCH,"
        "'dispatch_has_exec': 'run_command' in tools._DISPATCH,"
        "'provider_assembled': tools._SANDBOX is not None,"
        "'degraded_status': r.status}))"
    )
    env = {**os.environ, "SANDBOX_ENABLED": "false"}
    r = subprocess.run([sys.executable, "-c", code], cwd=AI_ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert "write_file" not in data["names"]
    assert "run_command" not in data["names"]
    assert data["dispatch_has_write"] is False
    assert data["dispatch_has_exec"] is False
    assert data["provider_assembled"] is False
    assert data["degraded_status"] == "error"
