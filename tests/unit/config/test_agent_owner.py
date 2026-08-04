# -*- coding: utf-8 -*-
"""用户隔离：智能体归属字段测试。"""
from qwenpaw.config.config import AgentProfileRef


def test_legacy_agent_profile_allows_missing_owner():
    """旧配置没有 owner_user_id 时，仍然可以被正常读取。"""
    agent = AgentProfileRef.model_validate(
        {
            "id": "legacy_agent",
            "workspace_dir": "/tmp/legacy_agent",
        },
    )

    assert agent.owner_user_id is None


def test_agent_profile_stores_owner_user_id():
    """新智能体配置能够保存稳定的所属用户 ID。"""
    agent = AgentProfileRef(
        id="alice_agent",
        workspace_dir="/tmp/alice_agent",
        owner_user_id="user_alice",
    )

    assert agent.owner_user_id == "user_alice"
    assert agent.model_dump()["owner_user_id"] == "user_alice"