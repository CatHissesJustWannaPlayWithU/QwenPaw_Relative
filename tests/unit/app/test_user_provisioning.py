# -*- coding: utf-8 -*-
"""用户隔离：旧数据归属迁移与新用户工作区初始化测试。"""
from pathlib import Path

from qwenpaw.app import user_provisioning
from qwenpaw.config.config import AgentProfileRef, Config


def test_migrate_legacy_agents_assigns_only_all_unowned_profiles(monkeypatch):
    """纯旧版配置应归属首个管理员，混合配置不能被自动改写。"""
    config = Config()
    config.agents.profiles = {
        "default": AgentProfileRef(
            id="default",
            workspace_dir="/tmp/default",
        ),
        "qa": AgentProfileRef(id="qa", workspace_dir="/tmp/qa"),
    }
    saved = []

    import qwenpaw.app.auth as auth_module

    monkeypatch.setattr(user_provisioning, "load_config", lambda: config)
    monkeypatch.setattr(user_provisioning, "save_config", saved.append)
    monkeypatch.setattr(auth_module, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        auth_module,
        "list_public_users",
        lambda: [
            {
                "id": "user_admin",
                "username": "admin",
                "role": "admin",
                "status": "active",
            },
        ],
    )

    assert user_provisioning.migrate_legacy_agents_to_first_admin() == 2
    assert {
        ref.owner_user_id for ref in config.agents.profiles.values()
    } == {"user_admin"}
    assert saved == [config]

    # 已有新归属时，保留尚待人工处理的 legacy Agent，不能擅自改写。
    config.agents.profiles["qa"].owner_user_id = None
    assert user_provisioning.migrate_legacy_agents_to_first_admin() == 0
    assert config.agents.profiles["qa"].owner_user_id is None


def test_ensure_personal_agent_creates_private_owned_workspace(monkeypatch, tmp_path):
    """无 Agent 的用户首次登录时得到独立目录及 owner_user_id。"""
    config = Config()
    config.agents.profiles = {}
    config.agents.agent_order = []
    saved = []
    initialized = []
    saved_agent_configs = []

    monkeypatch.setattr(user_provisioning, "WORKING_DIR", Path(tmp_path))
    monkeypatch.setattr(user_provisioning, "load_config", lambda: config)
    monkeypatch.setattr(user_provisioning, "save_config", saved.append)
    monkeypatch.setattr(
        user_provisioning,
        "generate_short_agent_id",
        lambda: "alice01",
    )
    monkeypatch.setattr(
        user_provisioning,
        "_initialize_workspace",
        lambda workspace, language: initialized.append((workspace, language)),
    )
    monkeypatch.setattr(
        user_provisioning,
        "save_agent_config",
        lambda agent_id, agent_config: saved_agent_configs.append(
            (agent_id, agent_config),
        ),
    )

    agent_id = user_provisioning.ensure_personal_agent(
        "user_alice",
        "alice",
    )

    assert agent_id == "alice01"
    profile = config.agents.profiles[agent_id]
    assert profile.owner_user_id == "user_alice"
    assert Path(profile.workspace_dir) == tmp_path / "workspaces" / agent_id
    assert config.agents.agent_order == [agent_id]
    assert initialized == [(tmp_path / "workspaces" / agent_id, "zh")]
    assert saved_agent_configs[0][1].name == "alice 的智能体"
    assert saved == [config]

    # 同一用户再次登录时复用已有 Agent，不创建第二份工作区。
    assert user_provisioning.ensure_personal_agent("user_alice", "alice") == agent_id
    assert len(saved_agent_configs) == 1
