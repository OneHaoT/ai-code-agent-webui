"""
工作区路径沙箱（阶段1）。

所有只读工具只能访问 WORKSPACE_ROOT 目录树内的文件：
  - 入参可以是相对工作区的相对路径，也可以是恰好位于工作区内的绝对路径；
  - resolve() 会做完整规范化（展开 ~、解析 .. 与符号链接），
    规范化后目标不在根目录树内一律抛 WorkspaceViolation；
  - 符号链接解析依赖 Path.resolve(strict=False)：对不存在的路径会解析其
    已存在前缀中的链接，剩余部分原样拼接，因此“链接指向根外”也会被拦截。

默认工作区根为 ai/ 的上一级（项目根 c:/Users/tgjon/Desktop/ai），
可用环境变量 WORKSPACE_ROOT 覆盖（支持 ~ 与相对当前工作目录的路径）。
"""
from __future__ import annotations

import os
from pathlib import Path

# ai/workspace.py 的上两级即项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 遍历搜索时默认跳过的目录名（依赖目录 / VCS / 构建产物）
DEFAULT_IGNORE_DIRS = frozenset({
    ".git", ".svn", ".hg",
    "node_modules", ".venv", "venv", "__pycache__",
    "target", "dist", "build", ".idea", ".vscode",
})


class WorkspaceViolation(Exception):
    """目标路径越出工作区沙箱。"""


class Workspace:
    """只读工具的路径边界：一切目标路径先过本沙箱再碰文件系统。"""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        if root is None or str(root).strip() == "":
            base = PROJECT_ROOT
        else:
            base = Path(str(root).strip()).expanduser()
            if not base.is_absolute():
                # 相对路径相对进程当前工作目录解析（.env 里一般写绝对路径）
                base = Path.cwd() / base
        self.root = base.resolve(strict=False)

    def resolve(self, rel_path: str | os.PathLike[str] | None) -> Path:
        """把入参路径规范化为沙箱内绝对路径。

        - None / 空字符串视为工作区根；
        - 相对路径相对工作区根拼接；绝对路径必须本身就在沙箱内；
        - 越界（含 .. 穿越、盘外绝对路径、指向根外的符号链接）抛 WorkspaceViolation。
        """
        if rel_path is None or str(rel_path).strip() == "":
            target = self.root
        else:
            raw = Path(str(rel_path).strip().replace("\\", "/")).expanduser()
            target = raw if raw.is_absolute() else self.root / raw
            # strict=False：目标允许尚不存在；仍会解析已存在前缀中的符号链接
            target = target.resolve(strict=False)
        if target != self.root and self.root not in target.parents:
            raise WorkspaceViolation(f"路径越出工作区边界: {rel_path!r}")
        return target

    def is_within(self, target: str | os.PathLike[str]) -> bool:
        """布尔版边界判定（供目录遍历层逐条目复检）：解析后不在根树内即拒绝。

        与 resolve() 同一套 resolve(strict=False) 语义，可识别：
        - 指向根外的符号链接文件/目录；
        - Windows 目录联接（junction）——os.walk 不把它当 symlink，
          遍历层若不逐条目复检就会递归进入根外目录。
        任何解析异常（权限/悬空）一律按越界处理（fail-closed）。
        """
        try:
            resolved = Path(target).resolve(strict=False)
        except OSError:
            return False
        return resolved == self.root or self.root in resolved.parents

    def relative(self, target: Path) -> str:
        """绝对路径 -> 相对工作区的 POSIX 风格展示路径（用于工具输出）。"""
        try:
            return target.resolve(strict=False).relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return target.name
