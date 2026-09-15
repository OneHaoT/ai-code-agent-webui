"""
阶段3本地受限沙箱：write_file / run_command 的执行核心（SandboxProvider）。

威胁模型（诚实声明，不得夸大）
------------------------------
本模块提供的是 **防 AI 误操作** 的受限执行，不是防恶意代码的安全隔离：
  - write_file 是 Python API 级硬边界：一切路径先经 Workspace.resolve()
    规范化校验，越界（../ 穿越 / 盘外绝对路径 / 指向根外的 symlink、
    junction / 8.3 短名变体）在触碰任何字节之前拒绝——与阶段1 只读工具同级；
  - run_command 是防呆级：白名单首词 + 黑名单模式 + cwd 锁定工作区根 +
    硬超时 taskkill /F /T 杀整树。子进程本质运行在宿主机上，白名单命令的
    内联参数（如 python -c）可执行任意逻辑——**它不是安全边界**；
  - 以上两者的关键补偿控制是 confirm 人机确认（agent 层每次写/执行前
    经用户确认），拒绝/超时时磁盘零写入、零子进程。

SandboxProvider 抽象：默认 LocalSandboxProvider（项目内置、零安装）；
DockerSandboxProvider 仅为未来升级保留接口与配置位（SANDBOX_PROVIDER=docker
在装配期明确报错拒绝，不静默回退）。

配置（ai/.env，全带默认值）：
  SANDBOX_ENABLED=true            写/执行工具总开关（装配期，false 回退阶段2）
  SANDBOX_PROVIDER=local          local | docker（docker 未实现，启动报错）
  WRITE_MAX_BYTES=262144          write_file 单文件内容上限（256KB，UTF-8 字节）
  EXEC_WHITELIST=...              逗号分隔命令白名单（首词匹配）
  EXEC_BLACKLIST=...              逗号分隔追加黑名单正则（与内置默认合并）
  EXEC_TIMEOUT_SECONDS=60         run_command 硬超时（超时杀整棵进程树）
  EXEC_MAX_OUTPUT_BYTES=65536     命令输出捕获上限（64KB，超限截断）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass

from workspace import Workspace, WorkspaceViolation


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


# ---- 配置（与 tools.py 同模式：模块装配期读取） ----
SANDBOX_ENABLED = os.getenv("SANDBOX_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
SANDBOX_PROVIDER_NAME = (
    (os.getenv("SANDBOX_PROVIDER") or "local").strip().lower() or "local"
)
WRITE_MAX_BYTES = _int_env("WRITE_MAX_BYTES", 256 * 1024)
EXEC_TIMEOUT_SECONDS = _float_env("EXEC_TIMEOUT_SECONDS", 60.0)
EXEC_MAX_OUTPUT_BYTES = _int_env("EXEC_MAX_OUTPUT_BYTES", 64 * 1024)

# 默认命令白名单（首词匹配；Windows 下文件系统大小写不敏感，统一小写比较）
DEFAULT_EXEC_WHITELIST = (
    "python,pip,pytest,node,npm,npx,git,mvn,java,javac,dir,ls,echo,type,cd"
)


def _load_whitelist() -> frozenset[str]:
    raw = os.getenv("EXEC_WHITELIST", DEFAULT_EXEC_WHITELIST) or ""
    items = {t.strip().lower() for t in raw.split(",") if t.strip()}
    return frozenset(items)


EXEC_WHITELIST = _load_whitelist()

# 内置黑名单正则（对整条命令串做 IGNORECASE search）；env EXEC_BLACKLIST 追加。
# 原则：覆盖"破坏性/逃逸面"操作；白名单外命令本来就进不来，这里是第二道。
_DEFAULT_EXEC_BLACKLIST: tuple[str, ...] = (
    r"\brm\b.+\s-{1,2}[a-z]*r",            # rm 递归删除
    r"\bdel\b\s+/[sq]",                     # del /s /q
    r"\b(rmdir|rd)\b\s+/s",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\blogoff\b",
    r"\btaskkill\b",                        # 工具内部杀树不受此限（不经本检查）
    r"\btskill\b",
    r"\breg(\.exe)?\b",
    r"\bschtasks\b",
    r"\bmklink\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\bvssadmin\b",
    r"\bnet\b\s+(user|localgroup|use|stop|start|pause)",
    r"\bwevtutil\b",
    r"\bcipher\b",
    r"\battrib\b",
    r"\brundll32\b",
    r"\bregsvr32\b",
    r"\bmshta\b",
    r"\bcertutil\b",
    r"\bbitsadmin\b",
    r"\bpowershell\b",
    r"\bpwsh\b",
    r"\bwscript\b",
    r"\bcscript\b",
    r"\bcurl\b",
    r"\bwget\b",
)


def _load_blacklist() -> list[re.Pattern[str]]:
    patterns = list(_DEFAULT_EXEC_BLACKLIST)
    extra = (os.getenv("EXEC_BLACKLIST") or "").strip()
    if extra:
        patterns += [p.strip() for p in extra.split(",") if p.strip()]
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue  # 配置错误不阻断启动，跳过该条（fail-open 仅影响该规则）
    return compiled


EXEC_BLACKLIST_PATTERNS = _load_blacklist()

# 后台无窗口执行（防 ai 服务子进程闪出控制台窗）
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class SandboxError(Exception):
    """写/执行操作失败（含沙箱拒绝），消息为面向模型的中文说明。"""


class SandboxConfigError(Exception):
    """沙箱装配配置错误（如 SANDBOX_PROVIDER=docker），启动期即失败。"""


@dataclass
class WriteResult:
    """write_file 执行结果摘要（tools 层负责最终格式化）。"""

    rel_path: str
    size: int          # 写入字节数（UTF-8）
    overwrite: bool    # 是否覆盖了已存在文件


@dataclass
class CommandResult:
    """run_command 执行结果摘要。"""

    exit_code: int | None   # None = 超时被终止（无法取得退出码）
    output: str             # stdout+stderr 合并文本（已按上限截断）
    truncated: bool
    timed_out: bool


class SandboxProvider(ABC):
    """沙箱抽象：写文件与命令执行的统一入口（阶段4+ 可扩展 Docker 实现）。"""

    @abstractmethod
    def write_file(self, ws: Workspace, path: str, content: str) -> WriteResult:
        """在工作区内写入/覆盖一个文本文件。越界/超限抛 SandboxError。"""

    @abstractmethod
    def run_command(self, ws: Workspace, command: str) -> CommandResult:
        """在工作区根执行一条白名单命令。拒绝/超时抛 SandboxError 或
        返回 timed_out 结果。"""


class LocalSandboxProvider(SandboxProvider):
    """默认实现：宿主机上的受限本地执行（零安装、零外部依赖）。

    write_file：路径硬边界（resolve 前置，越界零磁盘 IO）；
    run_command：白名单 + 黑名单 + cwd 锁定 + 超时杀树（防呆级，见模块头声明）。
    """

    # ---------------- write_file（硬边界） ----------------

    def write_file(self, ws: Workspace, path: str, content: str) -> WriteResult:
        if not isinstance(path, str) or not path.strip():
            raise SandboxError("参数 path 必须是非空字符串（相对工作区根）")
        if not isinstance(content, str):
            raise SandboxError("参数 content 必须是字符串")

        # 先校验后触碰：越界路径不产生任何磁盘 IO
        try:
            target = ws.resolve(path)
        except WorkspaceViolation:
            raise SandboxError("路径越出工作区边界，写入已拒绝") from None
        if target == ws.root:
            raise SandboxError("写入目标不能是工作区根目录本身")

        if target.exists() and not target.is_file():
            raise SandboxError(f"写入目标已存在且不是文件：{path}")

        encoded = content.encode("utf-8")
        if len(encoded) > WRITE_MAX_BYTES:
            raise SandboxError(
                f"内容超过单文件写入上限（{WRITE_MAX_BYTES // 1024}KB），"
                f"当前 {len(encoded)} 字节")

        overwrite = target.is_file()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
        except OSError as e:
            raise SandboxError(f"写入文件失败：{e}") from None
        return WriteResult(rel_path=ws.relative(target), size=len(encoded),
                           overwrite=overwrite)

    # ---------------- run_command（防呆级） ----------------

    def run_command(self, ws: Workspace, command: str) -> CommandResult:
        if not isinstance(command, str) or not command.strip():
            raise SandboxError("参数 command 必须是非空字符串")

        first = _first_token(command)
        if first not in EXEC_WHITELIST:
            raise SandboxError(
                f"命令 {first!r} 不在执行白名单中，已拒绝；"
                f"可用命令：{', '.join(sorted(EXEC_WHITELIST))}")
        for pat in EXEC_BLACKLIST_PATTERNS:
            if pat.search(command):
                raise SandboxError("命令包含被禁止的操作，已拒绝")

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(ws.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # 合并捕获，保持输出顺序上下文
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as e:
            raise SandboxError(f"命令启动失败：{e}") from None

        raw: bytes | None = None
        timed_out = False
        try:
            raw, _ = proc.communicate(timeout=EXEC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                raw, _ = proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                raw = b""

        text = ""
        truncated = False
        if raw:
            if len(raw) > EXEC_MAX_OUTPUT_BYTES:
                raw = raw[:EXEC_MAX_OUTPUT_BYTES]
                truncated = True
            text = _decode_output(raw)
        if timed_out:
            return CommandResult(exit_code=None, output=text,
                                 truncated=truncated, timed_out=True)
        return CommandResult(exit_code=proc.returncode, output=text,
                             truncated=truncated, timed_out=False)


class DockerSandboxProvider(SandboxProvider):
    """Docker 沙箱占位（未来升级项）：本期仅保留接口与配置位。

    SANDBOX_PROVIDER=docker 时装配期即抛 SandboxConfigError 拒绝启动，
    绝不静默回退 local——诚实优于伪装。
    """

    def write_file(self, ws: Workspace, path: str, content: str) -> WriteResult:
        raise NotImplementedError("Docker 沙箱为未来升级项，本期未实现")

    def run_command(self, ws: Workspace, command: str) -> CommandResult:
        raise NotImplementedError("Docker 沙箱为未来升级项，本期未实现")


def get_sandbox_provider() -> SandboxProvider:
    """装配期工厂：按 SANDBOX_PROVIDER 选择实现；未知/未实现配置即报错。"""
    if SANDBOX_PROVIDER_NAME == "local":
        return LocalSandboxProvider()
    if SANDBOX_PROVIDER_NAME == "docker":
        raise SandboxConfigError(
            "SANDBOX_PROVIDER=docker 尚未实现：Docker 沙箱为未来升级项，"
            "本期请配置 SANDBOX_PROVIDER=local")
    raise SandboxConfigError(
        f"未知 SANDBOX_PROVIDER：{SANDBOX_PROVIDER_NAME}（可选 local/docker）")


# ---------------- 内部辅助 ----------------

def _first_token(command: str) -> str:
    """提取命令首词并归一化（剥引号、小写），供白名单匹配。"""
    parts = command.strip().split(None, 1)
    if not parts:
        return ""
    return parts[0].strip().strip('"').strip("'").lower()


def _kill_tree(pid: int) -> None:
    """强杀整棵进程树：Windows 用 taskkill /F /T（系统自带，零新依赖）。

    /T 限定为该 PID 的子树，不波及无关进程；失败静默（外层已按超时语义
    返回 error，不因清理失败挂死请求）。
    """
    if sys.platform != "win32":
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, check=False, timeout=10,
            creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _decode_output(raw: bytes) -> str:
    """命令输出解码：UTF-8 优先，GBK 兜底，仍失败替换非法字节。

    与 tools._decode 同语义；因 tools → sandbox 单向依赖（避免循环导入）
    而在本地实现。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
