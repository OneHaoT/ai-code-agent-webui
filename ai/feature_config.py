"""阶段4C：前端功能开关配置存储（features.json 持久化 + env 回落）。

职责
----
1. 进程内功能状态（MCP / Multi-Agent 开关 + MCP server 列表）的持久化：
   `config/features.json`（原子写；目录自动创建）。
2. 加载优先级：文件存在按文件；文件或字段缺失时逐字段回落 .env
   （MULTI_AGENT_ENABLED / MCP_ENABLED / MCP_SERVERS）——env 永远只读，
   程序不回写（.env 是手编文件，程序只写自己的 features.json）。
3. 承接 parse_servers_config（自 mcp_client.py 迁入）：它是纯函数、零外部
   依赖，放在这里让「MCP_ENABLED=false 零 mcp 模块加载」契约继续成立
   （feature_config 可被 main.py 顶层 import 而不引入 mcp SDK）。

安全
----
mcp_servers 条目的 env 字段可能含 token → config/ 必须在 .gitignore
（不入库）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

logger = logging.getLogger("ai-assist.feature_config")

AI_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(AI_ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "features.json")


def _env_flag(key: str) -> bool:
    return os.getenv(key, "false").strip().lower() in ("1", "true", "yes", "on")


def sanitize_servers(servers) -> list[dict]:
    """server 条目级净化：非对象/缺 name/command 的条目 WARN 跳过。

    条目格式：{"name": "fs", "command": "npx", "args": [...], "env": {...}}；
    name/command 必填，args/env 可选且统一转字符串形态。
    """
    valid: list[dict] = []
    for i, item in enumerate(servers or []):
        if (not isinstance(item, dict) or not str(item.get("name", "")).strip()
                or not str(item.get("command", "")).strip()):
            logger.warning("mcp_servers[%d] 缺 name/command 或不是对象，已跳过", i)
            continue
        valid.append({
            "name": str(item["name"]).strip(),
            "command": str(item["command"]).strip(),
            "args": [str(a) for a in item.get("args") or []],
            "env": {str(k): str(v) for k, v in (item.get("env") or {}).items()},
        })
    return valid


def parse_servers_config(raw: str | None) -> list[dict]:
    """解析 MCP_SERVERS JSON 配置；坏 JSON / 非数组 WARN 后按无 server 处理。"""
    try:
        servers = json.loads(raw or "[]")
    except json.JSONDecodeError as e:
        logger.warning("MCP_SERVERS 不是合法 JSON：%s（按无 server 处理）", e)
        return []
    if not isinstance(servers, list):
        logger.warning("MCP_SERVERS 必须是 JSON 数组（按无 server 处理）")
        return []
    return sanitize_servers(servers)


def load_features() -> dict:
    """加载功能状态：features.json 优先，字段缺失逐字段回落 .env。

    坏文件 WARN 后整体回落 env，不阻断启动。返回：
    {"multi_agent_enabled": bool, "mcp_enabled": bool, "mcp_servers": [..]}
    """
    state = {
        "multi_agent_enabled": _env_flag("MULTI_AGENT_ENABLED"),
        "mcp_enabled": _env_flag("MCP_ENABLED"),
        "mcp_servers": parse_servers_config(os.getenv("MCP_SERVERS")),
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state
    except json.JSONDecodeError as e:
        logger.warning("features.json 不是合法 JSON：%s（回落 .env 默认）", e)
        return state
    if not isinstance(data, dict):
        logger.warning("features.json 必须是 JSON 对象（回落 .env 默认）")
        return state
    for key in ("multi_agent_enabled", "mcp_enabled"):
        if isinstance(data.get(key), bool):
            state[key] = data[key]
    if isinstance(data.get("mcp_servers"), list):
        state["mcp_servers"] = sanitize_servers(data["mcp_servers"])
    return state


def save_features(*, multi_agent_enabled=None, mcp_enabled=None,
                  mcp_servers=None) -> dict:
    """合并保存（None=不改该字段），原子写，返回保存后的完整状态。"""
    current = load_features()
    if multi_agent_enabled is not None:
        current["multi_agent_enabled"] = bool(multi_agent_enabled)
    if mcp_enabled is not None:
        current["mcp_enabled"] = bool(mcp_enabled)
    if mcp_servers is not None:
        current["mcp_servers"] = sanitize_servers(mcp_servers)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.info("features.json 已保存：%s", current)
    return current
