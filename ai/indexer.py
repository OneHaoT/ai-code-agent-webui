"""
阶段2：工作区语义检索索引器（全项目首个拥有"写权限"的组件）。

沙箱策略（硬约束，违反即缺陷）
--------------------------------
1. 遍历**只经 tools._iter_files**（含逐条目 is_within 复检、ignore 目录剪枝、
   WALK_ENTRY_LIMIT 条目上限），禁止自开 os.walk/glob——越界字节零接触语义
   与四件套只读工具完全一致；
2. 写入范围**硬锁**在 RAG_INDEX_ROOT（默认 ai/index_store/）内：每次写盘前
   _assert_under_root 断言，越出立即抛错；workspace_root 只读不写，绝不向
   用户代码库写入任何索引文件；
3. embedding 模型缓存锁定在 EMBEDDING_CACHE_DIR（默认 ai/model_cache/），
   同样在 AI 模块自有目录内；
4. 索引是可随时重建的派生缓存，不入业务库（Flyway 不涉及），存储格式：
   {RAG_INDEX_ROOT}/{sha256(规范化根)[:16]}/ 下
   - chunks.jsonl   每行一个 chunk（rel_path / 行号范围 / 文本）
   - vectors.npy    float32 矩阵，与 chunks.jsonl 同序，行已 L2 归一化
   - meta.json      模型名/维度/分块参数/指纹表/capped 标记（最后写入，作提交标记）

并发模型
--------
- 每工作区一把锁 + _building 标志：同一工作区同时只跑一个刷新线程；
- 查询只读不可变快照（IndexSnapshot），刷新完成后整体原子换新；
- 刷新在独立后台线程执行（索引构建耗时不可控，绝不能进 execute_tool
  的 10s 超时池——超时强杀会打断模型下载/批量编码）；
- 进程内按工作区根做 LRU 快照缓存（默认 4 个），重启后磁盘索引自动复用
  （meta 中模型名/分块参数不匹配则整库重建）。

增量策略
--------
指纹 = (st_size, st_mtime_ns) 按文件；TTL 内不重复遍历（RAG_FRESHNESS_TTL），
过期后 walk 全量指纹对比：仅新增/变更文件重新分块+编码，删除文件清 chunk；
walk 触达 WALK_ENTRY_LIMIT 时跳过删除步骤（防误删），置 capped 标记。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from embedding import EmbeddingError, EmbeddingProvider, FastEmbedProvider
from tools import DEFAULT_IGNORE_DIRS, _WalkCapped, _decode, _iter_files, _looks_binary
from workspace import PROJECT_ROOT, Workspace

logger = logging.getLogger("ai-assist")

# ---------------- 配置（env 可覆盖，均有默认值） ----------------


def _int_env(key: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(key, "").strip() or default))
    except ValueError:
        logger.warning("%s 非法，回退默认值 %s", key, default)
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(key, "").strip() or default))
    except ValueError:
        logger.warning("%s 非法，回退默认值 %s", key, default)
        return default


RAG_TOP_K = _int_env("RAG_TOP_K", 8)
RAG_MAX_CHUNKS = _int_env("RAG_MAX_CHUNKS", 50_000)
RAG_MAX_FILE_BYTES = _int_env("RAG_MAX_FILE_BYTES", 512 * 1024)
RAG_CHUNK_LINES = _int_env("RAG_CHUNK_LINES", 80)
RAG_CHUNK_OVERLAP = _int_env("RAG_CHUNK_OVERLAP", 10)
RAG_FRESHNESS_TTL = _float_env("RAG_FRESHNESS_TTL", 30.0)
RAG_BUILD_WAIT_SECONDS = _float_env("RAG_BUILD_WAIT_SECONDS", 8.0)
# 索引根与模型缓存：默认都在 AI 模块自有目录内，绝不指向用户工作区
RAG_INDEX_ROOT = Path(
    os.getenv("RAG_INDEX_ROOT", "").strip() or PROJECT_ROOT / "ai" / "index_store"
).expanduser()
EMBEDDING_CACHE_DIR = Path(
    os.getenv("EMBEDDING_CACHE_DIR", "").strip() or PROJECT_ROOT / "ai" / "model_cache"
).expanduser()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip()

# 派生上限（非 env 配置）
_MAX_CHUNKS_PER_FILE = 500      # 单文件 chunk 上限，防超大生成文件刷爆总量
_MAX_LINE_CHARS = 65_536        # 单行超长（minified/lock 文件特征）→ 跳过该文件
_EMBED_BATCH = 256              # 编码批大小，平衡吞吐与内存
_SNAPSHOT_LRU = 4               # 进程内快照缓存的工作区个数上限
_META_VERSION = 1


def _now() -> float:
    """时间源（测试可 monkeypatch）。"""
    return time.monotonic()


# ---------------- 数据结构 ----------------


@dataclass(frozen=True)
class Chunk:
    rel_path: str   # 相对工作区根，posix 分隔
    line_start: int  # 1-based 闭区间
    line_end: int
    text: str        # 含文件头注释行（自包含，直接参与编码）


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class IndexSnapshot:
    root_key: str
    model_name: str
    dim: int
    chunks: tuple[Chunk, ...]
    matrix: np.ndarray              # [n, dim] float32，行已 L2 归一化
    fingerprints: dict[str, tuple[int, int]]  # rel -> (size, mtime_ns)
    capped: bool


# ---------------- 工具函数 ----------------


def _assert_under_root(path: Path, root: Path) -> None:
    """写盘前硬断言：目标路径必须位于索引根内（防配置错误污染任意目录）。"""
    rp = path.resolve(strict=False)
    rr = root.resolve(strict=False)
    if rp != rr and rr not in rp.parents:
        raise RuntimeError(f"索引写入路径越出索引根，已拒绝写入：{path}")


def workspace_hash(root: Path) -> str:
    """工作区根的稳定哈希（Windows 大小写不敏感），作索引目录名。"""
    norm = str(root.resolve(strict=False))
    if os.name == "nt":
        norm = norm.lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def chunk_file_lines(lines: list[str], *, chunk_lines: int | None = None,
                     overlap: int | None = None) -> list[tuple[int, int, list[str]]]:
    """行窗口启发式分块。返回 [(line_start, line_end, 行列表)]（1-based 闭区间）。

    断点优先级：窗口尾部 50% 内，从窗口末尾向前找——空行（含其后） >
    顶层定义行（def/class/func 等行首）之前；找不到则硬切。
    保证结果覆盖全部行且相邻窗口有 overlap 行重叠。
    """
    window = chunk_lines if chunk_lines is not None else RAG_CHUNK_LINES
    ov = overlap if overlap is not None else RAG_CHUNK_OVERLAP
    n = len(lines)
    if n == 0:
        return []
    top_level = ("def ", "async def ", "class ", "function ", "func ",
                 "pub fn", "impl ", "struct ", "export ")
    out: list[tuple[int, int, list[str]]] = []
    start = 0  # 0-based
    while start < n:
        end = min(start + window, n)  # exclusive
        break_kind = None  # None=硬切 / "blank"=空行 / "def"=顶层定义
        if end < n:
            # 仅在窗口后半段找断点，避免窗口越切越小
            search_lo = start + max(1, window // 2)
            best = -1
            for i in range(end - 1, search_lo - 1, -1):
                stripped = lines[i].strip()
                if not stripped:
                    best, break_kind = i + 1, "blank"  # 空行归当前块
                    break
                if not lines[i][:1].isspace() and stripped.startswith(top_level):
                    best, break_kind = i, "def"        # def 行归下一块
                    break
            if best > start:
                end = best
        out.append((start + 1, end, lines[start:end]))
        if end >= n:
            break
        if break_kind == "def":
            start = end                     # def 行起新块：语义边界不重叠
        else:
            start = max(end - ov, start + 1)  # 重叠窗口，且保证前进防死循环
    return out


def _chunk_text(rel: str, line_start: int, line_end: int, body: list[str]) -> str:
    """chunk 参与编码的文本：文件头 + 行内容（自包含提升检索质量）。"""
    return f"【文件: {rel} · L{line_start}-{line_end}】\n" + "\n".join(body)


def _file_chunks(path: Path, rel: str) -> list[Chunk] | None:
    """读单文件并分块；二进制/超限/解码灾难返回 None（跳过不入索引）。"""
    try:
        if path.stat().st_size > RAG_MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if _looks_binary(raw[:8192]):
        return None
    text = _decode(raw)
    lines = text.splitlines()
    if any(len(line) > _MAX_LINE_CHARS for line in lines):
        return None  # minified / lock 类文件，切了也是噪音
    if not lines:
        return None
    chunks: list[Chunk] = []
    for s, e, body in chunk_file_lines(lines):
        chunks.append(Chunk(rel, s, e, _chunk_text(rel, s, e, body)))
        if len(chunks) >= _MAX_CHUNKS_PER_FILE:
            break  # 单文件上限，超出部分放弃（指纹不变，不会反复重编）
    return chunks


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype(np.float32)


# ---------------- 工作区索引 ----------------


class WorkspaceIndex:
    """单个工作区的向量索引：懒构建 + 指纹增量 + 原子落盘。"""

    def __init__(self, ws: Workspace, provider: EmbeddingProvider | None = None,
                 index_root: Path | None = None):
        self._ws = ws
        self._provider = provider
        self._root_key = workspace_hash(ws.root)
        self._index_root = index_root
        self._lock = threading.RLock()
        self._snapshot: IndexSnapshot | None = None
        self._disk_checked = False
        self._building = False
        self._build_done = threading.Event()
        self._last_check = 0.0
        self._refresh_error: str | None = None

    # ---- 属性 ----

    @property
    def provider(self) -> EmbeddingProvider:
        if self._provider is None:
            self._provider = get_provider()
        return self._provider

    @property
    def index_dir(self) -> Path:
        root = self._index_root if self._index_root is not None else RAG_INDEX_ROOT
        return root / self._root_key

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._refresh_error

    @property
    def snapshot(self) -> IndexSnapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def building(self) -> bool:
        with self._lock:
            return self._building

    # ---- 对外主流程 ----

    def ensure_ready(self, wait_seconds: float | None = None) -> str:
        """确保索引可用；返回 "ready" | "building" | "error"。

        - TTL 内已检查过 → 直接 ready（不重复 walk）；
        - 需要刷新时在后台线程执行（绝不阻塞调用线程做重活），
          最多同步等待 wait_seconds（默认 RAG_BUILD_WAIT_SECONDS，须小于
          工具超时 10s），等不到返回 building，由模型稍后重试。
        """
        wait = RAG_BUILD_WAIT_SECONDS if wait_seconds is None else wait_seconds
        self._ensure_disk_loaded()
        with self._lock:
            fresh = (self._snapshot is not None
                     and (_now() - self._last_check) < RAG_FRESHNESS_TTL)
            if fresh:
                return "ready"
            if not self._building:
                self._start_refresh_locked()
        if self._build_done.wait(wait):
            with self._lock:
                if self._refresh_error:
                    return "error"
                if self._snapshot is not None:
                    return "ready"
        return "building"

    def refresh_once(self, *, force: bool = True) -> None:
        """同步刷新（测试入口与后台线程共用）；force 忽略 TTL。"""
        self._ensure_disk_loaded()
        with self._lock:
            if not force and self._snapshot is not None \
                    and (_now() - self._last_check) < RAG_FRESHNESS_TTL:
                return
            if self._building:
                self._build_done.wait()
                return
            self._building = True
            self._build_done.clear()
            self._refresh_error = None
        try:
            snap = self._do_refresh()
            with self._lock:
                self._snapshot = snap
                self._last_check = _now()
        except EmbeddingError as e:
            with self._lock:
                self._refresh_error = str(e)
            logger.warning("索引刷新失败（embedding）：%s", e)
        except Exception as e:  # noqa: BLE001 —— 后台线程兜底，错误状态可查询
            with self._lock:
                self._refresh_error = f"索引刷新失败: {e}"
            logger.exception("索引刷新异常")
        finally:
            with self._lock:
                self._building = False
            self._build_done.set()

    def search(self, query_vec: list[float], top_k: int | None = None,
               path_prefix: str = "") -> list[Hit]:
        """在当前快照上做余弦检索（矩阵行与查询均已归一化，点积即余弦）。"""
        with self._lock:
            snap = self._snapshot
        if snap is None or len(snap.chunks) == 0 or not query_vec:
            return []
        k = top_k if top_k is not None else RAG_TOP_K
        q = np.asarray(query_vec, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm == 0.0:
            return []
        q = q / norm
        if q.shape[0] != snap.matrix.shape[1]:  # 模型/维度不匹配的陈旧快照
            return []
        scores = snap.matrix @ q
        prefix = (path_prefix or "").replace("\\", "/").strip("/")
        hits: list[Hit] = []
        # 命中通常远少于 n：先取 top-k*4 候选再按 path 过滤，避免全量排序
        order = np.argsort(-scores)[: max(k * 8, 64)]
        for idx in order:
            c = snap.chunks[int(idx)]
            if prefix:
                if c.rel_path != prefix and not c.rel_path.startswith(prefix + "/"):
                    continue
            hits.append(Hit(c, float(scores[int(idx)])))
            if len(hits) >= k:
                break
        return hits

    # ---- 内部 ----

    def _start_refresh_locked(self) -> None:
        self._building = True
        self._build_done.clear()
        self._refresh_error = None

        def _run():
            try:
                snap = self._do_refresh()
                with self._lock:
                    self._snapshot = snap
                    self._last_check = _now()
            except EmbeddingError as e:
                with self._lock:
                    self._refresh_error = str(e)
                logger.warning("索引刷新失败（embedding）：%s", e)
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._refresh_error = f"索引刷新失败: {e}"
                logger.exception("索引刷新异常")
            finally:
                with self._lock:
                    self._building = False
                self._build_done.set()

        threading.Thread(target=_run, name=f"rag-index-{self._root_key[:8]}",
                         daemon=True).start()

    def _ensure_disk_loaded(self) -> None:
        """进程首次访问时尝试复用磁盘索引（不触发模型加载）。"""
        with self._lock:
            if self._disk_checked:
                return
            self._disk_checked = True
        snap = self._load_from_disk()
        if snap is not None:
            with self._lock:
                if self._snapshot is None:
                    self._snapshot = snap
                    self._last_check = _now()  # 磁盘指纹刚校验过，视为新鲜起点
                    logger.info("复用磁盘索引 %s：%d chunks（workspace=%s）",
                                self.index_dir.name, len(snap.chunks),
                                self._ws.root)

    def _load_from_disk(self) -> IndexSnapshot | None:
        """读磁盘索引；任何不一致（模型/参数变更、文件损坏）都返回 None 重建。"""
        meta_path = self.index_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if meta.get("version") != _META_VERSION \
                or meta.get("root_key") != self._root_key \
                or meta.get("model_name") != self.provider.model_name \
                or meta.get("chunk_lines") != RAG_CHUNK_LINES \
                or meta.get("chunk_overlap") != RAG_CHUNK_OVERLAP:
            return None
        try:
            chunks = tuple(
                Chunk(rel_path=item["p"], line_start=item["s"], line_end=item["e"],
                      text=item["t"])
                for item in (json.loads(line) for line in
                             (self.index_dir / "chunks.jsonl")
                             .read_text(encoding="utf-8").splitlines() if line)
            )
            matrix = np.load(self.index_dir / "vectors.npy")
        except (OSError, ValueError, KeyError):
            return None
        dim = int(meta.get("dim") or 0)
        if (matrix.ndim != 2 or matrix.shape[0] != len(chunks)
                or matrix.shape[1] != dim or dim <= 0):
            return None
        fps = {k: (int(v[0]), int(v[1]))
               for k, v in (meta.get("fingerprints") or {}).items()}
        return IndexSnapshot(self._root_key, self.provider.model_name, dim,
                             chunks, matrix.astype(np.float32), fps,
                             bool(meta.get("capped")))

    def _collect_fingerprints(self) -> tuple[dict[str, tuple[int, int]], bool]:
        """经 _iter_files 收集指纹；触达遍历上限返回 capped=True。"""
        fps: dict[str, tuple[int, int]] = {}
        try:
            for f in _iter_files(self._ws, self._ws.root):
                try:
                    st = f.stat()
                except OSError:
                    continue
                fps[self._ws.relative(f).replace("\\", "/")] = (
                    st.st_size, st.st_mtime_ns)
        except _WalkCapped:
            return fps, True
        return fps, False

    def _do_refresh(self) -> IndexSnapshot:
        old = self.snapshot
        old_fps: dict[str, tuple[int, int]] = old.fingerprints if old else {}
        old_chunks: tuple[Chunk, ...] = old.chunks if old else ()
        old_matrix = old.matrix if old is not None \
            else np.zeros((0, 0), dtype=np.float32)

        fps, walk_capped = self._collect_fingerprints()

        # 增量 diff：仅变更/新增文件重编；删除文件清 chunk。
        # walk 触顶时指纹表不完整，跳过删除步骤防误删已索引文件。
        changed = {rel for rel, fp in fps.items() if old_fps.get(rel) != fp}
        removed = set() if walk_capped else (set(old_fps) - set(fps))
        affected = changed | removed

        kept_idx = [i for i, c in enumerate(old_chunks) if c.rel_path not in affected]
        kept_chunks = [old_chunks[i] for i in kept_idx]
        if len(old_chunks):
            # 空列表也要保形：(0, dim)——全量替换时 vstack 才能对齐列数
            kept_matrix = old_matrix[np.asarray(kept_idx, dtype=np.int64)]
        else:
            kept_matrix = np.zeros((0, 0), dtype=np.float32)

        # 变更文件重新分块
        new_chunks: list[Chunk] = []
        for rel in sorted(changed):
            path = self._ws.resolve(rel)
            file_chunks = _file_chunks(path, rel)
            if file_chunks:
                new_chunks.extend(file_chunks)

        # 总量上限：优先保留旧 chunk，新 chunk 截断并置 capped
        capped = walk_capped
        budget = RAG_MAX_CHUNKS - len(kept_chunks)
        if budget <= 0:
            new_chunks = []
            capped = True
        elif len(new_chunks) > budget:
            new_chunks = new_chunks[:budget]
            capped = True

        # 批量编码新 chunk（批量小批，控内存）
        if new_chunks:
            vectors: list[list[float]] = []
            for i in range(0, len(new_chunks), _EMBED_BATCH):
                batch = [c.text for c in new_chunks[i:i + _EMBED_BATCH]]
                vectors.extend(self.provider.embed_documents(batch))
            new_matrix = _normalize_rows(np.asarray(vectors, dtype=np.float32))
        else:
            new_matrix = np.zeros((0, kept_matrix.shape[1] if kept_matrix.size
                                   else 0), dtype=np.float32)

        matrix: np.ndarray
        parts = [m for m in (kept_matrix, new_matrix) if m.size and m.ndim == 2]
        if len(parts) > 1:
            matrix = np.vstack(parts)
        elif parts:
            matrix = parts[0]
        else:
            matrix = np.zeros((0, 0), dtype=np.float32)
        chunks = tuple(kept_chunks) + tuple(new_chunks)
        snap = IndexSnapshot(self._root_key, self.provider.model_name,
                             int(matrix.shape[1]) if matrix.ndim == 2 else 0,
                             chunks, matrix, fps, capped)
        self._save_to_disk(snap)
        logger.info("索引刷新完成 workspace=%s chunks=%d capped=%s",
                    self._ws.root, len(chunks), capped)
        return snap

    def _save_to_disk(self, snap: IndexSnapshot) -> None:
        """原子落盘：临时文件 + os.replace；meta 最后写（作提交标记）。

        三文件无跨文件事务：崩溃中断时旧 meta 仍指向旧集，加载端按
        meta 校验（版本/模型/维度/行数一致），不一致即整库重建。
        """
        d = self.index_dir
        root = self._index_root if self._index_root is not None else RAG_INDEX_ROOT
        _assert_under_root(d / "meta.json", root)
        _assert_under_root(d / "chunks.jsonl", root)
        _assert_under_root(d / "vectors.npy", root)
        d.mkdir(parents=True, exist_ok=True)

        chunks_tmp = d / "chunks.jsonl.tmp"
        with open(chunks_tmp, "w", encoding="utf-8", newline="\n") as f:
            for c in snap.chunks:
                f.write(json.dumps({"p": c.rel_path, "s": c.line_start,
                                    "e": c.line_end, "t": c.text},
                                   ensure_ascii=False) + "\n")
        os.replace(chunks_tmp, d / "chunks.jsonl")

        vec_tmp = d / "vectors.npy.tmp"
        with open(vec_tmp, "wb") as f:
            np.save(f, snap.matrix)
        os.replace(vec_tmp, d / "vectors.npy")

        meta_tmp = d / "meta.json.tmp"
        meta = {
            "version": _META_VERSION,
            "root_key": self._root_key,
            "workspace_root": str(self._ws.root),
            "model_name": snap.model_name,
            "dim": snap.dim,
            "chunk_lines": RAG_CHUNK_LINES,
            "chunk_overlap": RAG_CHUNK_OVERLAP,
            "capped": snap.capped,
            "fingerprints": {k: list(v) for k, v in snap.fingerprints.items()},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(meta_tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(meta_tmp, d / "meta.json")


# ---------------- 进程级注册表与 Provider 单例 ----------------


_PROVIDER: EmbeddingProvider | None = None
_PROVIDER_LOCK = threading.Lock()


def get_provider() -> EmbeddingProvider:
    """进程级 embedding provider 单例（惰性：不触发模型加载）。"""
    global _PROVIDER
    if _PROVIDER is None:
        with _PROVIDER_LOCK:
            if _PROVIDER is None:
                _PROVIDER = FastEmbedProvider(EMBEDDING_MODEL,
                                              cache_dir=EMBEDDING_CACHE_DIR)
    return _PROVIDER


_INDEXES: OrderedDict[str, WorkspaceIndex] = OrderedDict()
_REGISTRY_LOCK = threading.Lock()


def get_workspace_index(ws: Workspace) -> WorkspaceIndex:
    """按工作区根取/建索引（LRU 上限 _SNAPSHOT_LRU，防内存无界增长）。"""
    key = workspace_hash(ws.root)
    with _REGISTRY_LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = WorkspaceIndex(ws)
            _INDEXES[key] = idx
            while len(_INDEXES) > _SNAPSHOT_LRU:
                _INDEXES.popitem(last=False)
        else:
            _INDEXES.move_to_end(key)
        return idx


def reset_registry() -> None:
    """清空注册表（测试用；不影响磁盘索引）。"""
    with _REGISTRY_LOCK:
        _INDEXES.clear()
