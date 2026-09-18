"""阶段4C：前端功能开关测试（features.json 配置存储 + 工具注销 + 管理端点）。

覆盖：
- feature_config：env 回落 / 文件覆盖 / 坏文件回落 / 合并保存 / 原子写建目录；
- tools：unregister_delegate_tool / unregister_mcp_tools 幂等与四处一致；
- 端点：GET /ai/features 与 /health 一致、POST 开关热切换（Fake manager，
  零真实子进程）、mcp_servers 结构校验 400 且状态与文件不变、
  lifespan 按 features.json 装配（与运行时切换共用 _apply_feature_changes）。

运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe -m pytest tests/test_features.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import feature_config  # noqa: E402
import main  # noqa: E402
import mcp_client  # noqa: E402
import tools  # noqa: E402
from workspace import Workspace  # noqa: E402


# ---------------- 隔离夹具 ----------------

@pytest.fixture
def fresh_state(monkeypatch, tmp_path):
    """隔离：features.json 指向 tmp、env 全 false、功能态复位、注册表快照。"""
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(feature_config, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(feature_config, "CONFIG_PATH",
                        str(cfg_dir / "features.json"))
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "false")
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setenv("MCP_SERVERS", "[]")
    main._features_state.clear()
    main._features_state.update(feature_config.load_features())
    snap = (list(tools.TOOL_SCHEMAS), dict(tools._DISPATCH),
            dict(tools._TOOL_TIMEOUTS), dict(tools._MCP_TOOL_META))
    yield cfg_dir
    # 注册表就地恢复（agent 持同一列表对象）
    tools.TOOL_SCHEMAS[:] = snap[0]
    tools._DISPATCH.clear(); tools._DISPATCH.update(snap[1])
    tools._TOOL_TIMEOUTS.clear(); tools._TOOL_TIMEOUTS.update(snap[2])
    tools._MCP_TOOL_META.clear(); tools._MCP_TOOL_META.update(snap[3])
    mgr = main._mcp_manager
    if mgr is not None:
        try:
            mgr.close()
        except Exception:  # noqa: BLE001 —— 测试清理尽力而为
            pass
        main._mcp_manager = None


@pytest.fixture
def client(fresh_state):
    """带 lifespan 的测试客户端（_features_lock 由 lifespan 创建）。"""
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(str(root))


def _read_cfg(cfg_dir):
    with open(cfg_dir / "features.json", "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------- feature_config：加载回落 / 覆盖 ----------------

def test_load_features_env_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(feature_config, "CONFIG_PATH",
                        str(tmp_path / "no" / "features.json"))
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "true")
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    monkeypatch.setenv("MCP_SERVERS", '[{"name":"s","command":"c"}]')
    state = feature_config.load_features()
    assert state == {
        "multi_agent_enabled": True,
        "mcp_enabled": False,          # 缺省回落 false
        "mcp_servers": [{"name": "s", "command": "c",
                         "args": [], "env": {}}],
    }


def test_load_features_file_overrides_env_per_field(monkeypatch, tmp_path):
    monkeypatch.setattr(feature_config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(feature_config, "CONFIG_PATH",
                        str(tmp_path / "features.json"))
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "true")
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVERS", "[]")
    # 文件只写 mcp_enabled：其余两字段逐字段回落 env
    (tmp_path / "features.json").write_text(
        '{"mcp_enabled": false, "mcp_servers": [{"name":"f","command":"x"}]}',
        encoding="utf-8")
    state = feature_config.load_features()
    assert state["multi_agent_enabled"] is True          # 回落 env
    assert state["mcp_enabled"] is False                 # 文件覆盖
    assert [s["name"] for s in state["mcp_servers"]] == ["f"]


def test_load_features_bad_file_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(feature_config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(feature_config, "CONFIG_PATH",
                        str(tmp_path / "features.json"))
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVERS", '[{"name":"s","command":"c"}]')

    (tmp_path / "features.json").write_text("not-json", encoding="utf-8")
    state = feature_config.load_features()
    assert state["multi_agent_enabled"] is True and state["mcp_enabled"] is False

    # 坏文件整体回落 env（env 的 mcp_servers 同样生效，而非空）
    (tmp_path / "features.json").write_text("[1,2]", encoding="utf-8")
    state2 = feature_config.load_features()
    assert [s["name"] for s in state2["mcp_servers"]] == ["s"]

    # 字段类型非法：bool 字段必须 isinstance bool、mcp_servers 必须 list
    (tmp_path / "features.json").write_text(json.dumps({
        "multi_agent_enabled": "yes", "mcp_enabled": 1,
        "mcp_servers": {"name": "x"}}), encoding="utf-8")
    state = feature_config.load_features()
    assert state["multi_agent_enabled"] is True and state["mcp_enabled"] is False
    assert [s["name"] for s in state["mcp_servers"]] == ["s"]


# ---------------- feature_config：合并保存 / 原子写 ----------------

def test_save_features_merge_and_creates_dir(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(feature_config, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(feature_config, "CONFIG_PATH",
                        str(cfg_dir / "features.json"))
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "false")
    monkeypatch.setenv("MCP_SERVERS", "[]")  # 隔离真实 ai/.env 的 demo server

    out = feature_config.save_features(mcp_enabled=True)
    assert out == {"multi_agent_enabled": False, "mcp_enabled": True,
                   "mcp_servers": []}
    assert _read_cfg(cfg_dir) == out
    assert not [p for p in cfg_dir.iterdir() if p.suffix == ".tmp"]  # 无残留临时文件

    # 合并语义：只改一个字段，其余保持文件态
    out2 = feature_config.save_features(multi_agent_enabled=True,
                                        mcp_servers=[{"name": "s", "command": "c"}])
    assert out2 == {"multi_agent_enabled": True, "mcp_enabled": True,
                    "mcp_servers": [{"name": "s", "command": "c",
                                     "args": [], "env": {}}]}


def test_save_features_sanitizes_entries(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(feature_config, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(feature_config, "CONFIG_PATH",
                        str(cfg_dir / "features.json"))
    out = feature_config.save_features(
        mcp_servers=[{"name": " a ", "command": " npx.cmd ", "args": [1],
                      "env": {"K": 2}}, "junk", {"command": "no-name"}])
    assert out["mcp_servers"] == [{"name": "a", "command": "npx.cmd",
                                   "args": ["1"], "env": {"K": "2"}}]


# ---------------- tools：注销幂等与四处一致 ----------------

def test_unregister_delegate_tool_idempotent():
    tools.register_delegate_tool(None)  # 兜底执行器，便于测试注册路径
    assert tools.tool_available("delegate_task")
    assert tools.unregister_delegate_tool() is True
    assert tools.unregister_delegate_tool() is False    # 幂等
    assert not tools.tool_available("delegate_task")
    assert all(s["function"]["name"] != "delegate_task" for s in tools.TOOL_SCHEMAS)
    assert "delegate_task" not in tools._DISPATCH
    assert "delegate_task" not in tools._TOOL_TIMEOUTS


def test_unregister_mcp_tools_idempotent_and_consistent():
    entries = [("srv", {"name": "t", "description": "d",
                        "input_schema": {"type": "object", "properties": {}},
                        "read_only_hint": False})]
    registered = tools.register_mcp_tools(_FakeManager(entries))
    assert registered == ["mcp_srv_t"]
    assert tools.unregister_mcp_tools() == ["mcp_srv_t"]
    assert tools.unregister_mcp_tools() == []           # 幂等
    assert all(s["function"]["name"] != "mcp_srv_t" for s in tools.TOOL_SCHEMAS)
    assert "mcp_srv_t" not in tools._DISPATCH
    assert "mcp_srv_t" not in tools._TOOL_TIMEOUTS
    assert tools._MCP_TOOL_META == {}


class _FakeManager:
    """register_mcp_tools / main 生命周期所需的最小鸭子类型 manager。"""

    def __init__(self, entries=None, health=None):
        self._entries = entries or []
        self._health = health if health is not None else []
        self.tool_timeout = 60.0
        self.started = False
        self.closed = False
        self.servers_json: str | None = None

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def health(self):
        return self._health

    def iter_connected_tools(self):
        yield from self._entries

    def call_tool(self, server, tool, args):
        return (False, "ok")


# ---------------- 端点：GET 一致性 / POST 热切换 ----------------

def test_get_features_matches_health(client):
    feats = client.get("/ai/features").json()
    health = client.get("/health").json()
    assert feats["multi_agent_enabled"] == health["multi_agent_enabled"] is False
    assert feats["mcp_enabled"] == health["mcp_enabled"] is False
    assert feats["mcp_servers_health"] == []
    assert isinstance(feats["mcp_servers"], list)
    assert set(feats) == {"multi_agent_enabled", "mcp_enabled",
                          "mcp_servers", "mcp_servers_health"}


def test_post_toggle_multi_agent_registers_and_unregisters(client, ws, fresh_state):
    cfg_dir = fresh_state
    r = client.post("/ai/features", json={"multi_agent_enabled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["multi_agent_enabled"] is True
    assert tools.tool_available("delegate_task")
    assert any(s["function"]["name"] == "delegate_task" for s in tools.TOOL_SCHEMAS)
    assert _read_cfg(cfg_dir)["multi_agent_enabled"] is True   # 保存→应用→返回

    # 注销后走"未知工具"降级，流不断
    r2 = client.post("/ai/features", json={"multi_agent_enabled": False})
    assert r2.status_code == 200 and r2.json()["multi_agent_enabled"] is False
    assert not tools.tool_available("delegate_task")
    out = tools.execute_tool(ws, "delegate_task", {})
    assert out.status == "error" and "未知工具" in out.output
    assert _read_cfg(cfg_dir)["multi_agent_enabled"] is False

    # 幂等：重复 POST 同值无副作用
    assert client.post("/ai/features",
                       json={"multi_agent_enabled": False}).status_code == 200
    assert not tools.tool_available("delegate_task")


def test_post_toggle_mcp_hot_switch_with_fake_manager(client, monkeypatch, fresh_state):
    fake = _FakeManager(health=[{"name": "s", "status": "connected", "tools": 0}])
    created = []

    def fake_from_env(servers_json=None):
        m = _FakeManager(health=fake._health)
        m.servers_json = servers_json
        created.append(m)
        return m

    monkeypatch.setattr(mcp_client.MCPManager, "from_env",
                        classmethod(lambda cls, servers_json=None:
                                    fake_from_env(servers_json)))

    servers = [{"name": "s", "command": "npx.cmd", "args": ["-y", "demo"],
                "env": {"K": "V"}}]
    r = client.post("/ai/features", json={"mcp_enabled": True,
                                          "mcp_servers": servers})
    assert r.status_code == 200
    body = r.json()
    assert body["mcp_enabled"] is True
    assert body["mcp_servers_health"] == fake._health
    assert len(created) == 1 and created[0].started and not created[0].closed
    # from_env 收到净化后的 server 配置（内存态与落盘态一致）
    assert json.loads(created[0].servers_json) == [
        {"name": "s", "command": "npx.cmd", "args": ["-y", "demo"],
         "env": {"K": "V"}}]
    assert main._mcp_manager is created[0]

    # 关闭：注销工具 + close manager，健康清单清空
    r2 = client.post("/ai/features", json={"mcp_enabled": False})
    assert r2.status_code == 200
    assert r2.json()["mcp_enabled"] is False
    assert r2.json()["mcp_servers_health"] == []
    assert created[0].closed
    assert main._mcp_manager is None
    assert tools._MCP_TOOL_META == {}

    # 幂等重复开启：旧 manager 先注销关闭，再建新 manager
    r3 = client.post("/ai/features", json={"mcp_enabled": True})
    assert r3.status_code == 200 and len(created) == 2
    assert created[0].closed and created[1].started


def test_post_servers_only_while_enabled_keeps_mcp_on_and_reconnects(
        client, monkeypatch, fresh_state):
    """回归（E2E 实锤 bug）：MCP 已开启时仅 POST mcp_servers（不传
    mcp_enabled）必须沿用开启态——旧 manager 关闭、按新配置重建，
    返回/内存/落盘三处 mcp_enabled 恒为 true，绝不因保存配置而关闭。"""
    cfg_dir = fresh_state
    created = []

    def fake_from_env(servers_json=None):
        m = _FakeManager(health=[{"name": json.loads(servers_json)[0]["name"],
                                  "status": "connected", "tools": 0}])
        m.servers_json = servers_json
        created.append(m)
        return m

    monkeypatch.setattr(mcp_client.MCPManager, "from_env",
                        classmethod(lambda cls, servers_json=None:
                                    fake_from_env(servers_json)))

    # 先开启（server v1）
    r = client.post("/ai/features", json={
        "mcp_enabled": True,
        "mcp_servers": [{"name": "v1", "command": "c1"}]})
    assert r.status_code == 200 and r.json()["mcp_enabled"] is True
    assert len(created) == 1 and main._mcp_manager is created[0]

    # 仅更新 servers（不传 mcp_enabled）：热重连，保持开启
    r2 = client.post("/ai/features", json={
        "mcp_servers": [{"name": "v2", "command": "c2"}]})
    assert r2.status_code == 200
    body = r2.json()
    assert body["mcp_enabled"] is True                                   # 不被关闭
    assert [s["name"] for s in body["mcp_servers"]] == ["v2"]
    assert body["mcp_servers_health"] == [
        {"name": "v2", "status": "connected", "tools": 0}]
    assert len(created) == 2                                             # 重建了 manager
    assert created[0].closed and created[1].started                      # 旧关新开
    assert main._mcp_manager is created[1]
    assert json.loads(created[1].servers_json) == [
        {"name": "v2", "command": "c2", "args": [], "env": {}}]
    # 落盘态同样保持开启（内存态=落盘态）
    cfg = _read_cfg(cfg_dir)
    assert cfg["mcp_enabled"] is True
    assert [s["name"] for s in cfg["mcp_servers"]] == ["v2"]


def test_post_invalid_servers_400_keeps_state_and_file(client, fresh_state):
    cfg_dir = fresh_state
    before = dict(main._features_state)
    for bad in ("not-a-list", [{"name": "x"}], [{"command": "c"}],
                [None], [{"name": "", "command": "c"}], [{"name": 1}]):
        r = client.post("/ai/features", json={"mcp_servers": bad})
        assert r.status_code == 400, bad
        assert "name/command" in r.json()["detail"] or "数组" in r.json()["detail"]
    # 状态与文件均不变
    assert main._features_state == before
    assert not (cfg_dir / "features.json").exists()
    assert main._mcp_manager is None


def test_post_servers_only_updates_config_without_connect(client, monkeypatch,
                                                          fresh_state):
    """仅传 mcp_servers（开关关闭态）：更新配置+落盘，不建立连接。"""
    created = []
    monkeypatch.setattr(mcp_client.MCPManager, "from_env",
                        classmethod(lambda cls, servers_json=None:
                                    created.append(_FakeManager()) or created[-1]))
    r = client.post("/ai/features", json={"mcp_servers": [{"name": "s",
                                                           "command": "c"}]})
    assert r.status_code == 200
    assert r.json()["mcp_enabled"] is False
    assert [s["name"] for s in r.json()["mcp_servers"]] == ["s"]
    assert created == []                                # 未连接


def test_lifespan_assembles_from_features_file(monkeypatch, fresh_state):
    """lifespan 与运行时切换共用 _apply_feature_changes：文件态驱动装配。"""
    cfg_dir = fresh_state
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "features.json").write_text(json.dumps(
        {"multi_agent_enabled": True, "mcp_enabled": False}), encoding="utf-8")
    main._features_state.clear()
    main._features_state.update(feature_config.load_features())

    fake = _FakeManager()
    monkeypatch.setattr(mcp_client.MCPManager, "from_env",
                        classmethod(lambda cls, servers_json=None: fake))
    with TestClient(main.app) as c:
        health = c.get("/health").json()
        assert health["multi_agent_enabled"] is True
        assert health["mcp_enabled"] is False
        assert tools.tool_available("delegate_task")
        assert main._mcp_manager is None               # mcp 关闭不建 manager
    # 退出 lifespan：delegate 保持注册（开关语义不随连接断开回退）
    assert tools.tool_available("delegate_task")
    tools.unregister_delegate_tool()                   # 清理（快照夹具兜底）
