"""
阶段2 Task3：search_code 工具 + N1 list_dir 流式化测试。

TR-3.1 检索行为与输出契约、TR-3.2 RAG 开关装配（子进程隔离）、
TR-3.3 list_dir 流式语义。零真实模型、零网络（FakeProvider）。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_search_tool.py -q
"""
import math
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indexer  # noqa: E402
import tools  # noqa: E402
from embedding import EmbeddingError, EmbeddingProvider  # noqa: E402
from indexer import WorkspaceIndex, reset_registry  # noqa: E402
from workspace import Workspace  # noqa: E402

AI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MARKERS = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 3}


class FakeProvider(EmbeddingProvider):
    model_name = "fake-embed-model"

    @property
    def dim(self) -> int:
        return 4

    @staticmethod
    def _vec(text: str) -> list[float]:
        v = [0.0] * 4
        for m, i in _MARKERS.items():
            if m in text:
                v[i] = 1.0
        v[3] = 0.05
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class BuildingIndex:
    """ensure_ready 恒返回 building 的桩。"""
    provider = FakeProvider()
    last_error = None

    def ensure_ready(self, wait_seconds=None):
        return "building"


class ErrorIndex(BuildingIndex):
    def ensure_ready(self, wait_seconds=None):
        return "error"

    @property
    def last_error(self):
        return "模型炸了"


class EmbedBoomIndex(BuildingIndex):
    def ensure_ready(self, wait_seconds=None):
        return "ready"

    @property
    def provider(self):
        class Boom(FakeProvider):
            def embed_query(self, text):
                raise EmbeddingError("查询编码失败: boom")
        return Boom()


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws2"
    root.mkdir()
    return Workspace(root)


@pytest.fixture
def env(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    ws = Workspace(ws_dir)
    idx_root = tmp_path / "index_store"
    provider = FakeProvider()
    idx = WorkspaceIndex(ws, provider=provider, index_root=idx_root)
    return ws, idx, provider


def write(ws: Workspace, rel: str, content: str) -> None:
    p = ws.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def patch_index(monkeypatch, idx):
    monkeypatch.setattr(indexer, "get_workspace_index", lambda ws: idx)


# ---------------- TR-3.1 search_code 行为与输出 ----------------

def test_search_happy_path_format(env, monkeypatch):
    ws, idx, provider = env
    write(ws, "src/auth.py", "alpha login check\n" * 5)
    idx.refresh_once()
    patch_index(monkeypatch, idx)

    out = tools.search_code(ws, query="alpha")
    assert out.status == "success" and not out.truncated
    assert out.output.startswith("[search_code] alpha @ ./")
    assert "1. src/auth.py L1-5 相关度 1.00" in out.output
    assert "   1: alpha login check" in out.output          # 带行号预览
    assert "【文件:" not in out.output                       # 头注释不进预览
    assert "共 1 个命中（相关度降序）" in out.output


def test_search_empty_query_rejected(env, monkeypatch):
    ws, idx, _ = env
    patch_index(monkeypatch, idx)
    assert tools.search_code(ws, query="   ").status == "error"
    assert tools.search_code(ws, query=None).status == "error"


def test_search_path_outside_sandbox_rejected(env, monkeypatch):
    ws, idx, _ = env
    patch_index(monkeypatch, idx)
    out = tools.search_code(ws, query="alpha", path="../outside")
    assert out.status == "error"
    assert "越出工作区边界" in out.output


def test_search_path_prefix_filter(env, monkeypatch):
    ws, idx, _ = env
    write(ws, "src/a.py", "alpha\n" * 5)
    write(ws, "docs/b.md", "alpha\n" * 5)
    idx.refresh_once()
    patch_index(monkeypatch, idx)

    out = tools.search_code(ws, query="alpha", path="src")
    assert "src/a.py" in out.output and "docs/b.md" not in out.output
    out2 = tools.search_code(ws, query="alpha", path="src/a.py")  # 单文件限定
    assert "src/a.py" in out2.output and "docs/b.md" not in out2.output


def test_search_building_returns_success_not_error(env, monkeypatch):
    ws, _, _ = env
    patch_index(monkeypatch, BuildingIndex())
    out = tools.search_code(ws, query="alpha")
    assert out.status == "success"
    assert "索引正在后台构建" in out.output
    assert "稍后重新调用" in out.output


def test_search_error_status_surfaces_reason(env, monkeypatch):
    ws, _, _ = env
    patch_index(monkeypatch, ErrorIndex())
    out = tools.search_code(ws, query="alpha")
    assert out.status == "error"
    assert "模型炸了" in out.output


def test_search_embed_failure_converges_to_error(env, monkeypatch):
    ws, _, _ = env
    patch_index(monkeypatch, EmbedBoomIndex())
    out = tools.search_code(ws, query="alpha")
    assert out.status == "error"
    assert "查询编码失败" in out.output


def test_search_no_hits_hint(env, monkeypatch):
    ws, idx, _ = env
    (ws.root / "blob.bin").write_bytes(b"\x00\x01")
    idx.refresh_once()  # 无文本文件 → 空索引
    patch_index(monkeypatch, idx)
    out = tools.search_code(ws, query="alpha")
    assert out.status == "success"
    assert "无命中" in out.output and "grep" in out.output


def test_search_capped_note(env, monkeypatch):
    ws, idx, _ = env
    write(ws, "a.py", "alpha\n" * 200)  # 80 行窗口 + 10 重叠 → 恰 3 个 chunk
    monkeypatch.setattr(indexer, "RAG_MAX_CHUNKS", 2)  # 预算 2 < 3 → 触顶
    idx.refresh_once()
    patch_index(monkeypatch, idx)
    out = tools.search_code(ws, query="alpha")
    assert out.status == "success"
    assert "索引不完整" in out.output


def test_search_preview_line_cap(env, monkeypatch):
    ws, idx, _ = env
    write(ws, "a.py", "alpha\n" * 200)  # chunk 80 行 > 预览 20 行
    idx.refresh_once()
    patch_index(monkeypatch, idx)
    out = tools.search_code(ws, query="alpha")
    assert "仅预览前 20 行" in out.output
    assert "read_file 续读" in out.output


def test_search_output_byte_truncation(env, monkeypatch):
    ws, idx, _ = env
    write(ws, "a.py", "alpha " + "x" * 300 + "\n" * 5)
    idx.refresh_once()
    patch_index(monkeypatch, idx)
    monkeypatch.setattr(tools, "MAX_BYTES", 300)
    out = tools.search_code(ws, query="alpha")
    assert out.truncated and "超限截断" in out.output


# ---------------- TR-3.2 RAG 开关装配（子进程隔离） ----------------

def _run_py(code: str, extra_env: dict) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    e.update(extra_env)
    return subprocess.run([sys.executable, "-c", code], cwd=AI_ROOT, env=e,
                          capture_output=True, text=True, timeout=180)


def test_schemas_include_search_code_by_default():
    code = ("import sys; sys.path.insert(0, '.'); import tools;"
            "names = [s['function']['name'] for s in tools.TOOL_SCHEMAS];"
            "print('HAS' if 'search_code' in names else 'MISSING',"
            "'search_code' in tools._DISPATCH,"
            "[n for n in names if n != 'search_code'])")
    r = _run_py(code, {})
    assert r.returncode == 0, r.stderr
    assert "HAS True" in r.stdout
    assert "['read_file', 'list_dir', 'glob', 'grep']" in r.stdout


def test_schemas_exclude_search_code_when_rag_disabled():
    code = ("import sys; sys.path.insert(0, '.'); import tools;"
            "names = [s['function']['name'] for s in tools.TOOL_SCHEMAS];"
            "print('HAS' if 'search_code' in names else 'MISSING',"
            "'search_code' in tools._DISPATCH,"
            "'fastembed' in sys.modules)")
    r = _run_py(code, {"RAG_ENABLED": "false"})
    assert r.returncode == 0, r.stderr
    assert "MISSING False False" in r.stdout  # 无 search_code、无 fastembed 加载


# ---------------- TR-3.3 list_dir 流式化（N1） ----------------

def test_list_dir_large_dir_contract_unchanged(ws, monkeypatch):
    """600 项目录：总数字正确、只显示前 500、排序契约与全量排序逐字节一致。"""
    d = ws.root / "big"
    d.mkdir()
    for i in range(600):
        (d / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(tools, "LIST_LIMIT", 500)

    out = tools.list_dir(ws, path="big")
    assert out.status == "success" and out.truncated
    lines = out.output.splitlines()
    assert lines[-1] == f"共 600 项，仅显示前 {tools.LIST_LIMIT} 项（请进入子目录查看）"
    shown = [l for l in lines[1:-1]]
    assert len(shown) == 500
    assert shown[0] == "  [文件] f0000.txt (1 B)"
    assert shown[-1] == "  [文件] f0499.txt (1 B)"

    # 与全量物化排序的参考实现逐字节对齐
    names = sorted((p.name for p in d.iterdir()), key=str.lower)[:500]
    assert [l.split("] ", 1)[1].rsplit(" (", 1)[0] for l in shown] == names


def test_list_dir_dirs_first_and_mixed_kinds(ws):
    d = ws.root / "mix"
    d.mkdir()
    (d / "b_dir").mkdir()
    (d / "a_file.txt").write_text("hello", encoding="utf-8")
    out = tools.list_dir(ws, path="mix")
    lines = out.output.splitlines()
    assert lines[1] == "  [目录] b_dir/"
    assert lines[2] == "  [文件] a_file.txt (5 B)"
    assert lines[3] == "共 2 项"


def test_listdir_entries_streaming_classification(ws):
    d = ws.root / "cls"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "x.txt").write_text("abc", encoding="utf-8")
    entries = list(tools._listdir_entries(d))
    kinds = {name: (group, kind) for group, name, _p, kind, _s in entries}
    assert kinds["sub"] == (0, "dir")
    assert kinds["x.txt"] == (1, "file")


def test_list_dir_existing_contract_tests_still_green():
    """既有 list_dir 用例与本文件并存即回归护栏（此处仅防呆提示，实际由
    pytest 全量运行 test_tools.py 验证，零改动）。"""
    assert tools.RAG_ENABLED is True
