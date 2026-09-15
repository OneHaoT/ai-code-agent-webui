"""
阶段2 Task1：EmbeddingProvider 单元测试（Fake loader，零真实模型、零网络）。

覆盖 TR-1.1（惰性加载/加载一次/异常收敛）与接口语义；
真实 fastembed 模型的可选集成用例见文件末尾（未下载则 skip）。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_embedding.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from embedding import (  # noqa: E402
    BGE_QUERY_INSTRUCTION,
    EmbeddingError,
    FastEmbedProvider,
)

DIM = 4


class FakeModel:
    """模拟 fastembed.TextEmbedding：记录调用并返回确定性向量。"""

    def __init__(self):
        self.embed_calls: list[list[str]] = []
        self.query_calls: list[list[str]] = []

    def _vec(self, text: str) -> list[float]:
        return [float((len(text) % DIM) + 1)] * DIM

    def embed(self, texts):
        self.embed_calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def query_embed(self, texts):
        self.query_calls.append(list(texts))
        return [self._vec(t) for t in texts]


class PlainModel:
    """无 query_embed 方法的模型：验证回退到手动指令前缀拼接。"""

    def __init__(self):
        self.embed_calls: list[list[str]] = []

    def embed(self, texts):
        self.embed_calls.append(list(texts))
        return [[1.0] * DIM for _ in texts]


def make_provider(calls: list, model=..., **kwargs) -> FastEmbedProvider:
    def loader(name, cache_dir):
        calls.append((name, cache_dir))
        if isinstance(model, Exception):
            raise model
        return FakeModel() if model is ... else model

    return FastEmbedProvider(cache_dir="cache-x", loader=loader, **kwargs)


# ---------------- 惰性加载（TR-1.1） ----------------

def test_construct_does_not_load_model():
    calls: list = []
    p = make_provider(calls)
    assert p._model is None
    assert calls == []
    assert p.model_name == FastEmbedProvider().__doc__ is None or p.model_name


def test_first_embed_loads_once_and_caches():
    calls: list = []
    p = make_provider(calls)
    p.embed_documents(["a", "b"])
    p.embed_documents(["c"])
    p.embed_query("查询")
    assert len(calls) == 1  # 三次调用只加载一次模型
    assert calls[0][0] == p.model_name
    assert p._model is not None


def test_dim_property_triggers_probe_load_once():
    calls: list = []
    p = make_provider(calls)
    assert p.dim == DIM
    p.embed_documents(["x"])
    assert len(calls) == 1  # dim 探测后不再重复加载


def test_dim_cached_after_embed():
    p = make_provider([])
    p.embed_documents(["hello"])
    assert p.dim == DIM


# ---------------- 接口语义 ----------------

def test_embed_documents_empty_returns_empty_without_load():
    calls: list = []
    p = make_provider(calls)
    assert p.embed_documents([]) == []
    assert calls == []  # 空批次不触发模型加载


def test_query_uses_query_embed_without_prefix():
    p = make_provider([])
    p.embed_query("登录校验在哪")
    model = p._model
    assert model.query_calls == [["登录校验在哪"]]
    assert model.embed_calls == []  # 查询不走文档通道


def test_query_falls_back_to_manual_prefix_when_no_query_embed():
    p = make_provider([], model=PlainModel())
    p.embed_query("登录校验在哪")
    model = p._model
    assert len(model.embed_calls) == 1
    assert model.embed_calls[0][0] == BGE_QUERY_INSTRUCTION + "登录校验在哪"


def test_batch_order_and_determinism():
    p = make_provider([])
    vecs = p.embed_documents(["abc", "abcd"])
    assert vecs[0] == [float((3 % DIM) + 1)] * DIM
    assert vecs[1] == [float((4 % DIM) + 1)] * DIM


# ---------------- 异常收敛（TR-1.1） ----------------

def test_loader_failure_converges_to_embedding_error():
    p = make_provider([], model=RuntimeError("网络炸了"))
    with pytest.raises(EmbeddingError) as ei:
        p.embed_documents(["a"])
    assert "embedding 模型加载失败" in str(ei.value)
    assert "网络炸了" in str(ei.value)


def test_embed_inference_failure_converges_to_embedding_error():
    class BoomModel(FakeModel):
        def embed(self, texts):
            raise ValueError("onnx boom")

    p = make_provider([], model=BoomModel())
    with pytest.raises(EmbeddingError) as ei:
        p.embed_documents(["a"])
    assert "文档编码失败" in str(ei.value)


def test_query_inference_failure_converges_to_embedding_error():
    class BoomQueryModel(FakeModel):
        def query_embed(self, texts):
            raise ValueError("query boom")

    p = make_provider([], model=BoomQueryModel())
    with pytest.raises(EmbeddingError) as ei:
        p.embed_query("q")
    assert "查询编码失败" in str(ei.value)


def test_empty_query_rejected_without_load():
    calls: list = []
    p = make_provider(calls)
    with pytest.raises(EmbeddingError):
        p.embed_query("   ")
    assert calls == []


# ---------------- main 装配冒烟（TR-1.2） ----------------

def test_health_reports_rag_fields():
    import main
    from fastapi.testclient import TestClient

    r = TestClient(main.app).get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["rag_enabled"] is True
    assert data["embedding_model"] == "BAAI/bge-small-zh-v1.5"


def test_import_main_does_not_load_fastembed():
    """import main / 创建 provider 均不触发 fastembed 加载（子进程隔离验证，
    与 pytest 内其他用例是否加载过 fastembed 无关）。"""
    import subprocess

    ai_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.path.insert(0, '.');"
        "import main;"
        "from embedding import FastEmbedProvider;"
        "FastEmbedProvider();"
        "assert 'fastembed' not in sys.modules, 'fastembed 被过早加载';"
        "print('SMOKE_OK')"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ai_root,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "SMOKE_OK" in r.stdout


# ---------------- 可选集成：真实模型（未下载则 skip） ----------------

def _model_cached() -> bool:
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "model_cache")
    if not os.path.isdir(cache):
        return False
    return any("bge-small-zh" in name.lower() for name in os.listdir(cache))


@pytest.mark.skipif(not _model_cached(), reason="bge 模型未下载（联网集成用例）")
def test_fastembed_real_model_integration():
    pytest.importorskip("fastembed")
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "model_cache")
    p = FastEmbedProvider(cache_dir=cache)
    qv = p.embed_query("登录校验逻辑在哪")
    dv = p.embed_documents(["def login(): pass", "class Auth:"])
    assert p.dim == 512
    assert len(qv) == 512 and len(dv) == 2 and len(dv[0]) == 512
    assert all(isinstance(x, float) for x in qv[:8])
