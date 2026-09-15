"""
阶段2 工作区文件上传端点（POST /workspace/files）自测。

用 fastapi TestClient 发请求，monkeypatch 把 DEFAULT_WORKSPACE_DIR / MAX_UPLOAD_BYTES
重定向到 pytest tmp_path，保证测试不向真实 ai/workspace_default/ 写入任何文件。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_workspace_api.py -q
"""
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def make_client() -> TestClient:
    return TestClient(main.app)


def test_upload_success_saves_to_default_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFAULT_WORKSPACE_DIR", tmp_path)
    payload = "你好 workspace".encode("utf-8")
    r = make_client().post(
        "/workspace/files",
        files={"file": ("hello.txt", payload, "text/plain")})

    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "hello.txt"
    assert data["size"] == len(payload)
    assert (tmp_path / "hello.txt").read_bytes() == payload


def test_upload_strips_path_components_from_filename(monkeypatch, tmp_path):
    """文件名只保留 basename：拖入的文件永远落在默认工作区根，不进子目录。"""
    monkeypatch.setattr(main, "DEFAULT_WORKSPACE_DIR", tmp_path)
    r = make_client().post(
        "/workspace/files",
        files={"file": ("some/nested/dir/evil.txt", b"x", "text/plain")})

    assert r.status_code == 200
    assert r.json()["name"] == "evil.txt"
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1 and saved[0].name == "evil.txt"


def test_upload_rejects_dotdot_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFAULT_WORKSPACE_DIR", tmp_path)
    r = make_client().post(
        "/workspace/files",
        files={"file": ("..", b"x", "text/plain")})

    assert r.status_code == 400
    assert not list(tmp_path.iterdir())


def test_upload_oversize_truncated_to_413_and_cleaned(monkeypatch, tmp_path):
    """超过上限立刻 413，且半写文件必须被清理，不留垃圾。"""
    monkeypatch.setattr(main, "DEFAULT_WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 8)
    r = make_client().post(
        "/workspace/files",
        files={"file": ("big.bin", b"1234567890", "application/octet-stream")})

    assert r.status_code == 413
    assert not (tmp_path / "big.bin").exists()
