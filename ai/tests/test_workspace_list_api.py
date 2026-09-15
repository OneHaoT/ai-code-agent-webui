"""阶段3.5：GET /workspace/list 项目树端点单测。

覆盖：root 默认/非法、path 越界（..、绝对路径、junction）、ignore 剪枝、
排序与 size 字段、空目录、LIST_LIMIT 截断、symlink 只显名。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_workspace_list_api.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from workspace import Workspace  # noqa: E402
from test_sandbox import make_dir_link  # noqa: E402


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def ws_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


# ---------------- 正常路径 ----------------

def test_list_root_entries_sorted_with_size(client, ws_root):
    (ws_root / "zeta.py").write_text("print('z')", encoding="utf-8")
    (ws_root / "alpha.py").write_text("x", encoding="utf-8")
    (ws_root / "src").mkdir()

    r = client.get("/workspace/list", params={"root": str(ws_root)})
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is False and body["total"] == 3
    assert body["path"] == "."
    # 目录在前、组内按名称排序；文件在后
    assert [(e["name"], e["type"]) for e in body["entries"]] == [
        ("src", "dir"), ("alpha.py", "file"), ("zeta.py", "file")]
    sizes = {e["name"]: e.get("size") for e in body["entries"]}
    assert sizes["alpha.py"] == 1 and sizes["zeta.py"] == 10
    # dir 不带 size
    assert "size" not in body["entries"][0]


def test_list_subdir_lazy(client, ws_root):
    sub = ws_root / "src"
    sub.mkdir()
    (sub / "inner.py").write_text("i", encoding="utf-8")
    r = client.get("/workspace/list", params={"root": str(ws_root), "path": "src"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "src"
    assert [e["name"] for e in body["entries"]] == ["inner.py"]


def test_empty_dir(client, ws_root):
    body = client.get("/workspace/list", params={"root": str(ws_root)}).json()
    assert body["entries"] == [] and body["total"] == 0


def test_default_workspace_when_root_empty(client, monkeypatch, tmp_path):
    fake = tmp_path / "default_ws"
    fake.mkdir()
    (fake / "hello.md").write_text("h", encoding="utf-8")
    monkeypatch.setattr(main, "workspace", Workspace(fake))
    body = client.get("/workspace/list").json()
    assert [e["name"] for e in body["entries"]] == ["hello.md"]
    assert body["root"] == fake.resolve().as_posix()


def test_truncated_with_monkeypatched_limit(client, ws_root, monkeypatch):
    monkeypatch.setattr(main, "LIST_LIMIT", 5)
    for i in range(8):
        (ws_root / f"f{i}.txt").write_text("x", encoding="utf-8")
    body = client.get("/workspace/list", params={"root": str(ws_root)}).json()
    assert body["truncated"] is True and body["total"] == 8
    assert len(body["entries"]) == 5


# ---------------- ignore 剪枝 ----------------

def test_ignore_dirs_pruned(client, ws_root):
    for d in ("node_modules", ".git", "__pycache__", "dist"):
        (ws_root / d).mkdir()
    (ws_root / "app").mkdir()
    names = [e["name"] for e in client.get(
        "/workspace/list", params={"root": str(ws_root)}).json()["entries"]]
    assert names == ["app"]


@pytest.mark.skipif(sys.platform != "win32", reason="大小写剪枝规则仅 win32/darwin")
def test_ignore_dirs_case_insensitive_on_windows(client, ws_root):
    (ws_root / "NODE_MODULES").mkdir()
    (ws_root / "keep").mkdir()
    names = [e["name"] for e in client.get(
        "/workspace/list", params={"root": str(ws_root)}).json()["entries"]]
    assert names == ["keep"]


# ---------------- 沙箱边界 ----------------

def test_path_escape_dotdot_and_absolute(client, ws_root):
    for bad in ("..", "C:/Windows", "c:\\Windows"):
        r = client.get("/workspace/list", params={"root": str(ws_root), "path": bad})
        assert r.status_code == 400, bad


def test_junction_escape_rejected(client, ws_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws_root / "jump"
    if not make_dir_link(outside, link):
        pytest.skip("无法创建目录 symlink/junction")
    r = client.get("/workspace/list", params={"root": str(ws_root), "path": "jump"})
    assert r.status_code == 400


def test_file_symlink_listed_without_target_info(client, ws_root):
    real = ws_root / "real.txt"
    real.write_text("hello", encoding="utf-8")
    try:
        os.symlink(real, ws_root / "ln.txt")
    except (OSError, NotImplementedError):
        pytest.skip("无法创建 symlink")
    body = client.get("/workspace/list", params={"root": str(ws_root)}).json()
    ln = [e for e in body["entries"] if e["name"] == "ln.txt"]
    # symlink 只显名：type=link、不带 size、不泄露目标信息
    assert len(ln) == 1 and ln[0]["type"] == "link" and "size" not in ln[0]


# ---------------- 非法输入 ----------------

def test_root_invalid(client, tmp_path):
    r = client.get("/workspace/list", params={"root": str(tmp_path / "no_such")})
    assert r.status_code == 400
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    r = client.get("/workspace/list", params={"root": str(f)})
    assert r.status_code == 400


def test_path_errors(client, ws_root):
    (ws_root / "f.txt").write_text("x", encoding="utf-8")
    # 路径不存在
    r = client.get("/workspace/list", params={"root": str(ws_root), "path": "nope"})
    assert r.status_code == 400
    # 目标是文件而非目录
    r = client.get("/workspace/list", params={"root": str(ws_root), "path": "f.txt"})
    assert r.status_code == 400
