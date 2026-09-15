"""
阶段1只读工具自测：工作区沙箱 + read_file/list_dir/glob/grep。

不调用真实大模型、不依赖真实项目目录，全部用 pytest 的 tmp_path 构造隔离沙箱。
运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_tools.py -q
"""
import builtins
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
from workspace import Workspace  # noqa: E402


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def write(path, text="", encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def make_file_symlink(target, link) -> bool:
    """创建文件符号链接；当前权限不允许时返回 False（调用方 skip）。"""
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError):
        return False


def make_dir_link(target, link) -> bool:
    """创建指向目录的链接：优先真正的 symlink，Windows 退化为 junction。

    junction（mklink /J）创建不需要管理员权限，且不被 CPython 识别为
    symlink——正是生产遍历层必须靠 resolve 复检剪枝的对象。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import subprocess
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True)
        return r.returncode == 0 and link.exists()
    return False


# ---------------- read_file ----------------

def test_read_file_basic_and_range(ws):
    write(ws.root / "a.txt", "l1\nl2\nl3\n")
    out = tools.read_file(ws, "a.txt")
    assert out.status == "success"
    assert out.truncated is False
    assert "1 | l1" in out.output and "3 | l3" in out.output

    sub = tools.read_file(ws, "a.txt", offset=2, limit=1)
    assert sub.status == "success"
    assert "2 | l2" in sub.output
    assert "l1" not in sub.output and "l3" not in sub.output


def test_read_file_chinese_utf8(ws):
    write(ws.root / "cn.txt", "你好，世界\n第二行")
    out = tools.read_file(ws, "cn.txt")
    assert out.status == "success"
    assert "你好，世界" in out.output


def test_read_file_offset_beyond_eof(ws):
    write(ws.root / "a.txt", "only\n")
    out = tools.read_file(ws, "a.txt", offset=99)
    assert out.status == "success"
    assert "没有内容" in out.output


def test_read_file_missing_and_directory(ws):
    assert tools.read_file(ws, "nope.txt").status == "error"
    (ws.root / "d").mkdir()
    out = tools.read_file(ws, "d")
    assert out.status == "error" and "目录" in out.output


def test_read_file_bad_params(ws):
    write(ws.root / "a.txt", "x\n")
    assert tools.read_file(ws, "a.txt", offset=0).status == "error"
    assert tools.read_file(ws, "a.txt", offset="2").status == "error"
    assert tools.read_file(ws, 123).status == "error"


def test_read_file_binary_rejected(ws):
    # PNG 签名 + 足够多的非文本字节
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(1, 200)) * 20
    (ws.root / "pic.png").write_bytes(data)
    out = tools.read_file(ws, "pic.png")
    assert out.status == "error"
    assert "二进制" in out.output


def test_read_file_truncated_2000_lines(ws):
    write(ws.root / "big.txt", "".join(f"line{i}\n" for i in range(1, 2002)))
    out = tools.read_file(ws, "big.txt")
    assert out.status == "success"
    assert out.truncated is True
    # 恰好 2000 个带行号的行（行首才是行号，header 里的 " | " 不算）
    numbered = [ln for ln in out.output.splitlines()
                if ln.lstrip()[:1].isdigit() and " | " in ln]
    assert len(numbered) == 2000
    assert "共 2001 行" in out.output
    assert "offset" in out.output


# ---------------- 沙箱边界 ----------------

def test_parent_traversal_blocked(ws, tmp_path):
    secret = write(tmp_path / "secret.txt", "SUPERSECRET-123")
    out = tools.read_file(ws, "../secret.txt")
    assert out.status == "error"
    assert "边界" in out.output
    assert "SUPERSECRET-123" not in out.output
    assert secret.read_text(encoding="utf-8") == "SUPERSECRET-123"


def test_absolute_outside_path_blocked(ws, tmp_path):
    write(tmp_path / "outside.txt", "OUTSIDE-SECRET")
    out = tools.read_file(ws, str(tmp_path / "outside.txt"))
    assert out.status == "error"
    assert "OUTSIDE-SECRET" not in out.output


def test_outside_file_never_opened(ws, tmp_path, monkeypatch):
    write(tmp_path / "secret.txt", "SUPERSECRET")
    real_open = builtins.open
    opened_paths = []

    def spy_open(file, *args, **kwargs):
        opened_paths.append(os.fspath(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    out = tools.read_file(ws, "../secret.txt")
    assert out.status == "error"
    # resolve 边界检查在任何 open() 之前，越界访问不应产生任何 open 调用
    assert opened_paths == []


def test_symlink_escape_blocked(ws, tmp_path):
    write(tmp_path / "target.txt", "LINK-TARGET-SECRET")
    link = ws.root / "evil.lnk"
    if not make_file_symlink(tmp_path / "target.txt", link):
        pytest.skip("当前 Windows 权限不允许创建符号链接，跳过 symlink 越界用例")
    out = tools.read_file(ws, "evil.lnk")
    assert out.status == "error"
    assert "LINK-TARGET-SECRET" not in out.output


def test_symlink_file_escape_skipped_in_walk(ws, tmp_path):
    """遍历型工具必须跳过根内指向根外的文件符号链接（NFR-1 Blocker 回归）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside / "secret.txt", "WALK-SECRET-LINK")
    link = ws.root / "secret-link.txt"
    if not make_file_symlink(outside / "secret.txt", link):
        pytest.skip("当前 Windows 权限不允许创建符号链接，跳过 symlink 遍历用例")
    g = tools.glob(ws, "**/*.txt", ".")
    assert g.status == "success"
    assert "secret-link" not in g.output
    assert "WALK-SECRET-LINK" not in g.output
    gr = tools.grep(ws, "WALK-SECRET-LINK", ".")
    assert gr.status == "success"
    assert gr.output.startswith("在 . 下未找到")  # 回显含 pattern，按"无命中行"判定
    assert "secret-link" not in gr.output
    # list_dir：链接名作为根内目录项可见，但元数据不跟随（无大小/斜杠）
    ls = tools.list_dir(ws, ".")
    assert "[链接] secret-link.txt" in ls.output


def test_junction_dir_escape_pruned_in_walk(ws, tmp_path):
    """Windows 目录联接不被 is_symlink 识别，遍历层必须靠 resolve 剪枝。"""
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    write(outside / "junction-secret.txt", "JUNCTION-SECRET-XYZ")
    link = ws.root / "linkdir"
    if not make_dir_link(outside, link):
        pytest.skip("无法创建目录 symlink/junction，跳过联接剪枝用例")
    try:
        g = tools.glob(ws, "**/*.txt", ".")
        assert g.status == "success"
        assert "junction-secret" not in g.output
        assert "linkdir" not in g.output  # 联接目录被剪枝：名字与目标内容都不出现
        gr = tools.grep(ws, "JUNCTION-SECRET-XYZ", ".")
        assert gr.status == "success"
        assert gr.output.startswith("在 . 下未找到")  # 注意：未找到回显含 pattern 本身
        assert "linkdir/" not in gr.output
        # 单层列表不递归：联接以目录项出现（根内名字），但目标内容绝不出现
        ls = tools.list_dir(ws, ".")
        assert "linkdir" in ls.output
        assert "junction-secret" not in ls.output
    finally:
        # 只删 reparse point 本身（os.rmdir 不触碰 junction 目标目录）
        try:
            os.rmdir(link)
        except OSError:
            pass


# ---------------- list_dir ----------------

def test_list_dir_single_level_sorted(ws):
    write(ws.root / "a.txt", "a")
    write(ws.root / "b.txt", "bb")
    write(ws.root / "d" / "x.txt", "x")  # 子目录内容不应出现在单层列表
    out = tools.list_dir(ws, ".")
    assert out.status == "success"
    lines = out.output.splitlines()
    assert "[目录] d/" in lines[1]                      # 目录排在前
    assert any("[文件] a.txt" in ln for ln in lines)
    assert any("(2 B)" in ln for ln in lines if "b.txt" in ln)
    assert "x.txt" not in out.output                    # 不递归
    assert "共 3 项" in out.output


def test_list_dir_missing(ws):
    assert tools.list_dir(ws, "no-such-dir").status == "error"


# ---------------- glob ----------------

def test_glob_recursive_and_single_level(ws):
    for rel in ("a.py", "x.js", "d/b.py", "d/d2/c.py"):
        write(ws.root / rel, "#")
    rec = tools.glob(ws, "**/*.py", ".")
    assert rec.status == "success"
    for rel in ("a.py", "d/b.py", "d/d2/c.py"):
        assert rel in rec.output
    assert "x.js" not in rec.output
    assert "共 3 个匹配" in rec.output

    top = tools.glob(ws, "*.py", ".")
    assert "a.py" in top.output
    assert "b.py" not in top.output                      # 单层语义


def test_glob_ignores_dependency_dirs_and_no_escape(ws, tmp_path):
    write(ws.root / "node_modules" / "pkg" / "x.js", "x")
    write(tmp_path / "outer.py", "outer")
    out = tools.glob(ws, "**/*.js", ".")
    assert out.status == "success"
    assert "node_modules" not in out.output
    assert "outer.py" not in out.output


def test_ignore_dirs_name_case_follows_platform(ws):
    """.GIT / NODE_MODULES 等大写变体：大小写不敏感平台必须同样剪枝（N5b）。"""
    write(ws.root / "NODE_MODULES" / "pkg" / "x.js", "x")
    write(ws.root / ".GIT" / "refs" / "y", "y")
    out = tools.glob(ws, "**/*", ".")
    if tools._IGNORE_NAME_CASE:
        assert "NODE_MODULES" not in out.output
        assert ".GIT" not in out.output
    else:
        # Linux 大小写敏感文件系统：大写变体是不同的普通目录，正常可见
        assert "NODE_MODULES" in out.output


def test_glob_no_match(ws):
    write(ws.root / "a.txt", "x")
    out = tools.glob(ws, "**/*.java", ".")
    assert out.status == "success"
    assert "没有匹配" in out.output


# ---------------- grep ----------------

def test_grep_basic_case_and_glob(ws):
    write(ws.root / "a.py", "print('hello world')\n# HELLO again\n")
    write(ws.root / "b.txt", "hello in txt\n")

    out = tools.grep(ws, "hello", ".", file_glob="*.py")
    assert out.status == "success"
    assert "a.py:1:" in out.output
    assert "b.txt" not in out.output                     # file_glob 过滤
    assert "HELLO" not in out.output                     # 默认大小写敏感
    assert "找到 1 处" in out.output

    ci = tools.grep(ws, "hello", ".", ignore_case=True)
    assert "HELLO again" in ci.output


def test_grep_ignores_dirs_and_binary(ws):
    write(ws.root / "__pycache__" / "x.py", "TOKEN_HERE")
    (ws.root / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
    write(ws.root / "src.py", "TOKEN_HERE\n")
    out = tools.grep(ws, "TOKEN_HERE", ".")
    assert "__pycache__" not in out.output
    assert "bin.dat" not in out.output
    assert "src.py:1:" in out.output


def test_grep_no_match(ws):
    write(ws.root / "a.txt", "abc\n")
    out = tools.grep(ws, "zzz", ".")
    assert out.status == "success"
    assert "未找到" in out.output


def test_grep_bad_regex_and_params(ws):
    assert tools.grep(ws, "[unterminated", ".").status == "error"
    assert tools.grep(ws, "", ".").status == "error"


def test_grep_truncated_at_100(ws):
    write(ws.root / "many.txt", "".join("hello\n" for _ in range(105)))
    out = tools.grep(ws, "hello", ".")
    assert out.status == "success"
    assert out.truncated is True
    assert out.output.count(": hello") == 100
    assert "前 100 处" in out.output


def test_list_dir_truncated_at_500(ws):
    d = ws.root / "many"
    d.mkdir()
    for i in range(505):
        (d / f"f{i:03d}").touch()
    out = tools.list_dir(ws, "many")
    assert out.status == "success"
    assert out.truncated is True
    assert "共 505 项" in out.output
    assert "仅显示前 500 项" in out.output
    assert out.output.count("[文件]") == 500


def test_glob_truncated_at_200(ws):
    d = ws.root / "many"
    d.mkdir()
    for i in range(205):
        (d / f"f{i:03d}.txt").touch()
    out = tools.glob(ws, "*.txt", "many")
    assert out.status == "success"
    assert out.truncated is True
    assert sum(1 for ln in out.output.splitlines() if ln.endswith(".txt")) == 200
    assert "仅显示前 200 个" in out.output


def test_walk_entry_cap_marks_truncated(ws, monkeypatch):
    """遍历条目触顶不再静默：glob/grep 都置 truncated 并在尾注说明。"""
    monkeypatch.setattr(tools, "WALK_ENTRY_LIMIT", 5)
    for i in range(8):
        write(ws.root / f"f{i}.txt", "TOKMARK\n")
    g = tools.glob(ws, "**/*.txt", ".")
    assert g.status == "success" and g.truncated is True
    assert "遍历条目超过 5" in g.output
    gr = tools.grep(ws, "TOKMARK", ".")
    assert gr.status == "success" and gr.truncated is True
    assert "遍历条目超过 5" in gr.output


def test_read_file_binary_hidden_after_legal_head(ws):
    """二进制 NUL 藏在 8KB 采样区之后：read_file 必须按整个可输出窗口判定。"""
    data = b"hello\n" * 2000  # 约 12KB 合法文本，超过旧的 8KB 采样
    data += b"\x00\x01\x02HIDDEN-BINARY"
    (ws.root / "mixed.dat").write_bytes(data)
    out = tools.read_file(ws, "mixed.dat")
    assert out.status == "error"
    assert "二进制" in out.output
    assert "HIDDEN-BINARY" not in out.output


def test_grep_binary_tail_of_large_file_not_leaked(ws):
    """>256KB 文件后段夹带二进制：惰性逐行扫描遇 NUL 放弃整个文件，密文不泄露。

    夹具体积必须真实超过 SMALL_FILE_BYTES(262144)：6 字节行 × 50000 = 300000，
    走大文件惰性路径（曾误用 40000 行=240000，实际落在小文件全量分支，假绿）。
    """
    data = b"x = 1\n" * 50000  # 300000 字节 > 262144
    data += b"\x00\x00SECRET-BINARY-TAIL\x00\n"
    assert (ws.root / "big.bin.txt").write_bytes(data) > 256 * 1024
    out = tools.grep(ws, "SECRET-BINARY-TAIL", ".")
    assert out.status == "success"
    assert out.output.startswith("在 . 下未找到")  # 回显含 pattern 本身，按"无命中行"判定
    assert "big.bin.txt" not in out.output


def test_grep_late_column_nul_in_long_line_not_leaked(ws):
    """NUL 藏在超长行第 301 列：旧实现 raw[:256] 检查漏判（复审 M1 回归）。"""
    data = b"x = 1\n" * 45000  # 270000 字节，走大文件惰性路径
    data += b"A" * 300 + b"\x00LATE-NUL-SECRET\n"
    assert (ws.root / "late.bin.txt").write_bytes(data) > 256 * 1024
    out = tools.grep(ws, "LATE-NUL-SECRET", ".")
    assert out.status == "success"
    assert out.output.startswith("在 . 下未找到")
    assert "late.bin.txt" not in out.output


def test_read_file_deep_offset_binary_rejected(ws):
    """头部 128KB 全合法、深 offset 行夹带 NUL：read_file 输出窗口逐行复检拒读（M2）。"""
    data = b"x = 1\n" * 45000  # 270000 字节，头部采样区全是合法文本
    data += b"\x00DEEP-OFFSET-SECRET\n"
    (ws.root / "deep.dat").write_bytes(data)
    # 默认从前读：头部窗口合法，正常返回且不含深行密文
    head = tools.read_file(ws, "deep.dat")
    assert head.status == "success"
    assert "DEEP-OFFSET-SECRET" not in head.output
    # 深 offset 直达二进制行：必须 error（旧实现头部采样覆盖不到而漏判）
    deep = tools.read_file(ws, "deep.dat", offset=45001)
    assert deep.status == "error"
    assert "二进制" in deep.output
    assert "DEEP-OFFSET-SECRET" not in deep.output


def test_grep_dangling_symlink_does_not_fail(ws, tmp_path):
    """悬空链接（遍历层跳过）不影响同目录正常文件的搜索。"""
    link = ws.root / "dangling.txt"
    if not make_file_symlink(tmp_path / "never-exists.txt", link):
        pytest.skip("当前 Windows 权限不允许创建符号链接，跳过悬空链接用例")
    write(ws.root / "real.txt", "REALTOKEN\n")
    out = tools.grep(ws, "REALTOKEN", ".")
    assert out.status == "success"
    assert "real.txt:1:" in out.output
    assert "dangling" not in out.output


def test_grep_file_oserror_is_skipped_not_fatal(ws, monkeypatch):
    """单文件读取 OSError（权限/IO）：计数跳过继续，不终止整次搜索。"""
    write(ws.root / "a.txt", "TOKEN-A\n")
    write(ws.root / "b.txt", "TOKEN-B\n")
    real_open = tools._file_text_lines

    def flaky(path):
        if path.name == "a.txt":
            raise OSError("simulated io error")
        return real_open(path)

    monkeypatch.setattr(tools, "_file_text_lines", flaky)
    out = tools.grep(ws, r"TOKEN-[AB]", ".")
    assert out.status == "success"
    assert "a.txt" not in out.output
    assert "b.txt:1:" in out.output
    assert "1 个文件读取失败已跳过" in out.output
    assert out.truncated is False


# ---------------- 分发入口 ----------------

def test_execute_tool_dispatch(ws):
    write(ws.root / "a.txt", "hello\n")
    ok = tools.execute_tool(ws, "read_file", {"path": "a.txt"})
    assert ok.status == "success" and "hello" in ok.output

    assert tools.execute_tool(ws, "rm_rf", {"path": "."}).status == "error"
    # 缺少必填 path：TypeError 收敛为 error 结果回传模型，由模型修正后重试
    missing = tools.execute_tool(ws, "read_file", None)
    assert missing.status == "error" and "path" in missing.output
    assert tools.execute_tool(ws, "read_file", "not-an-object").status == "error"


def test_path_optional_defaults_to_workspace_root(ws):
    """schema 已不强制 path：glob/grep 省略 path 时默认搜索工作区根。"""
    write(ws.root / "a.txt", "TOKENZZZ\n")
    gr = tools.execute_tool(ws, "grep", {"pattern": "TOKENZZZ"})
    assert gr.status == "success" and "a.txt" in gr.output
    gl = tools.execute_tool(ws, "glob", {"pattern": "*.txt"})
    assert gl.status == "success" and "a.txt" in gl.output


def test_execute_tool_unexpected_exception_is_contained(ws, monkeypatch):
    """工作线程内的任意未知异常都收敛为 error 工具结果（防兜底回归）。"""
    def boom(ws_arg, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(tools._DISPATCH, "boom_tool", boom)
    out = tools.execute_tool(ws, "boom_tool", {})
    assert out.status == "error"
    assert "工具执行异常" in out.output and "disk on fire" in out.output


def test_execute_tool_hard_timeout(ws, monkeypatch):
    """协作式 deadline 无法中断的工具（如灾难正则）：线程池 future 硬超时兜底。"""
    def slow(ws_arg, **kwargs):
        time.sleep(2.0)
        return tools.ToolOutput("slow", "success", "ok")

    monkeypatch.setitem(tools._DISPATCH, "slow_tool", slow)
    monkeypatch.setattr(tools, "TIMEOUT_SECONDS", 0.3)
    t0 = time.monotonic()
    out = tools.execute_tool(ws, "slow_tool", {})
    elapsed = time.monotonic() - t0
    assert out.status == "error" and "超时" in out.output
    # 调用方必须在超时后立刻拿到 error，而不是等满 sleep
    assert elapsed < 1.5


def test_tool_schemas_complete():
    names = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    # 阶段2：RAG_ENABLED 默认 true，search_code 自动装配进工具集
    assert names == {"read_file", "list_dir", "glob", "grep", "search_code"}
    required_map = {
        spec["function"]["name"]:
            spec["function"]["parameters"].get("required", [])
        for spec in tools.TOOL_SCHEMAS
    }
    # path 实现均有默认值（工作区根），不得在 schema 中误导模型为必填
    assert required_map["read_file"] == ["path"]
    assert required_map["list_dir"] == []
    assert required_map["glob"] == ["pattern"]
    assert required_map["grep"] == ["pattern"]
    assert required_map["search_code"] == ["query"]


# ---------------- read_file：PDF 文档分支（阶段2） ----------------

def _make_pdf(path, pages):
    """用 PyMuPDF 现场生成多页 PDF；pages 是每页的文本行列表（ASCII 保提取可靠）。"""
    import fitz
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line)
            y += 20
    doc.save(str(path))
    doc.close()


def test_read_file_pdf_multi_page(ws):
    """PDF 走 PyMuPDF 提取：按页标注 [pN] 前缀，行号从 1 连续编号。"""
    _make_pdf(ws.root / "doc.pdf", [["alpha first"], ["beta second"]])
    out = tools.read_file(ws, "doc.pdf")
    assert out.status == "success"
    assert "[p1]" in out.output and "alpha first" in out.output
    assert "[p2]" in out.output and "beta second" in out.output
    assert out.truncated is False


def test_read_file_pdf_offset_limit(ws):
    """PDF 同样支持 offset/limit 续读：offset 按提取后的连续行计数。"""
    _make_pdf(ws.root / "doc.pdf", [["line-one", "line-two", "line-three"]])
    out = tools.read_file(ws, "doc.pdf", offset=2, limit=1)
    assert out.status == "success"
    assert "2 | [p1] line-two" in out.output
    assert "line-one" not in out.output and "line-three" not in out.output


def test_read_file_pdf_corrupted_is_error(ws):
    """损坏的 PDF 收敛为 error（不抛异常、不中断工具循环）。"""
    (ws.root / "bad.pdf").write_bytes(b"this is not a pdf at all")
    out = tools.read_file(ws, "bad.pdf")
    assert out.status == "error"
    assert "PDF" in out.output
