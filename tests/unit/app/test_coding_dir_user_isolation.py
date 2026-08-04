# -*- coding: utf-8 -*-
"""用户隔离：Coding Mode 项目目录边界测试。"""
from types import SimpleNamespace

from qwenpaw.app import agent_context


def test_coding_dir_falls_back_when_legacy_config_points_outside_workspace(
    monkeypatch,
    tmp_path,
):
    """旧 project_dir 指向外部目录时，代码接口必须回到自己的工作区。"""
    workspace_dir = tmp_path / "alice-workspace"
    workspace_dir.mkdir()
    other_users_dir = tmp_path / "bob-workspace"
    other_users_dir.mkdir()
    workspace = SimpleNamespace(agent_id="alice", workspace_dir=workspace_dir)
    config = SimpleNamespace(
        coding_mode=SimpleNamespace(project_dir=str(other_users_dir)),
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config",
        lambda _agent_id: config,
    )

    assert agent_context.get_coding_dir(workspace) == workspace_dir.resolve()


def test_coding_dir_allows_project_inside_own_workspace(monkeypatch, tmp_path):
    """工作区 coding_projects 下的项目仍可正常作为活动项目。"""
    workspace_dir = tmp_path / "alice-workspace"
    project_dir = workspace_dir / "coding_projects" / "demo"
    project_dir.mkdir(parents=True)
    workspace = SimpleNamespace(agent_id="alice", workspace_dir=workspace_dir)
    config = SimpleNamespace(
        coding_mode=SimpleNamespace(project_dir=str(project_dir)),
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config",
        lambda _agent_id: config,
    )

    assert agent_context.get_coding_dir(workspace) == project_dir.resolve()
