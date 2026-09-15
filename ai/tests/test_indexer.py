"""
阶段2 Task2：索引器测试（FakeEmbeddingProvider，零真实模型、零网络）。

覆盖 TR-2.1 沙箱（遍历复用/写入隔离/工作区零写入）、TR-2.2 增量、
TR-2.3 原子写与重启复用、TR-2.4 总量上限，以及分块器单元语义。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_indexer.py -q
"""
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indexer  # noqa: E402
from embedding import EmbeddingProvider  # noqa: E402
from indexer import (  # noqa: E402
    Chunk,
    WorkspaceIndex,
    chunk_file_lines,
    reset_registry,
    workspace_hash,
)
from workspace import Workspace  # noqa: E402

# ---------------- Fake embedding：按标记词造确定性向量 ----------------

_MARKERS = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 3}


class FakeProvider(EmbeddingProvider):
    model_name = "fake-embed-model"

    def __init__(self):
        self.embed_batches = 0
        self.embedded_texts: list[str] = []

    @property
    def dim(self) -> int:
        return 4

    @staticmethod
    def _vec(text: str) -> list[float]:
        v = [0.0] * 4
        for m, i in _MARKERS.items():
            if m in text:
                v[i] = 1.0
        v[3] = 0.05  # 防全零向量
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def embed_documents(self, texts):
        self.embed_batches += 1
        self.embedded_texts.extend(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture()
def env(tmp_path):
    """(工作区, 索引根, provider)；互不污染真实目录。"""
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    idx_root = tmp_path / "index_store"
    return Workspace(str(ws_dir)), idx_root, FakeProvider()


def make_index(env) -> WorkspaceIndex:
    ws, idx_root, provider = env
    return WorkspaceIndex(ws, provider=provider, index_root=idx_root)


def write(ws: Workspace, rel: str, content: str) -> None:
    p = ws.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def tree_fingerprint(root) -> set:
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules"}]
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            out.add((os.path.relpath(p, root), st.st_size, st.st_mtime_ns))
    return out


# ---------------- 分块器单元 ----------------

def test_chunker_covers_all_lines_with_overlap():
    lines = [f"line {i}" for i in range(200)]
    out = chunk_file_lines(lines, chunk_lines=80, overlap=10)
    assert out[0][0] == 1 and out[-1][1] == 200  # 1-based 闭区间覆盖全文件
    for (s1, e1, _), (s2, e2, _) in zip(out, out[1:]):
        assert s2 <= e1  # 相邻窗口有重叠（或紧邻）
        assert s2 > s1   # 严格前进，防死循环
    assert all(e - s + 1 <= 80 for s, e, _ in out)


def test_chunker_breaks_at_blank_line_and_top_level_def():
    # 空行断点：首块末行是空行（空行归当前块）
    lines = ["x = 1"] + ["fill"] * 75 + ["", "def h():", "pass"] + ["tail"] * 5
    out = chunk_file_lines(lines, chunk_lines=80, overlap=10)
    first_end = out[0][1]
    assert lines[first_end - 1] == ""
    assert len(out) >= 2

    # 顶层 def 断点：def 行归下一块
    lines2 = ["x = 1"] + ["fill"] * 75 + ["def handler():", "    pass"] + ["t"] * 5
    out2 = chunk_file_lines(lines2, chunk_lines=80, overlap=10)
    assert any(body and body[0].startswith("def ") for _, _, body in out2[1:])


def test_chunker_tiny_file_single_chunk():
    out = chunk_file_lines(["a", "b", "c"], chunk_lines=80, overlap=10)
    assert out == [(1, 3, ["a", "b", "c"])]


def test_chunker_empty_file():
    assert chunk_file_lines([], chunk_lines=80, overlap=10) == []


# ---------------- TR-2.1 沙箱与写入隔离 ----------------

def test_index_only_root_text_files_and_workspace_untouched(env, monkeypatch):
    ws, idx_root, provider = env
    write(ws, "src/app.py", "alpha login\n" * 30)
    write(ws, "node_modules/lib.js", "alpha ignored")
    write(ws, ".git/config", "alpha ignored")
    (ws.root / "blob.bin").write_bytes(b"\x00\x01\x02alpha")
    big = ws.root / "big.py"
    big.write_text("alpha\n" * 200, encoding="utf-8")
    monkeypatch.setattr(indexer, "RAG_MAX_FILE_BYTES", 500)  # big.py=1200B 淘汰

    before = tree_fingerprint(ws.root)
    idx = make_index(env)
    idx.refresh_once()
    after = tree_fingerprint(ws.root)

    snap = idx.snapshot
    assert snap is not None and len(snap.chunks) > 0
    assert {c.rel_path for c in snap.chunks} == {"src/app.py"}  # 仅根内文本文件
    assert before == after  # 工作区目录树零变化（mtime/size 全等）


def test_index_files_all_under_index_root(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()
    files = {p.name for p in idx_root.rglob("*") if p.is_file()}
    assert files == {"meta.json", "chunks.jsonl", "vectors.npy"}
    assert idx.index_dir.name == workspace_hash(ws.root)


def test_write_outside_index_root_rejected(tmp_path):
    """写盘前断言（双保险）：目标不在索引根内必须拒绝。"""
    from indexer import _assert_under_root

    root = tmp_path / "idx_root"
    root.mkdir()
    _assert_under_root(root / "a" / "meta.json", root)      # 根内：放行
    with pytest.raises(RuntimeError, match="越出索引根"):
        _assert_under_root(tmp_path / "elsewhere" / "f.json", root)


def test_symlink_outside_root_excluded(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    outside = env[1].parent / "outside_secret.txt"
    outside.write_text("alpha secret", encoding="utf-8")
    link = ws.root / "leak.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前用户无符号链接权限")
    idx = make_index(env)
    idx.refresh_once()
    assert {c.rel_path for c in idx.snapshot.chunks} == {"a.py"}


# ---------------- TR-2.2 增量刷新 ----------------

def test_incremental_only_reembeds_changed(env, monkeypatch):
    ws, idx_root, provider = env
    monkeypatch.setattr(indexer, "RAG_FRESHNESS_TTL", 0.0)  # 每次都强制 walk
    write(ws, "keep.py", "alpha\n" * 5)
    write(ws, "edit.py", "beta\n" * 5)
    write(ws, "gone.py", "gamma\n" * 5)
    idx = make_index(env)
    idx.refresh_once()
    assert {c.rel_path for c in idx.snapshot.chunks} == \
        {"keep.py", "edit.py", "gone.py"}
    keep_texts = [c.text for c in idx.snapshot.chunks if c.rel_path == "keep.py"]
    seen_after_build = len(provider.embedded_texts)

    write(ws, "edit.py", "beta beta\n" * 5)   # 变更
    write(ws, "new.py", "delta\n" * 5)        # 新增
    (ws.root / "gone.py").unlink()            # 删除
    idx.refresh_once(force=True)

    added_texts = provider.embedded_texts[seen_after_build:]
    assert added_texts, "变更文件应重新编码"
    assert all("keep.py" not in t for t in added_texts), "未变更文件不应重编"
    rels = {c.rel_path for c in idx.snapshot.chunks}
    assert rels == {"keep.py", "edit.py", "new.py"}
    assert [c.text for c in idx.snapshot.chunks
            if c.rel_path == "keep.py"] == keep_texts  # 旧向量/文本原样保留


def test_ttl_skips_walk_and_embed(env, monkeypatch):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()
    seen = len(provider.embedded_texts)

    # TTL 未过期：force=False 直接返回
    idx.refresh_once(force=False)
    assert len(provider.embedded_texts) == seen

    # TTL 过期（时间源前移）：重新 walk，但指纹无变化 → 不重新编码
    monkeypatch.setattr(indexer, "_now", lambda: 10 ** 9)
    idx.refresh_once(force=False)
    assert len(provider.embedded_texts) == seen


# ---------------- TR-2.3 原子写与重启复用 ----------------

def test_disk_index_reused_by_new_instance(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha beta\n" * 10)
    idx = make_index(env)
    idx.refresh_once()
    original = idx.snapshot

    idx2 = make_index(env)  # 模拟进程重启：同 provider 同配置
    assert idx2.ensure_ready(wait_seconds=0.0) == "ready"
    snap = idx2.snapshot
    assert snap is not None and not snap.capped
    assert snap.chunks == original.chunks
    assert np.allclose(snap.matrix, original.matrix)
    hits = idx2.search(provider.embed_query("alpha"))
    assert hits and hits[0].chunk.rel_path == "a.py"


def test_model_change_triggers_rebuild(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()

    class OtherModel(FakeProvider):
        model_name = "other-embed-model"

    other = OtherModel()
    idx2 = WorkspaceIndex(ws, provider=other, index_root=idx_root)
    assert idx2.snapshot is None  # 模型不匹配 → 不复用磁盘索引
    idx2.refresh_once()
    assert idx2.snapshot is not None


def test_corrupted_index_rebuilds(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()
    (idx_root / workspace_hash(ws.root) / "vectors.npy").write_bytes(b"garbage")

    idx2 = make_index(env)
    idx2.refresh_once()  # 损坏 → 重建而非报错
    assert idx2.snapshot is not None and len(idx2.snapshot.chunks) > 0


# ---------------- TR-2.4 总量上限 ----------------

def test_max_chunks_cap_marks_capped(env, monkeypatch):
    ws, idx_root, provider = env
    monkeypatch.setattr(indexer, "RAG_MAX_CHUNKS", 3)
    write(ws, "a.py", "alpha\n" * 200)  # 每文件 3 chunk
    write(ws, "b.py", "beta\n" * 200)
    idx = make_index(env)
    idx.refresh_once()
    snap = idx.snapshot
    assert len(snap.chunks) == 3 and snap.capped
    assert {c.rel_path for c in snap.chunks} == {"a.py"}  # 先入索引的文件保留


def test_walk_capped_keeps_existing_chunks(env, monkeypatch):
    """walk 触顶时指纹表不完整：跳过删除步骤，旧 chunk 不误删。"""
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()

    def capped_walk(_ws, _base, ignore_dirs=None):
        raise indexer._WalkCapped

    monkeypatch.setattr(indexer, "_iter_files", capped_walk)
    idx.refresh_once(force=True)
    rels = {c.rel_path for c in idx.snapshot.chunks}
    assert rels == {"a.py"} and idx.snapshot.capped


# ---------------- 检索语义（供 Task3 复用验证） ----------------

def test_search_ordering_and_topk(env):
    ws, idx_root, provider = env
    write(ws, "alpha1.py", "alpha\n" * 5)
    write(ws, "beta1.py", "beta\n" * 5)
    write(ws, "alpha2.py", "alpha alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()

    hits = idx.search(provider.embed_query("alpha"), top_k=2)
    assert len(hits) == 2
    assert {h.chunk.rel_path for h in hits} == {"alpha1.py", "alpha2.py"}
    assert abs(hits[0].score - 1.0) < 1e-5  # 同标记查询与文档完全同向

    hits_b = idx.search(provider.embed_query("beta"), top_k=10)
    assert hits_b[0].chunk.rel_path == "beta1.py"

    hits2 = idx.search(provider.embed_query("alpha"), top_k=1)
    assert len(hits2) == 1


def test_search_path_prefix_filter(env):
    ws, idx_root, provider = env
    write(ws, "src/a.py", "alpha\n" * 5)
    write(ws, "docs/b.md", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()
    hits = idx.search(provider.embed_query("alpha"), top_k=10,
                      path_prefix="src")
    assert {h.chunk.rel_path for h in hits} == {"src/a.py"}
    assert idx.search(provider.embed_query("alpha"), top_k=10,
                      path_prefix="src/a.py")  # 精确文件路径同样可过滤


def test_search_empty_index_returns_empty(env):
    ws, idx_root, provider = env
    idx = make_index(env)
    assert idx.search(provider.embed_query("alpha")) == []
    (ws.root / "only.bin").write_bytes(b"\x00\x01")  # 仅二进制 → 空索引但 ready
    idx.refresh_once()
    assert idx.snapshot is not None and len(idx.snapshot.chunks) == 0
    assert idx.search(provider.embed_query("alpha")) == []


def test_search_dim_mismatch_returns_empty(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 5)
    idx = make_index(env)
    idx.refresh_once()
    assert idx.search([0.1] * 999) == []  # 维度不匹配的陈旧查询向量


def test_chunks_carry_path_and_line_metadata(env):
    ws, idx_root, provider = env
    write(ws, "a.py", "alpha\n" * 200)
    idx = make_index(env)
    idx.refresh_once()
    c = idx.snapshot.chunks[0]
    assert c.rel_path == "a.py"
    assert c.line_start == 1 and 1 < c.line_end <= 80
    assert c.text.startswith("【文件: a.py · L1-")
