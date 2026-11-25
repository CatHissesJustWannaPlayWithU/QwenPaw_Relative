"""徐州招商插件级共享台账的授权与执行边界测试。"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from qwenpaw.app.principal import Principal
import qwenpaw.security.secret_store as secret_store


PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "tool" / "xuzhou-investment-ledger"
PACKAGE_NAME = "test_xuzhou_shared_ledger"


@pytest.fixture(autouse=True)
def _isolate_platform_evidence_root_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定测试进程的根密钥，确保共享目录测试不会访问真实钥匙串或业务台账。"""
    monkeypatch.setattr(secret_store, "_cached_master_key", bytes.fromhex("6b" * 32))


def _load_plugin_module(module_name: str):
    """以独立临时包加载插件模块，避免共享目录和用户函数替身泄漏到其他测试。"""
    sys.modules.pop(PACKAGE_NAME, None)
    for loaded_module_name in list(sys.modules):
        if loaded_module_name.startswith(f"{PACKAGE_NAME}."):
            sys.modules.pop(loaded_module_name, None)
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "xuzhou_investment_ledger.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{PACKAGE_NAME}.{module_name}")


def _load_shared_migration_script():
    """加载一次性迁移脚本，供纯本机复制规则测试，不执行登录或真实服务调用。"""
    script_path = PLUGIN_DIR / "scripts" / "migrate_default_ledger_to_shared.py"
    spec = importlib.util.spec_from_file_location("test_xuzhou_shared_ledger_migration", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_execution_rechecks_active_team_membership_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """工具与 Cron 每次执行都按服务器身份重查名册，撤权后不再可访问共享台账。

    输入是临时共享根目录、服务器执行上下文中的用户 ID 和平台用户目录替身；输出验证
    名册成员可取得固定目录、管理员不依赖名册，而普通成员被移出后立即失败。测试从不
    传递模型/前端用户 ID 或工作区路径，避免把共享设计误测成“任何智能体都能访问”。
    """
    shared = _load_plugin_module("shared_workspace")
    shared_root = tmp_path / "shared-ledger"
    monkeypatch.setenv(shared.SHARED_LEDGER_DIR_ENV, str(shared_root))

    member = shared.add_team_member(
        user_id="user_member_001",
        username="招商专员",
        added_by_user_id="user_admin_001",
    )["member"]
    assert member["user_id"] == "user_member_001"

    monkeypatch.setattr(shared, "get_current_user_id", lambda: "user_member_001")
    monkeypatch.setattr(
        shared,
        "get_user_by_id",
        lambda user_id: {
            "id": user_id,
            "username": "招商专员",
            "role": "user",
            "status": "active",
        },
    )
    assert shared.shared_ledger_dir_for_current_execution() == shared_root.resolve()

    # 即使用户目录查询返回“看似有效”的账号，ID 与服务器上下文不一致也不能被当作成员。
    monkeypatch.setattr(
        shared,
        "get_user_by_id",
        lambda _user_id: {
            "id": "user_substituted",
            "username": "被替换账号",
            "role": "user",
            "status": "active",
        },
    )
    with pytest.raises(PermissionError, match="目录记录与服务器上下文不一致"):
        shared.shared_ledger_dir_for_current_execution()
    monkeypatch.setattr(
        shared,
        "get_user_by_id",
        lambda user_id: {
            "id": user_id,
            "username": "招商专员",
            "role": "user",
            "status": "active",
        },
    )

    shared.remove_team_member(
        username="招商专员",
        removed_by_user_id="user_admin_001",
    )
    with pytest.raises(PermissionError, match="不是徐州招商团队成员"):
        shared.shared_ledger_dir_for_current_execution()

    monkeypatch.setattr(shared, "get_current_user_id", lambda: "user_admin_001")
    monkeypatch.setattr(
        shared,
        "get_user_by_id",
        lambda user_id: {
            "id": user_id,
            "username": "系统管理员",
            "role": "admin",
            "status": "active",
        },
    )
    assert shared.shared_ledger_dir_for_current_execution() == shared_root.resolve()


def test_shared_http_boundary_requires_team_membership_without_exposing_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """HTTP 业务入口只信任认证 Principal；非成员无法获得共享路径，管理员可访问。

    输入是模拟认证中间件写入的 ``request.state.principal``，输出是服务器固定共享目录或
    403。请求不含路径、用户 ID 或角色字段；本测试确保前端/模型即使知道目录也不能以
    自称成员的方式取得它，函数也不会读取线索、证据或任何业务内容。
    """
    shared = _load_plugin_module("shared_workspace")
    shared_root = tmp_path / "shared-ledger"
    monkeypatch.setenv(shared.SHARED_LEDGER_DIR_ENV, str(shared_root))

    non_member_request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(user_id="user_non_member", username="访客", role="user")
        )
    )
    with pytest.raises(HTTPException) as denied:
        shared.shared_ledger_dir_for_request(non_member_request)
    assert denied.value.status_code == 403

    admin_request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(user_id="user_admin", username="管理员", role="admin")
        )
    )
    assert shared.shared_ledger_dir_for_request(admin_request) == shared_root.resolve()


@pytest.mark.asyncio
async def test_team_api_resolves_username_server_side_and_rejects_claimed_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """名册接口只由管理员按用户名管理，客户端不能指定成员 user_id 或操作者。

    输入是管理员 Principal、用户名请求体和服务端用户目录替身；输出验证新成员使用平台
    目录的 ID，并且额外 ``user_id`` 字段会被 Pydantic 拒绝。添加/移除只改变独立名册和
    授权审计，不读写共享台账中的线索、证据、来源 URL、快照或评分。
    """
    team_api = _load_plugin_module("team_api")
    shared = importlib.import_module(f"{PACKAGE_NAME}.shared_workspace")
    monkeypatch.setenv(shared.SHARED_LEDGER_DIR_ENV, str(tmp_path / "shared-ledger"))
    admin = Principal(user_id="user_admin", username="管理员", role="admin")
    monkeypatch.setattr(team_api, "get_current_principal", lambda _request: admin)
    monkeypatch.setattr(
        team_api,
        "get_user_by_username",
        lambda username: {
            "id": "user_server_resolved",
            "username": username,
            "role": "user",
            "status": "active",
        },
    )

    response = await team_api.add_xuzhou_team_member(
        team_api.AddTeamMemberRequest(username="普通招商用户"),
        SimpleNamespace(),
    )
    assert response["member"]["user_id"] == "user_server_resolved"
    assert response["audit"]["operated_by_user_id"] == "user_admin"
    with pytest.raises(ValidationError):
        team_api.AddTeamMemberRequest.model_validate(
            {"username": "普通招商用户", "user_id": "forged-user"}
        )


def test_fixed_default_copy_preserves_candidates_and_refuses_formal_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """共享迁移只复制已验证的候选资产，绝不重签或复制正式 evidence。

    输入是固定默认工作区替身、空正式链台账、一个候选快照和固定共享根目录；输出验证源
    文件未变、目标保留候选/快照并只追加迁移审计。随后将源 evidence 改为非空，验证预检
    立即拒绝，防止目录切换成为重签或改写正式事实的旁路。
    """
    script = _load_shared_migration_script()
    source_workspace = tmp_path / "default-workspace"
    source_data_dir = source_workspace / "investment_attraction"
    source_snapshot_dir = source_data_dir / "candidate_observation_snapshots"
    source_snapshot_dir.mkdir(parents=True)
    source_ledger = {
        "schema_version": 1,
        "leads": [],
        "evidence": [],
        "discovery_candidates": [{"candidate_id": "candidate-test"}],
        "candidate_observations": [{"observation_id": "observation-test"}],
    }
    source_ledger_path = source_data_dir / "lead_evidence_ledger.json"
    source_ledger_path.write_text(json.dumps(source_ledger, ensure_ascii=False), encoding="utf-8")
    snapshot_path = source_snapshot_dir / "observation-test.json"
    snapshot_path.write_text('{"visible_text":"待补证"}', encoding="utf-8")
    source_bytes_before = source_ledger_path.read_bytes()

    storage = SimpleNamespace(load_ledger=lambda workspace_dir: source_ledger)
    shared_root = tmp_path / "shared-root"
    shared_root.mkdir()
    shared = SimpleNamespace(shared_ledger_root=lambda: shared_root)
    monkeypatch.setattr(script, "DEFAULT_SOURCE_WORKSPACE_DIR", source_workspace)
    monkeypatch.setattr(
        script,
        "_load_plugin_module",
        lambda module_name: storage if module_name == "storage" else shared,
    )

    _storage, loaded_shared, checked_source, raw_ledger, parsed_ledger, counts = script._preflight_source()
    result = script._copy_fixed_source_to_new_shared_target(
        shared_workspace=loaded_shared,
        source_workspace=checked_source,
        source_bytes=raw_ledger,
        source_ledger=parsed_ledger,
        snapshot_counts=counts,
    )
    target_ledger_path = Path(result["shared_ledger_path"])
    target_ledger = json.loads(target_ledger_path.read_text(encoding="utf-8"))
    assert source_ledger_path.read_bytes() == source_bytes_before
    assert target_ledger["leads"] == []
    assert target_ledger["evidence"] == []
    assert target_ledger["discovery_candidates"] == source_ledger["discovery_candidates"]
    assert target_ledger["candidate_observations"] == source_ledger["candidate_observations"]
    assert target_ledger["shared_ledger_migrations"][0]["source_ledger_sha256"]
    assert (target_ledger_path.parent / "candidate_observation_snapshots" / snapshot_path.name).read_bytes() == (
        snapshot_path.read_bytes()
    )

    source_ledger["evidence"] = [{"evidence_id": "evidence-not-permitted"}]
    source_ledger_path.write_text(json.dumps(source_ledger, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(script.UserFacingError, match="已有正式线索或证据"):
        script._preflight_source()


def test_team_management_helper_sends_username_only_to_server_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无前端运维助手只把用户名交给管理员 API，不能把客户端 user_id 带入名册请求。

    输入为已验证管理员令牌替身、``add`` 动作和用户名；输出验证固定团队路由仅收到 username
    与内存令牌。测试不访问真实服务或账号，确保脚本只是 API 包装层，真正的 user_id 解析、
    角色核验、追加审计和证据保护仍由服务器实现。
    """
    script_path = PLUGIN_DIR / "scripts" / "manage_shared_ledger_team.py"
    spec = importlib.util.spec_from_file_location("test_xuzhou_team_management_helper", script_path)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    requests: list[dict[str, object]] = []
    monkeypatch.setattr(script, "_require_enabled_authentication", lambda: None)
    monkeypatch.setattr(script, "_authenticate_existing_admin", lambda: "test-admin-token")

    def fake_request_json(path: str, **kwargs: object) -> dict[str, object]:
        """记录脚本转发字段，不连接本机服务。"""
        requests.append({"path": path, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(script, "_request_json", fake_request_json)
    assert script.manage_team("add", "已有普通用户") == {"ok": True}
    assert requests == [
        {
            "path": "/api/xuzhou-investment-ledger/team/members",
            "method": "POST",
            "bearer_token": "test-admin-token",
            "payload": {"username": "已有普通用户"},
        }
    ]
    with pytest.raises(script.UserFacingError):
        script._validated_username("\n")


class _FakeCronManager:
    """最小 Cron 管理器替身：按 ID 保存任务规格，不执行任何调度。"""

    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}

    async def get_job(self, job_id: str) -> object | None:
        return self.jobs.get(job_id)

    def get_state(self, job_id: str) -> None:
        return None

    async def create_or_replace_job(self, job: object) -> None:
        self.jobs[getattr(job, "id")] = job

    async def delete_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


def _fake_workspace(agent_id: str) -> SimpleNamespace:
    """构造只有 agent_id 与 Cron 管理器替身的工作区，不触碰平台多智能体设施。"""
    return SimpleNamespace(agent_id=agent_id, cron_manager=_FakeCronManager())


@pytest.mark.asyncio
async def test_shared_schedule_refuses_duplicate_enable_across_member_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """共享台账下同一固定计划不能被第二名成员在自己的智能体上重复启用。

    输入是两个不同成员的认证身份与各自的工作区替身、临时共享台账目录；输出验证：第一名
    成员启用成功且审计记录托管 agent_id，第二名成员启用被拒绝且共享台账不被改写，其在
    本机无任务时的暂停也被明确拒绝。原启用者暂停后，其他成员才可接管启用。全过程不触碰
    原始证据链，也不允许客户端伪造用户或智能体身份。
    """
    schedule = _load_plugin_module("automation_schedule")
    ledger_dir = tmp_path / "shared-ledger"

    workspace_a = _fake_workspace("agent-a")
    result = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert result["changed"] is True
    assert result["audit"]["agent_id"] == "agent-a"
    assert result["audit"]["operated_by_user_id"] == "user_member_a"

    # 同一成员同一智能体重复启用是幂等无变化，不追加第二条审计。
    again = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert again["changed"] is False

    workspace_b = _fake_workspace("agent-b")
    with pytest.raises(RuntimeError, match="已由其他招商成员或智能体启用"):
        await schedule.set_source_monitor_schedule_enabled(
            workspace_b,
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_b",
            enabled=True,
        )
    assert "xuzhou-source-monitor" not in [
        getattr(job, "id", "") for job in workspace_b.cron_manager.jobs.values()
    ]

    # 计划托管在别处时，本机无任务的“暂停”必须明确失败，不能静默无操作。
    with pytest.raises(RuntimeError, match="本智能体上没有可暂停的任务"):
        await schedule.set_source_monitor_schedule_enabled(
            workspace_b,
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_b",
            enabled=False,
        )

    # 原启用者在原智能体暂停后，审计最新动作变为 paused，其他成员才允许接管启用。
    paused = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=False,
    )
    assert paused["changed"] is True
    taken_over = await schedule.set_source_monitor_schedule_enabled(
        workspace_b,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_b",
        enabled=True,
    )
    assert taken_over["changed"] is True
    assert taken_over["audit"]["agent_id"] == "agent-b"


@pytest.mark.asyncio
async def test_schedule_status_reports_team_host_hint_when_hosted_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成员在自己的智能体上查状态时，能看到计划由团队托管的脱敏提示而不是“未配置”。

    输入是一名成员已在其智能体启用的固定计划和另一名成员的工作区替身；输出验证后者的
    状态查询附带 ``team_host_hint``，且只含最近动作与服务器时间，不含操作者或投递目标。
    从未配置过时提示为空。查询不联网、不执行任务、不写台账。
    """
    schedule = _load_plugin_module("automation_schedule")
    ledger_dir = tmp_path / "shared-ledger"

    empty_status = await schedule.get_source_monitor_schedule_status(
        _fake_workspace("agent-b"), ledger_dir=ledger_dir
    )
    assert empty_status["configured"] is False
    assert empty_status["team_host_hint"] == {}

    host_workspace = _fake_workspace("agent-a")
    await schedule.set_source_monitor_schedule_enabled(
        host_workspace,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    status_elsewhere = await schedule.get_source_monitor_schedule_status(
        _fake_workspace("agent-b"), ledger_dir=ledger_dir
    )
    assert status_elsewhere["configured"] is False
    hint = status_elsewhere["team_host_hint"]
    assert hint["team_last_action"] == "enabled"
    assert hint["hosted_by_current_agent"] is False
    assert "operated_by_user_id" not in hint

    status_on_host = await schedule.get_source_monitor_schedule_status(
        host_workspace, ledger_dir=ledger_dir
    )
    assert status_on_host["configured"] is True
    assert status_on_host["enabled"] is True
    assert "team_host_hint" not in status_on_host


@pytest.mark.asyncio
async def test_outcome_learning_schedule_has_same_single_host_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """每日结果学习计划与来源监控共用同一个“唯一托管者”守卫。

    输入是两个成员的认证身份与各自工作区替身；输出验证第二名成员重复启用被拒绝，且
    学习计划的启用审计同样记录托管 agent_id。守卫不运行学习、不改评分规则或原始证据。
    """
    schedule = _load_plugin_module("automation_schedule")
    ledger_dir = tmp_path / "shared-ledger"

    result = await schedule.set_outcome_learning_schedule_enabled(
        _fake_workspace("agent-a"),
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert result["changed"] is True
    assert result["audit"]["agent_id"] == "agent-a"

    with pytest.raises(RuntimeError, match="已由其他招商成员或智能体启用"):
        await schedule.set_outcome_learning_schedule_enabled(
            _fake_workspace("agent-b"),
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_b",
            enabled=True,
        )

    hint_status = await schedule.get_outcome_learning_schedule_status(
        _fake_workspace("agent-b"), ledger_dir=ledger_dir
    )
    assert hint_status["team_host_hint"]["team_last_action"] == "enabled"


def _strip_latest_audit_agent_id(
    storage, schedule, ledger_dir: Path, job_id: str
) -> None:
    """把指定计划最近一次审计的 agent_id 抹掉，模拟共享台账上线前的历史审计。

    输入是临时共享台账目录与服务器固定 job_id；输出是落盘后的台账仍通过完整性签名，
    但最近一条审计缺失托管智能体字段。函数不改动证据、候选或任何其他审计字段。
    """
    ledger = storage.load_ledger(ledger_dir)
    target: dict | None = None
    for record in ledger[schedule._AUTOMATION_AUDIT_FIELD]:
        if isinstance(record, dict) and record.get("job_id") == job_id:
            target = record
    assert target is not None, "测试前置失败：共享台账中没有该计划的审计"
    target.pop("agent_id", None)
    storage.save_ledger(ledger_dir, ledger)


@pytest.mark.asyncio
async def test_shared_schedule_heals_legacy_audit_missing_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """缺失 agent_id 的历史启用审计必须先“暂停再启用”治愈，不能跨智能体隐身重复运行。

    输入是同一认证用户在两个智能体上的工作区替身，以及一本被抹掉托管智能体的历史
    审计；输出验证：跨智能体启用被拒绝（未知托管者），原托管智能体上的幂等启用同样
    被拒绝（不能跳过托管核对），在原智能体暂停再启用后审计写入真实 agent_id，此后
    同智能体幂等启用恢复无变化。全程不运行采集、不改证据链。
    """
    schedule = _load_plugin_module("automation_schedule")
    storage = _load_plugin_module("storage")
    ledger_dir = tmp_path / "shared-ledger"

    workspace_a = _fake_workspace("agent-a")
    result = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert result["changed"] is True
    assert result["audit"]["agent_id"] == "agent-a"

    _strip_latest_audit_agent_id(
        storage, schedule, ledger_dir, schedule.SOURCE_MONITOR_SCHEDULE_JOB_ID
    )

    # 同一登录用户也不能在另一个智能体上借历史审计重复启用。
    workspace_b = _fake_workspace("agent-b")
    with pytest.raises(RuntimeError, match="未记录托管智能体"):
        await schedule.set_source_monitor_schedule_enabled(
            workspace_b,
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_a",
            enabled=True,
        )

    # 幂等提前返回同样被拦截：本机有启用任务但台账未确认本机为唯一托管者。
    with pytest.raises(RuntimeError, match="未确认本机为唯一托管者"):
        await schedule.set_source_monitor_schedule_enabled(
            workspace_a,
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_a",
            enabled=True,
        )

    # 治愈路径：原托管智能体暂停（写入含 agent_id 的 paused 审计），再重新启用。
    paused = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=False,
    )
    assert paused["changed"] is True
    assert paused["audit"]["agent_id"] == "agent-a"
    healed = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert healed["changed"] is True
    assert healed["audit"]["agent_id"] == "agent-a"

    # 治愈后同一智能体的幂等启用恢复为无变化，也不再追加审计。
    ledger = storage.load_ledger(ledger_dir)
    audit_count = len(ledger[schedule._AUTOMATION_AUDIT_FIELD])
    again = await schedule.set_source_monitor_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert again["changed"] is False
    ledger = storage.load_ledger(ledger_dir)
    assert len(ledger[schedule._AUTOMATION_AUDIT_FIELD]) == audit_count


@pytest.mark.asyncio
async def test_outcome_learning_schedule_heals_legacy_audit_missing_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """结果学习计划的历史审计治愈流程与来源监控一致。

    输入是同一认证用户的工作区替身与被抹掉托管智能体的历史启用审计；输出验证跨智能体
    启用与本机幂等启用均被拒绝，暂停再启用后写入真实 agent_id。守卫不运行学习、不改
    评分规则或原始证据。
    """
    schedule = _load_plugin_module("automation_schedule")
    storage = _load_plugin_module("storage")
    ledger_dir = tmp_path / "shared-ledger"

    workspace_a = _fake_workspace("agent-a")
    await schedule.set_outcome_learning_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    _strip_latest_audit_agent_id(
        storage,
        schedule,
        ledger_dir,
        schedule.OUTCOME_LEARNING_SCHEDULE_JOB_ID,
    )

    with pytest.raises(RuntimeError, match="未记录托管智能体"):
        await schedule.set_outcome_learning_schedule_enabled(
            _fake_workspace("agent-b"),
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_a",
            enabled=True,
        )
    with pytest.raises(RuntimeError, match="未确认本机为唯一托管者"):
        await schedule.set_outcome_learning_schedule_enabled(
            workspace_a,
            ledger_dir=ledger_dir,
            authenticated_user_id="user_member_a",
            enabled=True,
        )

    paused = await schedule.set_outcome_learning_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=False,
    )
    assert paused["changed"] is True
    assert paused["audit"]["agent_id"] == "agent-a"
    healed = await schedule.set_outcome_learning_schedule_enabled(
        workspace_a,
        ledger_dir=ledger_dir,
        authenticated_user_id="user_member_a",
        enabled=True,
    )
    assert healed["changed"] is True
    assert healed["audit"]["agent_id"] == "agent-a"
