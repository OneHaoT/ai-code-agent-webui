"""阶段3本地受限沙箱自测：write_file 硬边界 + run_command 防呆级行为。

不调用真实大模型；工作区用 pytest tmp_path 构造隔离沙箱；
命令执行用真实安全命令（python 脚本 / echo），危险命令零构造。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_sandbox.py -q
"""
import builtins
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sandbox  # noqa: E402
from sandbox import (  # noqa: E402
    CommandResult,
    DockerSandboxProvider,
    LocalSandboxProvider,
    SandboxError,
    WriteResult,
)
from workspace import Workspace  # noqa: E402

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


@pytest.fixture
def provider():
    return LocalSandboxProvider()


def make_dir_link(target, link) -> bool:
    """创建指向目录的链接：优先 symlink，Windows 退化为 junction（同阶段1手法）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True)
        return r.returncode == 0 and link.exists()
    return False


def _write_script(ws, name: str, body: str) -> None:
    (ws.root / name).write_text(body, encoding="utf-8")


# ================= write_file（硬边界） =================

def test_write_file_new(ws, provider):
    r = provider.write_file(ws, "hello.py", "print('hi')\n")
    assert isinstance(r, WriteResult)
    assert r.rel_path == "hello.py" and r.overwrite is False
    assert r.size == len("print('hi')\n".encode("utf-8"))
    assert (ws.root / "hello.py").read_bytes() == b"print('hi')\n"


def test_write_file_overwrite(ws, provider):
    provider.write_file(ws, "f.txt", "aaa")
    r = provider.write_file(ws, "f.txt", "bbbb")
    assert r.overwrite is True
    assert (ws.root / "f.txt").read_bytes() == b"bbbb"


def test_write_file_mkdir_parents(ws, provider):
    r = provider.write_file(ws, "a/b/c.txt", "x")
    assert (ws.root / "a" / "b" / "c.txt").read_bytes() == b"x"
    assert r.rel_path.replace("\\", "/") == "a/b/c.txt"


def test_write_file_escape_dotdot(ws, provider, tmp_path):
    with pytest.raises(SandboxError):
        provider.write_file(ws, "../evil.txt", "x")
    assert not (tmp_path / "evil.txt").exists()


def test_write_file_escape_absolute(ws, provider, tmp_path):
    with pytest.raises(SandboxError):
        provider.write_file(ws, str(tmp_path / "outside" / "evil.txt"), "x")
    assert not (tmp_path / "outside").exists()


def test_write_file_escape_junction(ws, provider, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "w-secret.txt").write_text("JUNCTION-SECRET")
    link = ws.root / "jump"
    if not make_dir_link(outside, link):
        pytest.skip("无法创建目录 symlink/junction")
    with pytest.raises(SandboxError):
        provider.write_file(ws, "jump/w-secret.txt", "data")
    # 目标文件字节未被触碰
    assert (outside / "w-secret.txt").read_text() == "JUNCTION-SECRET"


def test_write_file_escape_shortname(ws, provider, tmp_path):
    """8.3 短名向量：用短名绝对路径访问根外目标必须被拒（卷未启用则 skip）。"""
    outside = tmp_path / "a-very-long-directory-name-for-83-test"
    outside.mkdir()
    if sys.platform != "win32":
        pytest.skip("非 Windows 平台")
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(outside), buf, 1024)
    if n == 0 or buf.value == str(outside):
        pytest.skip("该卷未启用 8.3 短名")
    with pytest.raises(SandboxError):
        provider.write_file(ws, buf.value + "\\evil.txt", "x")
    assert not (outside / "evil.txt").exists()


def test_write_file_violation_zero_io(ws, provider, monkeypatch):
    """越界拒绝路径上零写打开（阶段1 手法：open 侦察）。"""
    write_opens = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **k):
        if "w" in str(mode) or "a" in str(mode):
            write_opens.append((file, mode))
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    with pytest.raises(SandboxError):
        provider.write_file(ws, "../evil.txt", "x")
    assert write_opens == []


def test_write_file_max_bytes(ws, provider, monkeypatch):
    monkeypatch.setattr(sandbox, "WRITE_MAX_BYTES", 10)
    with pytest.raises(SandboxError) as ei:
        provider.write_file(ws, "big.txt", "A" * 11)
    assert "上限" in str(ei.value)
    assert not (ws.root / "big.txt").exists()


def test_write_file_root_and_dir_targets(ws, provider):
    with pytest.raises(SandboxError):        # path 解析为工作区根本身
        provider.write_file(ws, "", "x")
    (ws.root / "adir").mkdir()
    with pytest.raises(SandboxError):        # 目标已是目录
        provider.write_file(ws, "adir", "x")


def test_write_file_bad_params(ws, provider):
    with pytest.raises(SandboxError):
        provider.write_file(ws, "a.txt", 123)
    with pytest.raises(SandboxError):
        provider.write_file(ws, "   ", "x")


# ================= run_command（防呆级） =================

def test_run_command_whitelist_success(ws, provider):
    r = provider.run_command(ws, "echo hello-phase3")
    assert isinstance(r, CommandResult)
    assert r.timed_out is False and r.truncated is False
    assert r.exit_code == 0
    assert "hello-phase3" in r.output


def test_run_command_cwd_locked_to_workspace_root(ws, provider):
    _write_script(ws, "cwd_out.py", "import os\nprint(os.getcwd())\n")
    r = provider.run_command(ws, "python cwd_out.py")
    assert r.exit_code == 0
    assert os.path.normcase(r.output.strip()) == os.path.normcase(str(ws.root))


def test_run_command_nonzero_exit_is_result_not_tool_error(ws, provider):
    _write_script(ws, "exit_out.py", "import sys\nsys.exit(3)\n")
    r = provider.run_command(ws, "python exit_out.py")
    assert r.exit_code == 3
    assert r.timed_out is False


def test_run_command_whitelist_reject_no_process(ws, provider, monkeypatch):
    calls = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        calls.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    with pytest.raises(SandboxError) as ei:
        provider.run_command(ws, "whoami")
    assert "白名单" in str(ei.value)
    assert calls == []          # 白名单在 Popen 之前拦截，零子进程


def test_run_command_blacklist_reject(ws, provider, monkeypatch):
    calls = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        calls.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    # echo 在白名单内，但整串命中黑名单（reg）——必须整串拒绝
    with pytest.raises(SandboxError) as ei:
        provider.run_command(ws, "echo hi & reg query HKLM\\SOFTWARE")
    assert "禁止" in str(ei.value)
    assert calls == []


def test_run_command_output_truncated(ws, provider, monkeypatch):
    monkeypatch.setattr(sandbox, "EXEC_MAX_OUTPUT_BYTES", 10)
    _write_script(ws, "big_out.py", "print('A' * 100)\n")
    r = provider.run_command(ws, "python big_out.py")
    assert r.truncated is True
    assert len(r.output.encode("utf-8")) <= 10 + 4   # 截断字节 + 解码替换余量


def test_run_command_timeout_kills_tree(ws, provider, monkeypatch):
    """超时必须终止整棵进程树（含孙进程），无孤儿残留。"""
    _write_script(ws, "slow_spawner.py", (
        "import subprocess, time\n"
        "p = subprocess.Popen(['ping', '-n', '30', '127.0.0.1'],\n"
        "                     stdout=subprocess.DEVNULL,\n"
        "                     stderr=subprocess.DEVNULL)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"))
    monkeypatch.setattr(sandbox, "EXEC_TIMEOUT_SECONDS", 1.0)
    r = provider.run_command(ws, "python slow_spawner.py")
    assert r.timed_out is True and r.exit_code is None
    grandchild = int(r.output.strip().splitlines()[-1])

    def pid_alive(pid: int) -> bool:
            # 不用 text=True：tasklist 输出为本地 ANSI(GBK)，PID 是 ASCII，
            # 直接在字节里查找，避免环境编码差异导致 reader 线程解码崩溃
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, creationflags=_CREATE_NO_WINDOW)
            return out.returncode == 0 and str(pid).encode() in (out.stdout or b"")

    deadline = time.time() + 5
    alive = True
    while time.time() < deadline:
        alive = pid_alive(grandchild)
        if not alive:
            break
        time.sleep(0.3)
    assert not alive, f"孙进程 {grandchild} 未被杀树清理"


def test_run_command_gbk_output_decoded(ws, provider):
    _write_script(ws, "gbk_out.py",
                  "import sys\nsys.stdout.buffer.write('中文测试'.encode('gbk'))\n")
    r = provider.run_command(ws, "python gbk_out.py")
    assert "中文测试" in r.output


def test_run_command_bad_params(ws, provider):
    with pytest.raises(SandboxError):
        provider.run_command(ws, "")
    with pytest.raises(SandboxError):
        provider.run_command(ws, "   ")
    with pytest.raises(SandboxError):
        provider.run_command(ws, 123)


def test_first_token_normalization():
    assert sandbox._first_token("  Python -V ") == "python"
    assert sandbox._first_token('"npm" run dev') == "npm"
    assert sandbox._first_token("") == ""


# ================= provider 装配与占位 =================

def test_docker_provider_placeholder(ws):
    p = DockerSandboxProvider()
    with pytest.raises(NotImplementedError):
        p.write_file(ws, "a.txt", "x")
    with pytest.raises(NotImplementedError):
        p.run_command(ws, "echo hi")


def test_get_sandbox_provider_factory(monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_PROVIDER_NAME", "local")
    assert isinstance(sandbox.get_sandbox_provider(), LocalSandboxProvider)
    monkeypatch.setattr(sandbox, "SANDBOX_PROVIDER_NAME", "docker")
    with pytest.raises(sandbox.SandboxConfigError) as ei:
        sandbox.get_sandbox_provider()
    assert "docker" in str(ei.value)
    monkeypatch.setattr(sandbox, "SANDBOX_PROVIDER_NAME", "k8s")
    with pytest.raises(sandbox.SandboxConfigError):
        sandbox.get_sandbox_provider()
