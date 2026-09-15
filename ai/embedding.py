"""
阶段2：Embedding 抽象层（工作区语义检索）。

设计要点
--------
1. 接口与实现分离：EmbeddingProvider 定义 embed_documents / embed_query /
   dim / model_name，索引器与工具层只依赖接口，便于测试注入 Fake；
2. FastEmbedProvider 基于 fastembed（ONNX Runtime，免 torch）：
   - 模型**惰性加载**：构造不触发下载/初始化，首次编码才加载——
     RAG_ENABLED=false 或纯四件套对话完全零模型开销；
   - fastembed 延迟 import：包未安装时仅在真正调用时报可读错误；
   - 模型缓存目录定向到 AI 模块自有目录（ai/model_cache/，.gitignore 排除），
     与代码库、用户工作区完全隔离；
3. 查询侧与文档侧分离：bge 中文检索模型要求查询加检索指令前缀
   （模型卡约定）；fastembed 的 query_embed 已内置前缀时优先用之，
   否则手动拼接，保证查询/文档向量空间一致；
4. 一切失败收敛为 EmbeddingError（含原文原因），由调用方转为工具
   error 结果，不杀整轮 SSE 流。

线程模型：模型加载按实例加锁（双重检查），ONNX 推理会话本身线程安全，
工具线程池并发调用 embed 安全。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("ai-assist")

# 默认 embedding 模型：bge-small-zh-v1.5（512 维，~95MB，中英混合检索可用）
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

# bge 中文系列模型卡的检索指令前缀（仅加在查询侧，不加在文档侧）
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingError(Exception):
    """embedding 模型加载或推理失败（调用方转为工具 error 结果）。"""


class EmbeddingProvider:
    """embedding 抽象接口（索引器与 search_code 只依赖本接口）。"""

    model_name: str

    @property
    def dim(self) -> int:
        raise NotImplementedError

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量编码文档 chunk（检索库侧，无指令前缀）。"""
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        """编码查询（检索侧，按模型要求加指令前缀）。"""
        raise NotImplementedError


class FastEmbedProvider(EmbeddingProvider):
    """fastembed（ONNX）实现的 EmbeddingProvider。

    Parameters
    ----------
    model_name : str
        fastembed 模型名（HuggingFace 模型 ID）。
    cache_dir : str | Path | None
        模型缓存目录；None 用 fastembed 默认（本地缓存）。生产路径由
        indexer.py 传入 AI 模块自有目录（ai/model_cache/）。
    loader : callable | None
        测试注入点：签名 (model_name, cache_dir) -> 模型实例；缺省时
        延迟 import fastembed.TextEmbedding。
    """

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL,
                 cache_dir: str | Path | None = None,
                 loader: Callable[[str, str | None], object] | None = None):
        self.model_name = (model_name or DEFAULT_EMBEDDING_MODEL).strip()
        self._cache_dir = str(Path(cache_dir).expanduser()) if cache_dir else None
        self._loader = loader or self._default_loader
        self._model: object | None = None
        self._dim: int | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _default_loader(model_name: str, cache_dir: str | None):
        # 延迟 import：fastembed 未安装时保持模块可导入（RAG 关闭场景零依赖）
        from fastembed import TextEmbedding  # noqa: PLC0415 —— 刻意延迟
        kwargs: dict = {"model_name": model_name}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        return TextEmbedding(**kwargs)

    # ---------- 内部 ----------

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:  # 双重检查，防止并发重复加载
                    logger.info("加载 embedding 模型 %s（首次调用，可能需联网下载）",
                                self.model_name)
                    try:
                        self._model = self._loader(self.model_name, self._cache_dir)
                    except Exception as e:  # noqa: BLE001 —— 收敛为 EmbeddingError
                        raise EmbeddingError(
                            f"embedding 模型加载失败（{self.model_name}）: {e}") from e
                    logger.info("embedding 模型 %s 加载完成", self.model_name)
        return self._model

    def _remember_dim(self, vectors: list[list[float]]) -> None:
        if self._dim is None and vectors:
            self._dim = len(vectors[0])

    @staticmethod
    def _to_float_lists(iterable) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in iterable]

    # ---------- 对外接口 ----------

    @property
    def dim(self) -> int:
        """向量维度（首次访问会触发模型加载与一次探测编码）。"""
        if self._dim is None:
            probe = self.embed_documents(["dim"])
            if not probe:  # 理论不可达：探测必返回 1 条
                raise EmbeddingError("embedding 模型未返回向量，无法确定维度")
        return int(self._dim)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure()
        try:
            vectors = self._to_float_lists(model.embed(list(texts)))
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001 —— ONNX/下载/内存等统一收敛
            raise EmbeddingError(f"文档编码失败: {e}") from e
        self._remember_dim(vectors)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            raise EmbeddingError("查询文本为空，无法编码")
        model = self._ensure()
        try:
            # fastembed 对支持查询前缀的模型提供 query_embed（内置指令前缀）；
            # 无该方法时按 bge 模型卡手动拼接前缀
            if hasattr(model, "query_embed"):
                vectors = self._to_float_lists(model.query_embed([text]))
            else:
                vectors = self._to_float_lists(
                    model.embed([BGE_QUERY_INSTRUCTION + text]))
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(f"查询编码失败: {e}") from e
        if not vectors:
            raise EmbeddingError("查询编码未返回向量")
        self._remember_dim(vectors)
        return vectors[0]
