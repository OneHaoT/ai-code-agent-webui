"""
阶段1只读工具集：read_file / list_dir / glob / grep。

统一契约
--------
1. 所有工具只访问 Workspace 沙箱内路径，越界 / 不存在 / 类型错误 / IO 错误
   一律返回 ``ToolOutput(status="error")`` 的中文说明，绝不向编排层抛异常；
2. 输出都有硬上限（行数 / 字节 / 条数 / 耗时），截断置 ``truncated=True``；
   单工具执行由独立线程池 + TOOL_TIMEOUT_SECONDS 硬超时兜底（NFR-2）；
3. 仅做读操作与目录列举，不产生任何文件系统写入；
4. 目录遍历对每个子目录/文件做沙箱复检，指向根外的符号链接与 Windows
   junction 一律剪枝/跳过（NFR-1，不依赖 os.walk 的 followlinks 语义）。

上限可用环境变量覆盖（均有默认值，不配置也能运行）：
TOOL_MAX_LINES=2000  TOOL_MAX_BYTES=131072  TOOL_LIST_LIMIT=500
TOOL_GLOB_LIMIT=200  TOOL_GREP_LIMIT=100    TOOL_TIMEOUT_SECONDS=10
"""
from __future__ import annotations

import fnmatch
import heapq
import os
import re
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from pathlib import Path

from workspace import DEFAULT_IGNORE_DIRS, Workspace, WorkspaceViolation


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# ---- 硬上限（NFR-2：成本与稳定） ----
MAX_LINES = _int_env("TOOL_MAX_LINES", 2000)
MAX_BYTES = _int_env("TOOL_MAX_BYTES", 128 * 1024)
LIST_LIMIT = _int_env("TOOL_LIST_LIMIT", 500)
GLOB_LIMIT = _int_env("TOOL_GLOB_LIMIT", 200)
GREP_LIMIT = _int_env("TOOL_GREP_LIMIT", 100)
TIMEOUT_SECONDS = _float_env("TOOL_TIMEOUT_SECONDS", 10.0)
WALK_ENTRY_LIMIT = 20_000          # glob/grep 遍历的目录条目保护
SMALL_FILE_BYTES = 256 * 1024      # 小于此值整文件读入再分行
BINARY_SAMPLE_BYTES = 8_192
GREP_LINE_CHARS = 1_000           # 单条匹配行展示上限
GREP_MAX_LINE_BYTES = 1024 * 1024  # 单行最大匹配字节（防单行 GB 级文件撑爆内存/正则输入）
MAX_OFFSET_LINES = 2_000_000       # read_file offset 硬上限（防巨大 offset 全文空扫）
COUNT_LINES_MAX_BYTES = 5 * 1024 * 1024  # 截断时只对此大小内文件数总行数

# ---- 阶段2：语义检索（RAG） ----
# 与 main.py / indexer.py 读同一 env；false 时工具集不含 search_code（四件套不变）
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
# Windows/macOS 文件系统大小写不敏感：忽略目录名匹配需同步小写化
_IGNORE_NAME_CASE = sys.platform in ("win32", "darwin")

# 工具执行隔离线程池：硬超时只能"放弃等待"（Python 无法安全强杀线程），
# worker 在工具自然结束后回池；超时任务长期占满池时后续工具统一超时失败（fail-closed），
# 不允许挂死请求线程。max_workers 有限，防止失控工具无限起线程
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ro-tool")


@dataclass
class ToolOutput:
    """工具执行结果（也是 tool_result 帧的载荷来源）。"""

    name: str
    status: str            # "success" | "error"
    output: str
    truncated: bool = False

    def to_frame(self, call_id: str | None = None) -> dict:
        d = {"name": self.name, "status": self.status,
             "output": self.output, "truncated": self.truncated}
        if call_id is not None:
            d = {"id": call_id, **d}
        return d


# ---------------- 辅助 ----------------

def _error(name: str, message: str) -> ToolOutput:
    return ToolOutput(name=name, status="error", output=message)


def _human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"


def _looks_binary(sample: bytes) -> bool:
    """采样判定二进制：UTF-8 能解码即文本；不能解码时按控制字符比例兜底。

    注意：
    - 不能按“>=0x80 字节占比”判定——UTF-8 中文几乎全是高位字节，会误伤；
    - 不能把 GBK 解码成功当作文本依据：GBK 双字节规则极宽松，PNG/压缩包
      等二进制常常也能“成功”解码。判定为文本后的输出解码才允许 GBK 兜底。
    """
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    # 走到这里说明不是合法 UTF-8：中文 GBK 文本几乎不含 <0x20 控制字节，
    # 而图片/压缩/目标文件含大量控制区字节；10% 阈值经 PNG/源码样本校准。
    controls = sum(1 for b in sample if b < 0x09 or 0x0B <= b <= 0x1F or b == 0x7F)
    return controls / len(sample) > 0.10


def _decode(raw: bytes) -> str:
    """单行 / 小块解码：UTF-8 优先，GBK 兜底，仍失败替换非法字节。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _as_positive_int(value, name: str, default: int | None = None,
                     maximum: int | None = None) -> tuple[int | None, str | None]:
    """把 JSON 来的参数安全转成正整数；返回 (值, 错误说明)。"""
    if value is None:
        return default, None
    if isinstance(value, bool) or isinstance(value, str):
        # bool 是 int 子类需显式拒绝；数字字符串不做隐式容错（JSON 入参应为数字）
        return None, f"参数 {name} 必须是整数"
    if isinstance(value, float) and not value.is_integer():
        return None, f"参数 {name} 必须是整数"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, f"参数 {name} 必须是整数"
    if n < 1:
        return None, f"参数 {name} 必须 >= 1"
    if maximum is not None and n > maximum:
        n = maximum  # 超出硬上限钳制而非报错
    return n, None


def _resolve_target(ws: Workspace, path, name: str, *, expect: str | None = None):
    """统一路径校验。返回 (target, None) 或 (None, ToolOutput)。"""
    if path is not None and not isinstance(path, str):
        return None, _error(name, "参数 path 必须是字符串")
    try:
        target = ws.resolve(path)
    except WorkspaceViolation:
        return None, _error(name, "路径越出工作区边界，访问已拒绝")
    if not target.exists():
        return None, _error(name, f"路径不存在：{path or '.'}")
    if expect == "file" and not target.is_file():
        kind = "目录" if target.is_dir() else "特殊文件"
        hint = "（列出目录请用 list_dir）" if target.is_dir() else ""
        return None, _error(name, f"目标是{kind}而非文件：{path}{hint}")
    if expect == "dir" and not target.is_dir():
        return None, _error(name, f"目标不是目录：{path}")
    return target, None


# ---------------- read_file ----------------

def _is_readable_document(suffix: str) -> bool:
    """当前已知可转文本的文档扩展名。"""
    return suffix.lower() in {".pdf"}


def _extract_pdf_text(path: Path) -> list[str] | None:
    """用 PyMuPDF 把 PDF 每页提文本并按页分行；失败返回 None。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        doc = fitz.open(str(path))
        lines: list[str] = []
        for page_idx, page in enumerate(doc):
            page_no = page_idx + 1
            text = page.get_text("text") or ""
            for line in text.splitlines():
                if line.strip():
                    lines.append(f"[p{page_no}] {line}")
        doc.close()
        return lines
    except Exception as e:
        return None


def read_file(ws: Workspace, path: str, offset=None, limit=None) -> ToolOutput:
    name = "read_file"
    target, err = _resolve_target(ws, path, name, expect="file")
    if err is not None:
        return err

    offset_v, bad = _as_positive_int(offset, "offset", default=1,
                                     maximum=MAX_OFFSET_LINES)
    if bad:
        return _error(name, bad)
    limit_v, bad = _as_positive_int(limit, "limit", default=MAX_LINES,
                                    maximum=MAX_LINES)
    if bad:
        return _error(name, bad)

    try:
        size = target.stat().st_size
        # ---- PDF 分支 ----
        suffix = target.suffix
        if _is_readable_document(suffix):
            pdf_lines = _extract_pdf_text(target)
            if pdf_lines is None:
                return _error(name, f"PDF 解析失败（需 PyMuPDF 依赖且文件未损坏）：{ws.relative(target)}")
            # PDF 走纯文本行流程
            return _format_lines_output(
                name=name, ws=ws, target=target, size=size,
                lines=pdf_lines, offset_v=offset_v, limit_v=limit_v,
                start_line=offset_v, unit_hint="行（PDF 提取）")

        # ---- 普通文本分支 ----
        with target.open("rb") as f:
            sample = f.read(MAX_BYTES)
        if _looks_binary(sample):
            return _error(name, f"疑似二进制文件，已拒绝读取：{ws.relative(target)}")

        selected: list[bytes] = []
        byte_count = 0
        truncated = False
        with target.open("rb") as f:
            for lineno, raw in enumerate(f, start=1):
                if lineno < offset_v:
                    continue
                if len(selected) >= limit_v or byte_count >= MAX_BYTES:
                    truncated = True
                    break
                window = raw.rstrip(b"\r\n")
                if b"\x00" in window:
                    return _error(
                        name,
                        f"疑似二进制内容，已拒绝读取：{ws.relative(target)}"
                        f"（第 {lineno} 行附近检测到非文本字节）")
                selected.append(window)
                byte_count += len(raw)
        total_hint = ""
        if truncated and size <= COUNT_LINES_MAX_BYTES:
            with target.open("rb") as f:
                total = sum(1 for _ in f)
            total_hint = f"，文件共 {total} 行"
        # 解码成字符串行，统一交给 _format_lines_output 处理
        # 文本分支已在上面按 offset_v 跳过前缀，selected[0] 就是源文件第 offset_v 行
        text_lines = [_decode(b) for b in selected]
        return _format_lines_output(
            name=name, ws=ws, target=target, size=size,
            lines=text_lines, offset_v=1, limit_v=limit_v,
            start_line=offset_v,
            truncated_early=truncated, total_hint=total_hint)
    except OSError as e:
        return _error(name, f"读取文件失败：{e}")


def _format_lines_output(*, name: str, ws: Workspace, target: Path,
                         size: int, lines: list[str], offset_v: int,
                         limit_v: int, start_line: int,
                         truncated_early: bool = False,
                         total_hint: str = "", unit_hint: str = "行") -> ToolOutput:
    """把一组字符串行按 offset/limit 切片、格式化 header/body，生成 ToolOutput。

    start_line: 传入 lines 列表第一行在源文件中的原始行号（文本分支已按 offset 跳过
                前缀，PDF 分支未跳过——调用方传实际值即可）。
    """
    end_in_lines = offset_v - 1 + limit_v
    selected = lines[offset_v - 1:end_in_lines]
    truncated = truncated_early or len(lines) > end_in_lines
    if truncated and not total_hint and size <= COUNT_LINES_MAX_BYTES:
        total_hint = f"，文件共 {len(lines)} {unit_hint}"

    rel = ws.relative(target)
    width = len(str(start_line + len(selected) - 1)) if selected else len(str(start_line))
    header = f"[文件] {rel} | {_human_size(size)}{total_hint}"
    if not selected:
        body = "（指定范围内没有内容；offset 可能超过文件行数）" if start_line > 1 else "（空文件）"
    else:
        body = "\n".join(
            f"{str(start_line + i).rjust(width)} | {line}"
            for i, line in enumerate(selected)
        )
    if truncated:
        body += (f"\n…（仅显示自第 {start_line} 行起最多 {limit_v} 行 / "
                 f"{MAX_BYTES // 1024}KB 内容{total_hint}，需要后续内容请加大 offset 续读）")
    return ToolOutput(name=name, status="success",
                      output=f"{header}\n{body}", truncated=truncated)


# ---------------- list_dir ----------------

def _listdir_entries(target: Path):
    """流式产出目录直接子项 ``(group, lower_name, path, kind, size)``。

    group：0=目录 1=文件/链接（与排序契约一致：目录在前、组内按名称排序）。
    一次 lstat 只取条目自身元数据：符号链接（无论指向根内/根外、文件/目录）
    统一只展示 "[链接]" 与名称——不 resolve、不跟随后缀斜杠、不泄露目标
    类型/大小/存在性等沙箱外元数据。
    """
    for p in target.iterdir():
        try:
            st = p.lstat()
        except OSError:
            yield (1, p.name.lower(), p, "file", None)
            continue
        if stat.S_ISLNK(st.st_mode):
            yield (1, p.name.lower(), p, "link", None)
        elif stat.S_ISDIR(st.st_mode):
            yield (0, p.name.lower(), p, "dir", None)
        else:
            yield (1, p.name.lower(), p, "file", st.st_size)


def list_dir(ws: Workspace, path: str = ".") -> ToolOutput:
    name = "list_dir"
    target, err = _resolve_target(ws, path, name, expect="dir")
    if err is not None:
        return err
    try:
        # 计数 O(1) 内存；展示只保留排序后前 LIST_LIMIT 项。
        # heapq.nsmallest 等价 sorted(iterable, key=key)[:k]，时间 O(n log k)、
        # 内存 O(k)——百万条目目录不再全量物化（阶段1 遗留 N1 修复），
        # 输出与全量排序后截断逐字节一致（sorted 稳定，同名大小写并列次序不变）。
        total = sum(1 for _ in target.iterdir())
        top = heapq.nsmallest(
            LIST_LIMIT, _listdir_entries(target), key=lambda t: (t[0], t[1]))
    except OSError as e:
        return _error(name, f"列出目录失败：{e}")

    truncated = total > LIST_LIMIT

    tag_map = {"dir": "[目录]", "file": "[文件]", "link": "[链接]"}
    lines = [f"[目录] {ws.relative(target) or '.'}/"]
    for _group, _key, p, kind, size in top:
        tag = tag_map[kind]
        suffix = "/" if kind == "dir" else ""
        size_text = f" ({_human_size(size)})" if size is not None else ""
        lines.append(f"  {tag} {p.name}{suffix}{size_text}")
    lines.append(f"共 {total} 项"
                 + (f"，仅显示前 {LIST_LIMIT} 项（请进入子目录查看）" if truncated else ""))
    return ToolOutput(name=name, status="success",
                      output="\n".join(lines), truncated=truncated)


# ---------------- glob ----------------

def _compile_glob(pattern: str) -> re.Pattern:
    """把 glob 表达式翻译为“相对 POSIX 路径”的完整匹配正则。

    语义：``*`` / ``?`` 不跨目录分隔符；``**/`` 匹配零或多层目录；
    ``/**`` 匹配目录内任意深度；裸 ``**`` 匹配一切。
    """
    p = pattern.replace("\\", "/").strip()
    out = ["^"]
    i = 0
    while i < len(p):
        c = p[i]
        if c == "*" and i + 1 < len(p) and p[i + 1] == "*":
            if i + 2 >= len(p):                       # 结尾 **
                out.append(".*")
                i += 2
            elif p[i + 2] == "/":                     # **/
                out.append("(?:.*/)?")
                i += 3
            else:                                    # **xxx 退化为跨层 *
                out.append(".*")
                i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


class _WalkCapped(Exception):
    """内部控制流：遍历条目数触达 WALK_ENTRY_LIMIT，不对外暴露。"""


def _iter_files(ws: Workspace, base: Path, ignore_dirs=frozenset(DEFAULT_IGNORE_DIRS)):
    """自上而下遍历文件，产出已通过沙箱复检的绝对 Path。

    安全要点（NFR-1，逐层防御）：
    - ``os.walk(followlinks=False)`` 只阻止"符号链接目录"的递归；指向文件的
      符号链接照样出现在 files 中，且 Windows 目录联接（junction，创建无需
      管理员）不被 Python 识别为 symlink、会被当普通目录递归进入；
    - 因此对每个子目录和文件都做一次 ``ws.is_within`` 复检：
      dirs 解析后越界直接剪枝（不进入 junction 目标），files 越界直接跳过
      （不 stat / 不 read_bytes，根外字节零接触）；
    - 依赖目录名（.git/node_modules/.venv/target 等）一律剪枝；
      Windows/macOS 文件系统大小写不敏感，按平台对目录名做小写比较，
      防止 .GIT / Node_Modules 之类变体漏剪（仅性能卫生项，安全仍由
      is_within 逐条目复检兜底）；
    - 遍历条目数受 WALK_ENTRY_LIMIT 保护，触顶抛 _WalkCapped 由调用方置截断提示。
    """
    walked = 0
    for root, dirs, files in os.walk(base, followlinks=False):
        kept_dirs = []
        for d in dirs:
            d_key = d.lower() if _IGNORE_NAME_CASE else d
            if d_key in ignore_dirs:
                continue
            if not ws.is_within(Path(root) / d):
                continue  # 指向根外的链接目录 / junction：剪枝，绝不递归
            kept_dirs.append(d)
        dirs[:] = sorted(kept_dirs)
        for fname in files:
            f = Path(root) / fname
            if not ws.is_within(f):
                continue  # 根内符号链接文件指向根外：跳过，不触碰目标字节
            yield f
            walked += 1
            if walked >= WALK_ENTRY_LIMIT:
                raise _WalkCapped


def glob(ws: Workspace, pattern: str, path: str = ".") -> ToolOutput:
    name = "glob"
    if not isinstance(pattern, str) or not pattern.strip():
        return _error(name, "参数 pattern 必须是非空字符串，例如 **/*.py")
    target, err = _resolve_target(ws, path, name, expect="dir")
    if err is not None:
        return err
    try:
        regex = _compile_glob(pattern)
    except re.error as e:
        return _error(name, f"glob 表达式非法：{e}")

    matches: list[str] = []
    truncated = False
    capped = False
    try:
        for f in _iter_files(ws, target):
            rel = os.path.relpath(f, target).replace("\\", "/")
            if not regex.fullmatch(rel):
                continue
            # _iter_files 已做沙箱复检（含符号链接文件/junction 目录），命中即可收集
            matches.append(rel)
            if len(matches) >= GLOB_LIMIT:
                truncated = True
                break
    except _WalkCapped:
        capped = True
    except OSError as e:
        return _error(name, f"遍历目录失败：{e}")

    truncated = truncated or capped
    matches.sort()
    if not matches:
        output = f"在 {ws.relative(target) or '.'}/ 下没有匹配 {pattern} 的文件"
    else:
        notes = []
        if len(matches) >= GLOB_LIMIT:
            notes.append(f"仅显示前 {GLOB_LIMIT} 个（请用 path 缩小范围）")
        if capped:
            notes.append(f"遍历条目超过 {WALK_ENTRY_LIMIT:,}，结果可能不完整")
        head = f"[glob] {pattern} @ {ws.relative(target) or '.'}/"
        tail = f"共 {len(matches)} 个匹配" + ("，" + "；".join(notes) if notes else "")
        output = head + "\n" + "\n".join(matches) + "\n" + tail
    return ToolOutput(name=name, status="success", output=output,
                      truncated=truncated)


# ---------------- grep ----------------

def _iter_grep_files(ws: Workspace, base: Path, file_glob: str | None, deadline: float):
    """产出待搜文件绝对路径（均已通过遍历层沙箱复检）；path 为文件时只搜该文件。"""
    if base.is_file():
        yield base
        return
    for f in _iter_files(ws, base):
        if time.monotonic() > deadline:
            return
        if file_glob and not fnmatch.fnmatch(f.name, file_glob):
            continue
        yield f


def _file_text_lines(path: Path):
    """小文件整读、大文件按二进制行惰性迭代；二进制文件返回 None。

    小文件（≤SMALL_FILE_BYTES）对**全量内容**判二进制，避免"开头合法文本、
    后段夹带二进制"漏判；大文件只采样前段，由调用方对后续行做 NUL 兜底。
    """
    if path.stat().st_size <= SMALL_FILE_BYTES:
        data = path.read_bytes()
        if _looks_binary(data):
            return None
        return [ln for ln in data.splitlines()]
    with path.open("rb") as f:
        sample = f.read(BINARY_SAMPLE_BYTES)
        if _looks_binary(sample):
            return None
    return path.open("rb")


def grep(ws: Workspace, pattern: str, path: str = ".",
         file_glob=None, ignore_case=None) -> ToolOutput:
    name = "grep"
    if not isinstance(pattern, str) or not pattern.strip():
        return _error(name, "参数 pattern 必须是非空正则表达式")
    if file_glob is not None and not isinstance(file_glob, str):
        return _error(name, "参数 file_glob 必须是字符串，例如 *.py")
    flags = re.IGNORECASE if ignore_case is True else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return _error(name, f"正则表达式非法：{e}")

    target, err = _resolve_target(ws, path, name)
    if err is not None:
        return err
    deadline = time.monotonic() + TIMEOUT_SECONDS

    hits: list[str] = []
    files_searched = 0
    skipped = 0
    truncated = False
    timed_out = False
    capped = False
    handle = None
    try:
        for f in _iter_grep_files(ws, target, file_glob, deadline):
            if time.monotonic() > deadline:
                timed_out = True
                break
            handle = None
            try:
                lines = _file_text_lines(f)
                if lines is None:
                    continue  # 二进制跳过
                handle = lines if not isinstance(lines, list) else None
                files_searched += 1
                rel = ws.relative(f)
                file_binary_tail = False
                for lineno, raw in enumerate(lines, start=1):
                    # 先把行收敛到"可能进入匹配/输出"的字节窗口，再在
                    # **同一窗口**内查 NUL：保证任何会被解码输出的字节都经过
                    # 二进制复检（窗口外的部分会被截断丢弃，不可能泄露密文）
                    if len(raw) > GREP_MAX_LINE_BYTES:
                        raw = raw[:GREP_MAX_LINE_BYTES]
                    if b"\x00" in raw:
                        file_binary_tail = True
                        break
                    text = _decode(raw).rstrip("\r\n")
                    if regex.search(text):
                        if len(text) > GREP_LINE_CHARS:
                            text = text[:GREP_LINE_CHARS] + "…"
                        hits.append(f"{rel}:{lineno}: {text}")
                        if len(hits) >= GREP_LIMIT:
                            truncated = True
                            break
                    if lineno % 200 == 0 and time.monotonic() > deadline:
                        timed_out = True
                        break
                if file_binary_tail:
                    continue
                if truncated or timed_out:
                    break
            except OSError:
                # 悬空符号链接 / 权限错误 / 读取中途 IO 错误：跳过该文件继续，
                # 不让单个坏条目终止整次搜索（glob 对同类情况同样 continue）
                skipped += 1
            finally:
                if handle is not None:
                    handle.close()
                    handle = None
    except _WalkCapped:
        capped = True
    except OSError as e:
        return _error(name, f"搜索失败：{e}")

    truncated = truncated or capped
    scope = ws.relative(target)
    if timed_out:
        prefix = "\n".join(hits) + ("\n" if hits else "")
        return _error(
            name,
            f"{prefix}搜索超时（>{TIMEOUT_SECONDS:.0f}s），已搜索 {files_searched} 个文件、"
            f"返回 {len(hits)} 条结果，请用 path/file_glob 缩小范围后重试")
    if not hits:
        output = (f"在 {scope} 下未找到匹配 /{pattern}/ 的内容"
                  + (f"（文件过滤 {file_glob}）" if file_glob else ""))
    else:
        head = f"[grep] /{pattern}/ @ {scope}" + (f" [{file_glob}]" if file_glob else "")
        tail = f"找到 {len(hits)} 处匹配（已搜索 {files_searched} 个文件）"
        notes = []
        if len(hits) >= GREP_LIMIT:
            notes.append(f"仅显示前 {GREP_LIMIT} 处（请缩小范围）")
        if capped:
            notes.append(f"遍历条目超过 {WALK_ENTRY_LIMIT:,}，结果可能不完整")
        if skipped:
            notes.append(f"{skipped} 个文件读取失败已跳过")
        if notes:
            tail += "，" + "；".join(notes)
        output = head + "\n" + "\n".join(hits) + "\n" + tail
    return ToolOutput(name=name, status="success", output=output,
                      truncated=truncated)


# ---------------- search_code（阶段2：语义检索） ----------------

_SEARCH_PREVIEW_LINES = 20     # 每条命中预览行数上限
_SEARCH_PREVIEW_CHARS = 200    # 预览单行字符上限


def search_code(ws: Workspace, query: str, path: str = ".") -> ToolOutput:
    """语义检索：查询向量 vs 索引快照余弦相似度，返回 top-k 命中片段。

    只读语义与四件套一致：查的是索引快照与既有文件预览，零写入。
    索引刷新由 indexer 后台线程执行（绝不占工具超时池做重活）：
    ensure_ready 最多同步等 RAG_BUILD_WAIT_SECONDS，等不到返回"构建中"
    （success 状态，由模型决定稍后重试或直接作答，不杀流）。
    """
    name = "search_code"
    if not isinstance(query, str) or not query.strip():
        return _error(name, "参数 query 必须是非空字符串")
    target, err = _resolve_target(ws, path, name)  # 允许目录或单个文件
    if err is not None:
        return err

    # 延迟导入：RAG_ENABLED=false 时 tools 模块保持零 RAG 依赖
    from embedding import EmbeddingError
    from indexer import get_workspace_index

    idx = get_workspace_index(ws)
    try:
        status = idx.ensure_ready()  # 冷启动时模型下载/加载在后台线程进行
    except Exception as e:  # noqa: BLE001 —— 防御：任何索引异常都不杀流
        return _error(name, f"索引不可用：{e}")
    if status == "error":
        return _error(name, idx.last_error or "索引构建失败")
    if status == "building":
        return ToolOutput(
            name=name, status="success", truncated=False, output=(
                f"[search_code] {query.strip()} @ {ws.relative(target) or '.'}/\n"
                "索引正在后台构建（首次检索需遍历并编码工作区文件），本轮暂无结果。\n"
                "请稍后重新调用本工具，或先用 glob/grep 定位。"))

    try:
        qvec = idx.provider.embed_query(query)
    except EmbeddingError as e:
        return _error(name, str(e))

    rel = ws.relative(target)
    # 根目录的 relative 返回 "."，归一化为空前缀（否则过滤掉全部命中）
    prefix = "" if rel in ("", ".") else rel.replace("\\", "/").strip("/")
    hits = idx.search(qvec, path_prefix=prefix)
    snap = idx.snapshot

    head = f"[search_code] {query.strip()} @ {prefix + '/' if prefix else './'}"
    if not hits:
        output = (head + "\n无命中。可尝试换更具体的表述（如涉及的关键类型/函数名），"
                  "或改用 grep 做精确文本匹配。")
        return ToolOutput(name=name, status="success", truncated=False, output=output)

    lines = [head, f"共 {len(hits)} 个命中（相关度降序）：", ""]
    for rank, hit in enumerate(hits, start=1):
        c = hit.chunk
        lines.append(f"{rank}. {c.rel_path} L{c.line_start}-{c.line_end}"
                     f" 相关度 {hit.score:.2f}")
        body = c.text.splitlines()[1:]  # 首行是【文件: …】头，预览跳过
        for offset, src_line in enumerate(body[:_SEARCH_PREVIEW_LINES]):
            text = src_line[:_SEARCH_PREVIEW_CHARS]
            lines.append(f"   {c.line_start + offset}: {text}")
        if len(body) > _SEARCH_PREVIEW_LINES:
            lines.append(f"   …（该片段共 {len(body)} 行，仅预览前 "
                         f"{_SEARCH_PREVIEW_LINES} 行，可用 read_file 续读）")
        lines.append("")
    notes = []
    if snap is not None and snap.capped:
        notes.append("索引不完整（工作区超过 chunk 上限，仅部分文件入索引）")
    if notes:
        lines.append("；".join(notes))
    output = "\n".join(lines).rstrip() + "\n"

    truncated = False
    raw = output.encode("utf-8")
    if len(raw) > MAX_BYTES:
        output = raw[:MAX_BYTES].decode("utf-8", errors="ignore") \
            + "\n…（输出超限截断）"
        truncated = True
    return ToolOutput(name=name, status="success", truncated=truncated,
                      output=output)


# ---------------- OpenAI 工具声明与分发（供 agent 使用） ----------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取工作区内某个文本文件的内容（只读）。返回带行号的文本，"
                "默认最多 2000 行，超长内容被截断时可用 offset 续读。"
                "二进制文件会被拒绝。路径相对于工作区根。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内文件的相对路径，如 ai/main.py"},
                    "offset": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
                    "limit": {"type": "integer", "description": "读取行数上限，默认 2000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "列出工作区内某个目录的直接子项（单层、不递归），"
                "区分目录/文件/链接并给文件大小。路径相对于工作区根，默认为根。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录相对路径，默认工作区根"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "按 glob 模式查找工作区内的文件路径，例如 **/*.py 递归查找、"
                "*.md 只匹配根层。返回匹配文件的相对路径排序列表。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，支持 *、?、**"},
                    "path": {"type": "string", "description": "限定搜索的子目录，默认工作区根"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "在工作区内按 Python 正则逐行搜索文件内容，返回 “相对路径:行号:匹配行”。"
                "默认忽略 .git/node_modules/.venv/target/dist 等目录与二进制文件。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python 正则表达式"},
                    "path": {"type": "string", "description": "搜索起始目录或单个文件，默认工作区根"},
                    "file_glob": {"type": "string", "description": "文件名通配过滤，如 *.py"},
                    "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
                },
                "required": ["pattern"],
            },
        },
    },
]

_BASE_TOOL_SCHEMAS = TOOL_SCHEMAS

# 阶段2：语义检索工具（RAG_ENABLED=false 时整体不进工具集，四件套不受影响）
if RAG_ENABLED:
    TOOL_SCHEMAS = TOOL_SCHEMAS + [
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": (
                    "按语义（自然语言）检索工作区代码，返回最相关的代码片段"
                    "（含文件路径、行号范围、相关度与内容预览）。"
                    "适合“某某逻辑在哪实现/处理”这类模糊问题；"
                    "精确关键词匹配请用 grep。首次调用会自动构建索引，"
                    "索引未就绪时返回构建中提示，稍后重试即可。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "自然语言查询，如“登录校验逻辑在哪”"},
                        "path": {"type": "string",
                                 "description": "限定检索的子目录或单个文件，默认整个工作区"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]

_DISPATCH = {
    "read_file": read_file,
    "list_dir": list_dir,
    "glob": glob,
    "grep": grep,
}
if RAG_ENABLED:
    _DISPATCH["search_code"] = search_code


def execute_tool(ws: Workspace, name: str, args) -> ToolOutput:
    """编排层统一入口：任何异常都收敛为 error 工具结果，不允许杀整轮流。

    所有工具在隔离线程池中执行并施加 TOOL_TIMEOUT_SECONDS 硬超时：
    灾难性正则回溯 / 网络盘 IO 挂起等无法在工具内部自检的场景，由
    future.result(timeout) 统一兜底，保证请求线程以确定的 error 结果终结。
    Python 不能安全强杀线程：超时后仅放弃等待，worker 自然结束后回池。
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return _error(name or "<unknown>", f"未知工具：{name}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error(name, "工具参数必须是 JSON 对象")
    future = _TOOL_EXECUTOR.submit(fn, ws, **args)
    try:
        return future.result(timeout=TIMEOUT_SECONDS)
    except FuturesTimeout:
        future.cancel()
        return _error(
            name,
            f"工具执行超时（>{TIMEOUT_SECONDS:.0f}s），已终止等待；"
            "请缩小 path 范围或简化正则后重试")
    except TypeError as e:
        return _error(name, f"工具参数不合法：{e}")
    except Exception as e:  # noqa: BLE001 —— 工具层最后的收敛防线
        return _error(name, f"工具执行异常：{type(e).__name__}: {e}")
