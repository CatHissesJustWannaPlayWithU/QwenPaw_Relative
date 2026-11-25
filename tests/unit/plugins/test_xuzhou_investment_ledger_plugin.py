"""徐州招商线索与证据台账第一模块的核心行为测试。"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from qwenpaw.app.principal import Principal
import qwenpaw.security.secret_store as secret_store


PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "tool" / "xuzhou-investment-ledger"
PACKAGE_NAME = "test_xuzhou_investment_ledger"


@pytest.fixture(autouse=True)
def _isolate_platform_evidence_root_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """为插件测试固定平台根密钥，避免测试访问真实钥匙串或用户密钥目录。

    新版证据完整性从 QwenPaw ``secret_store`` 派生工作区密钥；测试只替换其进程内缓存，
    因此不会创建、读取或覆盖开发机钥匙串、SECRET_DIR 或真实招商台账，且每个临时工作区
    仍会因路径不同得到不同派生 HMAC 密钥。
    """
    monkeypatch.setattr(secret_store, "_cached_master_key", bytes.fromhex("5a" * 32))


def _load_plugin_module(module_name: str):
    """以临时包加载指定插件模块，隔离每个测试的模块级状态。

    输入是插件内模块名，输出是兼容相对导入的已加载模块。测试不复用全局模块缓存，
    以防前一项测试的替换或台账状态影响后续认证、证据链和复核断言。
    """
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
    if module_name == "__entry__":
        return module
    return importlib.import_module(f"{PACKAGE_NAME}.{module_name}")


def _load_core():
    """加载招商台账核心模块，供不依赖 HTTP 的业务规则测试使用。"""
    return _load_plugin_module("core")


@pytest.fixture()
def core_module():
    """为每个测试提供独立加载的核心模块。"""
    return _load_core()


def _evidence_arguments(**overrides):
    """构造一条可复用的高质量一手公开证据。"""
    raw_content = b"test public evidence raw response"
    arguments = {
        "entity_name": "示例具身智能企业",
        "entity_kind": "企业",
        "sector": "具身智能",
        "source_url": "https://example.com/news/fundraising",
        "source_title": "企业官方融资公告",
        "source_published_at": "2026-08-24",
        "source_level": "一手",
        "claim_type": "融资",
        "evidence_quality": 5,
        "excerpt": "示例具身智能企业在官方公告中披露完成新一轮融资，并计划拓展具身智能产品研发与商业化。",
        "xuzhou_association_tags": ["业务关联", "产业链"],
        # 测试基线模拟已由服务器候选抓取流程生成的元数据，正式证据不能靠手工声明。
        "capture_metadata": {
            "source_type": "automated_public_search_capture",
            "collection_method": "public_search_then_snapshot",
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "discovery_run_id": "test-discovery-run",
            "discovery_candidate_id": "test-discovery-candidate",
        },
        "raw_content": raw_content,
    }
    arguments.update(overrides)
    if "raw_content" in overrides and "capture_metadata" not in overrides:
        arguments["capture_metadata"] = {
            **arguments["capture_metadata"],
            "raw_sha256": hashlib.sha256(arguments["raw_content"]).hexdigest(),
        }
    return arguments


def _followup_approval_arguments(**overrides):
    """构造完整招商跟进审批卡输入，所有勾选项均是人工业务判断。"""
    arguments = {
        "checklist": {
            "identity_and_subject_checked": True,
            "formal_evidence_checked": True,
            "industry_fit_checked": True,
            "geographic_basis_checked": True,
            "key_claim_corroborated": True,
            "risk_and_conflict_checked": True,
        },
        "business_value_reason": "已核对正式公开证据，企业方向与徐州重点产业匹配，建议进入人工招商跟进。",
        "next_action": "internal_industry_matching",
        "next_action_detail": "由负责人完成产业与载体匹配研判，不自动联系企业。",
        "owner_name": "测试招商主管",
        "target_date": date.today() + timedelta(days=7),
        "review_note": "测试：已完成招商跟进审批卡的全部人工判断。",
        "authenticated_user_id": "verified-followup-approver",
    }
    arguments.update(overrides)
    return arguments


def _eligible_candidate_scope_assessment() -> dict[str, object]:
    """构造已通过服务器范围门槛的候选元数据，供观察流程的离线单测使用。

    测试中的候选由台账 JSON 直接搭建，不会经过真实搜索发现器；因此必须显式模拟发现器
    已完成的“中国/徐州业务机会”筛选。该辅助数据不是正式证据或企业事实，只让测试验证
    后续快照、robots 和审计逻辑不会把无范围评估的历史候选重新放入自动队列。
    """
    return {
        "gate_version": "china_xuzhou_business_opportunity_gate_v1",
        "eligible": True,
        "source_hostname": "example.cn",
        "language_hint": "contains_chinese_characters",
        "china_context_terms": ["中国"],
        "xuzhou_context_terms": [],
        "business_subject_terms": ["企业"],
        "opportunity_signal_terms": ["融资"],
        "rejection_reasons": [],
        "decision_reason": "仅满足自动补证前的中国/徐州业务机会筛选；页面原文仍需核验。",
        "operation_scope": "server_side_public_discovery_scope_filter_only",
    }


def test_empty_legacy_migration_helper_only_accepts_an_empty_unsigned_v1_ledger(
    tmp_path, monkeypatch
):
    """本机迁移助手只能读取空旧台账，不能触及已有证据链或重复迁移。"""
    helper_path = PLUGIN_DIR / "scripts" / "migrate_empty_legacy_ledger.py"
    spec = importlib.util.spec_from_file_location(
        "xuzhou_empty_legacy_migration_helper", helper_path
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    ledger_path = tmp_path / "lead_evidence_ledger.json"
    empty_legacy = {"schema_version": 1, "leads": [], "evidence": []}
    raw_empty_legacy = json.dumps(empty_legacy, ensure_ascii=False).encode("utf-8")
    ledger_path.write_bytes(raw_empty_legacy)
    assert helper.load_empty_unsigned_legacy_ledger_sha256(ledger_path) == hashlib.sha256(
        raw_empty_legacy
    ).hexdigest()

    ledger_path.write_text(
        json.dumps(
            {"schema_version": 1, "leads": [], "evidence": [{"id": "ev-1"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(helper.UserFacingError, match="正式线索或证据"):
        helper.load_empty_unsigned_legacy_ledger_sha256(ledger_path)

    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leads": [],
                "evidence": [],
                "ledger_integrity": {"algorithm": "HMAC-SHA256"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(helper.UserFacingError, match="完整性元数据"):
        helper.load_empty_unsigned_legacy_ledger_sha256(ledger_path)

    # 登录接口的真实字段名为 token；助手必须从该字段取令牌，并且仍由服务端返回
    # 的角色决定是否可以继续，不允许调用者自称为管理员。
    monkeypatch.setattr("builtins.input", lambda _prompt: "test-admin")
    monkeypatch.setattr(helper.getpass, "getpass", lambda _prompt: "hidden-password")
    monkeypatch.setattr(
        helper,
        "_request_json",
        lambda _path, **_kwargs: {
            "token": "server-issued-short-lived-token",
            "user": {"role": "admin"},
        },
    )
    assert helper.authenticate_existing_admin() == "server-issued-short-lived-token"

    monkeypatch.setattr(
        helper,
        "_request_json",
        lambda _path, **_kwargs: {
            "token": "server-issued-user-token",
            "user": {"role": "user"},
        },
    )
    with pytest.raises(helper.UserFacingError, match="不是管理员"):
        helper.authenticate_existing_admin()

    # 迁移接口把不可变审计嵌套在 migration 中；助手只展示服务器返回的审计标识和
    # 操作范围，不能把客户端提供的值写回台账或将成功结果误显示为 null。
    ledger_path.write_bytes(raw_empty_legacy)
    monkeypatch.setattr(helper, "DEFAULT_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(helper, "authenticate_existing_admin", lambda: "test-admin-token")

    def _migration_response(path, **_kwargs):
        if path == "/api/auth/status":
            return {"enabled": True, "has_users": True}
        assert path == "/api/xuzhou-investment-ledger/evidence-integrity/migrate-legacy"
        return {
            "ok": True,
            "migration": {
                "migration_id": "evidence-integrity-migration-test",
                "operation_scope": "admin_verified_legacy_evidence_integrity_migration_only",
            },
            "migrated_evidence_count": 0,
            "ledger_path": str(ledger_path),
        }

    monkeypatch.setattr(helper, "_request_json", _migration_response)
    assert helper.migrate_default_empty_ledger() == {
        "status": "迁移请求已完成",
        "migration_id": "evidence-integrity-migration-test",
        "migrated_evidence_count": 0,
        "operation_scope": "admin_verified_legacy_evidence_integrity_migration_only",
        "ledger_path": str(ledger_path),
    }


def test_empty_ledger_reseal_helper_only_requests_the_admin_protected_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机恢复助手只读空的当前签名台账，并且不传递操作者或证据字段。

    测试输入是一个格式正确但整本 HMAC 已失效的空台账；输出验证助手只将原始字节的
    SHA-256 与核验说明交给管理员接口。它不能把用户名、管理员 ID、签名、证据或来源
    字段放进请求，因此实际操作者始终应由服务端认证主体决定。
    """
    helper_path = PLUGIN_DIR / "scripts" / "migrate_empty_legacy_ledger.py"
    spec = importlib.util.spec_from_file_location(
        "xuzhou_empty_ledger_reseal_helper", helper_path
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    ledger_path = tmp_path / "lead_evidence_ledger.json"
    resealable_empty_ledger = {
        "schema_version": 1,
        "leads": [],
        "evidence": [],
        "evidence_integrity": {
            "scheme": "hmac-sha256-evidence-chain-v2",
            "record_count": 0,
            "chain_head_signature": None,
        },
        "ledger_integrity": {
            "scheme": "hmac-sha256-ledger-v1",
            "signature": "0" * 64,
        },
    }
    raw_ledger = json.dumps(resealable_empty_ledger, ensure_ascii=False).encode("utf-8")
    ledger_path.write_bytes(raw_ledger)
    expected_sha256 = hashlib.sha256(raw_ledger).hexdigest()
    assert helper.load_empty_resealable_ledger_sha256(ledger_path) == expected_sha256

    resealable_empty_ledger["evidence"].append({"evidence_id": "must-not-reseal"})
    ledger_path.write_text(json.dumps(resealable_empty_ledger, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(helper.UserFacingError, match="正式线索或证据"):
        helper.load_empty_resealable_ledger_sha256(ledger_path)

    ledger_path.write_bytes(raw_ledger)
    monkeypatch.setattr(helper, "DEFAULT_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(helper, "authenticate_existing_admin", lambda: "test-admin-token")
    captured_request: dict[str, object] = {}

    def _reseal_response(path: str, **kwargs: object) -> dict[str, object]:
        if path == "/api/auth/status":
            return {"enabled": True, "has_users": True}
        assert path == "/api/xuzhou-investment-ledger/evidence-integrity/reseal-empty-ledger"
        captured_request.update(kwargs)
        return {
            "ok": True,
            "reseal": {
                "reseal_id": "ledger-integrity-reseal-test",
                "operation_scope": "admin_verified_empty_ledger_integrity_reseal_only",
            },
            "ledger_path": str(ledger_path),
        }

    monkeypatch.setattr(helper, "_request_json", _reseal_response)
    assert helper.reseal_default_empty_ledger() == {
        "status": "空台账整本签名恢复请求已完成",
        "reseal_id": "ledger-integrity-reseal-test",
        "operation_scope": "admin_verified_empty_ledger_integrity_reseal_only",
        "ledger_path": str(ledger_path),
    }
    assert captured_request["method"] == "POST"
    assert captured_request["bearer_token"] == "test-admin-token"
    assert captured_request["payload"] == {
        "expected_ledger_sha256": expected_sha256,
        "verification_note": helper.RESEAL_NOTE,
    }


def test_source_monitor_schedule_helper_uses_fixed_authenticated_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计划助手只提交固定动作，不会传递用户 ID、Cron 或工具参数。"""
    helper_path = PLUGIN_DIR / "scripts" / "configure_source_monitor_schedule.py"
    spec = importlib.util.spec_from_file_location("xuzhou_source_monitor_schedule_helper", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    calls: list[tuple[str, dict[str, object]]] = []

    def _response(path: str, **kwargs: object) -> dict[str, object]:
        calls.append((path, kwargs))
        if path == "/api/auth/status":
            return {"enabled": True, "has_users": True}
        assert path == "/api/xuzhou-investment-ledger/source-monitor-schedule/enable"
        return {
            "ok": True,
            "changed": True,
            "configured": True,
            "enabled": True,
            "operation_scope": "authenticated_user_fixed_source_monitor_schedule_only",
        }

    monkeypatch.setattr(helper, "_request_json", _response)
    monkeypatch.setattr(helper, "authenticate_existing_user", lambda: "short-lived-token")
    result = helper.configure_source_monitor_schedule("--enable")
    assert result == {
        "status": "后台来源监控计划操作已完成",
        "action": "--enable",
        "changed": True,
        "schedule": {"configured": True, "enabled": True},
        "operation_scope": "authenticated_user_fixed_source_monitor_schedule_only",
    }
    assert calls == [
        ("/api/auth/status", {}),
        (
            "/api/xuzhou-investment-ledger/source-monitor-schedule/enable",
            {"method": "POST", "bearer_token": "short-lived-token"},
        ),
    ]
    with pytest.raises(helper.UserFacingError, match="仅支持"):
        helper.configure_source_monitor_schedule("--enable --unsafe-cron")


def test_manual_source_monitor_runner_uses_only_fixed_authenticated_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动运行助手只能触发已启用的固定任务，且不提交身份、URL 或工具参数。"""
    helper_path = PLUGIN_DIR / "scripts" / "run_source_monitor_once.py"
    spec = importlib.util.spec_from_file_location("xuzhou_manual_source_monitor_runner", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    calls: list[tuple[str, dict[str, object]]] = []

    def _response(path: str, **kwargs: object) -> dict[str, object]:
        calls.append((path, kwargs))
        if path == "/api/auth/status":
            return {"enabled": True, "has_users": True}
        if path == "/api/xuzhou-investment-ledger/source-monitor-schedule/":
            return {
                "ok": True,
                "schedule": {
                    "configured": True,
                    "enabled": True,
                    "job_id": "xuzhou-investment-public-source-monitor-v1",
                    "fixed_cron": "17 */6 * * *",
                    "timezone": "Asia/Shanghai",
                },
            }
        if path == "/api/cron/jobs/xuzhou-investment-public-source-monitor-v1/run":
            return {"started": True}
        assert path == "/api/cron/jobs/xuzhou-investment-public-source-monitor-v1/state"
        return {"last_run_at": None, "last_status": "running"}

    monkeypatch.setattr(helper._shared, "_request_json", _response)
    monkeypatch.setattr(helper._shared, "authenticate_existing_user", lambda: "short-lived-token")
    result = helper.run_source_monitor_once()
    assert result == {
        "status": "已请求立即执行徐州招商后台来源监控",
        "job_id": "xuzhou-investment-public-source-monitor-v1",
        "trigger": "manual",
        "trigger_result": {"started": True},
        "runtime_state": {"last_run_at": None, "last_status": "running"},
        "schedule": {
            "configured": True,
            "enabled": True,
            "fixed_cron": "17 */6 * * *",
            "timezone": "Asia/Shanghai",
        },
        "operation_scope": "authenticated_user_fixed_manual_source_monitor_run_only",
    }
    assert calls == [
        ("/api/auth/status", {}),
        (
            "/api/xuzhou-investment-ledger/source-monitor-schedule/",
            {"bearer_token": "short-lived-token"},
        ),
        (
            "/api/cron/jobs/xuzhou-investment-public-source-monitor-v1/run",
            {
                "method": "POST",
                "bearer_token": "short-lived-token",
                "timeout_seconds": 300,
            },
        ),
        (
            "/api/cron/jobs/xuzhou-investment-public-source-monitor-v1/state",
            {"bearer_token": "short-lived-token"},
        ),
    ]


def test_manual_source_monitor_runner_refuses_paused_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计划被人工暂停时，助手必须停止，不能借手动入口绕过暂停状态。"""
    helper_path = PLUGIN_DIR / "scripts" / "run_source_monitor_once.py"
    spec = importlib.util.spec_from_file_location("xuzhou_manual_source_monitor_runner_paused", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    calls: list[str] = []

    def _response(path: str, **kwargs: object) -> dict[str, object]:
        calls.append(path)
        if path == "/api/auth/status":
            return {"enabled": True, "has_users": True}
        assert path == "/api/xuzhou-investment-ledger/source-monitor-schedule/"
        return {
            "ok": True,
            "schedule": {
                "configured": True,
                "enabled": False,
                "job_id": "xuzhou-investment-public-source-monitor-v1",
            },
        }

    monkeypatch.setattr(helper._shared, "_request_json", _response)
    monkeypatch.setattr(helper._shared, "authenticate_existing_user", lambda: "short-lived-token")
    with pytest.raises(helper._shared.UserFacingError, match="已暂停"):
        helper.run_source_monitor_once()
    assert calls == [
        "/api/auth/status",
        "/api/xuzhou-investment-ledger/source-monitor-schedule/",
    ]


def test_schedule_helper_translates_network_timeout_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机服务超时时，脚本必须给出可操作提示而不是泄露调用栈或认证细节。"""
    helper_path = PLUGIN_DIR / "scripts" / "configure_source_monitor_schedule.py"
    spec = importlib.util.spec_from_file_location("xuzhou_schedule_timeout_helper", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def _timeout(*args: object, **kwargs: object) -> object:
        raise TimeoutError("socket timed out")

    monkeypatch.setattr(helper, "urlopen", _timeout)
    with pytest.raises(helper.UserFacingError, match="响应超时"):
        helper._request_json("/api/xuzhou-investment-ledger/source-monitor-schedule/")


def test_outcome_learning_schedule_helper_uses_fixed_authenticated_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每日学习助手复用认证层但只允许固定启停/查看动作，不提交权重或身份。"""
    helper_path = PLUGIN_DIR / "scripts" / "configure_outcome_learning_schedule.py"
    spec = importlib.util.spec_from_file_location("xuzhou_outcome_learning_schedule_helper", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    calls: list[tuple[str, dict[str, object]]] = []

    def _response(path: str, **kwargs: object) -> dict[str, object]:
        calls.append((path, kwargs))
        if path == "/api/auth/status":
            return {"enabled": True, "has_users": True}
        assert path == "/api/xuzhou-investment-ledger/outcome-learning-schedule/enable"
        return {
            "ok": True,
            "changed": True,
            "configured": True,
            "enabled": True,
            "operation_scope": "authenticated_user_fixed_outcome_learning_schedule_only",
        }

    monkeypatch.setattr(helper._shared, "_request_json", _response)
    monkeypatch.setattr(helper._shared, "authenticate_existing_user", lambda: "short-lived-token")
    result = helper.configure_outcome_learning_schedule("--enable")
    assert result == {
        "status": "每日结果学习计划操作已完成",
        "action": "--enable",
        "changed": True,
        "schedule": {"configured": True, "enabled": True},
        "operation_scope": "authenticated_user_fixed_outcome_learning_schedule_only",
    }
    assert calls == [
        ("/api/auth/status", {}),
        (
            "/api/xuzhou-investment-ledger/outcome-learning-schedule/enable",
            {"method": "POST", "bearer_token": "short-lived-token"},
        ),
    ]


def test_primary_opportunity_evidence_waits_for_review_without_enterprise_risk_alert(core_module, tmp_path):
    """融资等正向机会只待复核，不能因重要性被误写成企业风险红色预警。"""
    result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())

    assert result["created"] is True
    assert result["lead"]["review_state"] == "pending_user_review"
    assert result["lead"]["red_alert"] is False
    assert result["lead"]["risk_alert_level"] is None
    assert result["lead"]["risk_alert_evidence_ids"] == []
    assert result["evidence"]["xuzhou_association_tags"] == [
        "business_connection",
        "industry_chain",
    ]


def test_primary_explicit_risk_evidence_creates_traceable_critical_alert(core_module, tmp_path):
    """一手原文明确披露资金链断裂时才建严重风险预警，且绑定触发证据。"""
    result = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            claim_type="风险",
            source_title="企业关于风险事项的官方公告",
            excerpt="示例具身智能企业在官方公告中披露资金链断裂，正在推进破产重整程序。",
        ),
    )

    lead = result["lead"]
    assert lead["review_state"] == "pending_user_review"
    assert lead["red_alert"] is True
    assert lead["risk_alert_level"] == "critical"
    assert lead["risk_alert_type"] == "liquidity_or_solvency"
    assert lead["risk_alert_status"] == "open"
    assert lead["risk_alert_evidence_ids"] == [result["evidence"]["evidence_id"]]
    assert "资金链或偿债风险" in lead["alert_reason"]


def test_risk_alert_requires_the_named_entity_in_the_same_source_sentence(core_module, tmp_path):
    """风险词属于其他主体或相隔句子时，不能借风险类别误标当前企业。"""
    result = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/other-company-risk",
            source_title="行业风险观察",
            claim_type="风险",
            excerpt="另一家企业披露资金链断裂。示例具身智能企业正在发布新的具身智能产品。",
        ),
    )

    assert result["lead"]["red_alert"] is False
    assert result["lead"]["risk_alert_level"] is None
    assert result["lead"]["risk_alert_evidence_ids"] == []


def test_subjective_claim_label_without_text_support_is_saved_as_unclassified(core_module, tmp_path):
    """调用方把融资页面称为合作时，只保留请求审计，不触发合作动作或事实分类。"""
    action_center = _load_plugin_module("action_center")
    scoring_rules = _load_plugin_module("scoring_rules")
    result = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/subjective-label",
            claim_type="合作",
            excerpt="示例具身智能企业在官方公告中披露完成新一轮融资，并计划投入产品研发。",
            xuzhou_association_tags=[],
        ),
    )

    evidence = result["evidence"]
    assert evidence["claim_type"] == "unclassified"
    assert evidence["claim_type_requested"] == "cooperation"
    assert evidence["claim_type_matched_terms"] == []
    assert evidence["claim_type_text_support"] == "unclassified_no_fixed_term_match"
    brief = action_center.get_outreach_preparation_brief(
        tmp_path, lead_id=result["lead"]["lead_id"]
    )
    assert {card["action_key"] for card in brief["action_cards"]} == {
        "collect_traceable_evidence_before_outreach"
    }
    score_component = scoring_rules._dimension_components(
        result["lead"],
        [evidence],
        [{"key": "landing_intent", "weight": 100, "enabled": True}],
        today=scoring_rules.date(2026, 8, 25),
    )[0]
    assert score_component["raw_score"] == 0
    assert score_component["evidence_ids"] == []


def test_claim_type_requires_entity_and_fixed_term_in_the_same_sentence(core_module, tmp_path):
    """行业背景中的融资词不能跨句贴到当前企业，避免“同篇报道”被误当成企业事实。"""
    result = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/cross-sentence-financing",
            source_title="行业融资动态汇总",
            claim_type="融资",
            excerpt="另一家企业完成新一轮融资。示例具身智能企业正在展示新的具身智能产品。",
            xuzhou_association_tags=[],
        ),
    )

    evidence = result["evidence"]
    assert evidence["claim_type"] == "unclassified"
    assert evidence["claim_type_requested"] == "financing"
    assert evidence["claim_type_matched_terms"] == []
    assert evidence["claim_type_text_support"] == "unclassified_no_fixed_term_match"


def test_explicit_risk_text_overrides_subjective_positive_claim_label(core_module, tmp_path):
    """明确企业风险不能被调用方写成融资而绕过风险分类和人工红色队列。"""
    result = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/risk-overrides-request",
            source_title="示例具身智能企业风险事项公告",
            claim_type="融资",
            excerpt="示例具身智能企业在官方公告中披露资金链断裂，正在推进破产重整程序。",
        ),
    )

    assert result["evidence"]["claim_type"] == "risk"
    assert result["evidence"]["claim_type_requested"] == "financing"
    assert result["evidence"]["claim_type_text_support"] == "server_reclassified_risk"
    assert result["lead"]["red_alert"] is True
    assert result["lead"]["risk_alert_level"] == "critical"


def test_risk_review_keeps_evidence_and_only_closes_after_final_business_decision(
    core_module, tmp_path
):
    """风险复核只变更状态审计；完整审批卡才可关闭提醒且不删风险依据。"""
    lead_followup = _load_plugin_module("lead_followup")
    created = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/risk-review",
            source_title="示例具身智能企业风险事项公告",
            claim_type="风险",
            excerpt="示例具身智能企业在官方公告中披露资金链紧张，项目已停产等待后续安排。",
        ),
    )
    evidence_before = copy.deepcopy(
        core_module.get_investment_lead(tmp_path, lead_id=created["lead"]["lead_id"])["evidence"]
    )

    need_evidence = core_module.review_investment_lead(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        decision="待补证",
        review_note="需核验公告是否仍反映当前资金与项目状态。",
        reviewer_user_id="verified-risk-reviewer",
    )["lead"]
    assert need_evidence["review_state"] == "need_evidence"
    assert need_evidence["red_alert"] is True
    assert need_evidence["risk_alert_status"] == "need_evidence"
    assert need_evidence["risk_alert_level"] == "critical"
    assert core_module.get_investment_lead(
        tmp_path, lead_id=created["lead"]["lead_id"]
    )["evidence"] == evidence_before

    approved = lead_followup.approve_lead_for_followup(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        **_followup_approval_arguments(
            business_value_reason="风险事项已由业务人员核对并说明处置边界，可在附带风险说明的前提下继续研判。",
            review_note="已人工确认风险处理判断，按审批卡进入后续人工跟进。",
            authenticated_user_id="verified-risk-reviewer",
        ),
    )
    advanced = approved["lead"]
    assert advanced["review_state"] == "advance"
    assert advanced["red_alert"] is False
    assert advanced["risk_alert_status"] == "closed"
    assert advanced["risk_alert_evidence_ids"] == [created["evidence"]["evidence_id"]]
    assert advanced["status_history"][-1]["old_state"] == "need_evidence"
    assert advanced["status_history"][-1]["new_state"] == "advance"
    assert core_module.get_investment_lead(
        tmp_path, lead_id=created["lead"]["lead_id"]
    )["evidence"] == evidence_before


def test_legacy_opportunity_red_flag_is_not_reported_or_prioritized_as_enterprise_risk(
    core_module, tmp_path
):
    """旧版融资飘红只读兼容为非风险，统计和行动中心不能继续误报。"""
    action_center = _load_plugin_module("action_center")
    reports = _load_plugin_module("reports")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    legacy_lead = ledger["leads"][0]
    legacy_lead["red_alert"] = True
    legacy_lead["alert_reason"] = "旧版：融资公告需要优先跟进。"
    storage.save_ledger(tmp_path, ledger)
    evidence_before = copy.deepcopy(
        core_module.get_investment_lead(tmp_path, lead_id=created["lead"]["lead_id"])["evidence"]
    )

    assert core_module.has_open_enterprise_risk_alert(legacy_lead) is False
    center = action_center.get_investment_action_center(tmp_path)
    summary = reports.get_investment_work_summary(tmp_path, window_days=30)
    assert center["summary"]["red_alert_lead_count"] == 0
    assert not any(item["item_kind"] == "red_alert_review" for item in center["items"])
    assert summary["current_totals"]["red_alert_lead_count"] == 0

    reviewed = core_module.review_investment_lead(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        decision="观察",
        review_note="旧版正向机会改按普通招商观察处理。",
        reviewer_user_id="verified-legacy-reviewer",
    )["lead"]
    assert reviewed["red_alert"] is False
    assert reviewed["alert_reason"] is None
    assert core_module.get_investment_lead(
        tmp_path, lead_id=created["lead"]["lead_id"]
    )["evidence"] == evidence_before


def test_startup_team_is_supported_across_evidence_draft_confirmation_and_graph(
    core_module, tmp_path: Path
) -> None:
    """创业团队应走与企业相同的证据与人工确认边界，不能成为绕过入口。

    输入是一条服务器原文快照已验证的创业团队证据；输出证明中文类型会归一化为
    ``startup_team``，草稿确认白名单和关系图谱也接受该受控类型。测试刻意复用正式证据
    ID 登记节点，确保新增主体类型不允许无来源建图，也不改变既有 URL、摘录、采集时间
    或 HMAC 证据链。
    """
    draft_confirmation = _load_plugin_module("evidence_draft_confirmation")
    draft_module = _load_plugin_module("evidence_draft")
    relationship_graph = _load_plugin_module("relationship_graph")
    created = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="徐州未来具身创业团队",
            entity_kind="创业团队",
            excerpt="徐州未来具身创业团队在公开原文中披露完成早期融资，并计划拓展具身智能产品研发。",
        ),
    )

    assert created["lead"]["entity_kind"] == "startup_team"
    assert draft_module._entity_kind_candidate("徐州未来具身创业团队") == "startup_team"
    assert draft_confirmation._draft_capture_fields(
        {
            "candidate_id": "candidate-startup-team",
            "entity_name": "徐州未来具身创业团队",
            "entity_kind": "startup_team",
            "source_published_at": "2026-08-24",
            "claim_type": "financing",
            "excerpt": "公开原文中的融资摘录。",
        }
    )["entity_kind"] == "startup_team"

    node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州未来具身创业团队",
        node_kind="创业团队",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="测试创业团队节点必须引用已有正式公开证据。",
        authenticated_user_id="ordinary-startup-team-user",
    )["node"]
    assert node["node_kind"] == "startup_team"


def test_plugin_entry_uses_distinct_http_prefixes_for_review_and_integrity_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件入口不得重复注册 FastAPI 前缀，否则热更新会卸载旧插件后无法重新装载。

    输入是只记录注册调用的最小插件 API 替身；输出验证普通用户复核根路径与仅管理员可用
    的历史完整性迁移路径不同，同时保留预期的最终迁移 URL 结构。测试不真正挂载路由、不
    写入配置，也不调用工具，因此不会接触工作区、认证或原始证据链。
    """
    entry = _load_plugin_module("__entry__")
    monkeypatch.setattr(entry, "_register_governance_tools", lambda: None)

    class RecordingPluginApi:
        """只保存插件入口登记的路由前缀和工具名，拒绝产生平台副作用。"""

        def __init__(self) -> None:
            self.prefixes: list[str] = []
            self.tools: list[str] = []

        def register_http_router(self, _router, *, prefix: str, tags: list[str]) -> None:
            """记录路由前缀以验证唯一性，不实际挂载 HTTP 路由或变更认证策略。"""
            assert tags == ["xuzhou-investment-ledger"]
            self.prefixes.append(prefix)

        def register_tool(self, *, tool_name: str, **_kwargs) -> None:
            """记录只读或受控工具名称，不执行工具函数、不写入工作区。"""
            self.tools.append(tool_name)

    api = RecordingPluginApi()
    entry.XuzhouInvestmentLedgerPlugin().register(api)

    assert len(api.prefixes) == len(set(api.prefixes))
    assert "/xuzhou-investment-ledger" in api.prefixes
    assert "/xuzhou-investment-ledger/dashboard" in api.prefixes
    assert "/xuzhou-investment-ledger/evidence-integrity" in api.prefixes
    assert "run_xuzhou_source_monitor" in api.tools


def test_plugin_http_routers_mount_together_with_the_expected_migration_url() -> None:
    """全部插件路由应能真实挂载，且管理员迁移 URL 不能因拆分前缀而改变。

    输入是内存中的临时 FastAPI 应用和真实插件路由对象；输出确认所有前缀可同时登记，
    并保留外部约定的迁移地址。测试不启动 HTTP 服务、不发送请求、不读取当前认证身份，
    因此不会接触真实工作区、台账、证据链或运行中的 QwenPaw。
    """
    from fastapi import FastAPI
    from qwenpaw.plugins.registry import PluginRegistry

    entry = _load_plugin_module("__entry__")
    app = FastAPI()
    registry = PluginRegistry()
    registry.set_plugin_http_app(app)
    for router, prefix in (
        (entry.review_router, "/xuzhou-investment-ledger"),
        (entry.dashboard_router, "/xuzhou-investment-ledger/dashboard"),
        (entry.evidence_draft_router, "/xuzhou-investment-ledger/evidence-drafts"),
        (entry.scoring_rules_router, "/xuzhou-investment-ledger/scoring-rules"),
        (entry.source_catalog_router, "/xuzhou-investment-ledger/trusted-sources"),
        (entry.source_recipe_learning_router, "/xuzhou-investment-ledger/source-recipe-learning"),
        (entry.automation_schedule_router, "/xuzhou-investment-ledger/source-monitor-schedule"),
        (entry.dingtalk_daily_digest_router, "/xuzhou-investment-ledger/dingtalk-daily-digest"),
        (entry.outcome_learning_schedule_router, "/xuzhou-investment-ledger/outcome-learning-schedule"),
        (entry.relationship_graph_router, "/xuzhou-investment-ledger/relationship-graph"),
        (entry.relationship_draft_router, "/xuzhou-investment-ledger/relationship-drafts"),
        (entry.co_investment_signal_router, "/xuzhou-investment-ledger/co-investment-signals"),
        (entry.integrity_migration_router, "/xuzhou-investment-ledger/evidence-integrity"),
    ):
        registry.register_http_router(
            "xuzhou-investment-ledger",
            router,
            prefix=prefix,
            tags=["xuzhou-investment-ledger"],
        )

    assert str(app.url_path_for("migrate_xuzhou_legacy_evidence_integrity")) == (
        "/api/xuzhou-investment-ledger/evidence-integrity/migrate-legacy"
    )
    assert str(app.url_path_for("list_xuzhou_dashboard_leads")) == (
        "/api/xuzhou-investment-ledger/dashboard/leads"
    )
    assert str(
        app.url_path_for(
            "get_xuzhou_dashboard_candidate_observation",
            observation_id="candidate-observation-route-test",
        )
    ) == (
        "/api/xuzhou-investment-ledger/dashboard/candidate-observations/"
        "candidate-observation-route-test"
    )
    assert str(app.url_path_for("list_xuzhou_dashboard_discovery_candidates")) == (
        "/api/xuzhou-investment-ledger/dashboard/discovery-candidates"
    )
    assert str(
        app.url_path_for(
            "get_xuzhou_dashboard_candidate_corroboration",
            candidate_id="discovery-candidate-corroboration-route-test",
        )
    ) == (
        "/api/xuzhou-investment-ledger/dashboard/discovery-candidates/"
        "discovery-candidate-corroboration-route-test/corroboration"
    )
    assert str(
        app.url_path_for(
            "review_xuzhou_discovery_candidate",
            candidate_id="discovery-candidate-route-test",
        )
    ) == (
        "/api/xuzhou-investment-ledger/discovery-candidates/"
        "discovery-candidate-route-test/business-review"
    )


def test_signed_evidence_chain_rejects_direct_ledger_tampering(core_module, tmp_path):
    """直接改写台账中的原始摘录后，读取必须拒绝而不能静默继续业务操作。"""
    storage = _load_plugin_module("storage")
    result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger_file = storage.ledger_path(tmp_path)
    raw_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    raw_ledger["evidence"][0]["excerpt"] = "攻击者试图改写已入库原始摘录。"
    ledger_file.write_text(
        json.dumps(raw_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="完整性校验失败"):
        storage.load_ledger(tmp_path)
    assert result["evidence"]["excerpt"] != raw_ledger["evidence"][0]["excerpt"]


def test_signed_legacy_field_set_loads_before_compatibility_defaults_are_added(
    tmp_path: Path,
) -> None:
    """缺少后来字段的可信旧整本签名必须先通过验证，再只在内存补默认结构。

    输入模拟历史迁移后已签名、但尚未拥有草稿、指标、图谱、恢复和后台计划审计字段的
    空台账；输出验证读取时不改文件、不会误报 HMAC 篡改，并在后续受控保存后才以完整
    当前结构重签。该兼容流程不能伪造线索、证据、来源 URL、摘录、采集时间或评分事实。
    """
    storage = _load_plugin_module("storage")
    integrity = _load_plugin_module("evidence_integrity")
    legacy_ledger = storage.empty_ledger()
    missing_later_fields = (
        "evidence_drafts",
        "evidence_draft_confirmation_audit",
        "evidence_draft_business_review_audit",
        "lead_followup_approval_audit",
        "industry_metric_drafts",
        "industry_metric_draft_reviews",
        "industry_business_metrics",
        "relationship_graph_nodes",
        "relationship_graph_edges",
        "relationship_graph_edge_evidence_supplements",
        "relationship_graph_audit",
        "relationship_drafts",
        "relationship_draft_reviews",
        "ledger_integrity_reseals",
        "automation_schedule_audit",
    )
    for field_name in missing_later_fields:
        legacy_ledger.pop(field_name)
    # 签名刻意覆盖当时真实存在的旧字段集合，模拟管理员历史迁移后的落盘结构。
    integrity.sign_ledger_integrity(tmp_path, legacy_ledger)
    ledger_file = storage.ledger_path(tmp_path)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_bytes = (
        json.dumps(legacy_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(legacy_bytes)

    compatible_ledger = storage.load_ledger(tmp_path)
    assert ledger_file.read_bytes() == legacy_bytes
    for field_name in missing_later_fields:
        assert compatible_ledger[field_name] == []
    assert compatible_ledger["leads"] == []
    assert compatible_ledger["evidence"] == []

    storage.save_ledger(tmp_path, compatible_ledger)
    reloaded = storage.load_ledger(tmp_path)
    for field_name in missing_later_fields:
        assert reloaded[field_name] == []
    assert reloaded["ledger_revision"] == legacy_ledger["ledger_revision"] + 1


def test_signed_evidence_snapshot_rejects_direct_raw_content_tampering(core_module, tmp_path: Path) -> None:
    """替换私有原文快照时，即使台账 JSON 未变，读取也必须失败关闭。"""
    storage = _load_plugin_module("storage")
    result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    snapshot_path = tmp_path / result["evidence"]["raw_snapshot_relative_path"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["raw_content_base64"] = base64.b64encode(b"tampered raw response").decode("ascii")
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="原始快照与已签名证据不一致"):
        storage.load_ledger(tmp_path)


def test_signed_ledger_rejects_direct_review_audit_tampering(core_module, tmp_path: Path) -> None:
    """只篡改复核状态或审计记录、保持 evidence 不变时，也必须拒绝读取。"""
    storage = _load_plugin_module("storage")
    result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger_file = storage.ledger_path(tmp_path)
    raw_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    raw_ledger["leads"][0]["review_state"] = "advance"
    raw_ledger["leads"][0]["status_history"].append(
        {
            "state": "advance",
            "reason": "攻击者直接修改本地 JSON。",
            "at": "2026-08-27T00:00:00+00:00",
            "reviewer_user_id": "forged-user",
        }
    )
    ledger_file.write_text(
        json.dumps(raw_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="台账完整性校验失败"):
        storage.load_ledger(tmp_path)
    assert result["lead"]["review_state"] == "pending_user_review"


def test_ledger_storage_rejects_private_directory_symlink_escape(tmp_path: Path) -> None:
    """工作区私有目录若被替换为符号链接，台账和快照都不得写到链接目标。"""
    storage = _load_plugin_module("storage")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    private_directory = tmp_path / "investment_attraction"
    private_directory.symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="私有目录不能是符号链接"):
        storage.load_ledger(tmp_path)
    with pytest.raises(ValueError, match="私有目录不能是符号链接"):
        storage.save_ledger(tmp_path, storage.empty_ledger())
    assert list(outside_directory.iterdir()) == []


def test_admin_legacy_evidence_integrity_migration_preserves_original_evidence(
    core_module,
    tmp_path: Path,
) -> None:
    """管理员可信迁移只新增 v2 签名和审计，不能修改既有来源证据字段。"""
    storage = _load_plugin_module("storage")
    created = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    original_evidence = copy.deepcopy(created["evidence"])
    ledger_file = storage.ledger_path(tmp_path)
    legacy_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    legacy_ledger.pop("evidence_integrity")
    legacy_ledger["evidence"][0].pop("integrity_signature")
    legacy_bytes = (
        json.dumps(legacy_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(legacy_bytes)

    result = storage.migrate_verified_legacy_evidence_integrity(
        tmp_path,
        expected_legacy_ledger_sha256=hashlib.sha256(legacy_bytes).hexdigest(),
        verification_note="管理员已离线核对历史来源快照、原始摘录和迁移前台账哈希。",
        authenticated_admin_user_id="admin-migration-user",
    )

    migrated_ledger = storage.load_ledger(tmp_path)
    migrated_evidence = migrated_ledger["evidence"][0]
    assert {
        key: value for key, value in migrated_evidence.items() if key != "integrity_signature"
    } == {
        key: value for key, value in original_evidence.items() if key != "integrity_signature"
    }
    assert migrated_ledger["evidence_integrity"]["scheme"] == "hmac-sha256-evidence-chain-v2"
    assert result["migrated_evidence_count"] == 1
    assert migrated_ledger["evidence_integrity_migrations"] == [result["migration"]]
    assert result["migration"]["migrated_by_admin_user_id"] == "admin-migration-user"
    assert result["migration"]["operation_scope"] == (
        "admin_verified_legacy_evidence_integrity_migration_only"
    )


def test_admin_migration_adds_ledger_signature_without_resigning_current_evidence(
    core_module,
    tmp_path: Path,
) -> None:
    """v2 证据链缺少整本台账签名时，管理员只能补签台账，不能改写证据。"""
    storage = _load_plugin_module("storage")
    created = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    original_evidence = copy.deepcopy(created["evidence"])
    ledger_file = storage.ledger_path(tmp_path)
    legacy_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    legacy_ledger.pop("ledger_integrity")
    legacy_bytes = (
        json.dumps(legacy_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(legacy_bytes)

    result = storage.migrate_verified_legacy_evidence_integrity(
        tmp_path,
        expected_legacy_ledger_sha256=hashlib.sha256(legacy_bytes).hexdigest(),
        verification_note="管理员已核验 v2 证据链，只补齐整本台账签名。",
        authenticated_admin_user_id="admin-ledger-signature-migration-user",
    )

    migrated_ledger = storage.load_ledger(tmp_path)
    assert migrated_ledger["evidence"] == [original_evidence]
    assert migrated_ledger["ledger_integrity"]["scheme"] == "hmac-sha256-ledger-v1"
    assert result["migrated_evidence_count"] == 0


def test_admin_legacy_migration_rejects_unverified_ledger_hash_without_writing(
    core_module,
    tmp_path: Path,
) -> None:
    """管理员迁移前文件哈希不一致时必须失败关闭，不能重签或修改历史台账。"""
    storage = _load_plugin_module("storage")
    core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger_file = storage.ledger_path(tmp_path)
    legacy_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    legacy_ledger.pop("evidence_integrity")
    legacy_ledger["evidence"][0].pop("integrity_signature")
    legacy_bytes = (
        json.dumps(legacy_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(legacy_bytes)

    with pytest.raises(RuntimeError, match="SHA-256与管理员核验值不一致"):
        storage.migrate_verified_legacy_evidence_integrity(
            tmp_path,
            expected_legacy_ledger_sha256="0" * 64,
            verification_note="故意使用错误哈希，验证迁移失败关闭。",
            authenticated_admin_user_id="admin-migration-user",
        )
    assert ledger_file.read_bytes() == legacy_bytes


def test_admin_reseals_only_empty_hmac_mismatch_without_touching_business_fields(
    tmp_path: Path,
) -> None:
    """整本恢复仅重签确实失效的空台账，且只改允许的安全字段。

    输入是由正常存储层创建、随后仅破坏整本签名的空台账；输出验证恢复追加服务器审计并
    重新通过 HMAC 校验。测试把所有非签名、修订时间、修订号和恢复审计字段逐字比较，
    确保恢复不会修改来源目录、评分规则、历史迁移审计或任何可能存在的原始证据字段。
    """
    storage = _load_plugin_module("storage")
    storage.save_ledger(tmp_path, storage.empty_ledger())
    ledger_file = storage.ledger_path(tmp_path)
    tampered_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    protected_before = copy.deepcopy(
        {
            key: value
            for key, value in tampered_ledger.items()
            if key
            not in {
                "ledger_integrity",
                "ledger_revision",
                "updated_at",
                "ledger_integrity_reseals",
            }
        }
    )
    tampered_ledger["ledger_integrity"]["signature"] = "0" * 64
    tampered_bytes = (
        json.dumps(tampered_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(tampered_bytes)
    expected_sha256 = hashlib.sha256(tampered_bytes).hexdigest()

    result = storage.reseal_verified_empty_ledger_integrity(
        tmp_path,
        expected_ledger_sha256=expected_sha256,
        verification_note="管理员已核验本机当前空台账哈希，仅恢复整本签名。",
        authenticated_admin_user_id="server-authenticated-admin",
    )

    restored_ledger = storage.load_ledger(tmp_path)
    protected_after = {
        key: value
        for key, value in restored_ledger.items()
        if key
        not in {
            "ledger_integrity",
            "ledger_revision",
            "updated_at",
            "ledger_integrity_reseals",
        }
    }
    assert protected_after == protected_before
    assert restored_ledger["leads"] == []
    assert restored_ledger["evidence"] == []
    assert restored_ledger["ledger_integrity_reseals"] == [result["reseal"]]
    assert result["reseal"]["resealed_by_admin_user_id"] == "server-authenticated-admin"
    assert result["reseal"]["pre_reseal_ledger_sha256"] == expected_sha256
    assert ledger_file.read_bytes() != tampered_bytes

    already_verified_bytes = ledger_file.read_bytes()
    with pytest.raises(ValueError, match="完整性校验已通过"):
        storage.reseal_verified_empty_ledger_integrity(
            tmp_path,
            expected_ledger_sha256=hashlib.sha256(already_verified_bytes).hexdigest(),
            verification_note="再次调用必须被拒绝。",
            authenticated_admin_user_id="server-authenticated-admin",
        )
    assert ledger_file.read_bytes() == already_verified_bytes


def test_admin_reseal_rejects_formal_evidence_without_writing(
    core_module,
    tmp_path: Path,
) -> None:
    """即使管理员已核验 SHA-256，含正式证据的台账也绝不能重签恢复。"""
    storage = _load_plugin_module("storage")
    core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger_file = storage.ledger_path(tmp_path)
    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    ledger["ledger_integrity"]["signature"] = "0" * 64
    protected_bytes = (
        json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(protected_bytes)

    with pytest.raises(ValueError, match="没有正式线索和正式证据"):
        storage.reseal_verified_empty_ledger_integrity(
            tmp_path,
            expected_ledger_sha256=hashlib.sha256(protected_bytes).hexdigest(),
            verification_note="有正式证据时必须拒绝恢复。",
            authenticated_admin_user_id="server-authenticated-admin",
        )
    assert ledger_file.read_bytes() == protected_bytes


@pytest.mark.asyncio
async def test_legacy_integrity_migration_api_requires_admin_and_rejects_claimed_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """历史证据迁移接口只信任服务器管理员身份，普通用户不能触发或伪造操作者。"""
    migration_api = _load_plugin_module("integrity_migration_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger_file = storage.ledger_path(tmp_path)
    legacy_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    legacy_ledger.pop("evidence_integrity")
    legacy_ledger["evidence"][0].pop("integrity_signature")
    legacy_bytes = (
        json.dumps(legacy_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(legacy_bytes)
    request_body = migration_api.MigrateLegacyEvidenceIntegrityRequest.model_validate(
        {
            "expected_legacy_ledger_sha256": hashlib.sha256(legacy_bytes).hexdigest(),
            "verification_note": "管理员已完成离线来源与迁移前文件哈希核验。",
        }
    )
    ordinary_principal = Principal(
        user_id="ordinary-user",
        username="ordinary-user",
        role="user",
    )
    monkeypatch.setattr(
        migration_api,
        "get_current_principal",
        lambda _request: ordinary_principal,
    )

    monkeypatch.setattr(
        migration_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    with pytest.raises(HTTPException) as denied:
        await migration_api.migrate_xuzhou_legacy_evidence_integrity(request_body, object())
    assert denied.value.status_code == 403
    with pytest.raises(ValidationError):
        migration_api.MigrateLegacyEvidenceIntegrityRequest.model_validate(
            {
                "expected_legacy_ledger_sha256": hashlib.sha256(legacy_bytes).hexdigest(),
                "verification_note": "尝试从请求体伪造管理员身份。",
                "authenticated_admin_user_id": "attacker-user",
            }
        )

    admin_principal = Principal(
        user_id="admin-migration-user",
        username="admin-migration-user",
        role="admin",
    )
    monkeypatch.setattr(
        migration_api,
        "get_current_principal",
        lambda _request: admin_principal,
    )
    response = await migration_api.migrate_xuzhou_legacy_evidence_integrity(request_body, object())
    assert response["ok"] is True
    assert response["migration"]["migrated_by_admin_user_id"] == admin_principal.user_id
    assert storage.load_ledger(tmp_path)["evidence_integrity"]["scheme"] == (
        "hmac-sha256-evidence-chain-v2"
    )


@pytest.mark.asyncio
async def test_empty_ledger_reseal_api_requires_admin_and_uses_server_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """恢复接口只接受管理员主体，且恢复审计的操作者来自服务器认证身份。"""
    migration_api = _load_plugin_module("integrity_migration_api")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    storage.save_ledger(tmp_path, storage.empty_ledger())
    ledger_file = storage.ledger_path(tmp_path)
    invalid_ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    invalid_ledger["ledger_integrity"]["signature"] = "0" * 64
    invalid_bytes = (
        json.dumps(invalid_ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ledger_file.write_bytes(invalid_bytes)
    request_body = migration_api.ResealEmptyLedgerIntegrityRequest.model_validate(
        {
            "expected_ledger_sha256": hashlib.sha256(invalid_bytes).hexdigest(),
            "verification_note": "管理员已核验空台账当前哈希，申请恢复整本签名。",
        }
    )

    monkeypatch.setattr(
        migration_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(
        migration_api,
        "get_current_principal",
        lambda _request: Principal(user_id="ordinary-user", username="ordinary-user", role="user"),
    )
    with pytest.raises(HTTPException) as denied:
        await migration_api.reseal_xuzhou_empty_ledger_integrity(request_body, object())
    assert denied.value.status_code == 403
    with pytest.raises(ValidationError):
        migration_api.ResealEmptyLedgerIntegrityRequest.model_validate(
            {
                "expected_ledger_sha256": hashlib.sha256(invalid_bytes).hexdigest(),
                "verification_note": "尝试伪造管理员身份。",
                "authenticated_admin_user_id": "attacker-user",
            }
        )

    admin_principal = Principal(
        user_id="server-admin-user",
        username="server-admin-user",
        role="admin",
    )
    monkeypatch.setattr(
        migration_api,
        "get_current_principal",
        lambda _request: admin_principal,
    )
    response = await migration_api.reseal_xuzhou_empty_ledger_integrity(request_body, object())
    assert response["ok"] is True
    assert response["reseal"]["resealed_by_admin_user_id"] == admin_principal.user_id
    assert storage.load_ledger(tmp_path)["ledger_integrity_reseals"] == [response["reseal"]]


def test_secondary_source_does_not_create_red_alert(core_module, tmp_path):
    """二手报道可等待用户复核，但不会以弱来源触发红色预警。"""
    result = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(source_level="二手", source_url="https://example.com/media/report"),
    )

    assert result["lead"]["review_state"] == "pending_user_review"
    assert result["lead"]["red_alert"] is False


def test_formal_evidence_rejects_hand_declared_source_fields(core_module, tmp_path):
    """缺少服务器抓取元数据的手工文本不能被写成正式证据或触发业务预警。"""
    arguments = _evidence_arguments()
    arguments.pop("capture_metadata")

    with pytest.raises(ValueError, match="必须来自服务器受控抓取"):
        core_module.record_public_evidence(tmp_path, **arguments)
    assert core_module.list_investment_leads(tmp_path)["leads"] == []


def test_duplicate_evidence_is_not_written_twice(core_module, tmp_path):
    """同一来源和摘要重复登记时，台账保持单条证据，避免人为放大信号。"""
    first_result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    second_result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())

    assert first_result["created"] is True
    assert second_result["created"] is False
    details = core_module.get_investment_lead(tmp_path, lead_id=first_result["lead"]["lead_id"])
    assert len(details["evidence"]) == 1


def test_followup_approval_records_audit_history_without_changing_evidence(core_module, tmp_path):
    """完整招商审批卡才可推进，且不得改变已有原始证据。"""
    lead_followup = _load_plugin_module("lead_followup")
    result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(
        core_module.get_investment_lead(tmp_path, lead_id=result["lead"]["lead_id"])["evidence"]
    )
    with pytest.raises(ValueError, match="招商跟进审批卡"):
        core_module.review_investment_lead(
            tmp_path,
            lead_id=result["lead"]["lead_id"],
            decision="advance",
            review_note="不能用一条泛化备注绕过完整审批卡。",
            reviewer_user_id="verified-user-001",
        )
    review_result = lead_followup.approve_lead_for_followup(
        tmp_path,
        lead_id=result["lead"]["lead_id"],
        **_followup_approval_arguments(
            business_value_reason="已核验企业官网公告和公开业务关联信息，可进入招商跟进池。",
            review_note="已核验企业官网公告和公开业务关联信息，可进入招商跟进池。",
            authenticated_user_id="verified-user-001",
        ),
    )

    assert review_result["lead"]["review_state"] == "advance"
    assert review_result["lead"]["red_alert"] is False
    audit_record = review_result["lead"]["status_history"][-1]
    assert audit_record["reviewer_user_id"] == "verified-user-001"
    assert audit_record["old_state"] == "pending_user_review"
    assert audit_record["new_state"] == "advance"
    assert audit_record["review_note"] == "已核验企业官网公告和公开业务关联信息，可进入招商跟进池。"
    assert audit_record["operation_scope"] == "authenticated_team_member_human_followup_approval_only"
    assert review_result["approval"]["owner_name"] == "测试招商主管"
    assert review_result["card"]["state"] == "approved_for_followup"
    evidence_after = core_module.get_investment_lead(
        tmp_path, lead_id=result["lead"]["lead_id"]
    )["evidence"]
    assert evidence_after == evidence_before


def test_list_filters_by_sector_and_review_state(core_module, tmp_path):
    """列表查询必须同时按产业和复核状态过滤，避免不同招商方向的线索混在一起。"""
    embodied_result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="示例半导体企业",
            sector="半导体",
            source_url="https://example.com/news/semiconductor",
            source_title="媒体产业报道",
            source_level="二手",
        ),
    )

    filtered = core_module.list_investment_leads(
        tmp_path,
        sector="具身智能",
        review_state="等待用户复核",
    )

    assert filtered["count"] == 1
    assert filtered["leads"][0]["lead_id"] == embodied_result["lead"]["lead_id"]


@pytest.mark.asyncio
async def test_followup_approval_api_uses_verified_principal_and_rejects_claimed_reviewer(
    monkeypatch, tmp_path
):
    """招商审批接口只接受受控业务字段，并将认证主体写入审计记录。

    测试用服务器侧 ``Principal`` 模拟认证中间件结果，客户端请求体故意不含操作者
    ID；额外传入 ``reviewer_user_id`` 必须被请求模型拒绝，从而防止前端或模型伪造
    审核人。复核完成后再次读取证据，确认接口没有覆盖原始证据链。
    """
    review_api = _load_plugin_module("review_api")
    core_module = importlib.import_module(f"{PACKAGE_NAME}.core")
    created = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(
        core_module.get_investment_lead(tmp_path, lead_id=created["lead"]["lead_id"])["evidence"]
    )
    verified_principal = Principal(
        user_id="ordinary-user-001",
        username="ordinary-user",
        role="user",
    )

    monkeypatch.setattr(
        review_api,
        "get_current_principal",
        lambda _request: verified_principal,
    )

    monkeypatch.setattr(
        review_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    response = await review_api.approve_xuzhou_lead_for_followup(
        created["lead"]["lead_id"],
        review_api.ApproveLeadForFollowupRequest(
            identity_and_subject_checked=True,
            formal_evidence_checked=True,
            industry_fit_checked=True,
            geographic_basis_checked=True,
            key_claim_corroborated=True,
            risk_and_conflict_checked=True,
            business_value_reason="普通登录用户已完成公开来源核验，建议进入招商跟进。",
            next_action="internal_industry_matching",
            next_action_detail="由业务人员完成内部产业匹配，不自动联系企业。",
            owner_name="普通用户负责人",
            target_date=date.today() + timedelta(days=7),
            review_note="普通登录用户已完成审批卡并确认进入招商跟进。",
        ),
        object(),
    )

    assert response["ok"] is True
    assert response["lead"]["review_state"] == "advance"
    assert response["lead"]["status_history"][-1]["reviewer_user_id"] == "ordinary-user-001"
    assert response["approval"]["approved_by_user_id"] == "ordinary-user-001"
    evidence_after = core_module.get_investment_lead(
        tmp_path, lead_id=created["lead"]["lead_id"]
    )["evidence"]
    assert evidence_after == evidence_before

    with pytest.raises(ValidationError):
        review_api.ApproveLeadForFollowupRequest.model_validate(
            {
                "identity_and_subject_checked": True,
                "formal_evidence_checked": True,
                "industry_fit_checked": True,
                "geographic_basis_checked": True,
                "key_claim_corroborated": True,
                "risk_and_conflict_checked": True,
                "business_value_reason": "试图伪造操作者。",
                "next_action": "internal_industry_matching",
                "next_action_detail": "仅用于测试。",
                "owner_name": "测试负责人",
                "target_date": str(date.today() + timedelta(days=7)),
                "review_note": "仅用于测试。",
                "reviewer_user_id": "attacker-user",
            }
        )


@pytest.mark.asyncio
async def test_discovery_candidate_business_review_is_append_only_and_uses_verified_principal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通用户可处置发现候选，但接口绝不能改写候选或证据链。

    测试先构造一条仅供人工补证的服务器发现候选及正式证据，再由服务端 Principal 追加“无效”
    判断。输出必须证明操作者来自认证中间件、候选原始链接/摘要/发现时间、观察列表和正式
    evidence 均逐字不变；请求体若夹带 ``reviewer_user_id`` 则会被模型拒绝，防止前端、模型
    或调用方伪造操作者或借复核接口覆盖原始材料。
    """
    review_api = _load_plugin_module("review_api")
    candidate_review = importlib.import_module(f"{PACKAGE_NAME}.candidate_review")
    core_module = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    candidate_id = "discovery-candidate-business-review-test"
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-business-review-test",
            "source_url": "https://example.cn/company/funding",
            "source_title": "待补证企业公开页面",
            "search_snippet": "页面摘要仅供人工判断是否补证。",
            "sector": "semiconductor",
            "source_catalog_id": "trusted-investment-index",
            "discovery_query": "服务器固定来源目录查询",
            "discovered_at": "2026-09-07T00:00:00+00:00",
            "source_type": "fixed_investment_event_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
            "requires_china_business_verification": True,
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    protected_before = {
        field_name: copy.deepcopy(storage.load_ledger(tmp_path)[field_name])
        for field_name in (
            "discovery_candidates",
            "candidate_observations",
            "leads",
            "evidence",
            "score_snapshots",
            "outcome_feedback",
        )
    }
    verified_principal = Principal(
        user_id="ordinary-user-002",
        username="ordinary-user-two",
        role="user",
    )
    monkeypatch.setattr(review_api, "get_current_principal", lambda _request: verified_principal)
    monkeypatch.setattr(
        review_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )

    response = await review_api.review_xuzhou_discovery_candidate(
        candidate_id,
        review_api.ReviewDiscoveryCandidateRequest(
            decision="invalid",
            review_note="当前仅有单一公开页面，缺少中国业务或徐州关联的可追溯补证。",
        ),
        object(),
    )

    assert response["ok"] is True
    assert response["candidate"]["business_review_state"] == "invalid"
    assert response["review"]["old_review_state"] == "pending_user_review"
    assert response["review"]["new_review_state"] == "invalid"
    assert response["review"]["reviewed_by_user_id"] == "ordinary-user-002"
    ledger_after = storage.load_ledger(tmp_path)
    for field_name, before_value in protected_before.items():
        assert ledger_after[field_name] == before_value
    assert len(ledger_after["candidate_business_reviews"]) == 1
    assert candidate_review.invalid_discovery_candidate_ids(ledger_after) == {candidate_id}
    listed = candidate_review.list_discovery_candidates(tmp_path, limit=10)
    assert listed["items"][0]["business_review_state"] == "invalid"

    with pytest.raises(ValidationError):
        review_api.ReviewDiscoveryCandidateRequest.model_validate(
            {
                "decision": "observe",
                "review_note": "试图伪造候选复核操作者。",
                "reviewer_user_id": "attacker-user",
            }
        )


def test_human_xuzhou_waiver_requires_china_basis_and_never_mutates_candidate_or_evidence(
    tmp_path: Path,
) -> None:
    """“无需徐州字段”必须是可审计的人类判断，而非直接入证旁路。"""
    candidate_review = _load_plugin_module("candidate_review")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    candidate_id = "discovery-candidate-human-xuzhou-waiver"
    ledger = storage.empty_ledger()
    candidate = {
        "candidate_id": candidate_id,
        "run_id": "discovery-run-human-xuzhou-waiver",
        "source_url": "https://media.example.com/financing/item",
        "source_title": "高价值中国企业融资页面",
        "search_snippet": "融资事件，待人工补足地域判断。",
        "sector": "biomedicine",
        "source_catalog_id": "trusted-media",
        "discovery_query": "服务器固定来源目录查询",
        "discovered_at": "2026-09-09T00:00:00+00:00",
        "source_type": "trusted_source_directory_discovery",
        "source_level": "lead_only",
        "state": "discovered",
        "capture_attempts": 0,
        "captured_evidence_id": None,
        "last_capture_error": None,
        "last_captured_at": None,
        "requires_china_business_verification": True,
        "eligibility_assessment": {
            **_eligible_candidate_scope_assessment(),
            "requires_china_business_verification": True,
        },
    }
    ledger["discovery_candidates"] = [copy.deepcopy(candidate)]
    storage.save_ledger(tmp_path, ledger)
    protected_before = copy.deepcopy(storage.load_ledger(tmp_path)["discovery_candidates"])

    with pytest.raises(ValueError, match="企业来源地"):
        candidate_review.review_discovery_candidate(
            tmp_path,
            candidate_id=candidate_id,
            decision="waive_xuzhou",
            review_note="没有徐州字段但希望进入正式证据。",
            authenticated_user_id="ordinary-user-geo",
        )

    result = candidate_review.review_discovery_candidate(
        tmp_path,
        candidate_id=candidate_id,
        decision="waive_xuzhou",
        review_note="已人工核对企业在北京经营；本条高价值融资材料无需徐州字段。",
        authenticated_user_id="ordinary-user-geo",
        enterprise_origin={
            "origin": "北京",
            "origin_kind": "headquarters",
            "evidence_basis": "已阅读保存页面和企业公开介绍，明确写明总部位于北京并在中国开展业务。",
            "china_business_confirmed": True,
        },
    )

    assert result["candidate"]["business_review_state"] == "waive_xuzhou"
    assert result["candidate"]["geographic_scope"]["can_request_server_recapture"] is True
    assert result["candidate"]["geographic_scope"]["enterprise_origin"]["origin"] == "北京"
    after = storage.load_ledger(tmp_path)
    assert after["discovery_candidates"] == protected_before
    assert after["evidence"] == []
    assert after["candidate_business_reviews"][-1]["geographic_scope_review"]["decision"] == "xuzhou_not_required_for_this_material"
    assert candidate_review.candidate_recapture_scope_blocker(after, candidate_id=candidate_id) is None


@pytest.mark.asyncio
async def test_dashboard_lists_discovery_candidates_without_exposing_internal_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """工作台必须展示刚发现候选，且浏览器只能读到脱敏摘要。

    输入是带服务器候选 ID 的临时共享台账与模拟认证目录；输出保留人工判断所需的来源链接、
    摘要、产业及中国业务待核验标记，但不暴露台账文件路径、原始快照路径或完整性字段。读取
    前后候选记录保持相同，证明“展示候选”不会触发抓取、补证或证据链改写。
    """
    dashboard_api = _load_plugin_module("dashboard_api")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    ledger = storage.empty_ledger()
    ledger["discovery_candidates"] = [
        {
            "candidate_id": "discovery-candidate-dashboard-test",
            "run_id": "discovery-run-dashboard-test",
            "source_url": "https://example.cn/company/round-a",
            "source_title": "示例中国企业融资页面",
            "search_snippet": "B轮融资，待补充徐州关联。",
            "sector": "biomedicine",
            "source_catalog_id": "trusted-investment-index",
            "discovery_query": "服务器固定来源目录查询",
            "discovered_at": "2026-09-07T00:00:00+00:00",
            "source_type": "fixed_investment_event_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "requires_china_business_verification": True,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    candidates_before = copy.deepcopy(storage.load_ledger(tmp_path)["discovery_candidates"])
    monkeypatch.setattr(
        dashboard_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )

    response = await dashboard_api.list_xuzhou_dashboard_discovery_candidates(
        object(),
        limit=10,
    )

    assert response["ok"] is True
    assert response["count"] == 1
    candidate = response["items"][0]
    assert candidate["source_url"] == "https://example.cn/company/round-a"
    assert candidate["business_review_state"] == "pending_user_review"
    assert candidate["requires_china_business_verification"] is True
    assert "ledger_path" not in response
    assert "snapshot_relative_path" not in candidate
    assert storage.load_ledger(tmp_path)["discovery_candidates"] == candidates_before


@pytest.mark.asyncio
async def test_dashboard_candidate_corroboration_route_uses_server_id_and_hides_internal_material(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """第二来源详情路由只传服务器候选 ID，并在 HTTP 响应前剥离敏感存储字段。

    输入是模拟的已认证共享目录和详情读取器；输出验证路由只把路径中的候选 ID 交给核心
    读取服务，且统一脱敏后才返回浏览器。替身故意包含 Base64、快照相对路径和台账路径，
    因而本测试不依赖真实网络或快照文件，也能证明工作台路由不是泄露原始材料的旁路。
    """
    dashboard_api = _load_plugin_module("dashboard_api")
    candidate_id = "discovery-candidate-corroboration-api-test"
    received: dict[str, object] = {}

    def fake_read_corroboration(
        workspace_dir: Path,
        *,
        candidate_id: str,
    ) -> dict[str, object]:
        """记录路由输入并返回含敏感字段的服务器内部样本，不读写任何真实台账。"""
        received["workspace_dir"] = workspace_dir
        received["candidate_id"] = candidate_id
        return {
            "corroboration": {"candidate_id": candidate_id},
            "snapshot": {
                "visible_text": "供人工核验的有限正文。",
                "raw_content_base64": "server-only-raw-content",
                "snapshot_relative_path": "investment_attraction/candidate_corroboration_snapshots/internal.json",
            },
            "ledger_path": "/server/private/lead_evidence_ledger.json",
        }

    monkeypatch.setattr(
        dashboard_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(
        dashboard_api,
        "get_candidate_official_link_corroboration",
        fake_read_corroboration,
    )

    response = await dashboard_api.get_xuzhou_dashboard_candidate_corroboration(
        candidate_id,
        object(),
    )

    assert response["ok"] is True
    assert received == {"workspace_dir": tmp_path.resolve(), "candidate_id": candidate_id}
    assert response["corroboration"]["candidate_id"] == candidate_id
    assert response["snapshot"] == {"visible_text": "供人工核验的有限正文。"}
    response_as_json = json.dumps(response, ensure_ascii=False)
    assert "raw_content_base64" not in response_as_json
    assert "snapshot_relative_path" not in response_as_json
    assert "ledger_path" not in response_as_json


@pytest.mark.asyncio
async def test_dashboard_api_is_readonly_and_hides_internal_evidence_storage_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """工作台读取接口只返回人工可审查字段，不能泄露本机快照位置或改写证据链。

    输入是一条已签名正式证据与被替换为临时共享目录守卫的认证请求；输出确认工作台保留
    URL、摘录、采集时间和服务器分类，却剥离快照路径、原始 Base64 与 HMAC 字段。调用
    前后完整 evidence 必须完全一致，证明浏览器读取不会把只读详情变成证据修改入口。
    """
    dashboard_api = _load_plugin_module("dashboard_api")
    core_module = importlib.import_module(f"{PACKAGE_NAME}.core")
    created = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(
        core_module.get_investment_lead(tmp_path, lead_id=created["lead"]["lead_id"])["evidence"]
    )
    monkeypatch.setattr(
        dashboard_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )

    response = await dashboard_api.get_xuzhou_dashboard_lead(
        created["lead"]["lead_id"],
        object(),
    )

    assert response["ok"] is True
    public_evidence = response["evidence"][0]
    assert public_evidence["source_url"] == created["evidence"]["source_url"]
    assert public_evidence["excerpt"] == created["evidence"]["excerpt"]
    assert public_evidence["source_published_at"] == created["evidence"]["source_published_at"]
    assert public_evidence["claim_type"] == created["evidence"]["claim_type"]
    assert "ledger_path" not in response
    assert "raw_snapshot_relative_path" not in public_evidence
    assert "integrity_signature" not in public_evidence
    evidence_after = core_module.get_investment_lead(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
    )["evidence"]
    assert evidence_after == evidence_before


def test_plugin_registers_authenticated_review_router(monkeypatch):
    """插件启动时必须注册复核 HTTP 路由，但不把复核注册为模型工具。

    测试使用只记录调用参数的 API 替身，输入不涉及真实服务器或认证配置；输出确认
    路由挂载前缀和标签正确。治理注册函数被替换为空操作，避免此单元测试污染全局
    注册表，同时仍保留真实插件注册流程的覆盖。
    """
    entry_module = _load_plugin_module("__entry__")
    registered_http_routers = []
    registered_tools = []
    registered_tool_options = {}

    class _RecordingPluginApi:
        """记录插件注册调用，供本测试核验公开边界。"""

        def register_http_router(self, router, *, prefix, tags):
            """保存路由、前缀和标签，不执行真实应用挂载。"""
            registered_http_routers.append((router, prefix, tags))

        def register_tool(self, **kwargs):
            """保存普通工具注册，确认复核路由没有混入模型工具列表。"""
            registered_tools.append(kwargs["tool_name"])
            registered_tool_options[kwargs["tool_name"]] = kwargs

    monkeypatch.setattr(entry_module, "_register_governance_tools", lambda: None)
    entry_module.XuzhouInvestmentLedgerPlugin().register(_RecordingPluginApi())

    assert registered_http_routers == [
        (
            entry_module.review_router,
            "/xuzhou-investment-ledger",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.dashboard_router,
            "/xuzhou-investment-ledger/dashboard",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.evidence_draft_router,
            "/xuzhou-investment-ledger/evidence-drafts",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.human_evidence_router,
            "/xuzhou-investment-ledger/human-evidence",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.industry_metrics_router,
            "/xuzhou-investment-ledger/industry-metrics",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.scoring_rules_router,
            "/xuzhou-investment-ledger/scoring-rules",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.source_catalog_router,
            "/xuzhou-investment-ledger/trusted-sources",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.public_source_supplement_router,
            "/xuzhou-investment-ledger/public-sources",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.source_recipe_learning_router,
            "/xuzhou-investment-ledger/source-recipe-learning",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.adaptive_learning_router,
            "/xuzhou-investment-ledger/adaptive-learning",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.automation_schedule_router,
            "/xuzhou-investment-ledger/source-monitor-schedule",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.dingtalk_daily_digest_router,
            "/xuzhou-investment-ledger/dingtalk-daily-digest",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.outcome_learning_schedule_router,
            "/xuzhou-investment-ledger/outcome-learning-schedule",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.relationship_graph_router,
            "/xuzhou-investment-ledger/relationship-graph",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.relationship_draft_router,
            "/xuzhou-investment-ledger/relationship-drafts",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.co_investment_signal_router,
            "/xuzhou-investment-ledger/co-investment-signals",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.integrity_migration_router,
            "/xuzhou-investment-ledger/evidence-integrity",
            ["xuzhou-investment-ledger"],
        ),
        (
            entry_module.team_router,
            "/xuzhou-investment-ledger/team",
            ["xuzhou-investment-ledger"],
        ),
    ]
    assert "review_xuzhou_investment_lead" not in registered_tools
    assert "create_xuzhou_scoring_rule_set" not in registered_tools
    assert "create_xuzhou_score_snapshot" not in registered_tools
    assert "record_xuzhou_outcome_feedback" not in registered_tools
    assert "confirm_xuzhou_evidence_draft" not in registered_tools
    assert "create_xuzhou_human_evidence" not in registered_tools
    assert "review_xuzhou_industry_metric_draft" not in registered_tools
    assert "migrate_xuzhou_legacy_evidence_integrity" not in registered_tools
    assert "discover_xuzhou_public_sources" in registered_tools
    assert "capture_xuzhou_discovery_candidate" in registered_tools
    assert "inspect_xuzhou_discovery_candidate" in registered_tools
    assert "get_xuzhou_investment_assessment_report" in registered_tools
    assert "get_xuzhou_investment_action_center" in registered_tools
    assert "get_xuzhou_outreach_preparation_brief" in registered_tools
    assert "get_xuzhou_industry_subdirection_coverage_report" in registered_tools
    assert "get_xuzhou_industry_business_metrics_report" in registered_tools
    assert "get_xuzhou_investment_work_summary" in registered_tools
    assert "get_xuzhou_relationship_graph" in registered_tools
    assert "list_xuzhou_relationship_review_queue" in registered_tools
    assert "list_xuzhou_relationship_drafts" in registered_tools
    assert "list_xuzhou_co_investment_signal_drafts" in registered_tools
    assert "list_xuzhou_candidate_observations" in registered_tools
    assert "get_xuzhou_candidate_observation" in registered_tools
    assert "run_xuzhou_source_monitor" in registered_tools
    assert "run_xuzhou_outcome_learning_cycle" in registered_tools
    assert "deliver_xuzhou_dingtalk_daily_digest" in registered_tools
    assert "get_xuzhou_source_coverage_report" in registered_tools
    assert registered_tool_options["run_xuzhou_source_monitor"]["cron_safe"] is True
    assert registered_tool_options["run_xuzhou_source_monitor"]["cron_fixed_arguments"] == {
        "run_scope": "scheduled_public_discovery"
    }
    assert registered_tool_options["run_xuzhou_outcome_learning_cycle"]["cron_safe"] is True
    assert registered_tool_options["run_xuzhou_outcome_learning_cycle"]["cron_fixed_arguments"] == {
        "run_scope": "scheduled_outcome_learning_cycle"
    }
    assert registered_tool_options["deliver_xuzhou_dingtalk_daily_digest"]["cron_safe"] is True
    assert registered_tool_options["deliver_xuzhou_dingtalk_daily_digest"]["cron_only"] is True
    assert registered_tool_options["deliver_xuzhou_dingtalk_daily_digest"]["cron_deliver_result"] is True


@pytest.mark.asyncio
async def test_authenticated_user_fixed_source_monitor_schedule_preserves_evidence(
    core_module,
    tmp_path: Path,
) -> None:
    """固定后台计划只可按服务器常量启停，审计和计划操作均不能改证据链。"""
    scheduling = _load_plugin_module("automation_schedule")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    evidence_result = core_module.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(
        core_module.get_investment_lead(
            tmp_path,
            lead_id=evidence_result["lead"]["lead_id"],
        )["evidence"]
    )

    class FakeCronManager:
        """最小化模拟平台 Cron 管理器，记录计划而不启动真实后台任务。"""

        def __init__(self) -> None:
            self.jobs: dict[str, object] = {}

        async def get_job(self, job_id: str):
            return self.jobs.get(job_id)

        async def create_or_replace_job(self, job) -> None:
            assert job.id is not None
            self.jobs[job.id] = job

        async def delete_job(self, job_id: str) -> bool:
            return self.jobs.pop(job_id, None) is not None

        def get_state(self, _job_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                model_dump=lambda mode: {
                    "next_run_at": None,
                    "last_run_at": None,
                    "last_status": None,
                    "last_error": None,
                }
            )

    manager = FakeCronManager()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        cron_manager=manager,
    )
    enabled = await scheduling.set_source_monitor_schedule_enabled(
        workspace,
        ledger_dir=tmp_path,
        authenticated_user_id="ordinary-server-user",
        enabled=True,
    )
    job = manager.jobs[scheduling.SOURCE_MONITOR_SCHEDULE_JOB_ID]
    assert enabled["changed"] is True
    assert job.task_type == "tool"
    assert job.tool.name == "run_xuzhou_source_monitor"
    assert job.tool.arguments == {"run_scope": "scheduled_public_discovery"}
    assert job.schedule.cron == "17 */6 * * *"
    assert job.schedule.timezone == "Asia/Shanghai"
    assert job.dispatch.target.user_id == "ordinary-server-user"
    assert job.dispatch.target.session_id.startswith("xuzhou-source-monitor-")
    assert job.dispatch.silent is True
    assert job.runtime.share_session is False
    assert job.runtime.max_concurrency == 1
    assert job.runtime.timeout_seconds == 240
    ledger_after_enable = storage.load_ledger(tmp_path)
    assert ledger_after_enable["automation_schedule_audit"] == [enabled["audit"]]
    assert core_module.get_investment_lead(
        tmp_path,
        lead_id=evidence_result["lead"]["lead_id"],
    )["evidence"] == evidence_before

    paused = await scheduling.set_source_monitor_schedule_enabled(
        workspace,
        ledger_dir=tmp_path,
        authenticated_user_id="ordinary-server-user",
        enabled=False,
    )
    assert paused["changed"] is True
    assert manager.jobs[scheduling.SOURCE_MONITOR_SCHEDULE_JOB_ID].enabled is False
    assert storage.load_ledger(tmp_path)["automation_schedule_audit"] == [
        enabled["audit"],
        paused["audit"],
    ]
    assert core_module.get_investment_lead(
        tmp_path,
        lead_id=evidence_result["lead"]["lead_id"],
    )["evidence"] == evidence_before


@pytest.mark.asyncio
async def test_source_monitor_schedule_api_uses_verified_user_and_rejects_unknown_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """计划接口只用认证主体身份，且不覆盖被同名陌生任务占用的 Cron ID。"""
    schedule_api = _load_plugin_module("automation_schedule_api")
    scheduling = importlib.import_module(f"{PACKAGE_NAME}.automation_schedule")

    class FakeCronManager:
        """模拟单工作区 Cron 存储，以验证 API 不读取客户端身份或任务参数。"""

        def __init__(self) -> None:
            self.jobs: dict[str, object] = {}

        async def get_job(self, job_id: str):
            return self.jobs.get(job_id)

        async def create_or_replace_job(self, job) -> None:
            assert job.id is not None
            self.jobs[job.id] = job

        async def delete_job(self, job_id: str) -> bool:
            return self.jobs.pop(job_id, None) is not None

        def get_state(self, _job_id: str) -> SimpleNamespace:
            return SimpleNamespace(model_dump=lambda mode: {})

    manager = FakeCronManager()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        cron_manager=manager,
    )
    server_principal = Principal(
        user_id="verified-ordinary-user",
        username="verified-ordinary-user",
        role="user",
    )
    monkeypatch.setattr(
        schedule_api,
        "get_current_principal",
        lambda _request: server_principal,
    )

    async def fake_get_agent_for_request(_request: object) -> SimpleNamespace:
        """模拟平台已按认证用户确认当前工作区归属。"""
        return workspace

    monkeypatch.setattr(schedule_api, "get_agent_for_request", fake_get_agent_for_request)
    monkeypatch.setattr(
        schedule_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    response = await schedule_api.enable_xuzhou_source_monitor_schedule(object())
    assert response["ok"] is True
    assert response["audit"]["operated_by_user_id"] == server_principal.user_id
    assert manager.jobs[scheduling.SOURCE_MONITOR_SCHEDULE_JOB_ID].dispatch.target.user_id == (
        server_principal.user_id
    )

    manager.jobs[scheduling.SOURCE_MONITOR_SCHEDULE_JOB_ID] = SimpleNamespace(
        id=scheduling.SOURCE_MONITOR_SCHEDULE_JOB_ID,
        task_type="tool",
        tool=SimpleNamespace(name="untrusted_tool", arguments={}),
        schedule=SimpleNamespace(type="cron", cron="* * * * *", timezone="UTC"),
        meta={},
        enabled=True,
    )
    with pytest.raises(HTTPException) as conflict:
        await schedule_api.enable_xuzhou_source_monitor_schedule(object())
    assert conflict.value.status_code == 409
    assert "不受控" in str(conflict.value.detail)


@pytest.mark.asyncio
async def test_outcome_learning_schedule_api_uses_verified_user_and_preserves_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """每日学习计划只登记固定工具，认证用户启停时不改任何原始证据。"""
    outcome_api = _load_plugin_module("outcome_learning_schedule_api")
    scheduling = importlib.import_module(f"{PACKAGE_NAME}.automation_schedule")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    evidence_result = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(
        core.get_investment_lead(tmp_path, lead_id=evidence_result["lead"]["lead_id"])["evidence"]
    )

    class FakeCronManager:
        """模拟 Cron 管理器以验证计划规格，不注册真实 APScheduler 作业。"""

        def __init__(self) -> None:
            self.jobs: dict[str, object] = {}

        async def get_job(self, job_id: str):
            return self.jobs.get(job_id)

        async def create_or_replace_job(self, job) -> None:
            assert job.id is not None
            self.jobs[job.id] = job

        async def delete_job(self, job_id: str) -> bool:
            return self.jobs.pop(job_id, None) is not None

        def get_state(self, _job_id: str) -> SimpleNamespace:
            return SimpleNamespace(model_dump=lambda mode: {})

    manager = FakeCronManager()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        cron_manager=manager,
    )
    principal = Principal(
        user_id="verified-learning-user",
        username="verified-learning-user",
        role="user",
    )
    monkeypatch.setattr(outcome_api, "get_current_principal", lambda _request: principal)

    async def fake_get_agent_for_request(_request: object) -> SimpleNamespace:
        """模拟认证层已完成当前用户对工作区的归属校验。"""
        return workspace

    monkeypatch.setattr(outcome_api, "get_agent_for_request", fake_get_agent_for_request)
    monkeypatch.setattr(
        outcome_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    enabled = await outcome_api.enable_xuzhou_outcome_learning_schedule(object())
    job = manager.jobs[scheduling.OUTCOME_LEARNING_SCHEDULE_JOB_ID]
    assert enabled["ok"] is True
    assert enabled["audit"]["operated_by_user_id"] == principal.user_id
    assert job.enabled is True
    assert job.schedule.cron == "35 3 * * *"
    assert job.schedule.timezone == "Asia/Shanghai"
    assert job.tool.name == "run_xuzhou_outcome_learning_cycle"
    assert job.tool.arguments == {"run_scope": "scheduled_outcome_learning_cycle"}
    assert job.runtime.timeout_seconds == 120
    assert job.runtime.share_session is False
    assert core.get_investment_lead(
        tmp_path,
        lead_id=evidence_result["lead"]["lead_id"],
    )["evidence"] == evidence_before

    paused = await outcome_api.pause_xuzhou_outcome_learning_schedule(object())
    assert paused["ok"] is True
    assert manager.jobs[scheduling.OUTCOME_LEARNING_SCHEDULE_JOB_ID].enabled is False
    audits = storage.load_ledger(tmp_path)["automation_schedule_audit"]
    assert [audit["action"] for audit in audits] == ["enabled", "paused"]
    assert {audit["job_id"] for audit in audits} == {scheduling.OUTCOME_LEARNING_SCHEDULE_JOB_ID}
    assert core.get_investment_lead(
        tmp_path,
        lead_id=evidence_result["lead"]["lead_id"],
    )["evidence"] == evidence_before


def test_scoring_rule_service_rejects_invalid_enabled_weight_total(tmp_path: Path) -> None:
    """评分服务必须拒绝启用维度权重不为 100 的规则，避免产生不可解释的评分口径。"""
    scoring_rules = _load_plugin_module("scoring_rules")
    active_rule_set = scoring_rules.get_active_scoring_rule_set(tmp_path)
    invalid_dimensions = copy.deepcopy(active_rule_set["dimensions"])
    invalid_dimensions[0]["weight"] = 34

    with pytest.raises(ValueError, match="weight之和必须为100"):
        scoring_rules.create_scoring_rule_set(
            tmp_path,
            dimensions=invalid_dimensions,
            penalty_rules=copy.deepcopy(active_rule_set["penalty_rules"]),
            change_reason="验证错误权重不能入库",
            authenticated_user_id="ordinary-user",
        )


@pytest.mark.asyncio
async def test_scoring_rules_api_uses_verified_identity_and_preserves_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通登录用户可创建规则版本，但客户端不能指定创建人，且证据链保持逐字不变。"""
    scoring_rules_api = _load_plugin_module("scoring_rules_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    scoring_rules = importlib.import_module(f"{PACKAGE_NAME}.scoring_rules")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")

    # 先写入一条真实结构的线索证据，验证后续规则配置变更不会触及该不可变数据。
    created_lead = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(created_lead["evidence"])
    original_active_rule_set = scoring_rules.get_active_scoring_rule_set(tmp_path)
    changed_dimensions = copy.deepcopy(original_active_rule_set["dimensions"])
    changed_dimensions[0]["weight"] = 30
    changed_dimensions[1]["weight"] = 30
    ordinary_principal = Principal(
        user_id="ordinary-operator",
        username="ordinary-operator",
        role="user",
    )

    def fake_get_current_principal(_request: object) -> Principal:
        """模拟认证中间件：身份只能由服务器认证结果提供。"""
        return ordinary_principal

    monkeypatch.setattr(scoring_rules_api, "get_current_principal", fake_get_current_principal)
    monkeypatch.setattr(
        scoring_rules_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    request_body = scoring_rules_api.CreateScoringRuleSetRequest.model_validate(
        {
            "dimensions": changed_dimensions,
            "penalty_rules": copy.deepcopy(original_active_rule_set["penalty_rules"]),
            "change_reason": "根据本轮人工招商复盘调整落地意愿和徐州关联的权重",
        }
    )

    response = await scoring_rules_api.create_xuzhou_scoring_rule_set(request_body, object())

    assert response["rule_set"]["version"] == 2
    assert response["rule_set"]["created_by_user_id"] == ordinary_principal.user_id
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"] == [evidence_before]
    assert ledger["leads"][0]["evidence_ids"] == [evidence_before["evidence_id"]]
    assert ledger["scoring_rule_sets"][0]["status"] == "superseded"
    assert ledger["scoring_rule_sets"][1]["status"] == "active"
    assert ledger["scoring_rule_sets"][1]["dimensions"] == changed_dimensions

    # 请求体声明额外的操作者字段必须被拒绝，防止模型或前端伪造责任归属。
    with pytest.raises(ValidationError):
        scoring_rules_api.CreateScoringRuleSetRequest.model_validate(
            {
                "dimensions": changed_dimensions,
                "penalty_rules": original_active_rule_set["penalty_rules"],
                "change_reason": "尝试伪造创建者",
                "created_by_user_id": "forged-user",
            }
        )


def test_score_snapshot_and_outcome_feedback_are_append_only_and_evidence_based(
    tmp_path: Path,
) -> None:
    """评分和结果反馈只能追加业务审计，不能覆盖线索、证据、规则或历史快照。"""
    core = _load_plugin_module("core")
    scoring_rules = importlib.import_module(f"{PACKAGE_NAME}.scoring_rules")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(created["evidence"])
    lead_before = copy.deepcopy(created["lead"])
    rules_before = copy.deepcopy(scoring_rules.get_active_scoring_rule_set(tmp_path))

    scored = scoring_rules.create_score_snapshot(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        authenticated_user_id="ordinary-scorer",
    )

    assert scored["created"] is True
    snapshot = scored["score_snapshot"]
    assert snapshot["scored_by_user_id"] == "ordinary-scorer"
    assert snapshot["evidence_ids"] == [evidence_before["evidence_id"]]
    assert 0 <= snapshot["total_score"] <= 100
    assert {component["rule_key"] for component in snapshot["components"]} >= {
        "landing_intent",
        "xuzhou_association",
        "industry_fit",
        "evidence_quality_recency",
    }
    duplicated = scoring_rules.create_score_snapshot(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        authenticated_user_id="another-ordinary-scorer",
    )
    assert duplicated["created"] is False
    feedback_result = scoring_rules.record_outcome_feedback(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        score_snapshot_id=snapshot["score_snapshot_id"],
        outcome="落地",
        note="项目完成签约并确认在徐州落地，作为人工结果反馈留档。",
        authenticated_user_id="ordinary-feedback-user",
    )
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"] == [evidence_before]
    assert ledger["leads"] == [lead_before]
    assert scoring_rules.get_active_scoring_rule_set(tmp_path) == rules_before
    assert ledger["score_snapshots"] == [snapshot]
    assert feedback_result["feedback"]["recorded_by_user_id"] == "ordinary-feedback-user"
    assert feedback_result["feedback"]["score_snapshot_id"] == snapshot["score_snapshot_id"]

    with pytest.raises(ValueError, match="不属于该招商线索"):
        scoring_rules.record_outcome_feedback(
            tmp_path,
            lead_id=created["lead"]["lead_id"],
            score_snapshot_id="score-snapshot-forged",
            outcome="落地",
            note="尝试绑定伪造评分快照。",
            authenticated_user_id="ordinary-feedback-user",
        )


@pytest.mark.asyncio
async def test_score_and_feedback_api_use_verified_identity_and_reject_claimed_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通用户可经认证接口评分和反馈，客户端不能伪造反馈人或提交权重。"""
    scoring_rules_api = _load_plugin_module("scoring_rules_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(created["evidence"])
    principal = Principal(user_id="ordinary-score-api-user", username="ordinary", role="user")
    monkeypatch.setattr(scoring_rules_api, "get_current_principal", lambda _request: principal)

    monkeypatch.setattr(
        scoring_rules_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    scored_response = await scoring_rules_api.create_xuzhou_score_snapshot(
        created["lead"]["lead_id"],
        object(),
    )
    snapshot = scored_response["score_snapshot"]
    feedback_request = scoring_rules_api.RecordOutcomeFeedbackRequest.model_validate(
        {
            "score_snapshot_id": snapshot["score_snapshot_id"],
            "outcome": "未转化",
            "note": "已完成多轮沟通但阶段与载体条件不匹配，人工确认暂不转化。",
        }
    )
    feedback_response = await scoring_rules_api.record_xuzhou_outcome_feedback(
        created["lead"]["lead_id"],
        feedback_request,
        object(),
    )

    assert snapshot["scored_by_user_id"] == principal.user_id
    assert feedback_response["feedback"]["recorded_by_user_id"] == principal.user_id
    assert storage.load_ledger(tmp_path)["evidence"] == [evidence_before]
    with pytest.raises(ValidationError):
        scoring_rules_api.RecordOutcomeFeedbackRequest.model_validate(
            {
                "score_snapshot_id": snapshot["score_snapshot_id"],
                "outcome": "落地",
                "note": "尝试伪造反馈人。",
                "recorded_by_user_id": "forged-user",
            }
        )


def test_outcome_feedback_learning_cycle_versions_rules_once_without_changing_evidence_or_leads(
    tmp_path: Path,
) -> None:
    """充分反馈只在固定周期创建一版等额平衡规则，不能改证据、状态或重复使用旧反馈。"""
    core = _load_plugin_module("core")
    scoring_rules = importlib.import_module(f"{PACKAGE_NAME}.scoring_rules")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    active_rules_before: dict[str, object] | None = None
    lead_states_before: dict[str, str] = {}
    for index in range(10):
        is_high_landing_signal = index < 5
        claim_type = "融资" if is_high_landing_signal else "产业方向"
        created = core.record_public_evidence(
            tmp_path,
            **_evidence_arguments(
                entity_name=f"学习样本企业-{index}",
                source_url=f"https://example.com/learning/{index}",
                source_title=f"学习样本公告-{index}",
                claim_type=claim_type,
                excerpt=(
                    f"学习样本企业-{index}完成新一轮融资并计划在徐州寻找合作伙伴。"
                    if is_high_landing_signal
                    else f"学习样本企业-{index}发布具身智能技术方向介绍，未披露融资或落地计划。"
                ),
                raw_content=f"learning raw response {index}".encode("utf-8"),
            ),
        )
        lead_id = created["lead"]["lead_id"]
        if active_rules_before is None:
            # 首条证据落盘后再读取真实规则，避免未创建台账时的临时初始化时间影响基线。
            active_rules_before = copy.deepcopy(scoring_rules.get_active_scoring_rule_set(tmp_path))
        lead_states_before[lead_id] = created["lead"]["review_state"]
        snapshot = scoring_rules.create_score_snapshot(
            tmp_path,
            lead_id=lead_id,
            authenticated_user_id="ordinary-learning-scorer",
        )["score_snapshot"]
        scoring_rules.record_outcome_feedback(
            tmp_path,
            lead_id=lead_id,
            score_snapshot_id=snapshot["score_snapshot_id"],
            outcome="落地" if is_high_landing_signal else "无效",
            note=f"学习样本 {index} 的人工确认最终结果。",
            authenticated_user_id="ordinary-learning-reviewer",
        )

    suggestion_result = scoring_rules.get_scoring_adjustment_suggestions(tmp_path)
    ledger = storage.load_ledger(tmp_path)
    landing_suggestion = next(
        suggestion
        for suggestion in suggestion_result["suggestions"]
        if suggestion["dimension_key"] == "landing_intent"
    )
    assert suggestion_result["ready"] is True
    assert suggestion_result["final_feedback_count"] == 10
    assert landing_suggestion["recommendation"] == "increase"
    assert landing_suggestion["suggested_weight_delta"] == 5
    assert active_rules_before is not None
    assert scoring_rules.get_active_scoring_rule_set(tmp_path) == active_rules_before
    assert {lead["lead_id"]: lead["review_state"] for lead in ledger["leads"]} == lead_states_before

    learning_result = scoring_rules.run_outcome_learning_cycle(tmp_path)
    updated_rules = scoring_rules.get_active_scoring_rule_set(tmp_path)
    updated_ledger = storage.load_ledger(tmp_path)
    updated_weights = {dimension["key"]: dimension["weight"] for dimension in updated_rules["dimensions"]}
    previous_weights = {
        dimension["key"]: dimension["weight"] for dimension in active_rules_before["dimensions"]
    }
    assert learning_result["applied"] is True
    assert updated_rules["version"] == 2
    assert updated_rules["created_by_user_id"] == "system-outcome-learning-v1"
    assert updated_weights["landing_intent"] == previous_weights["landing_intent"] + 5
    assert sum(updated_weights.values()) == 100
    assert updated_ledger["evidence"] == ledger["evidence"]
    assert updated_ledger["outcome_feedback"] == ledger["outcome_feedback"]
    assert {lead["lead_id"]: lead["review_state"] for lead in updated_ledger["leads"]} == lead_states_before

    # 模拟第二个 Cron 已持有旧版建议：即使它给出完整规则，也不能把 v2 再替换为 v3。
    with pytest.raises(RuntimeError, match="已变化"):
        scoring_rules.create_scoring_rule_set(
            tmp_path,
            dimensions=copy.deepcopy(updated_rules["dimensions"]),
            penalty_rules=copy.deepcopy(updated_rules["penalty_rules"]),
            change_reason="模拟并发任务尝试写入过期学习建议。",
            authenticated_user_id="system-outcome-learning-v1",
            expected_active_rule_set_id=active_rules_before["rule_set_id"],
            expected_active_rule_set_version=active_rules_before["version"],
        )

    # 新规则没有自己的最终反馈，旧版本的十条反馈不得被重复用于再调一次权重。
    new_rule_suggestions = scoring_rules.get_scoring_adjustment_suggestions(tmp_path)
    repeated_learning_result = scoring_rules.run_outcome_learning_cycle(tmp_path)
    assert new_rule_suggestions["ready"] is False
    assert new_rule_suggestions["final_feedback_count"] == 0
    assert repeated_learning_result["applied"] is False
    assert scoring_rules.get_active_scoring_rule_set(tmp_path) == updated_rules


@pytest.mark.asyncio
async def test_outcome_learning_cron_tool_rejects_nonfixed_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """自动学习入口只接受平台固定范围，不让模型借参数注入权重或身份。"""
    tools_module = _load_plugin_module("tools")
    monkeypatch.setattr(tools_module, "_workspace_dir", lambda: tmp_path)

    rejected = await tools_module.run_xuzhou_outcome_learning_cycle("arbitrary-model-scope")
    accepted = await tools_module.run_xuzhou_outcome_learning_cycle(
        "scheduled_outcome_learning_cycle"
    )

    assert rejected["ok"] is False
    assert "run_scope" in rejected["error"]
    assert accepted["ok"] is True
    assert accepted["applied"] is False
    assert accepted["source_recipe_learning"]["status"] == "queue_learning_active_recipe_order_requires_user_policy"
    assert accepted["adaptive_learning"] == accepted["source_recipe_learning"]


def test_source_recipe_learning_requires_human_approval_and_only_reorders_fixed_recipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """来源学习只能从已签名证据生成建议，普通用户批准后才重排固定配方。

    输入为两个校友配方和一个早期信号配方分别产生的三条正式证据；自动学习输出应仅
    追加待审批建议，不能自动改变初始规则。普通用户批准后，系统才创建版本 2 并让
    后续发现优先执行校友、早期信号配方；五条配方仍完整保留。测试还比较证据、线索
    与评分字段，证明建议和审批不能改写原始证据链或招商业务判断。
    """
    core = _load_plugin_module("core")
    discovery = importlib.import_module(f"{PACKAGE_NAME}.source_discovery")
    governance = importlib.import_module(f"{PACKAGE_NAME}.source_recipe_governance")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")

    def create_recipe_evidence(
        *,
        candidate_id: str,
        recipe_id: str,
        sequence: int,
    ) -> dict[str, object]:
        """创建一条带服务器候选关联的正式证据，供学习器按配方统计而非猜测名称。"""
        raw_content = f"source recipe learning raw content {sequence}".encode("utf-8")
        result = core.record_public_evidence(
            tmp_path,
            **_evidence_arguments(
                entity_name=f"来源学习样本企业-{sequence}",
                source_url=f"https://recipe-{sequence}.example.com/news/announcement",
                source_title=f"来源学习样本公告-{sequence}",
                excerpt=f"来源学习样本企业-{sequence}公开披露具身智能相关融资与合作信息。",
                raw_content=raw_content,
                capture_metadata={
                    "source_type": "automated_public_search_capture",
                    "collection_method": "public_search_then_snapshot",
                    "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
                    "discovery_run_id": "source-recipe-learning-run",
                    "discovery_candidate_id": candidate_id,
                },
            ),
        )
        return {
            "candidate_id": candidate_id,
            "recipe_id": recipe_id,
            "evidence": result["evidence"],
        }

    first_alumni = create_recipe_evidence(
        candidate_id="source-recipe-alumni-one",
        recipe_id="university_alumni",
        sequence=1,
    )
    second_alumni = create_recipe_evidence(
        candidate_id="source-recipe-alumni-two",
        recipe_id="university_alumni",
        sequence=2,
    )
    early_signal = create_recipe_evidence(
        candidate_id="source-recipe-early-one",
        recipe_id="public_early_signal",
        sequence=3,
    )
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = []
    for item in (first_alumni, second_alumni, early_signal):
        evidence = item["evidence"]
        assert isinstance(evidence, dict)
        ledger["discovery_candidates"].append(
            {
                "candidate_id": item["candidate_id"],
                "run_id": "source-recipe-learning-run",
                "source_url": evidence["source_url"],
                "source_title": evidence["source_title"],
                "search_snippet": "仅用于已签名证据与候选关联测试。",
                "sector": "embodied_intelligence",
                "source_catalog_id": item["recipe_id"],
                "discovery_query": "固定免费来源配方测试查询",
                "discovered_at": evidence["created_at"],
                "source_type": "public_search_index",
                "source_level": "lead_only",
                "state": "captured",
                "capture_attempts": 1,
                "captured_evidence_id": evidence["evidence_id"],
                "last_capture_error": None,
                "last_captured_at": evidence["created_at"],
            }
        )
    storage.save_ledger(tmp_path, ledger)
    before_learning = copy.deepcopy(storage.load_ledger(tmp_path))

    proposal_result = governance.generate_source_recipe_learning_proposal(tmp_path)

    assert proposal_result["status"] == "created_pending_user_review"
    proposal = proposal_result["proposal"]
    assert proposal["current_recipe_order"] == list(storage.SOURCE_RECIPE_BASELINE_ORDER)
    assert proposal["proposed_recipe_order"] == [
        "university_alumni",
        "public_early_signal",
        "xuzhou_connection",
        "cooperation_and_landing",
        "industry_business_metrics",
    ]
    assert proposal["skill_update_performed"] is False
    after_proposal = storage.load_ledger(tmp_path)
    assert after_proposal["evidence"] == before_learning["evidence"]
    assert after_proposal["leads"] == before_learning["leads"]
    assert after_proposal["score_snapshots"] == before_learning["score_snapshots"]
    assert after_proposal["source_recipe_rule_sets"] == before_learning["source_recipe_rule_sets"]
    listed = governance.list_source_recipe_learning_proposals(tmp_path)
    assert listed["proposals"][0]["review_state"] == "pending_user_review"

    approved = governance.review_source_recipe_learning_proposal(
        tmp_path,
        proposal_id=proposal["proposal_id"],
        decision="批准",
        review_note="普通登录用户核对三条正式证据产出后，批准仅调整固定来源配方执行顺序。",
        authenticated_user_id="ordinary-source-recipe-reviewer",
    )
    active_rule_set = governance.get_active_source_recipe_rule_set(tmp_path)["rule_set"]
    assert approved["review"]["reviewed_by_user_id"] == "ordinary-source-recipe-reviewer"
    assert approved["review"]["new_review_state"] == "approved"
    assert approved["new_rule_set"]["version"] == 2
    assert active_rule_set["version"] == 2
    assert active_rule_set["recipe_order"] == proposal["proposed_recipe_order"]
    assert set(active_rule_set["recipe_order"]) == set(storage.SOURCE_RECIPE_BASELINE_ORDER)
    with pytest.raises(RuntimeError, match="已经完成审批"):
        governance.review_source_recipe_learning_proposal(
            tmp_path,
            proposal_id=proposal["proposal_id"],
            decision="拒绝",
            review_note="重复审批必须失败。",
            authenticated_user_id="ordinary-source-recipe-reviewer",
        )

    observed_queries: list[str] = []

    def fake_search_feed(query: str) -> list[dict[str, str]]:
        """记录发现器实际采用的服务器规则顺序，不连接真实公开搜索源。"""
        observed_queries.append(query)
        return []

    monkeypatch.setattr(discovery, "fetch_public_search_feed", fake_search_feed)
    discovery_result = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=2,
        max_results_per_query=1,
    )
    assert discovery_result["run"]["source_recipe_rule_set_version"] == 2
    assert discovery_result["run"]["source_recipe_order"] == proposal["proposed_recipe_order"]
    assert len(observed_queries) == 2
    assert "校友" in observed_queries[0]
    early_signal_recipe = next(
        recipe
        for recipe in discovery.QUERY_RECIPES
        if recipe["catalog_id"] == "public_early_signal"
    )
    assert any(
        signal in observed_queries[1]
        for signal in early_signal_recipe["signals"].split(" OR ")
    )
    after_approval = storage.load_ledger(tmp_path)
    assert after_approval["evidence"] == before_learning["evidence"]
    assert after_approval["leads"] == before_learning["leads"]
    assert after_approval["score_snapshots"] == before_learning["score_snapshots"]


@pytest.mark.asyncio
async def test_source_recipe_learning_api_uses_verified_identity_and_rejects_claimed_reviewer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """来源学习审批接口只能传递服务器认证身份，拒绝客户端伪造审批人或配方字段。"""
    recipe_api = _load_plugin_module("source_recipe_learning_api")
    principal = Principal(
        user_id="verified-source-recipe-user",
        username="verified-source-recipe-user",
        role="user",
    )
    captured_call: dict[str, object] = {}
    monkeypatch.setattr(recipe_api, "get_current_principal", lambda _request: principal)

    def fake_review_source_recipe_learning_proposal(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录 API 向核心层传递的受控字段，不读取或写入任何测试台账。"""
        captured_call["workspace_dir"] = workspace_dir
        captured_call.update(kwargs)
        return {"review": {"reviewed_by_user_id": kwargs["authenticated_user_id"]}}

    monkeypatch.setattr(
        recipe_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(
        recipe_api,
        "review_source_recipe_learning_proposal",
        fake_review_source_recipe_learning_proposal,
    )
    body = recipe_api.ReviewSourceRecipeLearningProposalRequest.model_validate(
        {"decision": "批准", "review_note": "普通用户批准服务器已生成的测试建议。"}
    )

    response = await recipe_api.review_xuzhou_source_recipe_learning_proposal(
        "source-recipe-learning-proposal-test",
        body,
        object(),
    )

    assert response["ok"] is True
    assert captured_call["workspace_dir"] == tmp_path
    assert captured_call["authenticated_user_id"] == principal.user_id
    assert captured_call["proposal_id"] == "source-recipe-learning-proposal-test"
    with pytest.raises(ValidationError):
        recipe_api.ReviewSourceRecipeLearningProposalRequest.model_validate(
            {
                "decision": "批准",
                "review_note": "应拒绝客户端伪造审批人。",
                "reviewed_by_user_id": "forged-user",
            }
        )
    with pytest.raises(ValidationError):
        recipe_api.ReviewSourceRecipeLearningProposalRequest.model_validate(
            {
                "decision": "批准",
                "review_note": "应拒绝客户端提交配方顺序。",
                "recipe_order": ["university_alumni"],
            }
        )


def test_public_search_feed_parses_only_complete_public_results(monkeypatch) -> None:
    """搜索 RSS 只返回带公开链接和标题的候选，不把不完整项目传给后续抓取。"""
    source_capture = _load_plugin_module("source_capture")

    def fake_fetch_bytes(_url, *, allowed_content_types):
        """提供固定 RSS 响应，确保测试不连接任何真实搜索引擎。"""
        assert allowed_content_types == source_capture.RSS_CONTENT_TYPES
        return {
            "source_url": "https://www.bing.com/search?format=rss",
            "content_type": "application/rss+xml",
            "charset": "utf-8",
            "raw_content": """<?xml version='1.0' encoding='utf-8'?>
                <rss><channel>
                  <item><title>示例企业融资公告</title><link>https://example.com/news/a</link><description>公开摘要</description></item>
                  <item><title>无链接项目</title><description>应被忽略</description></item>
                </channel></rss>""".encode("utf-8"),
            "byte_size": 200,
        }

    monkeypatch.setattr(source_capture, "_fetch_public_bytes", fake_fetch_bytes)
    results = source_capture.fetch_public_search_feed("具身智能 融资")

    assert results == [
        {
            "source_title": "示例企业融资公告",
            "source_url": "https://example.com/news/a",
            "search_snippet": "公开摘要",
        }
    ]


def test_source_capture_rejects_private_or_unverifiable_connected_peer() -> None:
    """来源抓取在连接后仍须拒绝私网对端和无法核验对端的响应包装。"""
    source_capture = _load_plugin_module("source_capture")

    class FakeSocket:
        """提供固定对端地址，模拟 URL 响应包装底层已建立的套接字。"""

        def __init__(self, address: str) -> None:
            self._address = address

        def getpeername(self) -> tuple[str, int]:
            """返回测试指定的 TCP 对端地址，不进行真实网络连接。"""
            return self._address, 443

    public_response = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=FakeSocket("8.8.8.8"))))
    source_capture._ensure_public_connected_peer(public_response)

    private_response = SimpleNamespace(
        fp=SimpleNamespace(raw=SimpleNamespace(_sock=FakeSocket("127.0.0.1")))
    )
    with pytest.raises(ValueError, match="实际指向"):
        source_capture._ensure_public_connected_peer(private_response)
    with pytest.raises(ValueError, match="无法验证"):
        source_capture._ensure_public_connected_peer(SimpleNamespace())


def test_discovery_queries_are_china_first_and_rotate_subdirections() -> None:
    """查询固定中国企业语境、产业总称和单个细分词，不能退回宽泛 OR 搜索。"""
    discovery = _load_plugin_module("source_discovery")

    queries = discovery.build_discovery_queries("具身智能", max_queries=5)

    assert len(queries) == 5
    assert {query["source_catalog_id"] for query in queries} == {
        "public_early_signal",
        "xuzhou_connection",
        "university_alumni",
        "cooperation_and_landing",
        "industry_business_metrics",
    }
    assert all("具身智能" in query["query"] for query in queries)
    assert all("中国 企业" in query["query"] for query in queries)
    assert all(" OR " not in query["query"] for query in queries)
    assert all(
        any(
            subdirection in query["query"]
            for subdirection in discovery.SECTOR_DISCOVERY_CONFIG[
                "embodied_intelligence"
            ]["subdirections"]
        )
        for query in queries
    )
    recipe_signals = {
        recipe["catalog_id"]: recipe["signals"].split(" OR ")
        for recipe in discovery.QUERY_RECIPES
    }
    assert all(
        any(
            query["query"].endswith(signal)
            for signal in recipe_signals[query["source_catalog_id"]]
        )
        for query in queries
    )


def test_discovery_persists_deduplicated_candidates_without_creating_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """自动发现只保存待抓取链接与批次审计，重复链接不会凭搜索摘要写成证据。"""
    discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")

    def fake_search_feed(_query: str) -> list[dict[str, str]]:
        """模拟两个查询返回一个重复链接和一个新链接，不执行真实联网搜索。"""
        return [
            {
                "source_title": "中国具身智能示例企业融资消息",
                "source_url": "https://example.com/news/financing",
                "search_snippet": "企业披露融资与研发扩张计划。",
            },
            {
                "source_title": "中国具身智能示例企业合作消息",
                "source_url": "https://example.com/news/cooperation",
                "search_snippet": "企业披露产业合作计划。",
            },
        ]

    monkeypatch.setattr(discovery, "fetch_public_search_feed", fake_search_feed)
    result = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=2,
        max_results_per_query=2,
    )

    assert result["run"]["query_count"] == 2
    assert result["run"]["candidate_count"] == 2
    ledger = storage.load_ledger(tmp_path)
    assert ledger["leads"] == []
    assert ledger["evidence"] == []
    assert len(ledger["discovery_runs"]) == 1
    assert len(ledger["discovery_candidates"]) == 2
    assert {candidate["source_level"] for candidate in ledger["discovery_candidates"]} == {
        "lead_only"
    }
    assert {candidate["state"] for candidate in ledger["discovery_candidates"]} == {"discovered"}


def test_capture_discovery_candidate_requires_page_verification_and_preserves_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """候选抓取必须从正文核验字段，成功只追加新证据并保留既有证据逐字不变。"""
    discovery = _load_plugin_module("source_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    original_evidence = copy.deepcopy(original["evidence"])

    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "中国自动发现具身智能企业融资公告",
                "source_url": "https://example.com/news/auto-discovered",
                "search_snippet": "待抓取的公开搜索摘要。",
            }
        ],
    )
    candidate = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=1,
        max_results_per_query=1,
    )["candidates"][0]

    raw_content = "自动发现页面的原始响应。".encode("utf-8")

    def fake_fetch_public_source(_source_url: str) -> dict[str, str | int]:
        """返回可核验的页面快照，其中含实体名、日期和原始摘录。"""
        return {
            "source_url": "https://example.com/news/auto-discovered",
            "source_title": "自动发现具身智能企业完成融资公告",
            "content_type": "text/html",
            "visible_text": "2026年8月24日 自动发现具身智能企业宣布完成新一轮融资，并扩展机器人研发团队。",
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "byte_size": 180,
        }

    monkeypatch.setattr(discovery, "fetch_public_source", fake_fetch_public_source)
    monkeypatch.setattr(
        discovery,
        "fetch_public_source_for_observation",
        lambda source_url: {**fake_fetch_public_source(source_url), "raw_content": raw_content},
    )
    monkeypatch.setattr(
        discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )
    inspection = discovery.inspect_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
    )
    assert inspection["snapshot"]["source_title"] == "自动发现具身智能企业完成融资公告"
    assert "2026年8月24日" in inspection["snapshot"]["visible_text"]
    assert storage.load_ledger(tmp_path)["evidence"] == [original_evidence]
    assert storage.load_ledger(tmp_path)["discovery_candidates"][0]["state"] == "discovered"

    captured = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="自动发现具身智能企业",
        entity_kind="企业",
        claim_type="融资",
        source_published_at="2026-08-24",
        excerpt="自动发现具身智能企业宣布完成新一轮融资，并扩展机器人研发团队。",
    )

    assert captured["captured"] is True
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"][0] == original_evidence
    captured_evidence = ledger["evidence"][1]
    assert captured_evidence["source_type"] == "automated_public_search_capture"
    assert captured_evidence["collection_method"] == "public_search_then_snapshot"
    assert captured_evidence["raw_sha256"] == hashlib.sha256(raw_content).hexdigest()
    assert captured_evidence["discovery_candidate_id"] == candidate["candidate_id"]
    snapshot_path = tmp_path / captured_evidence["raw_snapshot_relative_path"]
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["raw_content_base64"] == base64.b64encode(raw_content).decode("ascii")
    assert captured_evidence["source_level"] == "lead_only"
    assert ledger["discovery_candidates"][0]["state"] == "captured"
    assert ledger["discovery_candidates"][0]["captured_evidence_id"] == captured_evidence["evidence_id"]
    assert core.get_investment_lead(tmp_path, lead_id=captured_evidence["lead_id"])["lead"]["red_alert"] is False

    repeated = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="自动发现具身智能企业",
        entity_kind="企业",
        claim_type="融资",
        source_published_at="2026-08-24",
        excerpt="自动发现具身智能企业宣布完成新一轮融资，并扩展机器人研发团队。",
    )
    assert repeated["captured"] is False
    assert len(storage.load_ledger(tmp_path)["evidence"]) == 2


def test_capture_discovery_candidate_marks_failure_without_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """页面缺少可验证发布日期时只能标记待处理失败，不能编造来源时间或新增证据。"""
    discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "中国示例半导体企业融资消息",
                "source_url": "https://example.com/news/undated",
                "search_snippet": "无日期消息。",
            }
        ],
    )
    candidate = discovery.discover_public_sources(
        tmp_path,
        sector="半导体",
        max_queries=1,
        max_results_per_query=1,
    )["candidates"][0]
    undated_raw_content = "无发布日期的页面原始响应。".encode("utf-8")
    undated_snapshot = {
            "source_url": "https://example.com/news/undated",
            "source_title": "示例半导体企业消息",
            "visible_text": "示例半导体企业正在扩大研发团队。",
            "raw_sha256": hashlib.sha256(undated_raw_content).hexdigest(),
            "content_type": "text/html",
            "byte_size": 100,
    }
    monkeypatch.setattr(discovery, "fetch_public_source", lambda _source_url: undated_snapshot)
    monkeypatch.setattr(
        discovery,
        "fetch_public_source_for_observation",
        lambda _source_url: {**undated_snapshot, "raw_content": undated_raw_content},
    )
    monkeypatch.setattr(
        discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )

    result = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="示例半导体企业",
        entity_kind="企业",
        claim_type="扩张",
        source_published_at="2026-08-24",
        excerpt="示例半导体企业正在扩大研发团队。",
    )

    assert result["captured"] is False
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"] == []
    failed_candidate = ledger["discovery_candidates"][0]
    assert failed_candidate["state"] == "capture_failed"
    assert "source_published_at" in failed_candidate["last_capture_error"]


@pytest.mark.asyncio
async def test_trusted_source_api_uses_verified_identity_and_preserves_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通用户可维护可信目录，但客户端不能伪造操作者且目录变更不改原始证据。"""
    source_catalog_api = _load_plugin_module("source_catalog_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt=(
                "示例具身智能企业与徐州示例产业园开展具身智能产业链配套合作，"
                "并披露完成新一轮融资。"
            ),
        ),
    )
    evidence_before = copy.deepcopy(created["evidence"])
    ordinary_principal = Principal(
        user_id="ordinary-catalog-operator",
        username="ordinary-catalog-operator",
        role="user",
    )

    def fake_get_current_principal(_request: object) -> Principal:
        """模拟由服务器认证中间件提供的普通登录主体。"""
        return ordinary_principal

    monkeypatch.setattr(source_catalog_api, "get_current_principal", fake_get_current_principal)
    monkeypatch.setattr(
        source_catalog_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    body = source_catalog_api.RegisterTrustedSourceRequest.model_validate(
        {
            "domain": "https://official.example.com/",
            "source_type": "enterprise_official",
            "source_level": "primary",
            "evidence_quality": 5,
            "allowed_claim_types": ["融资", "合作"],
            "change_reason": "已核验该域名为企业公告站点，允许支持融资和合作事实。",
        }
    )

    response = await source_catalog_api.register_xuzhou_trusted_source(body, object())

    assert response["source"]["created_by_user_id"] == ordinary_principal.user_id
    assert response["source"]["status"] == "active"
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"] == [evidence_before]
    assert ledger["trusted_source_audit"][-1]["operated_by_user_id"] == ordinary_principal.user_id

    status_response = await source_catalog_api.set_xuzhou_trusted_source_status(
        response["source"]["source_id"],
        source_catalog_api.SetTrustedSourceStatusRequest(
            new_status="disabled",
            change_reason="本轮来源复核后暂不再用于未来自动分级。",
        ),
        object(),
    )
    assert status_response["source"]["status"] == "disabled"
    assert storage.load_ledger(tmp_path)["evidence"] == [evidence_before]

    audit_response = await source_catalog_api.list_xuzhou_trusted_source_audit(
        object(),
        limit=10,
    )
    assert audit_response["count"] >= 2
    assert audit_response["audit_events"][0]["action"] == "source_status_changed"
    assert audit_response["audit_events"][0]["operated_by_user_id"] == ordinary_principal.user_id
    assert storage.load_ledger(tmp_path)["evidence"] == [evidence_before]

    with pytest.raises(ValidationError):
        source_catalog_api.RegisterTrustedSourceRequest.model_validate(
            {
                "domain": "official.example.com",
                "source_type": "enterprise_official",
                "source_level": "primary",
                "evidence_quality": 5,
                "allowed_claim_types": ["融资"],
                "change_reason": "试图伪造目录创建人。",
                "created_by_user_id": "attacker-user",
            }
        )


def test_human_public_source_preflight_registers_only_a_safe_public_webpage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """人工补充来源必须经服务器预检，且仅能作为低频候选发现入口。

    测试替换 robots 和公开页面读取，避免联网。通过时只允许追加一个 lead_only 的目录
    记录及审计；带 API 路径或参数的地址必须在网络调用之前被拒绝，不可写入来源目录或
    借通用可信来源状态接口启用。
    """
    supplement = _load_plugin_module("public_source_supplement")
    source_catalog = importlib.import_module(f"{PACKAGE_NAME}.source_catalog")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    calls: list[str] = []

    def fake_robots(source_url: str) -> dict[str, object]:
        calls.append(f"robots:{source_url}")
        return {"allowed": True, "reason": "robots_allow", "robots_url": "https://public.example.com/robots.txt", "sitemap_urls": []}

    def fake_fetch(source_url: str) -> dict[str, object]:
        calls.append(f"fetch:{source_url}")
        return {
            "source_url": "https://public.example.com/announcements",
            "content_type": "text/html",
            "byte_size": 321,
            "source_title": "公开招商公告",
        }

    monkeypatch.setattr(supplement, "check_robots_access", fake_robots)
    monkeypatch.setattr(supplement, "fetch_public_source_for_observation", fake_fetch)

    preflight = supplement.preflight_public_source("https://public.example.com/announcements")
    assert preflight["status"] == "ready_for_public_crawl"
    assert preflight["direct_crawl_allowed"] is True
    assert calls == [
        "robots:https://public.example.com/announcements",
        "fetch:https://public.example.com/announcements",
    ]

    result = supplement.add_public_source_supplement(
        tmp_path,
        source_url="https://public.example.com/announcements",
        note="人工补充公开公告入口，用于低频发现待核验候选。",
        authenticated_user_id="ordinary-public-source-operator",
    )
    source = result["source"]
    assert source["source_type"] == "human_added_public_source"
    assert source["source_level"] == "lead_only"
    assert source["status"] == "active"
    assert source["crawl_status"] == "ready_for_public_crawl"
    assert source["seed_url"] == "https://public.example.com/announcements"
    ledger = storage.load_ledger(tmp_path)
    assert ledger["trusted_source_audit"][-1]["action"] == "public_source_supplement_registered"

    with pytest.raises(ValueError, match="重新检测并启用"):
        source_catalog.set_trusted_source_status(
            tmp_path,
            source_id=source["source_id"],
            new_status="disabled",
            change_reason="不应绕过公开网页重新检测边界。",
            authenticated_user_id="ordinary-public-source-operator",
        )

    audit_before_generic_registration = copy.deepcopy(
        storage.load_ledger(tmp_path)["trusted_source_audit"]
    )
    with pytest.raises(ValueError, match="先在“人工补充公开来源”中暂停"):
        source_catalog.register_trusted_source(
            tmp_path,
            domain="public.example.com",
            source_type="enterprise_official",
            source_level="primary",
            evidence_quality=5,
            allowed_claim_types=["融资"],
            change_reason="不能通过通用目录把低频公开网页入口直接升级。",
            authenticated_user_id="ordinary-public-source-operator",
        )
    assert storage.load_ledger(tmp_path)["trusted_source_audit"] == audit_before_generic_registration


def test_human_public_source_rejects_api_or_parameterized_url_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """疑似 API、带参数临时链接和 IP 地址不得成为机器自动抓取来源。"""
    supplement = _load_plugin_module("public_source_supplement")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    network_attempted = False

    def unexpected_robots(_source_url: str) -> dict[str, object]:
        nonlocal network_attempted
        network_attempted = True
        raise AssertionError("不安全输入不应触发 robots 网络读取")

    monkeypatch.setattr(supplement, "check_robots_access", unexpected_robots)
    assert supplement.preflight_public_source("https://api.example.com/v1/projects")["status"] == "unsupported_api_or_credentials"
    assert supplement.preflight_public_source("https://public.example.com/news?token=secret")["status"] == "unsafe_target"
    assert supplement.preflight_public_source("https://127.0.0.1/internal")["status"] == "unsafe_target"
    assert network_attempted is False

    # 首次读取会补齐系统默认来源并生成时间戳；先持久化基线，才可准确断言失败输入没有
    # 追加或改写目录记录。
    storage.save_ledger(tmp_path, storage.load_ledger(tmp_path))
    before = storage.load_ledger(tmp_path)
    with pytest.raises(ValueError, match="API"):
        supplement.add_public_source_supplement(
            tmp_path,
            source_url="https://api.example.com/v1/projects",
            note=None,
            authenticated_user_id="ordinary-public-source-operator",
        )
    after = storage.load_ledger(tmp_path)
    assert after["trusted_sources"] == before["trusted_sources"]
    assert after["trusted_source_audit"] == before["trusted_source_audit"]


def test_trusted_catalog_capture_upgrades_matching_primary_opportunity_without_risk_alert(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """可信官网融资页面可升为一手证据，但融资机会不能被误报为企业风险。"""
    discovery = _load_plugin_module("source_discovery")
    source_catalog = importlib.import_module(f"{PACKAGE_NAME}.source_catalog")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    trusted_source = source_catalog.register_trusted_source(
        tmp_path,
        domain="official.example.com",
        source_type="enterprise_official",
        source_level="primary",
        evidence_quality=5,
        allowed_claim_types=["融资", "扩张"],
        change_reason="人工核验企业公告域名，用于未来自动页面分级。",
        authenticated_user_id="ordinary-operator",
    )["source"]
    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "中国可信官网半导体企业融资公告",
                "source_url": "https://news.official.example.com/announcement",
                "search_snippet": "企业完成融资。",
            }
        ],
    )
    candidate = discovery.discover_public_sources(
        tmp_path,
        sector="半导体",
        max_queries=1,
        max_results_per_query=1,
    )["candidates"][0]
    trusted_raw_content = "可信官网融资页面的原始响应。".encode("utf-8")
    trusted_snapshot = {
            "source_url": "https://news.official.example.com/announcement",
            "source_title": "可信半导体企业融资公告",
            "visible_text": "2026年8月24日 可信半导体企业完成新一轮融资并扩大芯片研发。",
            "raw_sha256": hashlib.sha256(trusted_raw_content).hexdigest(),
            "content_type": "text/html",
            "byte_size": 100,
    }
    monkeypatch.setattr(discovery, "fetch_public_source", lambda _source_url: trusted_snapshot)
    monkeypatch.setattr(
        discovery,
        "fetch_public_source_for_observation",
        lambda _source_url: {**trusted_snapshot, "raw_content": trusted_raw_content},
    )
    monkeypatch.setattr(
        discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )

    result = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="可信半导体企业",
        entity_kind="企业",
        claim_type="融资",
        source_published_at="2026-08-24",
        excerpt="可信半导体企业完成新一轮融资并扩大芯片研发。",
    )

    assert result["captured"] is True
    evidence = result["result"]["evidence"]
    assert evidence["source_level"] == "primary"
    assert evidence["evidence_quality"] == 5
    assert evidence["source_type"] == "automated_trusted_catalog_capture"
    assert evidence["trusted_source_id"] == trusted_source["source_id"]
    lead = core.get_investment_lead(tmp_path, lead_id=evidence["lead_id"])["lead"]
    assert lead["red_alert"] is False
    assert lead["risk_alert_level"] is None
    assert lead["risk_alert_evidence_ids"] == []
    assert storage.load_ledger(tmp_path)["trusted_sources"][-1]["source_id"] == trusted_source["source_id"]


def test_university_catalog_rejects_financing_claim_for_safe_downgrade(tmp_path: Path) -> None:
    """高校目录不能被配置为融资一手来源，避免借校友页面误报企业融资事实。"""
    source_catalog = _load_plugin_module("source_catalog")

    with pytest.raises(ValueError, match="不能支持事实类别"):
        source_catalog.register_trusted_source(
            tmp_path,
            domain="alumni.example.edu",
            source_type="university_alumni",
            source_level="primary",
            evidence_quality=4,
            allowed_claim_types=["融资"],
            change_reason="验证类型约束。",
            authenticated_user_id="ordinary-operator",
        )


def test_official_statistics_source_seeds_are_free_idempotent_and_cannot_upgrade_financing(
    tmp_path: Path,
) -> None:
    """官方统计来源只补目录与审计，不能借高等级域名自动证明企业融资或改写台账。

    输入是模拟的旧工作区：已保存一条正式证据，但可信来源目录尚没有国家统计局和国家
    数据平台。输出必须只新增两条固定免费来源及两条审计；重复调用或普通用户停用其中
    一条后，系统都不能覆盖人工配置。测试还确认统计来源只允许产业/扩张类事实，融资
    页面安全降级为 lead_only，从而避免把宏观统计网站错误地当作单企业融资一手证据。
    """
    source_catalog = _load_plugin_module("source_catalog")
    source_monitoring = importlib.import_module(f"{PACKAGE_NAME}.source_monitoring")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    ledger["trusted_sources"] = [
        source
        for source in ledger["trusted_sources"]
        if source.get("source_type") != "official_statistics"
    ]
    storage.save_ledger(tmp_path, ledger)
    before_seed = copy.deepcopy(storage.load_ledger(tmp_path))

    seeded = source_catalog.ensure_official_statistics_source_seeds(tmp_path)

    after_seed = storage.load_ledger(tmp_path)
    assert seeded["status"] == "seeded"
    assert {source["domain"] for source in seeded["added_sources"]} == {
        "stats.gov.cn",
        "data.stats.gov.cn",
    }
    for field_name in (
        "leads",
        "evidence",
        "score_snapshots",
        "outcome_feedback",
        "discovery_runs",
        "discovery_candidates",
        "candidate_observations",
        "relationship_graph_nodes",
        "relationship_graph_edges",
    ):
        assert after_seed[field_name] == before_seed[field_name]
    assert after_seed["evidence"] == [created["evidence"]]
    official_statistics_sources = [
        source
        for source in after_seed["trusted_sources"]
        if source.get("source_type") == "official_statistics"
    ]
    assert {
        (
            source["domain"],
            source["source_level"],
            source["evidence_quality"],
            tuple(source["allowed_claim_types"]),
        )
        for source in official_statistics_sources
    } == {
        ("stats.gov.cn", "primary", 5, ("expansion", "industry_focus")),
        ("data.stats.gov.cn", "primary", 5, ("expansion", "industry_focus")),
    }
    assert [item["action"] for item in after_seed["trusted_source_audit"][-2:]] == [
        "source_system_seeded",
        "source_system_seeded",
    ]
    assert {item["operated_by_user_id"] for item in after_seed["trusted_source_audit"][-2:]} == {
        "system-default"
    }

    assert source_catalog.classify_captured_source(
        tmp_path,
        source_url="https://www.stats.gov.cn/sj/zx/qg/index.html",
        claim_type="产业方向",
    )["source_level"] == "primary"
    assert source_catalog.classify_captured_source(
        tmp_path,
        source_url="https://data.stats.gov.cn/easyquery.htm",
        claim_type="融资",
    )["source_level"] == "lead_only"
    with pytest.raises(ValueError, match="不能支持事实类别"):
        source_catalog.register_trusted_source(
            tmp_path,
            domain="official-statistics.example.gov.cn",
            source_type="official_statistics",
            source_level="primary",
            evidence_quality=5,
            allowed_claim_types=["融资"],
            change_reason="故意尝试把官方统计来源配置为融资事实来源。",
            authenticated_user_id="ordinary-operator",
        )

    repeated = source_catalog.ensure_official_statistics_source_seeds(tmp_path)
    assert repeated["status"] == "already_configured"
    assert storage.load_ledger(tmp_path) == after_seed

    statistics_source = next(
        source for source in official_statistics_sources if source["domain"] == "stats.gov.cn"
    )
    source_catalog.set_trusted_source_status(
        tmp_path,
        source_id=statistics_source["source_id"],
        new_status="disabled",
        change_reason="普通用户暂时停用该免费统计来源，等待人工复核。",
        authenticated_user_id="ordinary-operator",
    )
    disabled_seed = source_catalog.ensure_official_statistics_source_seeds(tmp_path)
    assert disabled_seed["status"] == "already_configured"
    after_disabled_seed = storage.load_ledger(tmp_path)
    assert next(
        source
        for source in after_disabled_seed["trusted_sources"]
        if source["domain"] == "stats.gov.cn"
    )["status"] == "disabled"
    coverage = source_monitoring.get_source_coverage_report(tmp_path, window_days=30)
    assert coverage["official_statistics_sources"] == [
        {
            "source_id": "trusted-national-data-platform",
            "domain": "data.stats.gov.cn",
            "status": "active",
            "source_level": "primary",
            "allowed_claim_types": ["expansion", "industry_focus"],
        },
        {
            "source_id": "trusted-national-bureau-statistics",
            "domain": "stats.gov.cn",
            "status": "disabled",
            "source_level": "primary",
            "allowed_claim_types": ["expansion", "industry_focus"],
        },
    ]


def test_free_verified_media_seeds_only_add_secondary_discovery_entries(
    tmp_path: Path,
) -> None:
    """免费媒体种子只扩充可轮换发现目录，不能改写证据或升格为一手来源。

    输入是带有一条正式证据的临时台账；输出应为五条固定 secondary 媒体目录和对应审计。
    测试覆盖重复引导与人工停用后的保护，确保“模型/系统找到来源”不会覆盖普通用户的
    业务配置，更不会更改已有来源 URL、原始摘录、采集时间、评分或线索状态。
    """
    source_catalog = _load_plugin_module("source_catalog")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    before_seed = copy.deepcopy(storage.load_ledger(tmp_path))

    seeded = source_catalog.ensure_free_verified_media_source_seeds(tmp_path)

    after_seed = storage.load_ledger(tmp_path)
    expected_domains = {
        "stcn.com",
        "jiemian.com",
        "nbd.com.cn",
        "eastmoney.com",
        "pedaily.cn",
    }
    assert seeded["status"] == "seeded"
    assert {source["domain"] for source in seeded["added_sources"]} == expected_domains
    assert after_seed["evidence"] == [created["evidence"]]
    for field_name in (
        "leads",
        "evidence",
        "score_snapshots",
        "outcome_feedback",
        "discovery_runs",
        "discovery_candidates",
        "candidate_observations",
    ):
        assert after_seed[field_name] == before_seed[field_name]
    seeded_sources = [
        source for source in after_seed["trusted_sources"] if source.get("domain") in expected_domains
    ]
    assert len(seeded_sources) == 5
    assert {
        (source["source_type"], source["source_level"], source["evidence_quality"])
        for source in seeded_sources
    } == {("verified_media", "secondary", 3)}
    assert all(source["system_curated_chinese_business_media"] is True for source in seeded_sources)
    assert [item["action"] for item in after_seed["trusted_source_audit"][-5:]] == [
        "source_system_seeded",
        "source_system_seeded",
        "source_system_seeded",
        "source_system_seeded",
        "source_system_seeded",
    ]

    assert source_catalog.ensure_free_verified_media_source_seeds(tmp_path)["status"] == "already_configured"
    first_media_source = seeded_sources[0]
    source_catalog.set_trusted_source_status(
        tmp_path,
        source_id=first_media_source["source_id"],
        new_status="disabled",
        change_reason="人工暂时停用媒体来源，等待质量复核。",
        authenticated_user_id="ordinary-operator",
    )
    assert source_catalog.ensure_free_verified_media_source_seeds(tmp_path)["status"] == "already_configured"
    after_disabled = storage.load_ledger(tmp_path)
    assert next(
        source
        for source in after_disabled["trusted_sources"]
        if source["source_id"] == first_media_source["source_id"]
    )["status"] == "disabled"


def test_search_source_429_enters_cooldown_and_skips_remaining_queries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """公开搜索源报 429 后进入退避，本批其余查询不再访问且不写入任何证据。

    测试用固定异常模拟访问频率限制，不连接真实搜索引擎。断言健康度属于可再生治理
    数据、后续查询确实被跳过，并确认线索与原始证据列表仍为空，防止退避逻辑误把搜索
    摘要写成招商事实。
    """
    discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    calls: list[str] = []

    def fake_search_feed(query: str) -> list[dict[str, str]]:
        """记录唯一允许的一次请求后模拟公开 RSS 的 429 响应。"""
        calls.append(query)
        raise ValueError("公开搜索源 HTTP 状态码: 429")

    monkeypatch.setattr(discovery, "fetch_public_search_feed", fake_search_feed)
    result = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=2,
        max_results_per_query=1,
    )

    assert len(calls) == 1
    assert result["run"]["failed_query_count"] == 1
    assert result["run"]["skipped_due_to_cooldown_count"] == 1
    ledger = storage.load_ledger(tmp_path)
    assert ledger["leads"] == []
    assert ledger["evidence"] == []
    assert ledger["source_health"] == [
        {
            "source_key": "public_search_feed:bing_rss",
            "status": "cooldown",
            "consecutive_failures": 1,
            "last_success_at": None,
            "last_failure_at": ledger["source_health"][0]["last_failure_at"],
            "last_error_kind": "rate_limited",
            "last_error": "公开搜索源 HTTP 状态码: 429",
            "next_eligible_at": ledger["source_health"][0]["next_eligible_at"],
            "updated_at": ledger["source_health"][0]["updated_at"],
        }
    ]


def test_non_topical_search_response_enters_cooldown_without_creating_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """固定泛搜索结果属于来源质量失败，不能伪装成已完成的产业发现。"""
    discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    calls: list[str] = []

    def fake_search_feed(query: str) -> list[dict[str, str]]:
        """模拟无视查询词的百科/政府首页 RSS 响应。"""
        calls.append(query)
        return [
            {
                "source_title": "中华人民共和国_百度百科",
                "source_url": "https://baike.baidu.com/item/china",
                "search_snippet": "中国历史与地理概览。",
            }
        ]

    monkeypatch.setattr(discovery, "fetch_public_search_feed", fake_search_feed)
    result = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=2,
        max_results_per_query=1,
    )

    assert len(calls) == 1
    assert result["run"]["candidate_count"] == 0
    assert result["run"]["rejected_result_count"] == 0
    assert result["run"]["failed_query_count"] == 1
    assert result["run"]["non_topical_response_count"] == 1
    assert result["run"]["skipped_due_to_cooldown_count"] == 1
    ledger = storage.load_ledger(tmp_path)
    assert ledger["discovery_candidates"] == []
    assert ledger["evidence"] == []
    assert ledger["source_health"][0]["status"] == "cooldown"
    assert "未命中目标产业词" in ledger["source_health"][0]["last_error"]
    assert ledger["source_health"][0]["next_eligible_at"] is not None


def test_candidate_access_limit_enters_domain_cooldown_without_changing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """候选页报 403 时记录域名退避，重复抓取不会再访问页面或改写原始证据。

    输入使用服务器发现的候选 ID；输出应保持无新增证据。该测试区分“站点访问受限”
    与“正文事实不足”：前者才写来源健康度，且冷却期间的第二次调用不增加抓取次数。
    """
    discovery = _load_plugin_module("source_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(original["evidence"])
    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "中国受限站点半导体企业融资消息",
                "source_url": "https://limited.example.com/news/announcement",
                "search_snippet": "待验证公开链接。",
            }
        ],
    )
    candidate = discovery.discover_public_sources(
        tmp_path,
        sector="半导体",
        max_queries=1,
        max_results_per_query=1,
    )["candidates"][0]
    fetch_calls: list[str] = []

    def fake_fetch_public_source(source_url: str) -> dict[str, str]:
        """模拟候选域名明确拒绝自动访问，不返回可用页面快照。"""
        fetch_calls.append(source_url)
        raise ValueError("HTTP 状态码: 403")

    monkeypatch.setattr(discovery, "fetch_public_source", fake_fetch_public_source)
    monkeypatch.setattr(discovery, "fetch_public_source_for_observation", fake_fetch_public_source)
    monkeypatch.setattr(
        discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )
    first = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="受限站点企业",
        entity_kind="企业",
        claim_type="扩张",
        source_published_at="2026-08-24",
        excerpt="受限站点企业正在扩张。",
    )
    second = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="受限站点企业",
        entity_kind="企业",
        claim_type="扩张",
        source_published_at="2026-08-24",
        excerpt="受限站点企业正在扩张。",
    )

    assert first["captured"] is False
    assert second["captured"] is False
    assert "退避期" in second["message"]
    assert fetch_calls == ["https://limited.example.com/news/announcement"]
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"] == [evidence_before]
    assert ledger["discovery_candidates"][0]["capture_attempts"] == 1
    assert ledger["source_health"][-1]["source_key"] == "public_page_domain:limited.example.com"
    assert ledger["source_health"][-1]["status"] == "cooldown"
    assert ledger["source_health"][-1]["last_error_kind"] == "access_limited"


@pytest.mark.asyncio
async def test_manual_discovery_uses_curated_sources_before_any_bing_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """手动产业发现必须优先走固定可信目录，不能重走低质量 Bing RSS。

    输入是普通用户请求的英文半导体产业别名；输出应把它规范为 ``semiconductor``，补齐
    固定免费来源种子，并以媒体优先、产业限定方式调用目录发现器。测试不联网，且明确
    断言工具模块不再导入 Bing 发现函数，防止“半”字词典结果重新成为人工测试入口；返回
    的候选仍是待补证链接，不创建 evidence、线索、评分或人工复核写入。
    """
    tools = _load_plugin_module("tools")
    calls: dict[str, object] = {}
    monkeypatch.setattr(tools, "_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        tools,
        "build_discovery_queries",
        lambda sector, *, max_queries: [
            {"sector": "semiconductor", "query": f"fixed {sector} {max_queries}"}
        ],
    )
    monkeypatch.setattr(
        tools,
        "ensure_official_statistics_source_seeds",
        lambda workspace_dir: {"status": "already_configured", "workspace": str(workspace_dir)},
    )
    monkeypatch.setattr(
        tools,
        "ensure_free_verified_media_source_seeds",
        lambda workspace_dir: {"status": "seeded", "workspace": str(workspace_dir)},
    )

    def fake_directory_discovery(
        workspace_dir: Path,
        *,
        requested_sector: str | None,
        curated_media_first: bool,
        fixed_sector_index_only: bool,
    ) -> dict[str, object]:
        """记录工具传给可信目录发现器的固定范围，不连接任意搜索引擎。"""
        calls["workspace_dir"] = workspace_dir
        calls["requested_sector"] = requested_sector
        calls["curated_media_first"] = curated_media_first
        calls["fixed_sector_index_only"] = fixed_sector_index_only
        return {
            "run": {"source_feed_run_id": "manual-directory-run", "candidate_count": 1},
            "candidates": [{"candidate_id": "trusted-media-candidate", "source_level": "lead_only"}],
            "ledger_path": str(workspace_dir / "investment_attraction" / "lead_evidence_ledger.json"),
        }

    monkeypatch.setattr(tools, "discover_trusted_source_feeds", fake_directory_discovery)

    result = await tools.discover_xuzhou_public_sources(
        sector="semiconductor",
        max_queries=5,
        max_results_per_query=5,
    )

    assert not hasattr(tools, "discover_public_sources")
    assert result["ok"] is True
    assert result["discovery_mode"] == "trusted_sources_first_no_bing_manual_search"
    assert result["requested_sector"] == "semiconductor"
    assert result["candidates"] == [
        {"candidate_id": "trusted-media-candidate", "source_level": "lead_only"}
    ]
    assert calls == {
        "workspace_dir": tmp_path,
        "requested_sector": "semiconductor",
        "curated_media_first": True,
        "fixed_sector_index_only": True,
    }


def test_scheduled_monitor_writes_audit_and_coverage_report_without_changing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """固定后台监控遍历三条产业线、追加审计，并保证既有证据逐字不变。

    本测试替换发现函数，避免真实联网；返回值模拟每条产业各自已完成的发现批次。随后
    调用只读覆盖报告，确认报告能指出没有候选的信号目录，而不会触发重试、创建证据
    或覆盖调用前的来源 URL、摘录、采集时间和评分依据。
    """
    monitoring = _load_plugin_module("source_monitoring")
    source_feed_discovery = importlib.import_module(f"{PACKAGE_NAME}.source_feed_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(original["evidence"])
    discovered_sectors: list[str] = []

    def fake_directory_discovery(
        _workspace_dir: Path,
        *,
        requested_sector: str | None = None,
        curated_media_first: bool = False,
        fixed_sector_index_only: bool = False,
    ) -> dict[str, object]:
        """模拟固定产业栏目与普通目录，检查监控器绝不回落到公共搜索。"""
        if requested_sector is None:
            assert curated_media_first is False
            assert fixed_sector_index_only is False
            return {
                "run": {
                    "source_feed_run_id": "source-feed-run-supplement",
                    "status": "completed",
                    "candidate_count": 0,
                }
            }
        assert curated_media_first is True
        assert fixed_sector_index_only is True
        discovered_sectors.append(requested_sector)
        return {
            "run": {
                "source_feed_run_id": f"fixed-index-{requested_sector}",
                "status": "completed",
                "candidate_count": 0,
                "selected_source_ids": ["trusted-pedaily-investment-events"],
            }
        }

    monkeypatch.setattr(source_feed_discovery, "discover_trusted_source_feeds", fake_directory_discovery)
    result = monitoring.run_scheduled_public_discovery(tmp_path)
    report = monitoring.get_source_coverage_report(tmp_path, window_days=30)

    assert discovered_sectors == ["embodied_intelligence", "semiconductor", "biomedicine"]
    assert result["monitor_run"]["status"] == "completed"
    assert len(result["monitor_run"]["sector_results"]) == 3
    assert result["monitor_run"]["official_statistics_seed_result"]["status"] == "already_configured"
    assert result["monitor_run"]["consecutive_failure_count"] == 0
    assert result["monitor_run"]["failure_alert"] is None
    assert result["monitor_run"]["schedule"]["minimum_interval_hours"] == 6
    assert result["monitor_run"]["schedule"]["lock_strategy"] == "nonblocking_flock_per_workspace"
    ledger = storage.load_ledger(tmp_path)
    assert ledger["evidence"] == [evidence_before]
    assert len(ledger["monitoring_runs"]) == 1
    # 三条产业均新增“营业收入、产值与项目投资”来源目录，空台账时每条产业多一项
    # 可见覆盖缺口；这只反映待发现来源，不表示真实市场或企业数量。
    assert len(report["coverage_gaps"]) == 21
    assert report["restricted_sources"] == []
    assert report["monitor_schedule"]["is_due"] is False

    # Cron 被误设得过密时，第二次调用必须在联网前跳过，且不制造新的台账审计或版本变化。
    ledger_before_interval_skip = copy.deepcopy(storage.load_ledger(tmp_path))
    interval_skip = monitoring.run_scheduled_public_discovery(tmp_path)
    assert interval_skip["monitor_run"]["status"] == "skipped_not_due"
    assert interval_skip["monitor_run"]["not_persisted"] is True
    assert interval_skip["monitor_run"]["skipped_reason"] == "minimum_interval_not_elapsed"
    assert discovered_sectors == ["embodied_intelligence", "semiconductor", "biomedicine"]
    assert storage.load_ledger(tmp_path) == ledger_before_interval_skip

    # 有另一后台任务持锁时同样立即跳过，不能等待、并发访问来源或重叠保存台账。
    with storage.exclusive_scheduled_monitor_lock(tmp_path):
        lock_skip = monitoring.run_scheduled_public_discovery(tmp_path)
    assert lock_skip["monitor_run"]["status"] == "skipped_already_running"
    assert lock_skip["monitor_run"]["schedule"]["lock_state"] == "already_running"
    assert storage.load_ledger(tmp_path) == ledger_before_interval_skip


def test_source_coverage_effectiveness_feedback_is_readonly_and_never_auto_adjusts(
    tmp_path: Path,
) -> None:
    """来源有效性反馈只能关联既有候选、观察与正式证据，不能自行调配方或改台账。

    输入是同一固定搜索配方下的一条已签名正式证据、一条红色待补证观察和另一配方的
    抓取失败候选；输出必须分别呈现可追溯的数量与固定人工建议。测试在读取报告前后
    比较完整台账，证明该“学习反馈”不连接网络，不更改来源目录、搜索词、权重、线索、
    预警或证据链，也不会把候选数量写成企业事实或招商结论。
    """
    monitoring = _load_plugin_module("source_monitoring")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    raw_content = b"effective source evidence raw response"
    captured = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://effective.example.com/financing/announcement",
            excerpt="示例具身智能企业在公开公告中披露完成融资，原始内容可供人工核验。",
            raw_content=raw_content,
            capture_metadata={
                "source_type": "automated_public_search_capture",
                "collection_method": "public_search_then_snapshot",
                "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
                "discovery_run_id": "effectiveness-run",
                "discovery_candidate_id": "effective-candidate",
            },
        ),
    )
    observed_at = captured["evidence"]["created_at"]
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": "effective-candidate",
            "run_id": "effectiveness-run",
            "source_url": "https://effective.example.com/financing/announcement",
            "source_title": "有效来源候选",
            "search_snippet": "仅用于候选发现。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "固定免费早期信号查询",
            "discovered_at": observed_at,
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "captured",
            "capture_attempts": 1,
            "captured_evidence_id": captured["evidence"]["evidence_id"],
            "last_capture_error": None,
            "last_captured_at": observed_at,
        },
        {
            "candidate_id": "pending-observation-candidate",
            "run_id": "effectiveness-run",
            "source_url": "https://pending.example.net/cooperation/update",
            "source_title": "待补证候选",
            "search_snippet": "仅用于待补证观察。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "固定免费早期信号查询",
            "discovered_at": observed_at,
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
        },
        {
            "candidate_id": "failed-alumni-candidate",
            "run_id": "effectiveness-run",
            "source_url": "https://alumni.example.edu/startup/profile",
            "source_title": "受限校友候选",
            "search_snippet": "仅用于抓取失败统计。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "university_alumni",
            "discovery_query": "固定免费校友查询",
            "discovered_at": observed_at,
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "capture_failed",
            "capture_attempts": 1,
            "captured_evidence_id": None,
            "last_capture_error": "HTTP 状态码: 403",
            "last_captured_at": observed_at,
        },
    ]
    ledger["candidate_observations"] = [
        {
            "observation_id": "effectiveness-red-observation",
            "candidate_id": "pending-observation-candidate",
            "source_url": "https://pending.example.net/cooperation/update",
            "source_title": "待补证候选",
            "content_type": "text/html",
            "collected_at": observed_at,
            "raw_sha256": "e" * 64,
            "byte_size": 100,
            "snapshot_relative_path": "investment_attraction/candidate_observation_snapshots/effectiveness-red-observation.json",
            "signals": [],
            "review_priority": "red_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    ledger_before_report = copy.deepcopy(storage.load_ledger(tmp_path))

    report = monitoring.get_source_coverage_report(tmp_path, window_days=30)

    feedback = report["source_effectiveness_feedback"]
    assert feedback["feedback_type"] == "evidence_backed_source_recipe_effectiveness_read_only"
    assert feedback["automatic_source_catalog_change_performed"] is False
    assert feedback["automatic_query_or_weight_change_performed"] is False
    assert feedback["evidence_chain_mutation_performed"] is False
    early_signal = next(
        assessment
        for assessment in feedback["recipe_assessments"]
        if assessment["sector"] == "embodied_intelligence"
        and assessment["source_catalog_id"] == "public_early_signal"
    )
    assert early_signal == {
        "sector": "embodied_intelligence",
        "source_catalog_id": "public_early_signal",
        "assessment_status": "formal_evidence_yield_observed",
        "metrics": {
            "candidate_count": 2,
            "distinct_candidate_source_domain_count": 2,
            "captured_candidate_count": 1,
            "capture_failed_candidate_count": 0,
            "formal_evidence_count_in_window": 1,
            "candidate_observation_count": 1,
            "high_priority_observation_count": 1,
        },
        "recommended_manual_action": (
            "保留当前免费来源配方并继续人工核对已入证来源；不得因单个成功样本自动扩大、"
            "收缩或调权。"
        ),
    }
    failed_alumni = next(
        assessment
        for assessment in feedback["recipe_assessments"]
        if assessment["sector"] == "embodied_intelligence"
        and assessment["source_catalog_id"] == "university_alumni"
    )
    assert failed_alumni["assessment_status"] == "capture_barrier_needs_manual_review"
    assert failed_alumni["metrics"]["formal_evidence_count_in_window"] == 0
    assert "不得使用伪造指纹" in failed_alumni["recommended_manual_action"]
    assert storage.load_ledger(tmp_path) == ledger_before_report


def test_scheduled_monitor_records_consecutive_failure_alert_without_changing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """后台监控连续未完整成功三次才追加治理告警，告警不等同企业或线索预警。

    测试将搜索和可信目录发现替换为受控失败，避免真实联网；候选观察和草稿处理因空台账
    正常结束，故整轮为 ``partial_failure``。临时把最小间隔置零只为在单元测试内模拟三
    个已到期周期；每轮都必须保持既有正式证据不变，第三轮才可在运行审计中附加红色人工
    复核告警。该告警只要求检查部署、来源覆盖或网络，绝不设置任何企业 Lead 红色预警。
    """
    monitoring = _load_plugin_module("source_monitoring")
    source_feed_discovery = importlib.import_module(f"{PACKAGE_NAME}.source_feed_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    lead_red_alert_before = storage.load_ledger(tmp_path)["leads"][0]["red_alert"]
    monkeypatch.setattr(monitoring, "MIN_SCHEDULED_MONITOR_INTERVAL", monitoring.timedelta(0))

    def fail_source_directory(*_args: object, **_kwargs: object) -> dict[str, object]:
        """模拟固定栏目与普通可信目录都不可用，且不触碰候选或正式证据。"""
        raise RuntimeError("测试：固定产业栏目和可信目录暂时不可用")

    monkeypatch.setattr(source_feed_discovery, "discover_trusted_source_feeds", fail_source_directory)

    runs = [monitoring.run_scheduled_public_discovery(tmp_path) for _ in range(3)]
    persisted = storage.load_ledger(tmp_path)

    assert [run["monitor_run"]["status"] for run in runs] == [
        "partial_failure",
        "partial_failure",
        "partial_failure",
    ]
    assert [run["monitor_run"]["consecutive_failure_count"] for run in runs] == [1, 2, 3]
    assert runs[0]["monitor_run"]["failure_alert"] is None
    assert runs[1]["monitor_run"]["failure_alert"] is None
    third_alert = runs[2]["monitor_run"]["failure_alert"]
    assert third_alert["alert_type"] == "scheduled_monitor_consecutive_failure"
    assert third_alert["severity"] == "high"
    assert third_alert["consecutive_failure_count"] == 3
    assert len(persisted["monitoring_runs"]) == 3
    assert persisted["monitoring_runs"][-1]["failure_alert"] == third_alert
    assert persisted["evidence"] == [created["evidence"]]
    assert persisted["leads"][0]["red_alert"] == lead_red_alert_before


async def test_scheduled_monitor_creates_one_local_manual_review_alert_for_red_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """红色候选提醒仅写当前智能体收件箱一次，不能改写台账或宣称企业事实。"""
    tools = _load_plugin_module("tools")
    inbox_events: list[dict[str, object]] = []
    monitor_result = {
        "monitor_run": {
            "monitor_run_id": "monitor-run-red-review-test",
            "candidate_observation_result": {"high_priority_observation_count": 2},
            "failure_alert": None,
        },
        "ledger_path": str(tmp_path / "investment_attraction" / "lead_evidence_ledger.json"),
    }

    monkeypatch.setattr(tools, "_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(tools, "get_current_agent_id", lambda: "default")
    monkeypatch.setattr(
        tools,
        "run_scheduled_public_discovery",
        lambda _workspace_dir: copy.deepcopy(monitor_result),
    )

    async def fake_list_inbox_events(**_kwargs):
        """以测试内列表模拟当前智能体收件箱，避免写入真实 QwenPaw 事件文件。"""
        return copy.deepcopy(inbox_events)

    async def fake_append_event(**kwargs):
        """记录待写事件，确保测试只观察最小提醒载荷而不访问外部渠道。"""
        event = {"id": "inbox-event-red-review-test", **kwargs}
        inbox_events.append(event)
        return event

    monkeypatch.setattr(tools, "list_inbox_events", fake_list_inbox_events)
    monkeypatch.setattr(tools, "append_event", fake_append_event)

    first = await tools.run_xuzhou_source_monitor("scheduled_public_discovery")
    second = await tools.run_xuzhou_source_monitor("scheduled_public_discovery")

    assert first["ok"] is True
    assert first["notification"]["status"] == "created"
    assert second["notification"]["status"] == "already_exists"
    assert len(inbox_events) == 1
    notification = inbox_events[0]
    assert notification["agent_id"] == "default"
    assert notification["severity"] == "error"
    assert notification["status"] == "manual_review_required"
    assert notification["source_id"] == "monitor-run-red-review-test"
    assert notification["payload"] == {
        "monitor_run_id": "monitor-run-red-review-test",
        "alert_kinds": ["pending_evidence_high_priority_candidate"],
        "high_priority_observation_count": 2,
        "requires_manual_review": True,
        "automatic_business_conclusion": "not_generated",
        "evidence_mutation": "not_performed",
        "operation_scope": "free_local_inbox_manual_review_notification_only",
    }
    assert "不是企业融资、合作、投资或落地事实" in notification["body"]


def test_evidence_draft_generation_is_deterministic_pending_only_and_preserves_evidence_chain(
    tmp_path: Path,
) -> None:
    """候选快照只能生成可追溯的待确认草稿，不能自动入证或改写任何业务记录。

    测试直接构造一条已通过服务器路径保存的候选观察快照，不访问网络。第一次运行应从
    原文提取实体、日期、融资类别和完整原句；重复运行必须由稳定 ID 去重。随后篡改另一
    快照的原始字节，抽取器必须跳过它且完全不保存，从而证明草稿不会建立在不可验证内容
    上，也不会顺便改变线索、正式证据、评分、反馈、候选或原观察索引。
    """
    draft_module = _load_plugin_module("evidence_draft")
    observation_module = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    observation_id = "candidate-observation-draft-test"
    candidate_id = "discovery-candidate-draft-test"
    collected_at = "2026-08-26T01:02:03+00:00"
    visible_text = (
        "2026年8月26日，徐州智行机器人科技有限公司宣布完成A轮融资，并计划在徐州寻找产业合作伙伴。"
    )
    raw_content = visible_text.encode("utf-8")
    raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    snapshot_path = storage.save_candidate_observation_snapshot(
        tmp_path,
        observation_id=observation_id,
        payload={
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://draft.example.com/news/financing",
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "content_type": "text/html",
            "collected_at": collected_at,
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "visible_text": visible_text,
            "raw_content_base64": base64.b64encode(raw_content).decode("ascii"),
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        },
    )
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-draft-test",
            "source_url": "https://draft.example.com/news/financing",
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "search_snippet": "搜索结果仅供待核验。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器测试查询",
            "discovered_at": "2026-08-26T01:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "observation_attempts": 1,
            "last_observation_id": observation_id,
            "last_observation_error": None,
            "last_observed_at": collected_at,
        }
    ]
    ledger["candidate_observations"] = [
        {
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://draft.example.com/news/financing",
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "content_type": "text/html",
            "collected_at": collected_at,
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "snapshot_relative_path": str(snapshot_path.relative_to(tmp_path)),
            "signals": [
                {
                    "signal_key": "financing_event_terms",
                    "matched_terms": ["融资"],
                    "state": "pending_evidence_verification",
                    "review_priority": "red_review",
                }
            ],
            "review_priority": "red_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    before = storage.load_ledger(tmp_path)
    protected_before = {
        field_name: copy.deepcopy(before[field_name])
        for field_name in (
            "leads",
            "evidence",
            "score_snapshots",
            "outcome_feedback",
            "discovery_candidates",
            "candidate_observations",
        )
    }

    first = draft_module.generate_evidence_drafts_from_observations(tmp_path)
    after_first = storage.load_ledger(tmp_path)
    draft = after_first["evidence_drafts"][0]
    details = observation_module.get_candidate_observation(tmp_path, observation_id=observation_id)

    assert first["run"]["generated_draft_count"] == 1
    assert draft["state"] == "pending_human_confirmation"
    assert draft["operation_scope"] == "automatic_deterministic_candidate_observation_draft_only"
    assert draft["observation_id"] == observation_id
    assert draft["raw_sha256"] == raw_sha256
    assert draft["entity_name"] == "徐州智行机器人科技有限公司"
    assert draft["entity_kind"] == "enterprise"
    assert draft["source_published_at"] == "2026-08-26"
    assert draft["source_published_at_raw"] == "2026年8月26日"
    assert draft["claim_type"] == "financing"
    assert draft["excerpt"] == visible_text
    assert "重新受控抓取并校验来源当前正文、来源等级和正式证据字段" in draft[
        "needs_human_confirmation"
    ]
    assert details["evidence_draft_count"] == 1
    assert details["evidence_drafts"][0]["draft_id"] == draft["draft_id"]
    assert after_first["evidence"] == [created["evidence"]]
    for field_name, original_value in protected_before.items():
        assert after_first[field_name] == original_value

    repeated = draft_module.generate_evidence_drafts_from_observations(tmp_path)
    assert repeated["run"]["generated_draft_count"] == 0
    assert repeated["run"]["skipped_duplicate_count"] == 1
    assert storage.load_ledger(tmp_path) == after_first

    tampered_observation_id = "candidate-observation-draft-tampered"
    tampered_candidate_id = "discovery-candidate-draft-tampered"
    tampered_raw_content = b"2026-08-27\xe6\x9f\x90\xe5\x85\xac\xe5\x8f\xb8\xe5\xae\xa3\xe5\xb8\x83\xe8\x9e\x8d\xe8\xb5\x84\xe3\x80\x82"
    tampered_raw_sha256 = hashlib.sha256(tampered_raw_content).hexdigest()
    tampered_snapshot_path = storage.save_candidate_observation_snapshot(
        tmp_path,
        observation_id=tampered_observation_id,
        payload={
            "observation_id": tampered_observation_id,
            "candidate_id": tampered_candidate_id,
            "source_url": "https://draft.example.com/news/tampered",
            "source_title": "某公司融资页面",
            "content_type": "text/html",
            "collected_at": "2026-08-27T01:02:03+00:00",
            "raw_sha256": tampered_raw_sha256,
            "byte_size": len(tampered_raw_content),
            "visible_text": "2026-08-27某公司宣布融资。",
            "raw_content_base64": base64.b64encode(tampered_raw_content).decode("ascii"),
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        },
    )
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"].append(
        {
            **ledger["discovery_candidates"][0],
            "candidate_id": tampered_candidate_id,
            "source_url": "https://draft.example.com/news/tampered",
            "source_title": "某公司融资页面",
            "last_observation_id": tampered_observation_id,
        }
    )
    ledger["candidate_observations"].append(
        {
            **ledger["candidate_observations"][0],
            "observation_id": tampered_observation_id,
            "candidate_id": tampered_candidate_id,
            "source_url": "https://draft.example.com/news/tampered",
            "source_title": "某公司融资页面",
            "collected_at": "2026-08-27T01:02:03+00:00",
            "raw_sha256": tampered_raw_sha256,
            "byte_size": len(tampered_raw_content),
            "snapshot_relative_path": str(tampered_snapshot_path.relative_to(tmp_path)),
        }
    )
    storage.save_ledger(tmp_path, ledger)
    tampered_snapshot = json.loads(tampered_snapshot_path.read_text(encoding="utf-8"))
    tampered_snapshot["raw_content_base64"] = base64.b64encode(b"forged page body").decode("ascii")
    tampered_snapshot_path.write_text(
        json.dumps(tampered_snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    before_tampered_run = storage.load_ledger(tmp_path)

    rejected = draft_module.generate_evidence_drafts_from_observations(tmp_path)

    assert rejected["run"]["generated_draft_count"] == 0
    assert rejected["run"]["skipped_invalid_snapshot_count"] == 1
    assert storage.load_ledger(tmp_path) == before_tampered_run


def test_evidence_draft_prefers_quoted_title_entity_and_never_parses_partial_day() -> None:
    """投资新闻标题中的明确主体优先，日期不能把 18 日错误截成 1 日。"""
    draft_module = _load_plugin_module("evidence_draft")

    assert draft_module._entity_name_candidate(
        "「矩量光启」专注可扩展量子芯片整机研发_投资界",
        "公司成立不足一年即完成融资。",
    ) == "矩量光启"
    assert draft_module._source_date_candidate("页面日期：2026-05-18") == (
        "2026-05-18",
        "2026-05-18",
    )


def test_signed_legacy_ledger_adds_empty_evidence_drafts_only_after_successful_verification(
    tmp_path: Path,
) -> None:
    """升级时先验证旧版签名，再在内存补草稿列表，避免把可信台账误判为被篡改。"""
    storage = _load_plugin_module("storage")
    legacy_ledger = storage.empty_ledger()
    legacy_ledger.pop("evidence_drafts")
    legacy_ledger.pop("evidence_draft_confirmation_audit")
    legacy_ledger.pop("evidence_draft_business_review_audit")
    legacy_ledger.pop("lead_followup_approval_audit")
    storage.save_ledger(tmp_path, legacy_ledger)

    upgraded = storage.load_ledger(tmp_path)

    assert upgraded["evidence_drafts"] == []
    assert upgraded["evidence_draft_confirmation_audit"] == []
    assert upgraded["evidence_draft_business_review_audit"] == []
    assert upgraded["lead_followup_approval_audit"] == []
    storage.save_ledger(tmp_path, upgraded)
    assert storage.load_ledger(tmp_path)["evidence_drafts"] == []


def test_authenticated_draft_confirmation_recaptures_before_appending_evidence_and_keeps_draft_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通用户确认只能使用服务器草稿重抓核验，成功后追加证据和审计而不改草稿。

    测试替换 robots 与页面响应，确保不联网。确认服务必须把草稿内已签名字段传给既有
    受控抓取器，并由抓取器重新检查页面正文后才新增正式证据；操作者和备注只进入独立
    审计。重复确认不得再次访问网页或追加证据，草稿和候选观察在所有路径中逐字不变。
    """
    confirmation = _load_plugin_module("evidence_draft_confirmation")
    draft_module = importlib.import_module(f"{PACKAGE_NAME}.evidence_draft")
    source_discovery = importlib.import_module(f"{PACKAGE_NAME}.source_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    candidate_id = "discovery-candidate-confirm-test"
    draft_id = "evidence-draft-confirm-test"
    observation_id = "candidate-observation-confirm-test"
    source_url = "https://confirm.example.com/news/financing"
    visible_text = "2026-08-27 徐州智行机器人科技有限公司宣布完成A轮融资。"
    excerpt = visible_text
    raw_content = visible_text.encode("utf-8")
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-confirm-test",
            "source_url": source_url,
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "search_snippet": "候选页面待服务器重抓。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器测试查询",
            "discovered_at": "2026-08-27T01:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    ledger["candidate_observations"] = [
        {
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": source_url,
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "content_type": "text/html",
            "collected_at": "2026-08-27T01:01:00+00:00",
            "raw_sha256": hashlib.sha256(b"original-observation").hexdigest(),
            "byte_size": len(b"original-observation"),
            "snapshot_relative_path": "investment_attraction/candidate_observation_snapshots/candidate-observation-confirm-test.json",
            "signals": [],
            "review_priority": "normal_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        }
    ]
    ledger["evidence_drafts"] = [
        {
            "draft_id": draft_id,
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": source_url,
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "collected_at": "2026-08-27T01:01:00+00:00",
            "raw_sha256": hashlib.sha256(b"original-observation").hexdigest(),
            "entity_name": "徐州智行机器人科技有限公司",
            "entity_kind": "enterprise",
            "source_published_at": "2026-08-27",
            "source_published_at_raw": "2026-08-27",
            "claim_type": "financing",
            "excerpt": excerpt,
            "matched_terms": ["融资"],
            "needs_human_confirmation": ["重新受控抓取并校验来源当前正文、来源等级和正式证据字段"],
            "extractor_version": "deterministic-candidate-observation-v1",
            "created_at": "2026-08-27T01:02:00+00:00",
            "state": "pending_human_confirmation",
            "operation_scope": "automatic_deterministic_candidate_observation_draft_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    before = storage.load_ledger(tmp_path)
    fetch_calls: list[str] = []
    monkeypatch.setattr(
        source_discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )

    def _verified_fetch(requested_url: str) -> dict[str, object]:
        """返回含草稿实体、日期和完整摘录的服务器重抓内容，模拟可核验页面。"""
        fetch_calls.append(requested_url)
        return {
            "source_url": source_url,
            "source_title": "徐州智行机器人科技有限公司宣布完成A轮融资",
            "visible_text": visible_text,
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "raw_content": raw_content,
        }

    monkeypatch.setattr(source_discovery, "fetch_public_source_for_observation", _verified_fetch)
    result = confirmation.confirm_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        review_note="普通登录用户确认该草稿，应以服务器重新抓取结果为准。",
        authenticated_user_id="ordinary-user-confirm",
    )
    after = storage.load_ledger(tmp_path)
    public_draft = draft_module.evidence_drafts_for_observation(
        after, observation_id=observation_id
    )[0]

    assert result["confirmed"] is True
    assert fetch_calls == [source_url]
    assert len(after["evidence"]) == len(before["evidence"]) + 1
    assert after["evidence"][: len(before["evidence"])] == before["evidence"]
    assert after["evidence_drafts"] == before["evidence_drafts"]
    assert after["candidate_observations"] == before["candidate_observations"]
    audit = after["evidence_draft_confirmation_audit"][-1]
    assert audit["confirmed_by_user_id"] == "ordinary-user-confirm"
    assert audit["old_state"] == "pending_human_confirmation"
    assert audit["new_state"] == "confirmed_and_captured"
    assert audit["capture_result"] == "server_recapture_verified"
    assert audit["evidence_id"] == result["capture"]["result"]["evidence"]["evidence_id"]
    assert public_draft["confirmation_state"] == "confirmed_and_captured"
    assert public_draft["confirmation_history"][0]["confirmed_by_user_id"] == "ordinary-user-confirm"

    repeated = confirmation.confirm_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        review_note="重复请求不得重新抓取。",
        authenticated_user_id="another-ordinary-user",
    )
    assert repeated["confirmed"] is False
    assert fetch_calls == [source_url]
    assert len(storage.load_ledger(tmp_path)["evidence_draft_confirmation_audit"]) == 1
    assert storage.load_ledger(tmp_path)["evidence"] != [original["evidence"]]


def test_draft_confirmation_rejects_changed_page_without_creating_evidence_or_lead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """页面重抓不能验证草稿摘录时，只追加失败审计，不新增线索或正式证据。"""
    confirmation = _load_plugin_module("evidence_draft_confirmation")
    source_discovery = importlib.import_module(f"{PACKAGE_NAME}.source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    candidate_id = "discovery-candidate-confirm-failed"
    draft_id = "evidence-draft-confirm-failed"
    source_url = "https://confirm.example.com/news/changed"
    ledger = storage.empty_ledger()
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-confirm-failed",
            "source_url": source_url,
            "source_title": "页面已变化",
            "search_snippet": "候选页面待核验。",
            "sector": "semiconductor",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器测试查询",
            "discovered_at": "2026-08-27T01:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    ledger["evidence_drafts"] = [
        {
            "draft_id": draft_id,
            "observation_id": "candidate-observation-confirm-failed",
            "candidate_id": candidate_id,
            "source_url": source_url,
            "source_title": "页面已变化",
            "collected_at": "2026-08-27T01:01:00+00:00",
            "raw_sha256": hashlib.sha256(b"old snapshot").hexdigest(),
            "entity_name": "徐州芯元科技有限公司",
            "entity_kind": "enterprise",
            "source_published_at": "2026-08-27",
            "source_published_at_raw": "2026-08-27",
            "claim_type": "financing",
            "excerpt": "徐州芯元科技有限公司宣布完成融资。",
            "matched_terms": ["融资"],
            "needs_human_confirmation": ["重新受控抓取并校验来源当前正文、来源等级和正式证据字段"],
            "extractor_version": "deterministic-candidate-observation-v1",
            "created_at": "2026-08-27T01:02:00+00:00",
            "state": "pending_human_confirmation",
            "operation_scope": "automatic_deterministic_candidate_observation_draft_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    before = storage.load_ledger(tmp_path)
    changed_text = "2026-08-27 徐州芯元科技有限公司发布产品更新公告。"
    changed_raw = changed_text.encode("utf-8")
    monkeypatch.setattr(
        source_discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )
    monkeypatch.setattr(
        source_discovery,
        "fetch_public_source_for_observation",
        lambda _source_url: {
            "source_url": source_url,
            "source_title": "徐州芯元科技有限公司产品更新",
            "visible_text": changed_text,
            "raw_sha256": hashlib.sha256(changed_raw).hexdigest(),
            "raw_content": changed_raw,
        },
    )

    result = confirmation.confirm_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        review_note="页面正文与草稿摘录不一致，保留失败审计等待进一步补证。",
        authenticated_user_id="ordinary-user-failed-confirm",
    )
    after = storage.load_ledger(tmp_path)

    assert result["confirmed"] is False
    assert after["evidence"] == before["evidence"]
    assert after["leads"] == before["leads"]
    assert after["evidence_drafts"] == before["evidence_drafts"]
    assert after["candidate_observations"] == before["candidate_observations"]
    audit = after["evidence_draft_confirmation_audit"][-1]
    assert audit["confirmed_by_user_id"] == "ordinary-user-failed-confirm"
    assert audit["new_state"] == "confirmation_not_captured"
    assert audit["evidence_id"] is None
    assert audit["verification_failure_stage"] == "original_excerpt_changed_or_not_found"
    assert "excerpt" in audit["verification_failure_reason"]
    assert result["verification"]["stage"] == "original_excerpt_changed_or_not_found"
    assert "excerpt" in result["message"]


@pytest.mark.asyncio
async def test_evidence_draft_confirmation_api_uses_verified_identity_and_forbids_claimed_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """确认接口只把认证身份和备注传给核心，客户端伪造用户或证据字段必须被拒绝。"""
    confirmation_api = _load_plugin_module("evidence_draft_api")
    verified_principal = Principal(
        user_id="ordinary-user-api-confirm",
        username="ordinary-user",
        role="user",
    )
    received: dict[str, object] = {}
    monkeypatch.setattr(confirmation_api, "get_current_principal", lambda _request: verified_principal)

    async def _owned_workspace(_request: object) -> SimpleNamespace:
        """返回认证用户拥有的工作区，路由仍须先调用平台工作区归属校验。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    def _fake_confirmation(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录路由传递的服务器参数，不做网络或台账写入。"""
        received["workspace_dir"] = workspace_dir
        received.update(kwargs)
        return {"confirmed": False, "message": "测试替身未执行抓取"}

    monkeypatch.setattr(
        confirmation_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(confirmation_api, "confirm_evidence_draft", _fake_confirmation)
    response = await confirmation_api.confirm_xuzhou_evidence_draft(
        "evidence-draft-api-test",
        confirmation_api.ConfirmEvidenceDraftRequest(
            review_note="普通用户确认后应由服务器重新抓取。"
        ),
        object(),
    )

    assert response["ok"] is True
    assert received["authenticated_user_id"] == "ordinary-user-api-confirm"
    assert received["draft_id"] == "evidence-draft-api-test"
    assert "reviewer_user_id" not in received
    with pytest.raises(ValidationError):
        confirmation_api.ConfirmEvidenceDraftRequest.model_validate(
            {
                "review_note": "尝试伪造操作者和来源字段。",
                "reviewer_user_id": "attacker",
                "entity_name": "伪造企业",
                "source_url": "https://attacker.example.com/forged",
            }
        )


def test_incomplete_draft_business_review_is_append_only_and_invalid_blocks_recapture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """缺少关键字段的草稿只可追加业务判断，不能补造事实或触发无效草稿重抓。

    测试使用没有实体、日期、类别和摘录的草稿，依次写入待补证、观察、无效三个审计。
    每一步都必须保留草稿、候选观察、线索、证据、评分、反馈与确认历史不变。无效后调用
    确认服务应在联网之前拒绝；完整草稿也必须被待补证业务判断接口拒绝，避免绕开重抓核验。
    """
    business_review = _load_plugin_module("evidence_draft_business_review")
    confirmation = importlib.import_module(f"{PACKAGE_NAME}.evidence_draft_confirmation")
    draft_module = importlib.import_module(f"{PACKAGE_NAME}.evidence_draft")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    draft_id = "evidence-draft-incomplete-review-test"
    observation_id = "candidate-observation-incomplete-review-test"
    ledger = storage.empty_ledger()
    ledger["evidence_drafts"] = [
        {
            "draft_id": draft_id,
            "observation_id": observation_id,
            "candidate_id": "discovery-candidate-incomplete-review-test",
            "source_url": "https://incomplete.example.com/news/unknown",
            "source_title": "待补证候选页面",
            "collected_at": "2026-08-27T01:01:00+00:00",
            "raw_sha256": hashlib.sha256(b"incomplete draft snapshot").hexdigest(),
            "entity_name": None,
            "entity_kind": None,
            "source_published_at": None,
            "source_published_at_raw": None,
            "claim_type": None,
            "excerpt": None,
            "matched_terms": ["融资"],
            "needs_human_confirmation": ["实体名称", "来源发布日期", "原始摘录"],
            "extractor_version": "deterministic-candidate-observation-v1",
            "created_at": "2026-08-27T01:02:00+00:00",
            "state": "pending_human_confirmation",
            "operation_scope": "automatic_deterministic_candidate_observation_draft_only",
        },
        {
            "draft_id": "evidence-draft-complete-review-test",
            "observation_id": "candidate-observation-complete-review-test",
            "candidate_id": "discovery-candidate-complete-review-test",
            "source_url": "https://incomplete.example.com/news/complete",
            "source_title": "字段完整草稿",
            "collected_at": "2026-08-27T01:01:00+00:00",
            "raw_sha256": hashlib.sha256(b"complete draft snapshot").hexdigest(),
            "entity_name": "徐州示例科技有限公司",
            "entity_kind": "enterprise",
            "source_published_at": "2026-08-27",
            "source_published_at_raw": "2026-08-27",
            "claim_type": "financing",
            "excerpt": "徐州示例科技有限公司宣布完成融资。",
            "matched_terms": ["融资"],
            "needs_human_confirmation": ["重新受控抓取并校验来源当前正文、来源等级和正式证据字段"],
            "extractor_version": "deterministic-candidate-observation-v1",
            "created_at": "2026-08-27T01:02:00+00:00",
            "state": "pending_human_confirmation",
            "operation_scope": "automatic_deterministic_candidate_observation_draft_only",
        },
    ]
    storage.save_ledger(tmp_path, ledger)
    before = storage.load_ledger(tmp_path)
    protected_fields = (
        "leads",
        "evidence",
        "discovery_candidates",
        "candidate_observations",
        "evidence_drafts",
        "evidence_draft_confirmation_audit",
        "score_snapshots",
        "outcome_feedback",
    )
    protected_before = {field_name: copy.deepcopy(before[field_name]) for field_name in protected_fields}

    first = business_review.review_incomplete_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        decision="待补证",
        review_note="关键字段尚缺失，保留候选并等待后续可验证公开来源。",
        authenticated_user_id="ordinary-user-incomplete",
    )
    second = business_review.review_incomplete_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        decision="观察",
        review_note="当前仍无可验证实体和发布日期，暂时观察后续公开动态。",
        authenticated_user_id="ordinary-user-observe",
    )
    after_second = storage.load_ledger(tmp_path)
    public_draft = draft_module.evidence_drafts_for_observation(
        after_second, observation_id=observation_id
    )[0]

    assert first["business_review"]["old_state"] == "pending_human_confirmation"
    assert first["business_review"]["new_state"] == "need_evidence"
    assert second["business_review"]["old_state"] == "need_evidence"
    assert second["business_review_state"] == "observe"
    assert public_draft["business_review_state"] == "observe"
    assert len(public_draft["business_review_history"]) == 2
    assert public_draft["business_review_history"][0]["reviewed_by_user_id"] == "ordinary-user-observe"
    for field_name, original_value in protected_before.items():
        assert after_second[field_name] == original_value

    third = business_review.review_incomplete_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        decision="无效",
        review_note="长期无法补齐最低证据字段，普通用户决定暂时作废该草稿。",
        authenticated_user_id="ordinary-user-invalid",
    )
    assert third["business_review_state"] == "invalid"
    monkeypatch.setattr(
        confirmation,
        "capture_discovery_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("无效草稿不得联网重抓")),
    )
    with pytest.raises(ValueError, match="已被业务判断为无效"):
        confirmation.confirm_evidence_draft(
            tmp_path,
            draft_id=draft_id,
            review_note="无效后不能重抓。",
            authenticated_user_id="ordinary-user-retry",
        )
    with pytest.raises(ValueError, match="关键字段完整"):
        business_review.review_incomplete_evidence_draft(
            tmp_path,
            draft_id="evidence-draft-complete-review-test",
            decision="待补证",
            review_note="完整草稿必须走服务器重抓确认。",
            authenticated_user_id="ordinary-user-complete",
        )
    assert storage.load_ledger(tmp_path)["evidence_drafts"] == before["evidence_drafts"]


@pytest.mark.asyncio
async def test_incomplete_draft_business_review_api_uses_verified_identity_and_rejects_fact_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """业务判断接口只能提交状态与备注，身份取认证主体且拒绝原始事实字段。"""
    evidence_draft_api = _load_plugin_module("evidence_draft_api")
    verified_principal = Principal(
        user_id="ordinary-user-business-api",
        username="ordinary-user",
        role="user",
    )
    received: dict[str, object] = {}
    monkeypatch.setattr(evidence_draft_api, "get_current_principal", lambda _request: verified_principal)

    async def _owned_workspace(_request: object) -> SimpleNamespace:
        """提供认证用户可访问的工作区，保持路由实际归属校验调用。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    def _fake_business_review(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录路由传入参数，不联网、不写入台账，用于验证身份来源。"""
        received["workspace_dir"] = workspace_dir
        received.update(kwargs)
        return {"business_review_state": "need_evidence"}

    monkeypatch.setattr(
        evidence_draft_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(evidence_draft_api, "review_incomplete_evidence_draft", _fake_business_review)
    response = await evidence_draft_api.review_xuzhou_incomplete_evidence_draft(
        "evidence-draft-business-api-test",
        evidence_draft_api.ReviewIncompleteEvidenceDraftRequest(
            decision="待补证",
            review_note="普通用户只补充业务判断，不补写来源事实。",
        ),
        object(),
    )

    assert response["ok"] is True
    assert received["authenticated_user_id"] == "ordinary-user-business-api"
    assert received["decision"] == "待补证"
    assert "reviewed_by_user_id" not in received
    with pytest.raises(ValidationError):
        evidence_draft_api.ReviewIncompleteEvidenceDraftRequest.model_validate(
            {
                "decision": "待补证",
                "review_note": "尝试补写实体和来源。",
                "reviewed_by_user_id": "attacker",
                "entity_name": "伪造企业",
                "source_published_at": "2026-08-27",
                "excerpt": "伪造摘录",
            }
        )


def test_candidate_observation_saves_raw_snapshot_and_only_marks_pending_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """自动观察保存原始字节与词表命中，但绝不把命中升级为事实或业务动作。

    测试先写入真实证据和一个服务器发现候选，再替换 robots 与页面响应，避免联网。
    输出必须保留原有线索、证据、评分和反馈逐字不变；新增内容只能是候选的观察索引、
    0600 原始快照和 ``pending_evidence_verification`` 审计记录，红色优先级也只能要求
    人工查看，不能等同于 LeadRecord 的红色预警。
    """
    observation = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger_before = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(ledger_before["evidence"])
    leads_before = copy.deepcopy(ledger_before["leads"])
    scores_before = copy.deepcopy(ledger_before["score_snapshots"])
    feedback_before = copy.deepcopy(ledger_before["outcome_feedback"])
    ledger_before["discovery_candidates"] = [
        {
            "candidate_id": "discovery-candidate-observation-test",
            "run_id": "discovery-run-observation-test",
            "source_url": "https://candidate.example.com/news/fundraising",
            "source_title": "待补证的具身智能企业页面",
            "search_snippet": "搜索摘要不是事实。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器生成的测试查询",
            "discovered_at": "2026-08-26T00:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger_before)
    raw_content = "企业正在融资，并计划扩产和寻找合作伙伴。".encode("utf-8")

    monkeypatch.setattr(
        observation,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )
    monkeypatch.setattr(
        observation,
        "fetch_public_source_for_observation",
        lambda _source_url: {
            "source_url": "https://candidate.example.com/news/fundraising",
            "source_title": "示例企业公开页面",
            "content_type": "text/html",
            "visible_text": "示例企业正在融资，并计划扩产和寻找合作伙伴。",
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "byte_size": len(raw_content),
            "raw_content": raw_content,
        },
    )

    result = observation.observe_discovery_candidates(tmp_path)

    assert result["run"]["status"] == "completed"
    assert result["run"]["observation_count"] == 1
    assert result["run"]["high_priority_observation_count"] == 1
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == evidence_before
    assert ledger_after["leads"] == leads_before
    assert ledger_after["score_snapshots"] == scores_before
    assert ledger_after["outcome_feedback"] == feedback_before
    assert ledger_after["leads"][0]["lead_id"] == original["lead"]["lead_id"]
    candidate = ledger_after["discovery_candidates"][0]
    assert candidate["state"] == "discovered"
    assert candidate["capture_attempts"] == 0
    assert candidate["observation_attempts"] == 1
    observed = ledger_after["candidate_observations"][0]
    assert observed["state"] == "pending_evidence_verification"
    assert observed["review_priority"] == "high_priority_review"
    assert {signal["signal_key"] for signal in observed["signals"]} == {
        "fundraising_need_terms",
        "capacity_expansion_terms",
        "landing_window_terms",
    }
    snapshot_path = tmp_path / observed["snapshot_relative_path"]
    assert snapshot_path.exists()
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    saved_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert base64.b64decode(saved_snapshot["raw_content_base64"]) == raw_content
    assert saved_snapshot["raw_sha256"] == observed["raw_sha256"]
    assert observed["snapshot_storage_bytes"] == snapshot_path.stat().st_size
    assert saved_snapshot["operation_scope"] == "automatic_candidate_raw_snapshot_pending_evidence_only"


def test_invalid_candidate_business_review_stops_future_automatic_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """人工标记无效后，监控不应再次访问该候选页面。

    测试仅建立一条符合自动观察范围的候选，再通过候选级业务判断标为无效。页面抓取与
    robots 检查都替换为立即失败的函数：若实现错误地继续访问网络，测试会立刻失败。正确
    输出只记录本轮跳过审计，保留候选、候选业务判断、快照和正式 evidence，而不把“无效”
    删除或伪装成企业事实。
    """
    observation = _load_plugin_module("candidate_observation")
    candidate_review = importlib.import_module(f"{PACKAGE_NAME}.candidate_review")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    ledger = storage.empty_ledger()
    candidate_id = "discovery-candidate-invalid-observation-test"
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-invalid-observation-test",
            "source_url": "https://example.cn/company/should-not-fetch",
            "source_title": "已人工排除的候选页面",
            "search_snippet": "仅用于测试自动观察停止。",
            "sector": "semiconductor",
            "source_catalog_id": "trusted-investment-index",
            "discovery_query": "服务器固定来源目录查询",
            "discovered_at": "2026-09-07T00:00:00+00:00",
            "source_type": "fixed_investment_event_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    candidate_review.review_discovery_candidate(
        tmp_path,
        candidate_id=candidate_id,
        decision="invalid",
        review_note="人工确认当前材料不进入后续自动观察。",
        authenticated_user_id="ordinary-user-003",
    )

    def fail_if_network_is_used(*_args, **_kwargs):
        """阻止测试意外进行网络访问；被调用即说明无效判断没有生效。"""
        raise AssertionError("标记无效的候选不应再触发 robots 或页面抓取")

    monkeypatch.setattr(observation, "check_robots_access", fail_if_network_is_used)
    monkeypatch.setattr(observation, "fetch_public_source_for_observation", fail_if_network_is_used)

    result = observation.observe_discovery_candidates(tmp_path)

    assert result["run"]["observation_count"] == 0
    assert result["run"]["business_invalid_candidate_count"] == 1
    after = storage.load_ledger(tmp_path)
    assert after["discovery_candidates"][0].get("observation_attempts", 0) == 0
    assert after["candidate_observations"] == []
    assert after["candidate_business_reviews"][-1]["new_review_state"] == "invalid"


def test_official_link_corroboration_adds_second_source_snapshot_without_mutating_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """候选原页明确标记官网时，仅追加第二来源待核验快照。

    测试构造一份已校验的投资事件候选页面，其中唯一可用外链的锚文本明确写为“官方网站”。
    robots 和公网抓取均由本地替身提供，因而不会联网。输出必须保存独立第二来源原文快照，
    但候选、父快照、线索、正式 evidence、评分和人工业务判断均完全不变；尤其不能把官网
    链接候选自动写成“中国业务已确认”或正式招商线索。
    """
    corroboration = _load_plugin_module("candidate_corroboration")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    candidate_id = "discovery-candidate-corroboration-test"
    observation_id = "candidate-observation-corroboration-test"
    parent_url = "https://vc.pedaily.cn/company/123.html"
    parent_raw = (
        '<html><head><title>示例融资企业</title></head><body>'
        '<a href="https://official.example.cn/">官方网站</a>'
        '</body></html>'
    ).encode("utf-8")
    parent_hash = hashlib.sha256(parent_raw).hexdigest()
    parent_payload = {
        "observation_id": observation_id,
        "candidate_id": candidate_id,
        "source_url": parent_url,
        "source_title": "示例融资企业",
        "content_type": "text/html",
        "collected_at": "2026-09-07T00:00:00+00:00",
        "raw_sha256": parent_hash,
        "byte_size": len(parent_raw),
        "visible_text": "示例融资企业 官方网站",
        "raw_content_base64": base64.b64encode(parent_raw).decode("ascii"),
        "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
    }
    parent_snapshot_path = storage.save_candidate_observation_snapshot(
        tmp_path,
        observation_id=observation_id,
        payload=parent_payload,
    )
    ledger = storage.empty_ledger()
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-corroboration-test",
            "source_url": parent_url,
            "source_title": "示例融资企业",
            "search_snippet": "融资事件索引仅供补证。",
            "sector": "semiconductor",
            "source_catalog_id": "trusted-pedaily-investment-events",
            "discovery_query": "服务器固定产业栏目",
            "discovered_at": "2026-09-07T00:00:00+00:00",
            "source_type": "fixed_investment_event_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "requires_china_business_verification": True,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    ledger["candidate_observations"] = [
        {
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": parent_url,
            "source_title": "示例融资企业",
            "content_type": "text/html",
            "collected_at": "2026-09-07T00:00:00+00:00",
            "raw_sha256": parent_hash,
            "byte_size": len(parent_raw),
            "snapshot_storage_bytes": parent_snapshot_path.stat().st_size,
            "snapshot_relative_path": str(parent_snapshot_path.relative_to(tmp_path)),
            "signals": [],
            "review_priority": "normal_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
            "observation_kind": "initial",
            "previous_observation_id": None,
            "previous_raw_sha256": None,
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    protected_before = {
        field_name: copy.deepcopy(storage.load_ledger(tmp_path)[field_name])
        for field_name in (
            "discovery_candidates",
            "candidate_observations",
            "candidate_business_reviews",
            "leads",
            "evidence",
            "score_snapshots",
            "outcome_feedback",
        )
    }
    second_raw = "官网公开介绍：企业在中国开展研发。".encode("utf-8")
    monkeypatch.setattr(
        corroboration,
        "check_robots_access",
        lambda source_url: {"allowed": source_url == "https://official.example.cn/", "reason": "robots_allow"},
    )
    monkeypatch.setattr(
        corroboration,
        "fetch_public_source_for_observation",
        lambda _source_url: {
            "source_url": "https://official.example.cn/",
            "source_title": "示例企业官方网站",
            "content_type": "text/html",
            "visible_text": "官网公开介绍：企业在中国开展研发。",
            "raw_content": second_raw,
            "raw_sha256": hashlib.sha256(second_raw).hexdigest(),
            "byte_size": len(second_raw),
        },
    )

    result = corroboration.run_candidate_official_link_corroboration(tmp_path)

    assert result["run"]["official_site_link_candidate_count"] == 1
    after = storage.load_ledger(tmp_path)
    for field_name, before_value in protected_before.items():
        assert after[field_name] == before_value
    record = after["candidate_corroboration_attempts"][0]
    assert record["outcome"] == "official_site_link_candidate"
    assert record["source_url"] == "https://official.example.cn/"
    assert "official_website" in record["selector_reason"]
    snapshot_path = storage.candidate_corroboration_snapshot_path(
        tmp_path,
        record["corroboration_id"],
    )
    assert snapshot_path.is_file()
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    public_list = importlib.import_module(f"{PACKAGE_NAME}.candidate_review").list_discovery_candidates(
        tmp_path,
        limit=10,
    )
    assert public_list["items"][0]["latest_corroboration_outcome"] == "official_site_link_candidate"
    assert public_list["items"][0]["latest_corroboration_url"] == "https://official.example.cn/"

    # 详情读取会重新校验固定快照路径、审计字段、Base64、长度和哈希；返回仅保留人工
    # 可读的 URL、时间、哈希和有限正文，不能泄露原始 Base64 或服务器存储路径。
    ledger_before_detail_read = copy.deepcopy(after)
    detail = corroboration.get_candidate_official_link_corroboration(
        tmp_path,
        candidate_id=candidate_id,
    )
    assert detail["corroboration"]["candidate_id"] == candidate_id
    assert detail["snapshot"]["source_url"] == "https://official.example.cn/"
    assert detail["snapshot"]["raw_sha256"] == hashlib.sha256(second_raw).hexdigest()
    assert detail["snapshot"]["visible_text"] == "官网公开介绍：企业在中国开展研发。"
    assert "自动确认" in detail["caveat"]
    detail_as_json = json.dumps(detail, ensure_ascii=False)
    assert "raw_content_base64" not in detail_as_json
    assert "snapshot_relative_path" not in detail_as_json
    assert str(tmp_path) not in detail_as_json
    assert storage.load_ledger(tmp_path) == ledger_before_detail_read

    # 即使快照文件在服务器侧损坏，详情接口也只能安全失败，不能返回内部路径或静默使用
    # 未校验正文。文件损坏不应成为写入台账、覆盖候选或改变已存在正式证据的入口。
    corrupted_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    corrupted_snapshot["raw_content_base64"] = "not-valid-base64!"
    snapshot_path.write_text(
        json.dumps(corrupted_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="原始字节编码无效"):
        corroboration.get_candidate_official_link_corroboration(
            tmp_path,
            candidate_id=candidate_id,
        )
    assert storage.load_ledger(tmp_path) == ledger_before_detail_read


def test_candidate_reobservation_appends_changed_snapshot_and_skips_unchanged_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """候选页变更才追加快照和草稿，相同页面不覆盖历史也不重复落盘。

    测试输入是一条服务器发现的候选、两份不同的模拟公开页面字节，以及一个随后保持
    相同的页面响应。首次观察应创建 ``initial`` 快照；将服务器观察时间回拨到间隔之外
    后，内容哈希变化必须新增 ``content_changed`` 快照并指向旧快照。随后再次间隔外检查
    同一字节时，只能更新候选的可再生检查结果，不能新增观察或草稿；紧接着的间隔内调用
    更不得联网。全程比对正式证据、线索、评分、反馈及第一份快照，证明后台监控没有把
    自动页面变化写成招商事实，也没有覆盖任何原始证据链。
    """
    observation = _load_plugin_module("candidate_observation")
    evidence_draft = importlib.import_module(f"{PACKAGE_NAME}.evidence_draft")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": "discovery-candidate-reobservation-test",
            "run_id": "discovery-run-reobservation-test",
            "source_url": "https://reobserve.example.com/news/company",
            "source_title": "待持续观察的公开候选页面",
            "search_snippet": "搜索摘要仅是候选提示。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器生成的测试查询",
            "discovered_at": "2026-08-26T00:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    initial_raw = "徐州智行机器人科技有限公司发布产品介绍。".encode("utf-8")
    changed_raw = (
        "2026-08-27 徐州智行机器人科技有限公司宣布完成融资，并计划扩产和寻找合作伙伴。"
    ).encode("utf-8")
    page: dict[str, object] = {
        "raw_content": initial_raw,
        "source_title": "徐州智行机器人科技有限公司产品介绍",
        "visible_text": initial_raw.decode("utf-8"),
    }
    fetched_urls: list[str] = []

    monkeypatch.setattr(
        observation,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )

    def fake_fetch(source_url: str) -> dict[str, object]:
        """按当前测试页面返回服务器抓取结果，记录请求以验证间隔内不会联网。"""
        raw_content = page["raw_content"]
        assert isinstance(raw_content, bytes)
        source_title = page["source_title"]
        visible_text = page["visible_text"]
        assert isinstance(source_title, str)
        assert isinstance(visible_text, str)
        fetched_urls.append(source_url)
        return {
            "source_url": source_url,
            "source_title": source_title,
            "content_type": "text/html",
            "visible_text": visible_text,
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "byte_size": len(raw_content),
            "raw_content": raw_content,
        }

    monkeypatch.setattr(observation, "fetch_public_source_for_observation", fake_fetch)
    initial_result = observation.observe_discovery_candidates(tmp_path)
    initial_ledger = storage.load_ledger(tmp_path)
    initial_observation = copy.deepcopy(initial_ledger["candidate_observations"][0])
    evidence_before = copy.deepcopy(initial_ledger["evidence"])
    leads_before = copy.deepcopy(initial_ledger["leads"])
    scores_before = copy.deepcopy(initial_ledger["score_snapshots"])
    feedback_before = copy.deepcopy(initial_ledger["outcome_feedback"])

    assert initial_result["run"]["initial_observation_count"] == 1
    assert initial_observation["observation_kind"] == "initial"
    assert initial_observation["previous_observation_id"] is None
    assert initial_observation["previous_raw_sha256"] is None
    initial_detail_before = copy.deepcopy(initial_ledger)
    initial_detail = observation.get_candidate_observation(
        tmp_path,
        observation_id=initial_observation["observation_id"],
    )
    assert initial_detail["change_summary"] == {
        "available": False,
        "reason": "initial_or_legacy_observation_has_no_verified_predecessor",
        "comparison_method": "deterministic_visible_text_segment_diff_v1",
    }
    assert storage.load_ledger(tmp_path) == initial_detail_before

    # 测试显式提供过期的服务器时间，而不是让客户端决定调度频率；仅用于触发下一轮重访。
    initial_ledger["discovery_candidates"][0]["last_observed_at"] = "2020-01-01T00:00:00+00:00"
    storage.save_ledger(tmp_path, initial_ledger)
    page.update(
        {
            "raw_content": changed_raw,
            "source_title": "徐州智行机器人科技有限公司融资与扩产公告",
            "visible_text": changed_raw.decode("utf-8"),
        }
    )

    changed_result = observation.observe_discovery_candidates(tmp_path)
    changed_ledger = storage.load_ledger(tmp_path)
    changed_observation = changed_ledger["candidate_observations"][1]

    assert changed_result["run"]["observation_count"] == 1
    assert changed_result["run"]["initial_observation_count"] == 0
    assert changed_result["run"]["content_changed_observation_count"] == 1
    assert changed_ledger["candidate_observations"][0] == initial_observation
    assert changed_observation["observation_kind"] == "content_changed"
    assert changed_observation["previous_observation_id"] == initial_observation["observation_id"]
    assert changed_observation["previous_raw_sha256"] == initial_observation["raw_sha256"]
    assert changed_observation["raw_sha256"] == hashlib.sha256(changed_raw).hexdigest()
    assert changed_ledger["discovery_candidates"][0]["last_observation_id"] == changed_observation[
        "observation_id"
    ]
    assert changed_ledger["evidence"] == evidence_before == [created["evidence"]]
    assert changed_ledger["leads"] == leads_before
    assert changed_ledger["score_snapshots"] == scores_before
    assert changed_ledger["outcome_feedback"] == feedback_before

    # 详情差异摘要必须沿服务器保存的 ID 与哈希链回读前序快照，且只返回有限文本提示。
    changed_detail_before = copy.deepcopy(changed_ledger)
    changed_detail = observation.get_candidate_observation(
        tmp_path,
        observation_id=changed_observation["observation_id"],
    )
    change_summary = changed_detail["change_summary"]
    assert change_summary["available"] is True
    assert change_summary["previous_observation_id"] == initial_observation["observation_id"]
    assert change_summary["previous_raw_sha256"] == initial_observation["raw_sha256"]
    assert change_summary["current_raw_sha256"] == changed_observation["raw_sha256"]
    assert change_summary["added_segment_count"] == 1
    assert change_summary["removed_segment_count"] == 1
    assert "完成融资" in change_summary["added_segments"][0]
    assert "发布产品介绍" in change_summary["removed_segments"][0]
    assert change_summary["comparison_input_truncated"] is False
    assert change_summary["summary_truncated"] is False
    assert "不构成企业融资" in change_summary["caveat"]
    assert storage.load_ledger(tmp_path) == changed_detail_before

    # 队列仅重排只读元数据：变化快照优先，但不读取正文、不返回快照路径、更不写入台账。
    queue_before = copy.deepcopy(changed_ledger)
    queue = observation.list_candidate_observations(tmp_path, limit=10)
    assert queue["count"] == 2
    assert queue["content_changed_count"] == 1
    assert queue["content_changed_only"] is False
    assert queue["queue_order"] == "content_changed_then_collected_at_desc"
    assert [item["observation_id"] for item in queue["observations"]] == [
        changed_observation["observation_id"],
        initial_observation["observation_id"],
    ]
    assert "snapshot_relative_path" not in queue["observations"][0]
    assert storage.load_ledger(tmp_path) == queue_before

    # 筛选参数只接受实际布尔值；读取结果与非法参数拒绝均不能成为改写证据或快照的入口。
    changed_only_queue = observation.list_candidate_observations(
        tmp_path,
        content_changed_only=True,
        limit=10,
    )
    assert changed_only_queue["count"] == 1
    assert changed_only_queue["content_changed_count"] == 1
    assert changed_only_queue["content_changed_only"] is True
    assert [item["observation_id"] for item in changed_only_queue["observations"]] == [
        changed_observation["observation_id"]
    ]
    with pytest.raises(ValueError, match="content_changed_only必须是布尔值"):
        observation.list_candidate_observations(tmp_path, content_changed_only="true")  # type: ignore[arg-type]
    assert storage.load_ledger(tmp_path) == queue_before

    # 草稿生成器会对每个独立快照各生成一条待确认草稿，而非更新旧草稿或写入正式证据。
    draft_result = evidence_draft.generate_evidence_drafts_from_observations(tmp_path)
    ledger_with_drafts = storage.load_ledger(tmp_path)
    drafts_before_unchanged_check = copy.deepcopy(ledger_with_drafts["evidence_drafts"])
    assert draft_result["run"]["generated_draft_count"] == 2
    assert {
        draft["observation_id"] for draft in ledger_with_drafts["evidence_drafts"]
    } == {initial_observation["observation_id"], changed_observation["observation_id"]}

    # 再次让“服务器上次检查时间”过期，但页面字节保持与最新快照完全相同。
    ledger_with_drafts["discovery_candidates"][0]["last_observed_at"] = "2020-01-02T00:00:00+00:00"
    storage.save_ledger(tmp_path, ledger_with_drafts)
    unchanged_result = observation.observe_discovery_candidates(tmp_path)
    unchanged_ledger = storage.load_ledger(tmp_path)

    assert unchanged_result["run"]["observation_count"] == 0
    assert unchanged_result["run"]["unchanged_content_candidate_count"] == 1
    assert unchanged_result["run"]["unchanged_content_candidate_ids"] == [
        "discovery-candidate-reobservation-test"
    ]
    assert unchanged_ledger["candidate_observations"] == changed_ledger["candidate_observations"]
    assert unchanged_ledger["evidence_drafts"] == drafts_before_unchanged_check
    assert unchanged_ledger["evidence"] == evidence_before
    assert unchanged_ledger["leads"] == leads_before
    assert unchanged_ledger["score_snapshots"] == scores_before
    assert unchanged_ledger["outcome_feedback"] == feedback_before

    before_interval_result_ledger = copy.deepcopy(unchanged_ledger)
    interval_result = observation.observe_discovery_candidates(tmp_path)
    assert interval_result["run"]["selected_candidate_count"] == 0
    assert len(fetched_urls) == 3
    assert storage.load_ledger(tmp_path) == before_interval_result_ledger

    # 即便攻击者能够构造一份重新签名的异常台账，详情也不能相信变化记录指向的前序哈希。
    # 读取必须拒绝该链路，且不得借“展示摘要”修改任何候选、快照、证据或人工业务记录。
    tampered_ledger = storage.load_ledger(tmp_path)
    tampered_ledger["candidate_observations"][1]["previous_raw_sha256"] = "0" * 64
    storage.save_ledger(tmp_path, tampered_ledger)
    tampered_before_read = copy.deepcopy(storage.load_ledger(tmp_path))
    with pytest.raises(ValueError, match="前序快照哈希不一致"):
        observation.get_candidate_observation(
            tmp_path,
            observation_id=changed_observation["observation_id"],
        )
    assert storage.load_ledger(tmp_path) == tampered_before_read


def test_candidate_observation_stops_before_network_when_snapshot_quotas_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """快照数量或容量配额耗尽时，后台观察不得下载页面或改变候选业务状态。"""
    observation = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": "discovery-candidate-quota-test",
            "run_id": "discovery-run-quota-test",
            "source_url": "https://quota.example.com/news/fundraising",
            "source_title": "容量配额测试候选",
            "search_snippet": "搜索摘要不是事实。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器生成的测试查询",
            "discovered_at": "2026-08-26T00:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    evidence_before = copy.deepcopy(storage.load_ledger(tmp_path)["evidence"])

    def fail_if_fetched(_source_url: str) -> dict[str, object]:
        """容量门槛在网络请求之前生效，调用此替身即说明实现退化。"""
        raise AssertionError("快照配额已耗尽时不应下载页面")

    monkeypatch.setattr(observation, "fetch_public_source_for_observation", fail_if_fetched)
    for record_quota, storage_quota in ((1_000, 1), (0, 1_000_000_000)):
        monkeypatch.setattr(
            observation,
            "MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RECORDS",
            record_quota,
        )
        monkeypatch.setattr(
            observation,
            "MAX_CANDIDATE_OBSERVATION_SNAPSHOT_STORAGE_BYTES",
            storage_quota,
        )

        result = observation.observe_discovery_candidates(tmp_path)

        assert result["run"]["selected_candidate_count"] == 0
        assert result["run"]["observation_count"] == 0
        assert result["run"]["skipped_candidates"] == [
            {
                "candidate_id": "discovery-candidate-quota-test",
                "reason": "observation_snapshot_storage_quota_reached",
            }
        ]
        assert result["run"]["snapshot_storage_bytes_before"] == 0
        assert result["run"]["snapshot_storage_bytes_after"] == 0
        ledger_after = storage.load_ledger(tmp_path)
        assert ledger_after["candidate_observations"] == []
        assert "observation_attempts" not in ledger_after["discovery_candidates"][0]
        assert ledger_after["evidence"] == evidence_before == [created["evidence"]]


def test_candidate_observation_removes_new_snapshot_when_ledger_save_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """快照创建后若台账版本冲突，本轮新文件必须清理且不触碰任何历史记录。"""
    observation = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": "discovery-candidate-save-conflict",
            "run_id": "discovery-run-save-conflict",
            "source_url": "https://conflict.example.com/news/fundraising",
            "source_title": "保存冲突测试候选",
            "search_snippet": "搜索摘要不是事实。",
            "sector": "embodied_intelligence",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器生成的测试查询",
            "discovered_at": "2026-08-26T00:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    raw_content = b"public candidate body"
    monkeypatch.setattr(
        observation,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )
    monkeypatch.setattr(
        observation,
        "fetch_public_source_for_observation",
        lambda _source_url: {
            "source_url": "https://conflict.example.com/news/fundraising",
            "source_title": "保存冲突测试页面",
            "content_type": "text/plain",
            "visible_text": "public candidate body",
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "byte_size": len(raw_content),
            "raw_content": raw_content,
        },
    )

    def raise_save_conflict(_workspace_dir: Path, _ledger: dict[str, object]) -> Path:
        """模拟另一请求先保存后触发的版本冲突，不实际写入本轮观察结果。"""
        raise RuntimeError("招商线索台账已被其他操作更新，请重新读取后再提交")

    monkeypatch.setattr(observation, "save_ledger", raise_save_conflict)
    with pytest.raises(RuntimeError, match="已被其他操作更新"):
        observation.observe_discovery_candidates(tmp_path)

    snapshot_directory = (
        tmp_path / "investment_attraction" / "candidate_observation_snapshots"
    )
    assert not snapshot_directory.exists() or list(snapshot_directory.iterdir()) == []
    assert storage.load_ledger(tmp_path)["candidate_observations"] == []


def test_candidate_observation_respects_robots_without_fetching_page_or_changing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """robots 拒绝的候选只留访问治理记录，绝不下载原文、保存快照或改写业务数据。

    输入是已发现候选和模拟的 robots 禁止结果；页面抓取替身若被调用会立即使测试失败。
    输出应保留原始证据和线索不变，仅在候选上追加一次失败原因，并将对应域名健康度标为
    ``policy_blocked``，使后续定时任务不再以伪装或高频重试的方式访问该网站。
    """
    observation = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(original["evidence"])
    leads_before = copy.deepcopy(storage.load_ledger(tmp_path)["leads"])
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": "discovery-candidate-robots-denied",
            "run_id": "discovery-run-robots-denied",
            "source_url": "https://blocked.example.com/private/announcement",
            "source_title": "禁止自动读取的页面",
            "search_snippet": "候选摘要。",
            "sector": "semiconductor",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器生成的测试查询",
            "discovered_at": "2026-08-26T00:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    monkeypatch.setattr(
        observation,
        "check_robots_access",
        lambda _source_url: {"allowed": False, "reason": "robots_disallow"},
    )

    def fail_if_fetched(_source_url: str) -> dict[str, object]:
        """确认 robots 禁止路径不会进入任何页面下载逻辑。"""
        raise AssertionError("robots 禁止的候选不应抓取页面")

    monkeypatch.setattr(observation, "fetch_public_source_for_observation", fail_if_fetched)
    result = observation.observe_discovery_candidates(tmp_path)

    assert result["run"]["status"] == "completed"
    assert result["run"]["observation_count"] == 0
    assert result["run"]["skipped_candidates"] == [
        {
            "candidate_id": "discovery-candidate-robots-denied",
            "reason": "robots_disallow",
        }
    ]
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == [evidence_before]
    assert ledger_after["leads"] == leads_before
    assert ledger_after["candidate_observations"] == []
    denied_candidate = ledger_after["discovery_candidates"][0]
    assert denied_candidate["state"] == "discovered"
    assert denied_candidate["observation_attempts"] == 1
    assert denied_candidate["last_observation_error"].startswith("robots policy:")
    assert ledger_after["source_health"][-1]["source_key"] == "public_page_domain:blocked.example.com"
    assert ledger_after["source_health"][-1]["status"] == "policy_blocked"


def test_candidate_observation_read_services_verify_snapshot_and_do_not_mutate_ledger(
    tmp_path: Path,
) -> None:
    """只读列表和详情服务只返回受限快照视图，并验证原始字节后保持台账不变。

    测试构造一条服务器观察记录及其权限受限快照，不访问任何网络。列表不得泄露本地
    路径或 Base64 原始字节；详情必须按候选或观察 ID 读取固定位置、验证 SHA-256 和字节
    数，并将很长正文截断至工具输出上限。调用前后线索、证据、候选和观察记录必须完全
    一致，证明人工查看不会变成抓取、写证据或业务状态修改操作。
    """
    observation = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(tmp_path, **_evidence_arguments())
    observation_id = "candidate-observation-read-test"
    candidate_id = "discovery-candidate-read-test"
    visible_text = "公开页面可见正文。" + ("证" * 12_100)
    raw_content = visible_text.encode("utf-8")
    raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    collected_at = "2026-08-26T01:02:03+00:00"
    snapshot_path = storage.save_candidate_observation_snapshot(
        tmp_path,
        observation_id=observation_id,
        payload={
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://read.example.com/news/announcement",
            "source_title": "待补证页面",
            "content_type": "text/html",
            "collected_at": collected_at,
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "visible_text": visible_text,
            "raw_content_base64": base64.b64encode(raw_content).decode("ascii"),
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        },
    )
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-read-test",
            "source_url": "https://read.example.com/news/announcement",
            "source_title": "待补证页面",
            "search_snippet": "待核验搜索摘要。",
            "sector": "biomedicine",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器测试查询",
            "discovered_at": "2026-08-26T01:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "observation_attempts": 1,
            "last_observation_id": observation_id,
            "last_observation_error": None,
            "last_observed_at": collected_at,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    ledger["candidate_observations"] = [
        {
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://read.example.com/news/announcement",
            "source_title": "待补证页面",
            "content_type": "text/html",
            "collected_at": collected_at,
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "snapshot_relative_path": str(snapshot_path.relative_to(tmp_path)),
            "signals": [
                {
                    "signal_key": "fundraising_need_terms",
                    "matched_terms": ["正在融资"],
                    "state": "pending_evidence_verification",
                    "review_priority": "red_review",
                }
            ],
            "review_priority": "red_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    ledger_before = copy.deepcopy(storage.load_ledger(tmp_path))

    queue = observation.list_candidate_observations(
        tmp_path,
        review_priority="red_review",
        limit=10,
    )
    by_candidate = observation.get_candidate_observation(tmp_path, candidate_id=candidate_id)
    by_observation = observation.get_candidate_observation(tmp_path, observation_id=observation_id)

    assert queue["count"] == 1
    assert queue["observations"][0]["observation_id"] == observation_id
    assert "snapshot_relative_path" not in queue["observations"][0]
    assert "raw_content_base64" not in queue["observations"][0]
    assert by_candidate["matched_observation_count"] == 1
    assert by_candidate["candidate"]["candidate_id"] == candidate_id
    assert by_candidate["snapshot"]["raw_sha256"] == raw_sha256
    assert by_candidate["snapshot"]["visible_text"] == visible_text[:12_000]
    assert by_candidate["snapshot"]["visible_text_truncated"] is True
    assert "raw_content_base64" not in by_candidate["snapshot"]
    assert by_observation["observation"]["observation_id"] == observation_id
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after == ledger_before
    assert ledger_after["evidence"] == [created["evidence"]]


def test_candidate_observation_reader_rejects_tampered_snapshot_path_without_mutation(
    tmp_path: Path,
) -> None:
    """详情读取不能相信台账中的快照路径，篡改后必须拒绝且不改写任何记录。

    测试保存一份真实路径快照后故意把观察索引改为其他相对文件名。读取服务只能从观察
    ID 推导服务器固定路径，因此必须在读取文件前报错；台账、证据和历史快照均保持原样，
    证明只读接口不能被用来访问工作区中任意文件或修复、覆盖证据链。
    """
    observation = _load_plugin_module("candidate_observation")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    observation_id = "candidate-observation-path-test"
    candidate_id = "discovery-candidate-path-test"
    raw_content = b"public source body"
    raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    storage.save_candidate_observation_snapshot(
        tmp_path,
        observation_id=observation_id,
        payload={
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://path.example.com/news/announcement",
            "source_title": "路径校验页面",
            "content_type": "text/plain",
            "collected_at": "2026-08-26T02:00:00+00:00",
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "visible_text": "public source body",
            "raw_content_base64": base64.b64encode(raw_content).decode("ascii"),
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        },
    )
    ledger = storage.load_ledger(tmp_path)
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-path-test",
            "source_url": "https://path.example.com/news/announcement",
            "source_title": "路径校验页面",
            "search_snippet": "候选摘要。",
            "sector": "semiconductor",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器测试查询",
            "discovered_at": "2026-08-26T02:00:00+00:00",
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }
    ]
    ledger["candidate_observations"] = [
        {
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://path.example.com/news/announcement",
            "source_title": "路径校验页面",
            "content_type": "text/plain",
            "collected_at": "2026-08-26T02:00:00+00:00",
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "snapshot_relative_path": "investment_attraction/not-the-observation-file.json",
            "signals": [],
            "review_priority": "normal_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    ledger_before = copy.deepcopy(storage.load_ledger(tmp_path))

    with pytest.raises(ValueError, match="快照路径与服务器规则不一致"):
        observation.get_candidate_observation(tmp_path, observation_id=observation_id)

    assert storage.load_ledger(tmp_path) == ledger_before


def test_readonly_assessment_report_and_work_summary_preserve_evidence_and_observations(
    tmp_path: Path,
) -> None:
    """标准研判与工作统计只汇总真实台账，不将覆盖度或待补证信号写成招商结论。

    测试为同一企业登记两条不同域名的融资证据、一条团队徐州关联证据和一条自动抓取
    证据，并关联待补证候选观察。报告应保留完整证据链，融资只得到“多域名覆盖提示”，
    技术与风险章节明确待补证，候选观察仍保持未核验状态；统计应反映工作量而非企业
    意愿。两项只读调用前后台账必须逐字一致。
    """
    reports = _load_plugin_module("reports")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    first = core.record_public_evidence(tmp_path, **_evidence_arguments())
    second = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://media.example.net/report/financing",
            source_title="媒体对企业融资的可追溯报道",
            source_level="二手",
            evidence_quality=3,
            excerpt="媒体报道示例具身智能企业完成新一轮融资，并计划拓展具身智能产品研发与商业化。",
            xuzhou_association_tags=[],
        ),
    )
    team_connection = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://alumni.example.org/founder/xuzhou",
            source_title="创始人团队与徐州关联公开记录",
            source_level="一手",
            claim_type="徐州关联",
            evidence_quality=4,
            excerpt="示例具身智能企业创始团队公开介绍了其在徐州的成长经历与产业合作关注。",
            xuzhou_association_tags=["创始人或团队", "徐州祖籍"],
        ),
    )
    candidate_id = "discovery-candidate-report-link"
    captured = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://candidate.example.com/news/cooperation",
            source_title="自动发现的合作候选页面",
            source_level="线索",
            claim_type="合作",
            evidence_quality=1,
            excerpt="示例具身智能企业披露正在寻找产业合作伙伴，相关细节仍待人工核验。",
            xuzhou_association_tags=[],
            capture_metadata={
                "source_type": "automated_public_search_capture",
                "collection_method": "public_search_then_snapshot",
                "raw_sha256": hashlib.sha256(b"test public evidence raw response").hexdigest(),
                "discovery_run_id": "discovery-run-report-link",
                "discovery_candidate_id": candidate_id,
            },
        ),
    )
    core.review_investment_lead(
        tmp_path,
        lead_id=first["lead"]["lead_id"],
        decision="观察",
        review_note="报告测试：人工暂时观察，不能由报告自动推进。",
        reviewer_user_id="ordinary-report-reviewer",
    )
    ledger = storage.load_ledger(tmp_path)
    ledger["candidate_observations"] = [
        {
            "observation_id": "candidate-observation-report-link",
            "candidate_id": candidate_id,
            "source_url": "https://candidate.example.com/news/cooperation",
            "source_title": "候选页面原始快照",
            "content_type": "text/html",
            "collected_at": "2026-08-27T01:00:00+00:00",
            "raw_sha256": "b" * 64,
            "byte_size": 200,
            "snapshot_relative_path": "investment_attraction/candidate_observation_snapshots/candidate-observation-report-link.json",
            "signals": [
                {
                    "signal_key": "landing_window_terms",
                    "matched_terms": ["寻找合作伙伴"],
                    "state": "pending_evidence_verification",
                    "review_priority": "red_review",
                }
            ],
            "review_priority": "red_review",
            "state": "pending_evidence_verification",
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    ledger_before = copy.deepcopy(storage.load_ledger(tmp_path))

    assessment = reports.get_investment_assessment_report(
        tmp_path,
        lead_id=first["lead"]["lead_id"],
    )
    summary = reports.get_investment_work_summary(tmp_path, window_days=30)

    assert assessment["report_type"] == "xuzhou_investment_assessment_readonly_v6"
    assert assessment["lead"]["lead_id"] == first["lead"]["lead_id"]
    assert assessment["evidence_chain"] == [
        first["evidence"],
        second["evidence"],
        team_connection["evidence"],
        captured["evidence"],
    ]
    sections = {
        section["section_key"]: section for section in assessment["standardized_sections"]
    }
    assert sections["financing_history"]["status"] == "evidence_available"
    assert sections["financing_history"]["recorded_evidence_count"] == 2
    assert sections["core_team"]["status"] == "evidence_available"
    assert sections["xuzhou_association"]["status"] == "evidence_available"
    assert sections["technology_and_industry"]["status"] == "needs_evidence"
    assert sections["risk_and_counter_signals"]["status"] == "needs_evidence"
    financing_cards = sections["financing_history"]["evidence_cards"]
    assert {card["evidence_id"] for card in financing_cards} == {
        first["evidence"]["evidence_id"],
        second["evidence"]["evidence_id"],
    }
    assert all("raw_snapshot_relative_path" not in card for card in financing_cards)
    assert sections["financing_history"]["cross_source_validation"] == {
        "status": "multiple_source_domains_pending_event_alignment",
        "unique_source_domains": ["example.com", "media.example.net"],
        "unique_source_domain_count": 2,
        "source_levels": ["primary", "secondary"],
        "evidence_quality_values": [3, 5],
        "requires_manual_event_alignment": True,
        "manual_event_alignment_fields": [
            "source_published_at",
            "source_title",
            "excerpt",
            "source_url",
        ],
        "caveat": "当前章节已有多个不同域名记录；请人工核对日期、标题、原始摘录和链接是否指向同一具体事件。",
    }
    assert sections["core_team"]["evidence_cards"][0]["evidence_id"] == team_connection[
        "evidence"
    ]["evidence_id"]
    assert sections["technology_and_industry"]["evidence_cards"] == []
    assert sections["technology_and_industry"]["cross_source_validation"]["status"] == (
        "no_traceable_evidence"
    )
    timeline = assessment["evidence_timeline"]
    assert timeline["timeline_order"] == "source_published_at_then_collected_at_desc"
    assert timeline["recorded_evidence_count"] == 4
    assert timeline["truncated"] is False
    assert {card["evidence_id"] for card in timeline["records"]} == {
        first["evidence"]["evidence_id"],
        second["evidence"]["evidence_id"],
        team_connection["evidence"]["evidence_id"],
        captured["evidence"]["evidence_id"],
    }
    financing_check = next(
        check
        for check in assessment["cross_source_coverage_checks"]
        if check["claim_type"] == "financing"
    )
    assert financing_check["unique_source_domain_count"] == 2
    assert financing_check["coverage_status"] == "multiple_independent_source_domains"
    assert financing_check["requires_manual_event_alignment"] is True
    assert {card["evidence_id"] for card in financing_check["evidence_alignment_records"]} == {
        first["evidence"]["evidence_id"],
        second["evidence"]["evidence_id"],
    }
    assert "人工核验" in financing_check["caveat"]
    financing_consistency_check = next(
        check
        for check in assessment["cross_source_consistency_review"]["claim_type_checks"]
        if check["claim_type"] == "financing"
    )
    # 两条原有摘录没有明确人民币金额；报告只能提示多来源事件对齐，不能凭空补出金额。
    assert financing_consistency_check["status"] == "multiple_domains_pending_event_alignment"
    assert financing_consistency_check["observed_cny_amount_tokens"] == []
    assert financing_consistency_check["requires_manual_event_alignment"] is True
    assert assessment["cross_source_consistency_review"]["manual_review_claim_types"] == [
        "financing"
    ]
    completeness = assessment["report_completeness"]
    assert completeness["section_count"] == 6
    assert completeness["evidence_available_section_count"] == 4
    assert completeness["needs_evidence_sections"] == [
        "technology_and_industry",
        "risk_and_counter_signals",
    ]
    assert completeness["manual_event_alignment_sections"] == [
        "financing_history",
    ]
    assert completeness["recommended_human_followups"] == [
        {
            "section_key": "financing_history",
            "action": "align_existing_source_records",
            "instruction": "人工核对多个来源是否指向同一具体事件，不得因来源数量自动确认事实。",
        },
        {
            "section_key": "technology_and_industry",
            "action": "collect_traceable_evidence",
            "instruction": "核验具体技术方向、产品或基金行业研究方向与目标产业的匹配。",
        },
        {
            "section_key": "risk_and_counter_signals",
            "action": "collect_traceable_evidence",
            "instruction": "核验直接竞品、阶段不匹配、来源矛盾、过期或其他风险信息。",
        },
    ]
    assert "不生成企业研判结论" in completeness["caveat"]
    assert assessment["industry_chain_relationship_assessment"] == {
        "section_key": "industry_chain_relationship_graph",
        "title": "产业链与本地关系图谱",
        "status": "needs_evidence",
        "instruction": "逐条核验关系边引用的原始来源、上下游登记方向、徐州本地属性和人工复核状态。",
        "relationship_graph_scope": "evidence_backed_relationship_graph_read_only",
        "relationship_graph_summary": {
            "edge_count": 0,
            "evidence_backed_industry_chain_edge_count": 0,
            "xuzhou_external_industry_chain_edge_count": 0,
            "pending_evidence_edge_count": 0,
            "relationship_type_counts": {},
        },
        "relationship_review_state_summary": {
            "invalid": 0,
            "need_evidence": 0,
            "pending_user_review": 0,
            "valid": 0,
        },
        "recorded_edge_count": 0,
        "edge_cards": [],
        "edge_cards_truncated": False,
        "pending_evidence_edge_count": 0,
        "pending_evidence_edges": [],
        "pending_evidence_edges_truncated": False,
        "edge_evidence_reference_ids": [],
        "not_in_current_lead_evidence_chain_ids": [],
        "manual_review_checklist": [
            "当前台账尚未登记该线索的一跳证据化关系；应先基于可追溯原始来源人工登记，不得由报告推断。"
        ],
        "caveat": "当前未登记关系不代表企业与徐州不存在供应链、合作或其他关联，只表示台账尚无可追溯图谱记录。",
    }
    assert assessment["unverified_candidate_observations"] == [
        {
            "observation_id": "candidate-observation-report-link",
            "candidate_id": candidate_id,
            "source_url": "https://candidate.example.com/news/cooperation",
            "source_title": "候选页面原始快照",
            "collected_at": "2026-08-27T01:00:00+00:00",
            "raw_sha256": "b" * 64,
            "byte_size": 200,
            "signals": [
                {
                    "signal_key": "landing_window_terms",
                    "matched_terms": ["寻找合作伙伴"],
                    "state": "pending_evidence_verification",
                    "review_priority": "red_review",
                }
            ],
            "review_priority": "red_review",
            "state": "pending_evidence_verification",
        }
    ]
    assert assessment["automated_business_conclusion"]["status"] == "not_generated"
    assert summary["current_totals"] == {
        "lead_count": 1,
        "evidence_count": 4,
        "red_alert_lead_count": 0,
        "candidate_observation_count": 1,
        "pending_evidence_observation_count": 1,
        "relationship_review_queue_item_count": 0,
        "relationship_need_evidence_edge_count": 0,
        "industry_metric_draft_count": 0,
        "pending_user_review_industry_metric_draft_count": 0,
        "confirmed_industry_business_metric_count": 0,
        "key_person_relationship_draft_count": 0,
        "pending_key_person_relationship_draft_action_count": 0,
        "registered_key_person_relationship_draft_count": 0,
        "co_investment_signal_draft_count": 0,
        "pending_co_investment_signal_draft_action_count": 0,
    }
    assert summary["sector_summary"]["embodied_intelligence"]["lead_count"] == 1
    assert summary["review_state_counts"]["observe"]["count"] == 1
    assert summary["source_level_counts"] == {"lead_only": 1, "primary": 2, "secondary": 1}
    assert summary["interpretation_caveat"].startswith("统计仅反映")
    assert storage.load_ledger(tmp_path) == ledger_before


def test_assessment_cross_source_amounts_only_request_manual_alignment_without_writing(
    tmp_path: Path,
) -> None:
    """不同来源金额文本只能形成只读人工核对项，不得被报告自动裁决或改写。

    输入是同一线索、不同来源且金额文本不同的两条正式融资证据；输出的报告必须保留原始
    证据 ID 与可回看的标准化金额文本，并明确要求人工核对轮次、项目和统计口径。该函数
    仅验证读取报告的结果：不接受调用方的事实结论，不联网，不写入证据、线索、评分、状态
    或审计记录，从而保证金额差异不会越权变成“来源矛盾”或招商业务结论。
    """
    reports = _load_plugin_module("reports")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    first = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://official.example.com/financing/round",
            source_title="企业官方融资公告",
                excerpt="示例具身智能企业公告披露完成本轮融资1亿元，用于具身智能产品研发。",
        ),
    )
    second = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://media.example.net/financing/report",
            source_title="媒体融资报道",
            source_level="二手",
            evidence_quality=3,
                excerpt="媒体报道示例具身智能企业获得5000万元融资，具体轮次和统计口径待核验。",
            raw_content=b"independent public media financing report",
        ),
    )
    ledger_before_report = copy.deepcopy(storage.load_ledger(tmp_path))

    assessment = reports.get_investment_assessment_report(
        tmp_path,
        lead_id=first["lead"]["lead_id"],
    )

    financing_check = next(
        check
        for check in assessment["cross_source_consistency_review"]["claim_type_checks"]
        if check["claim_type"] == "financing"
    )
    assert financing_check["evidence_ids"] == [
        first["evidence"]["evidence_id"],
        second["evidence"]["evidence_id"],
    ]
    assert financing_check["status"] == (
        "multiple_domains_with_distinct_numeric_tokens_needs_manual_alignment"
    )
    assert financing_check["observed_cny_amount_tokens"] == [
        {
            "normalized_cny_yuan": "50000000",
            "observed_texts": ["5000万元"],
            "evidence_ids": [second["evidence"]["evidence_id"]],
        },
        {
            "normalized_cny_yuan": "100000000",
            "observed_texts": ["1亿元"],
            "evidence_ids": [first["evidence"]["evidence_id"]],
        },
    ]
    assert "不能自动视为矛盾" in financing_check["review_reason"]
    assert storage.load_ledger(tmp_path) == ledger_before_report


def test_assessment_report_includes_evidence_backed_relationship_graph_without_writing(
    tmp_path: Path,
) -> None:
    """标准研判报告应只读呈现已登记关系、待补证状态和原始证据引用。

    输入是一条同时出现徐州园区与目标企业的上游供应原始证据，以及普通用户人工标为
    待补证的关系边；输出报告必须保留登记方向、边证据 ID 和人工待办，而不能把边自动
    变为有效关系或生成招商结论。读取报告前后台账逐字相等，证明报告既不改证据链，也
    不修改图谱关系、复核历史、评分或线索状态。
    """
    reports = _load_plugin_module("reports")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt=(
                "徐州报告图谱园区为示例具身智能企业提供具身智能上游供应产业链配套，"
                "相关合作细节仍待持续核验。"
            ),
        ),
    )
    evidence_id = created["evidence"]["evidence_id"]
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州报告图谱园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[evidence_id],
        registration_reason="为标准研判报告测试登记徐州本地产业载体。",
        authenticated_user_id="ordinary-report-graph-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[evidence_id],
        registration_reason="为标准研判报告测试登记目标企业节点。",
        authenticated_user_id="ordinary-report-graph-user",
    )["node"]
    edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="上游供应",
        evidence_ids=[evidence_id],
        registration_reason="为标准研判报告测试登记一条待核验的上游供应关系。",
        authenticated_user_id="ordinary-report-graph-user",
    )["edge"]
    relationship_graph.review_relationship_edge(
        tmp_path,
        edge_id=edge["edge_id"],
        decision="待补证",
        review_note="关系方向已有原始摘录，但仍需补充可核验的合作范围和持续性来源。",
        reviewer_user_id="ordinary-report-reviewer",
    )
    ledger_before_report = copy.deepcopy(storage.load_ledger(tmp_path))

    assessment = reports.get_investment_assessment_report(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
    )

    relationship_section = assessment["industry_chain_relationship_assessment"]
    assert relationship_section["status"] == "evidence_available"
    assert relationship_section["relationship_graph_summary"] == {
        "edge_count": 1,
        "evidence_backed_industry_chain_edge_count": 1,
        "xuzhou_external_industry_chain_edge_count": 1,
        "pending_evidence_edge_count": 1,
        "relationship_type_counts": {"upstream_supplier": 1},
    }
    assert relationship_section["relationship_review_state_summary"] == {
        "invalid": 0,
        "need_evidence": 1,
        "pending_user_review": 0,
        "valid": 0,
    }
    assert relationship_section["edge_evidence_reference_ids"] == [evidence_id]
    assert relationship_section["not_in_current_lead_evidence_chain_ids"] == []
    assert relationship_section["pending_evidence_edge_count"] == 1
    assert relationship_section["pending_evidence_edges"] == [
        {
            "edge_id": edge["edge_id"],
            "relationship_type": "upstream_supplier",
            "review_state": "need_evidence",
            "reason": "该关系已被人工标记为待补证，应先核验新增原始来源再作业务判断。",
        }
    ]
    edge_card = relationship_section["edge_cards"][0]
    assert edge_card["edge_id"] == edge["edge_id"]
    assert edge_card["business_flow_direction"] == "from_node_to_to_node"
    assert edge_card["from_node"]["locality"] == "xuzhou_local"
    assert edge_card["to_node"]["locality"] == "target_or_external"
    assert edge_card["review_state"] == "need_evidence"
    assert edge_card["evidence_support"]["original_edge_evidence_ids"] == [evidence_id]
    assert edge_card["evidence_support"]["supplement_evidence_ids"] == []
    assert edge_card["evidence_support"]["business_relationship_strength_inferred"] is False
    assert assessment["automated_business_conclusion"]["status"] == "not_generated"
    assert storage.load_ledger(tmp_path) == ledger_before_report


def test_industry_subdirection_coverage_uses_fixed_text_signals_and_preserves_ledger(
    tmp_path: Path,
) -> None:
    """细分产业报告必须展示全部固定细分项，并把词匹配和覆盖缺口保守地区分开。

    输入为具身智能的“灵巧手”、半导体的“先进封装”及一条未命中任何具身细分词的
    正式证据；输出应只将字面出现的固定词标为有可追溯文本信号，同时保留其他细分项
    的待补证状态和宽查询来源目录缺口。报告不应把未命中写成市场不存在、企业不属于
    该产业或招商不可推进；测试在读取前后逐字对比台账，确认不改证据、来源、评分、
    图谱或人工复核，且来源卡片不泄露本地原始快照路径。
    """
    reports = _load_plugin_module("reports")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    dexterous_hand = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="灵巧手招商企业",
            source_url="https://embodied.example.com/news/dexterous-hand",
            source_title="灵巧手产品研发公开公告",
            excerpt="灵巧手招商企业披露正在推进灵巧手产品研发与商业化合作。",
            claim_type="合作",
            evidence_quality=3,
        ),
    )
    advanced_packaging = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="先进封装招商企业",
            entity_kind="企业",
            sector="半导体",
            source_url="https://semiconductor.example.com/news/advanced-packaging",
            source_title="先进封装技术合作公开公告",
            source_level="二手",
            claim_type="合作",
            evidence_quality=3,
            excerpt="先进封装招商企业公开介绍先进封装技术合作与产线扩展计划。",
            xuzhou_association_tags=[],
        ),
    )
    unclassified_embodied = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="具身宽词招商企业",
            source_url="https://embodied.example.com/news/general-embodied",
            source_title="具身智能企业公开动态",
            claim_type="扩张",
            evidence_quality=3,
            excerpt="具身宽词招商企业披露计划扩大研发团队，具体技术方向尚待原始来源补证。",
        ),
    )
    ledger_before_report = copy.deepcopy(storage.load_ledger(tmp_path))

    report = reports.get_industry_subdirection_coverage_report(tmp_path, window_days=30)

    assert report["report_type"] == "xuzhou_industry_subdirection_coverage_readonly_v1"
    assert report["recent_evidence_count"] == 3
    assert report["excluded_outside_or_invalid_window_evidence_count"] == 0
    embodied = report["sectors"]["embodied_intelligence"]
    semiconductor = report["sectors"]["semiconductor"]
    biomedicine = report["sectors"]["biomedicine"]
    assert embodied["configured_subdirection_count"] == 9
    assert semiconductor["configured_subdirection_count"] == 10
    assert biomedicine["configured_subdirection_count"] == 8
    assert embodied["subdirection_count_with_traceable_text_match"] == 1
    assert embodied["unclassified_recent_evidence_count"] == 1
    dexterous_entry = next(
        entry for entry in embodied["subdirection_coverage"] if entry["subdirection"] == "灵巧手"
    )
    assert dexterous_entry["coverage_status"] == "traceable_text_match_available"
    assert dexterous_entry["evidence_ids"] == [dexterous_hand["evidence"]["evidence_id"]]
    assert dexterous_entry["matched_leads"] == [
        {
            "lead_id": dexterous_hand["lead"]["lead_id"],
            "entity_name": "灵巧手招商企业",
            "review_state": "pending_user_review",
            "red_alert": False,
        }
    ]
    assert all(
        "raw_snapshot_relative_path" not in card for card in dexterous_entry["evidence_cards"]
    )
    advanced_packaging_entry = next(
        entry
        for entry in semiconductor["subdirection_coverage"]
        if entry["subdirection"] == "先进封装"
    )
    assert advanced_packaging_entry["recorded_evidence_count"] == 1
    assert advanced_packaging_entry["evidence_ids"] == [
        advanced_packaging["evidence"]["evidence_id"]
    ]
    assert any(
        item == {"sector": "biomedicine", "subdirection": "创新药"}
        for item in report["needs_evidence_subdirections"]
    )
    assert embodied["broad_discovery_coverage"]["current_window_gap_types"] == [
        "no_discovery_candidates"
    ]
    assert embodied["broad_discovery_coverage"]["fixed_recipe_coverage"][
        "public_early_signal"
    ] == {
        "discovered_candidates": 0,
        "captured_candidates": 0,
        "capture_failed_candidates": 0,
    }
    assert report["automated_industry_conclusion"]["status"] == "not_generated"
    assert storage.load_ledger(tmp_path) == ledger_before_report
    with pytest.raises(ValueError, match="window_days必须是1到90之间的整数"):
        reports.get_industry_subdirection_coverage_report(tmp_path, window_days=91)
    assert unclassified_embodied["evidence"]["evidence_id"] not in dexterous_entry["evidence_ids"]


def test_robots_policy_uses_specific_allow_and_disallow_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """robots 规则按最长路径优先，通配抓取器不能访问被禁止页但可访问更具体 Allow 页。

    测试替换底层公开响应，不连接真实网站。断言工具读取声明的 Sitemap 但只将其作为
    后续候选目录；robots 检查本身不创建线索、证据、评分或人工状态。
    """
    source_feed = _load_plugin_module("source_feed_discovery")

    def fake_fetch_bytes(source_url: str, *, allowed_content_types: set[str]) -> dict[str, object]:
        """返回固定 robots 文本，并确认策略文件只能按纯文本方式读取。"""
        assert source_url == "https://official.example.com/robots.txt"
        assert allowed_content_types == {"text/plain"}
        return {
            "source_url": source_url,
            "content_type": "text/plain",
            "charset": "utf-8",
            "raw_content": (
                b"User-agent: *\nDisallow: /private\nAllow: /private/approved\n"
                b"Sitemap: https://official.example.com/sitemap.xml\n"
            ),
            "byte_size": 120,
        }

    monkeypatch.setattr(source_feed, "_fetch_public_bytes", fake_fetch_bytes)
    blocked = source_feed.check_robots_access("https://official.example.com/private/report")
    allowed = source_feed.check_robots_access("https://official.example.com/private/approved/report")

    assert blocked["allowed"] is False
    assert blocked["reason"] == "robots_disallow"
    assert allowed["allowed"] is True
    assert allowed["sitemap_urls"] == ["https://official.example.com/sitemap.xml"]


def test_trusted_directory_rss_and_sitemap_create_candidates_without_creating_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """可信目录可自动从 robots、RSS 与 Sitemap 发现产业候选，但不把摘要升级为证据。

    测试模拟单个企业官网：robots 声明 Sitemap，首页声明 RSS，二者分别给出具身智能
    和半导体链接。断言只追加 ``lead_only`` 候选与来源发现审计；调用前的真实证据逐字
    保持一致，避免自动目录扩展污染不可变证据链。
    """
    source_feed = _load_plugin_module("source_feed_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(original["evidence"])
    ledger = storage.load_ledger(tmp_path)
    ledger["trusted_sources"] = [
        {
            "source_id": "trusted-official-example",
            "domain": "official.example.com",
            "source_type": "enterprise_official",
            "source_level": "primary",
            "evidence_quality": 5,
            "allowed_claim_types": ["financing", "expansion"],
            "status": "active",
            "created_at": "2026-08-24T00:00:00+00:00",
            "created_by_user_id": "ordinary-user",
            "change_reason": "测试可信企业公开目录。",
        }
    ]
    storage.save_ledger(tmp_path, ledger)

    def fake_fetch_bytes(source_url: str, *, allowed_content_types: set[str]) -> dict[str, object]:
        """为 robots、首页、RSS 和 Sitemap 提供受控公开响应，不允许额外网络地址。"""
        responses = {
            "https://official.example.com/robots.txt": (
                "text/plain",
                b"User-agent: *\nAllow: /\nSitemap: https://official.example.com/sitemap.xml\n",
            ),
            "https://official.example.com": (
                "text/html",
                b"<html><head><link rel='alternate' type='application/rss+xml' href='/feed.xml'></head></html>",
            ),
            "https://official.example.com/feed.xml": (
                "application/rss+xml",
                    "<rss><channel><item><title>中国具身智能企业合作公告</title><link>https://official.example.com/news/embodied</link><description>中国具身智能机器人企业披露产业合作。</description></item></channel></rss>".encode(
                    "utf-8"
                ),
            ),
            "https://official.example.com/sitemap.xml": (
                "application/xml",
                "<urlset><url><loc>https://official.example.com/news/半导体项目</loc></url></urlset>".encode("utf-8"),
            ),
        }
        assert source_url in responses
        content_type, raw_content = responses[source_url]
        assert content_type in allowed_content_types
        return {
            "source_url": source_url,
            "content_type": content_type,
            "charset": "utf-8",
            "raw_content": raw_content,
            "byte_size": len(raw_content),
        }

    monkeypatch.setattr(source_feed, "_fetch_public_bytes", fake_fetch_bytes)
    result = source_feed.discover_trusted_source_feeds(tmp_path)

    assert result["run"]["status"] == "completed"
    assert result["run"]["candidate_count"] == 1
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == [evidence_before]
    assert len(ledger_after["source_feed_runs"]) == 1
    assert {candidate["source_catalog_id"] for candidate in result["candidates"]} == {
        "trusted_rss_feed",
    }
    assert {candidate["sector"] for candidate in result["candidates"]} == {
        "embodied_intelligence",
    }
    assert {candidate["source_level"] for candidate in result["candidates"]} == {"lead_only"}


def test_trusted_source_rotation_prevents_new_sources_from_starving() -> None:
    """目录超过单轮上限时，未尝试来源先于近期已尝试来源进入本轮。

    输入是六条已启用目录记录与前一轮只尝试前五条的服务器审计；输出必须只选择未尝试
    的第六条与最久未尝试的四条。该测试只验证调度顺序，不联网、不创建候选或证据，也
    不以目录顺序改变任何企业事实、评分或人工复核结论。
    """
    source_feed = _load_plugin_module("source_feed_discovery")
    sources = [
        {
            "source_id": f"source-{index}",
            "domain": f"source-{index}.example.com",
            "status": "active",
        }
        for index in range(1, 7)
    ]
    previous_runs = [
        {
            "started_at": "2026-09-07T00:00:00+00:00",
            "source_results": [{"source_id": f"source-{index}"} for index in range(1, 6)],
        }
    ]

    selected = source_feed._select_active_sources_for_run(sources, previous_runs)

    assert [source["source_id"] for source in selected] == [
        "source-6",
        "source-1",
        "source-2",
        "source-3",
        "source-4",
    ]


def test_manual_trusted_source_selection_prioritizes_only_fixed_media_seeds() -> None:
    """手动测试可优先四个固定媒体来源，但不能把任意普通目录提升为优先来源。

    输入是四条带系统固定标记的媒体记录和两条普通激活目录；输出在五条上限内先选四条
    媒体、再按原轮换规则补一条普通目录。测试只验证服务器内部调度顺序，不联网、不添加
    候选/证据，也不允许普通来源通过名称、等级或模型字段伪造媒体优先资格。
    """
    source_feed = _load_plugin_module("source_feed_discovery")
    sources = [
        {
            "source_id": f"media-{index}",
            "domain": f"media-{index}.example.com",
            "status": "active",
            "system_curated_chinese_business_media": True,
        }
        for index in range(1, 5)
    ] + [
        {
            "source_id": f"ordinary-{index}",
            "domain": f"ordinary-{index}.example.com",
            "status": "active",
        }
        for index in range(1, 3)
    ]

    selected = source_feed._select_active_sources_for_run(
        sources,
        [],
        curated_media_first=True,
    )

    assert [source["source_id"] for source in selected] == [
        "media-1",
        "media-2",
        "media-3",
        "media-4",
        "ordinary-1",
    ]


def test_curated_media_candidate_requires_page_level_china_business_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """固定中国媒体仅可把“主体+机会”标题送入待补证，禁止直接入证。

    输入是模拟的系统固定媒体首页，其中标题写有企业融资和半导体、却没有“中国/徐州”。
    输出应为一条带 ``requires_china_business_verification`` 的 lead_only 候选；同一候选
    必须被正式抓取入口拒绝。测试证明此例外只增加原页快照补证机会，不会把中文媒体域名
    误写成企业中国业务或招商意愿事实，也不会创建 evidence、评分或推进线索。
    """
    source_feed = _load_plugin_module("source_feed_discovery")
    source_discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    ledger = storage.load_ledger(tmp_path)
    ledger["trusted_sources"] = [
        {
            "source_id": "trusted-curated-media-example",
            "domain": "media.example.com",
            "source_type": "verified_media",
            "source_level": "secondary",
            "evidence_quality": 3,
            "system_curated_chinese_business_media": True,
            "allowed_claim_types": ["financing", "industry_focus"],
            "status": "active",
            "created_at": "2026-09-07T00:00:00+00:00",
            "created_by_user_id": "system-default",
            "change_reason": "测试固定中国媒体候选补证边界。",
        }
    ]
    storage.save_ledger(tmp_path, ledger)

    def fake_fetch_bytes(source_url: str, *, allowed_content_types: set[str]) -> dict[str, object]:
        """只返回 robots 与一个无中国语境的媒体标题，防止测试访问其他地址。"""
        responses = {
            "https://media.example.com/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
            "https://media.example.com": (
                "text/html",
                "<a href='/news/semiconductor-financing'>半导体企业完成融资</a>".encode("utf-8"),
            ),
        }
        assert source_url in responses
        content_type, raw_content = responses[source_url]
        assert content_type in allowed_content_types
        return {
            "source_url": source_url,
            "content_type": content_type,
            "charset": "utf-8",
            "raw_content": raw_content,
            "byte_size": len(raw_content),
        }

    monkeypatch.setattr(source_feed, "_fetch_public_bytes", fake_fetch_bytes)
    result = source_feed.discover_trusted_source_feeds(tmp_path)

    assert result["run"]["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["requires_china_business_verification"] is True
    assert candidate["eligibility_assessment"]["eligible"] is True
    assert candidate["eligibility_assessment"]["china_context_terms"] == []
    assert candidate["eligibility_assessment"]["requires_china_business_verification"] is True
    with pytest.raises(ValueError, match="禁止直接抓取为正式证据"):
        source_discovery._assert_candidate_is_in_automatic_scope(candidate)
    assert storage.load_ledger(tmp_path)["evidence"] == []


def test_fixed_investment_event_index_creates_only_pending_company_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """固定投融资栏目解析公司链接和融资摘要，但只能新增待补证候选。

    输入是唯一源码固定来源 ``trusted-pedaily-investment-events`` 的模拟半导体栏目 HTML；
    输出应为一条公司页链接候选，并保留页面可见行业、轮次、日期和摘要。公司显示名没有
    “企业/公司”文字且没有中国语境时，只能靠固定事件卡片的受限结构进入待补证队列；
    测试断言其仍被标记为需核验中国业务，且不会新增 evidence、线索、评分或推进状态。
    任意普通来源没有对应的源码 URL 映射，因此无法触发此分支。
    """
    source_feed = _load_plugin_module("source_feed_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    ledger = storage.load_ledger(tmp_path)
    ledger["trusted_sources"] = [
        {
            "source_id": "trusted-pedaily-investment-events",
            "domain": "pedaily.cn",
            "source_type": "verified_media",
            "source_level": "secondary",
            "evidence_quality": 3,
            "system_curated_chinese_business_media": True,
            "allowed_claim_types": ["financing", "industry_focus"],
            "status": "active",
            "created_at": "2026-09-07T00:00:00+00:00",
            "created_by_user_id": "system-default",
            "change_reason": "测试固定投融资栏目来源。",
        }
    ]
    storage.save_ledger(tmp_path, ledger)

    def fake_fetch_bytes(source_url: str, *, allowed_content_types: set[str]) -> dict[str, object]:
        """只模拟固定栏目及其 robots，禁止测试访问任何额外 URL 或正文页。"""
        responses = {
            "https://vc.pedaily.cn/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
            "https://vc.pedaily.cn/invest/f2842": (
                "text/html",
                (
                    "<div class='item'><div class='top'><div class='t'><span>A轮</span>"
                    "<span>数亿元</span></div><div class='d'>2026-09-07</div></div>"
                    "<div class='title'><div class='name'><h3><a "
                    "href='https://vc.pedaily.cn/company/42.html'>光芯科技</a></h3>"
                    "<div class='ind'><a href='/invest/f2842'>芯片半导体</a></div></div></div>"
                    "<div class='ai-summary'>光芯科技完成数亿元A轮融资，资金用于芯片研发和量产。</div>"
                    "</div>"
                ).encode("utf-8"),
            ),
        }
        assert source_url in responses
        content_type, raw_content = responses[source_url]
        assert content_type in allowed_content_types
        return {
            "source_url": source_url,
            "content_type": content_type,
            "charset": "utf-8",
            "raw_content": raw_content,
            "byte_size": len(raw_content),
        }

    monkeypatch.setattr(source_feed, "_fetch_public_bytes", fake_fetch_bytes)
    result = source_feed.discover_trusted_source_feeds(
        tmp_path,
        requested_sector="semiconductor",
        curated_media_first=True,
    )

    assert result["run"]["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["source_url"] == "https://vc.pedaily.cn/company/42.html"
    assert candidate["source_title"] == "光芯科技"
    assert candidate["sector"] == "semiconductor"
    assert candidate["source_catalog_id"] == "trusted_fixed_investment_event_index"
    assert "页面行业：芯片半导体" in candidate["search_snippet"]
    assert "页面摘要：光芯科技完成数亿元A轮融资" in candidate["search_snippet"]
    assert candidate["requires_china_business_verification"] is True
    assert candidate["eligibility_assessment"]["business_subject_terms"] == [
        "fixed_structured_investment_event_subject"
    ]
    assert candidate["eligibility_assessment"]["china_context_terms"] == []
    assert storage.load_ledger(tmp_path)["evidence"] == []
    assert storage.load_ledger(tmp_path)["leads"] == []


def test_investment_establishment_terms_require_subject_and_remain_pending_verification() -> None:
    """“投资成立新公司”是明确资本动作，但不能因中文媒体而自动证明中国业务。

    输入是无中国语境的半导体公司设立标题及一个无主体的宏观标题；输出要求前者仅在
    固定媒体补证规则下可进入受限候选，后者仍被拒绝。测试防止词表扩展退化为把“投资”
    单字、产业新闻或模型主观判断当作招商机会事实。
    """
    source_discovery = _load_plugin_module("source_discovery")
    curated = source_discovery.assess_curated_chinese_media_candidate(
        source_url="https://media.example.cn/news/company-establishment",
        source_title="某半导体公司投资成立新公司",
        search_snippet="可信目录首页公开标题，尚待核验原页。",
    )
    generic = source_discovery.assess_public_discovery_candidate(
        source_url="https://media.example.cn/news/macro-investment",
        source_title="半导体产业投资持续升温",
        search_snippet="宏观新闻标题，未指向具体组织主体。",
    )

    assert curated["eligible"] is True
    assert curated["opportunity_signal_terms"] == ["投资成立"]
    assert curated["requires_china_business_verification"] is True
    assert generic["eligible"] is False
    assert "missing_enterprise_fund_or_team_subject" in generic["rejection_reasons"]


def test_trusted_directory_homepage_link_creates_only_verified_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """无 RSS/Sitemap 的可信首页也可提供产业链接，但不会绕过资格门槛或证据链。"""
    source_feed = _load_plugin_module("source_feed_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    ledger = storage.load_ledger(tmp_path)
    ledger["trusted_sources"] = [
        {
            "source_id": "trusted-xuzhou-portal-example",
            "domain": "official.example.com",
            "source_type": "government_investment_portal",
            "source_level": "primary",
            "evidence_quality": 5,
            "allowed_claim_types": ["cooperation", "expansion"],
            "status": "active",
            "created_at": "2026-09-07T00:00:00+00:00",
            "created_by_user_id": "ordinary-user",
            "change_reason": "测试可信首页公开链接发现。",
        }
    ]
    storage.save_ledger(tmp_path, ledger)

    def fake_fetch_bytes(source_url: str, *, allowed_content_types: set[str]) -> dict[str, object]:
        """只提供 robots 与首页，不允许测试通过未知链接绕过同域访问限制。"""
        responses = {
            "https://official.example.com/robots.txt": (
                "text/plain",
                b"User-agent: *\nAllow: /\n",
            ),
            "https://official.example.com": (
                "text/html",
                (
                    "<a href='/login'>登录</a>"
                    "<a href='/news/robotics'>徐州具身智能企业完成融资</a>"
                ).encode("utf-8"),
            ),
        }
        assert source_url in responses
        content_type, raw_content = responses[source_url]
        assert content_type in allowed_content_types
        return {
            "source_url": source_url,
            "content_type": content_type,
            "charset": "utf-8",
            "raw_content": raw_content,
            "byte_size": len(raw_content),
        }

    monkeypatch.setattr(source_feed, "_fetch_public_bytes", fake_fetch_bytes)
    result = source_feed.discover_trusted_source_feeds(tmp_path)

    assert result["run"]["candidate_count"] == 1
    assert result["run"]["source_results"][0]["homepage_link_count"] == 1
    assert result["run"]["source_results"][0]["homepage_candidate_count"] == 1
    assert result["candidates"][0]["source_catalog_id"] == "trusted_homepage_link"
    assert result["candidates"][0]["source_level"] == "lead_only"
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == []
    assert ledger_after["leads"] == []


def test_trusted_directory_uses_www_fallback_only_after_unavailable_apex_robots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """可信目录裸域名 robots 不可用时，只能同域尝试 www 并重新遵守其 robots 结论。

    输入是一个已保存的 ``university.example.edu`` 可信主域名，模拟裸域名 DNS 不可用、同域
    ``www`` robots 明确允许。输出验证系统记录实际解析到的同域首页，并且不创建候选、证据
    或线索。该测试防止为了提高免费高校来源覆盖而改为 IP、跨域重定向、关闭 TLS 校验或
    在 robots 明确拒绝时继续访问。
    """

    source_feed = _load_plugin_module("source_feed_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    ledger = storage.load_ledger(tmp_path)
    ledger["trusted_sources"] = [
        {
            "source_id": "trusted-university-example",
            "domain": "university.example.edu",
            "source_type": "university_alumni",
            "source_level": "primary",
            "evidence_quality": 4,
            "allowed_claim_types": ["xuzhou_connection"],
            "status": "active",
            "created_at": "2026-08-24T00:00:00+00:00",
            "created_by_user_id": "ordinary-user",
            "change_reason": "测试同域 www 官网入口回退。",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    checked_urls: list[str] = []

    def fake_check_robots(source_url: str) -> dict[str, object]:
        """模拟裸域名不可用与同域 www 许可，不允许测试访问其他 URL。"""

        checked_urls.append(source_url)
        if source_url == "https://university.example.edu":
            return {
                "allowed": False,
                "reason": "robots_unavailable:无法解析来源域名：university.example.edu",
                "robots_url": "https://university.example.edu/robots.txt",
                "sitemap_urls": [],
            }
        assert source_url == "https://www.university.example.edu"
        return {
            "allowed": True,
            "reason": "robots_allow",
            "robots_url": "https://www.university.example.edu/robots.txt",
            "sitemap_urls": [],
        }

    def fake_discover_homepage(origin: str, *, domain: str) -> dict[str, list[object]]:
        """验证后续首页探测只使用已获许可的同域 www 主机。"""

        assert origin == "https://www.university.example.edu"
        assert domain == "university.example.edu"
        return {"feed_urls": [], "homepage_items": []}

    monkeypatch.setattr(source_feed, "check_robots_access", fake_check_robots)
    monkeypatch.setattr(source_feed, "_discover_homepage_resources", fake_discover_homepage)
    result = source_feed.discover_trusted_source_feeds(tmp_path)

    assert checked_urls == [
        "https://university.example.edu",
        "https://www.university.example.edu",
    ]
    assert result["run"]["status"] == "completed"
    assert result["run"]["candidate_count"] == 0
    assert result["run"]["source_results"] == [
        {
            "source_id": "trusted-university-example",
            "status": "completed",
            "resolved_origin": "https://www.university.example.edu",
            "rss_feed_count": 0,
            "sitemap_count": 0,
            "homepage_link_count": 0,
            "rss_candidate_count": 0,
            "sitemap_candidate_count": 0,
            "homepage_candidate_count": 0,
        }
    ]
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == []
    assert ledger_after["leads"] == []


def test_robots_disallow_marks_candidate_without_requesting_page_or_changing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """robots 禁止的候选只记录策略失败并进入退避，绝不读取页面或写入证据。

    输入是服务器发现的候选 ID；robots 结果由模拟函数提供。断言页面抓取函数从未被
    调用、原有证据完全一致，并把来源健康度标为政策禁止，避免下一次自动任务重复访问。
    """
    discovery = _load_plugin_module("source_discovery")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(tmp_path, **_evidence_arguments())
    evidence_before = copy.deepcopy(original["evidence"])
    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "中国禁止自动访问的半导体企业融资公告",
                "source_url": "https://blocked.example.com/private/semiconductor",
                "search_snippet": "待验证公开链接。",
            }
        ],
    )
    candidate = discovery.discover_public_sources(
        tmp_path,
        sector="半导体",
        max_queries=1,
        max_results_per_query=1,
    )["candidates"][0]
    page_fetches: list[str] = []
    monkeypatch.setattr(
        discovery,
        "check_robots_access",
        lambda _source_url: {"allowed": False, "reason": "robots_disallow"},
    )
    monkeypatch.setattr(
        discovery,
        "fetch_public_source",
        lambda source_url: page_fetches.append(source_url),
    )

    result = discovery.capture_discovery_candidate(
        tmp_path,
        candidate_id=candidate["candidate_id"],
        entity_name="禁止自动访问企业",
        entity_kind="企业",
        claim_type="扩张",
        source_published_at="2026-08-24",
        excerpt="禁止自动访问企业正在扩张。",
    )

    assert result["captured"] is False
    assert page_fetches == []
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == [evidence_before]
    assert ledger_after["discovery_candidates"][0]["state"] == "capture_failed"
    assert ledger_after["source_health"][-1]["status"] == "policy_blocked"


def test_relationship_graph_registers_evidence_backed_nodes_and_edge_without_mutating_ledger(
    tmp_path: Path,
) -> None:
    """图谱节点和边必须只引用既有证据，且写入不改变线索与原始证据。

    输入是一条正式入库的目标企业公开证据和两个组织节点；输出应保存两个节点、一条
    关系边及三条图谱审计。测试逐字比对原线索、证据和评分列表，证明关系图谱只是
    附加引用层，不能把人工关系判断写回原始来源链或招商业务状态。
    """
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt=(
                "徐州示例产业园为示例具身智能企业提供具身智能上游供应产业链配套，"
                "并披露完成新一轮融资。"
            ),
        ),
    )
    lead_id = created["lead"]["lead_id"]
    evidence_id = created["evidence"]["evidence_id"]
    ledger_before = storage.load_ledger(tmp_path)
    protected_before = {
        field_name: copy.deepcopy(ledger_before[field_name])
        for field_name in (
            "leads",
            "evidence",
            "scoring_rule_sets",
            "score_snapshots",
            "outcome_feedback",
        )
    }

    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州示例产业园",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[evidence_id],
        registration_reason="该公开证据需要人工在图谱中标注其涉及的徐州本地载体，供后续核验。",
        authenticated_user_id="ordinary-graph-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=lead_id,
        evidence_ids=[evidence_id],
        registration_reason="把既有目标企业线索映射为图谱节点，不新增或改写任何公开事实。",
        authenticated_user_id="ordinary-graph-user",
    )["node"]
    edge_result = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="上游供应",
        evidence_ids=[evidence_id],
        registration_reason="该边仅记录需要人工持续核验的证据化产业链关联，不代表自动招商结论。",
        authenticated_user_id="ordinary-graph-user",
    )

    assert edge_result["created"] is True
    ledger_after = storage.load_ledger(tmp_path)
    for field_name, expected_value in protected_before.items():
        assert ledger_after[field_name] == expected_value
    assert len(ledger_after["relationship_graph_nodes"]) == 2
    assert len(ledger_after["relationship_graph_edges"]) == 1
    assert [item["action"] for item in ledger_after["relationship_graph_audit"]] == [
        "relationship_node_registered",
        "relationship_node_registered",
        "relationship_edge_registered",
    ]
    assert {item["operated_by_user_id"] for item in ledger_after["relationship_graph_audit"]} == {
        "ordinary-graph-user"
    }

    graph_ledger_before_read = storage.load_ledger(tmp_path)
    subgraph = relationship_graph.get_relationship_graph(tmp_path, lead_id=lead_id)
    assert storage.load_ledger(tmp_path) == graph_ledger_before_read
    assert subgraph["read_only"] is True
    assert subgraph["node_count"] == 2
    assert subgraph["edge_count"] == 1
    assert subgraph["evidence_reference_ids"] == [evidence_id]
    assert subgraph["edges"][0]["relationship_type"] == "upstream_supplier"
    assert subgraph["edges"][0]["operation_scope"] == (
        "evidence_backed_relationship_graph_registration_only"
    )
    analysis = subgraph["industry_chain_analysis"]
    assert analysis["business_relationship_strength_inferred"] is False
    assert analysis["summary"] == {
        "edge_count": 1,
        "evidence_backed_industry_chain_edge_count": 1,
        "xuzhou_external_industry_chain_edge_count": 1,
        "pending_evidence_edge_count": 1,
        "relationship_type_counts": {"upstream_supplier": 1},
    }
    edge_analysis = analysis["edge_analysis"][0]
    assert edge_analysis["business_flow_direction"] == "from_node_to_to_node"
    assert edge_analysis["from_node_role"] == "upstream_supplier"
    assert edge_analysis["to_node_role"] == "downstream_recipient"
    assert edge_analysis["pending_evidence"] is True
    assert edge_analysis["evidence_support"]["level"] == "single_evidence_reference"
    assert edge_analysis["evidence_support"]["business_relationship_strength_inferred"] is False


def test_relationship_graph_records_public_key_person_roles_without_personal_lead_or_evidence_rewrite(
    tmp_path: Path,
) -> None:
    """公开法人/实控人关系必须可追溯、由普通用户复核，且不能变成个人招商线索。

    输入是同一条原始公开摘录明确披露企业名称、张三姓名、徐州关联、法定代表人与实际
    控制人角色；输出应只新增组织/人员节点与两条待复核角色边。测试还验证“徐州关联”
    缺证、人员绑定 lead 和反向人员角色边都会被拒绝，证明系统既不会依据猜测标注乡土
    关系，也不会把个人收入、资产、联系方式或募资能力纳入招商台账。
    """
    reports = _load_plugin_module("reports")
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    relationship_graph_api = _load_plugin_module("relationship_graph_api")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt=(
                "示例具身智能企业公告披露：徐州籍张三为该企业法定代表人及实际控制人，"
                "相关任职信息以公告原文为准。"
            ),
        ),
    )
    lead_id = created["lead"]["lead_id"]
    evidence_id = created["evidence"]["evidence_id"]
    ledger_before_graph = copy.deepcopy(storage.load_ledger(tmp_path))

    organization = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=lead_id,
        evidence_ids=[evidence_id],
        registration_reason="把已有企业线索映射为公开关键人员关系的组织端，不新增事实。",
        authenticated_user_id="ordinary-key-person-user",
    )["node"]
    person = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="张三",
        node_kind="关键人员",
        locality="徐州关联",
        linked_lead_id=None,
        evidence_ids=[evidence_id],
        registration_reason="仅登记公告明确披露的姓名和宽泛徐州关联，供人工核验组织角色。",
        authenticated_user_id="ordinary-key-person-user",
    )["node"]
    legal_representative_edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=person["node_id"],
        to_node_id=organization["node_id"],
        relationship_type="法定代表人",
        evidence_ids=[evidence_id],
        registration_reason="仅记录公告明确的法定代表人角色，不能推断个人资金或招商意愿。",
        authenticated_user_id="ordinary-key-person-user",
    )["edge"]
    controller_edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=person["node_id"],
        to_node_id=organization["node_id"],
        relationship_type="实际控制人",
        evidence_ids=[evidence_id],
        registration_reason="仅记录公告明确的实际控制人角色，仍需普通用户人工复核。",
        authenticated_user_id="ordinary-key-person-user",
    )["edge"]

    ledger_after_graph = storage.load_ledger(tmp_path)
    for field_name in (
        "leads",
        "evidence",
        "scoring_rule_sets",
        "score_snapshots",
        "outcome_feedback",
    ):
        assert ledger_after_graph[field_name] == ledger_before_graph[field_name]
    assert person["node_kind"] == "person"
    assert person["locality"] == "xuzhou_related"
    assert person["linked_lead_id"] is None
    assert not {"phone", "address", "asset_balance", "fundraising_capacity"} & set(person)
    assert {legal_representative_edge["relationship_type"], controller_edge["relationship_type"]} == {
        "legal_representative",
        "actual_controller",
    }
    assert legal_representative_edge["review_state"] == "pending_user_review"

    graph_before_read = copy.deepcopy(storage.load_ledger(tmp_path))
    graph = relationship_graph.get_relationship_graph(tmp_path, lead_id=lead_id)
    assert storage.load_ledger(tmp_path) == graph_before_read
    key_person_analysis = graph["key_person_relationship_analysis"]
    assert key_person_analysis["person_financial_or_contact_data_collected"] is False
    assert key_person_analysis["personal_investment_or_landing_intent_inferred"] is False
    assert key_person_analysis["summary"] == {
        "evidence_backed_key_person_relationship_edge_count": 2,
        "distinct_public_key_person_count": 1,
        "xuzhou_related_public_key_person_count": 1,
        "pending_review_key_person_relationship_edge_count": 2,
        "relationship_type_counts": {
            "actual_controller": 1,
            "legal_representative": 1,
        },
    }
    assert {card["person"]["display_name"] for card in key_person_analysis["role_cards"]} == {"张三"}
    assert {card["organization"]["display_name"] for card in key_person_analysis["role_cards"]} == {
        "示例具身智能企业"
    }
    assert {
        evidence_id
        for card in key_person_analysis["role_cards"]
        for evidence_id in card["evidence_support"]["original_edge_evidence_ids"]
    } == {evidence_id}

    assessment_before_read = copy.deepcopy(storage.load_ledger(tmp_path))
    assessment = reports.get_investment_assessment_report(tmp_path, lead_id=lead_id)
    assert storage.load_ledger(tmp_path) == assessment_before_read
    person_section = assessment["public_key_person_relationship_assessment"]
    assert person_section["status"] == "evidence_available"
    assert person_section["recorded_relationship_count"] == 2
    assert person_section["evidence_reference_ids"] == [evidence_id]
    assert person_section["key_person_relationship_summary"]["distinct_public_key_person_count"] == 1
    assert assessment["industry_chain_relationship_assessment"]["status"] == "needs_evidence"
    assert assessment["industry_chain_relationship_assessment"]["recorded_edge_count"] == 0

    with pytest.raises(ValueError, match="不能绑定为个人招商线索"):
        relationship_graph.register_relationship_node(
            tmp_path,
            display_name="张三",
            node_kind="关键人员",
            locality="徐州关联",
            linked_lead_id=lead_id,
            evidence_ids=[evidence_id],
            registration_reason="故意尝试把公开人员绑定为招商线索。",
            authenticated_user_id="ordinary-key-person-user",
        )
    with pytest.raises(ValueError, match="必须从公开关键人员节点指向已登记组织节点"):
        relationship_graph.register_relationship_edge(
            tmp_path,
            from_node_id=organization["node_id"],
            to_node_id=person["node_id"],
            relationship_type="法定代表人",
            evidence_ids=[evidence_id],
            registration_reason="故意尝试登记反向关键人员关系。",
            authenticated_user_id="ordinary-key-person-user",
        )
    with pytest.raises(ValueError, match="姓名和徐州关联"):
        relationship_graph.register_relationship_node(
            tmp_path,
            display_name="张三",
            node_kind="关键人员",
            locality="徐州关联",
            linked_lead_id=None,
            evidence_ids=[core.record_public_evidence(
                tmp_path,
                **_evidence_arguments(
                    source_url="https://example.com/news/zhang-san-without-xuzhou",
                    excerpt="示例具身智能企业公告披露张三为法定代表人。",
                ),
            )["evidence"]["evidence_id"]],
            registration_reason="故意尝试用未披露徐州关联的证据标注人员。",
            authenticated_user_id="ordinary-key-person-user",
        )
    with pytest.raises(ValidationError):
        relationship_graph_api.RegisterRelationshipNodeRequest.model_validate(
            {
                "display_name": "张三",
                "node_kind": "关键人员",
                "locality": "徐州关联",
                "linked_lead_id": None,
                "evidence_ids": [evidence_id],
                "registration_reason": "故意尝试提交个人联系方式。",
                "phone": "13800000000",
            }
        )


def test_relationship_graph_rejects_edge_without_target_lead_evidence(tmp_path: Path) -> None:
    """关系边不能引用其他线索证据来伪造当前目标企业的关系事实。"""
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    target = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业与徐州示例高校开展产学研合作交流。",
        ),
    )
    unrelated = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="无关半导体企业",
            sector="半导体",
            source_url="https://example.com/news/unrelated",
            source_title="无关企业公告",
            excerpt="无关半导体企业披露扩张计划。",
            claim_type="扩张",
        ),
    )
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=target["lead"]["lead_id"],
        evidence_ids=[target["evidence"]["evidence_id"]],
        registration_reason="映射已有目标线索。",
        authenticated_user_id="ordinary-graph-user",
    )["node"]
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州示例高校",
        node_kind="高校",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[target["evidence"]["evidence_id"]],
        registration_reason="映射待核验本地组织节点。",
        authenticated_user_id="ordinary-graph-user",
    )["node"]

    with pytest.raises(ValueError, match="关联目标线索"):
        relationship_graph.register_relationship_edge(
            tmp_path,
            from_node_id=local_node["node_id"],
            to_node_id=target_node["node_id"],
            relationship_type="产学研合作",
            evidence_ids=[unrelated["evidence"]["evidence_id"]],
            registration_reason="故意尝试用无关线索证据登记关系。",
            authenticated_user_id="ordinary-graph-user",
        )
    assert storage.load_ledger(tmp_path)["relationship_graph_edges"] == []


def test_relationship_graph_rejects_evidence_without_explicit_relation_semantics(
    tmp_path: Path,
) -> None:
    """关系边不能仅因证据同时提及双方就被登记为某一业务关系。"""
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业到访徐州语义缺失园区开展交流，但公告未说明双方关系类型。",
        ),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州语义缺失园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为语义校验测试登记被原始证据明确提及的本地节点。",
        authenticated_user_id="ordinary-graph-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为语义校验测试登记已有目标线索节点。",
        authenticated_user_id="ordinary-graph-user",
    )["node"]

    with pytest.raises(ValueError, match="双方名称和关系关键词"):
        relationship_graph.register_relationship_edge(
            tmp_path,
            from_node_id=local_node["node_id"],
            to_node_id=target_node["node_id"],
            relationship_type="产业链",
            evidence_ids=[created["evidence"]["evidence_id"]],
            registration_reason="故意尝试将没有产业链表述的交流记录登记为产业链关系。",
            authenticated_user_id="ordinary-graph-user",
        )


@pytest.mark.asyncio
async def test_relationship_graph_api_uses_verified_identity_and_rejects_claimed_creator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """图谱 API 允许普通用户登记，但请求体不能伪造创建人或原始证据字段。"""
    relationship_graph_api = _load_plugin_module("relationship_graph_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="徐州示例基金已对示例具身智能企业完成投资。",
        ),
    )
    evidence_before = copy.deepcopy(created["evidence"])
    ordinary_principal = Principal(
        user_id="ordinary-api-graph-user",
        username="ordinary-api-graph-user",
        role="user",
    )

    def fake_get_current_principal(_request: object) -> Principal:
        """模拟服务器认证层返回普通用户，不从请求体读取用户身份。"""
        return ordinary_principal

    async def fake_get_agent_for_request(_request: object) -> SimpleNamespace:
        """模拟已由平台校验归属的工作区，不授予额外管理员权限。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    monkeypatch.setattr(
        relationship_graph_api,
        "get_current_principal",
        fake_get_current_principal,
    )
    monkeypatch.setattr(
        relationship_graph_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    target_response = await relationship_graph_api.register_xuzhou_relationship_node(
        relationship_graph_api.RegisterRelationshipNodeRequest.model_validate(
            {
                "display_name": "示例具身智能企业",
                "node_kind": "企业",
                "locality": "目标或外地",
                "linked_lead_id": created["lead"]["lead_id"],
                "evidence_ids": [created["evidence"]["evidence_id"]],
                "registration_reason": "由普通登录用户登记目标线索的证据化图谱节点。",
            }
        ),
        object(),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州示例基金",
        node_kind="基金",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为接口关系边测试准备的本地节点。",
        authenticated_user_id="setup-user",
    )["node"]
    edge_response = await relationship_graph_api.register_xuzhou_relationship_edge(
        relationship_graph_api.RegisterRelationshipEdgeRequest.model_validate(
            {
                "from_node_id": local_node["node_id"],
                "to_node_id": target_response["node"]["node_id"],
                "relationship_type": "投资",
                "evidence_ids": [created["evidence"]["evidence_id"]],
                "registration_reason": "普通用户以已入库证据登记待核验关系，不自动下结论。",
            }
        ),
        object(),
    )

    assert target_response["node"]["created_by_user_id"] == ordinary_principal.user_id
    assert edge_response["edge"]["created_by_user_id"] == ordinary_principal.user_id
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == [evidence_before]
    with pytest.raises(ValidationError):
        relationship_graph_api.RegisterRelationshipNodeRequest.model_validate(
            {
                "display_name": "尝试伪造身份的节点",
                "node_kind": "企业",
                "locality": "未知",
                "evidence_ids": [created["evidence"]["evidence_id"]],
                "registration_reason": "应被请求模型拒绝。",
                "created_by_user_id": "attacker-user",
            }
        )
    with pytest.raises(ValidationError):
        relationship_graph_api.RegisterRelationshipEdgeRequest.model_validate(
            {
                "from_node_id": local_node["node_id"],
                "to_node_id": target_response["node"]["node_id"],
                "relationship_type": "投资",
                "evidence_ids": [created["evidence"]["evidence_id"]],
                "registration_reason": "应被请求模型拒绝。",
                "evidence": {"source_url": "https://attacker.example"},
            }
        )


def test_relationship_edge_review_appends_history_without_changing_relation_or_evidence(
    tmp_path: Path,
) -> None:
    """普通用户复核关系边只能改变状态并追加审计，不能改写关系或原始证据。"""
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业与徐州复核测试园区建立产业链配套关系。",
        ),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州复核测试园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为关系边复核测试建立本地载体节点。",
        authenticated_user_id="setup-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为关系边复核测试建立目标线索节点。",
        authenticated_user_id="setup-user",
    )["node"]
    edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为关系边复核测试登记待核验的产业链关系。",
        authenticated_user_id="setup-user",
    )["edge"]
    ledger_before = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(ledger_before["evidence"])
    leads_before = copy.deepcopy(ledger_before["leads"])
    nodes_before = copy.deepcopy(ledger_before["relationship_graph_nodes"])
    audit_before = copy.deepcopy(ledger_before["relationship_graph_audit"])
    immutable_before = {
        field_name: copy.deepcopy(edge[field_name])
        for field_name in relationship_graph._EDGE_IMMUTABLE_FIELDS
    }

    reviewed = relationship_graph.review_relationship_edge(
        tmp_path,
        edge_id=edge["edge_id"],
        decision="有效",
        review_note="普通登录用户已核对引用证据，暂将关系标记为有效，后续仍可继续补证。",
        reviewer_user_id="ordinary-review-user",
    )["edge"]

    assert edge["review_state"] == "pending_user_review"
    assert reviewed["review_state"] == "valid"
    assert reviewed["review_history"][-1]["old_review_state"] == "pending_user_review"
    assert reviewed["review_history"][-1]["new_review_state"] == "valid"
    assert reviewed["review_history"][-1]["reviewer_user_id"] == "ordinary-review-user"
    assert reviewed["review_history"][-1]["operation_scope"] == "relationship_graph_business_review_only"
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == evidence_before
    assert ledger_after["leads"] == leads_before
    assert ledger_after["relationship_graph_nodes"] == nodes_before
    for field_name, expected_value in immutable_before.items():
        assert ledger_after["relationship_graph_edges"][0][field_name] == expected_value
    assert ledger_after["relationship_graph_audit"][:-1] == audit_before
    assert ledger_after["relationship_graph_audit"][-1]["action"] == "relationship_edge_reviewed"
    assert ledger_after["relationship_graph_audit"][-1]["operated_by_user_id"] == "ordinary-review-user"
    assert ledger_after["relationship_graph_audit"][-1]["operation_scope"] == (
        "relationship_graph_business_review_only"
    )


@pytest.mark.asyncio
async def test_relationship_edge_review_api_uses_verified_identity_and_rejects_claimed_reviewer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """关系边复核 API 允许普通用户，但拒绝请求体伪造复核人并保持证据不变。"""
    relationship_graph_api = _load_plugin_module("relationship_graph_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业与徐州接口复核园区建立产业链配套关系。",
        ),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州接口复核园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为认证接口测试建立本地节点。",
        authenticated_user_id="setup-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为认证接口测试建立目标节点。",
        authenticated_user_id="setup-user",
    )["node"]
    edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为认证接口测试登记待复核关系。",
        authenticated_user_id="setup-user",
    )["edge"]
    evidence_before = copy.deepcopy(storage.load_ledger(tmp_path)["evidence"])
    ordinary_principal = Principal(
        user_id="ordinary-edge-review-user",
        username="ordinary-edge-review-user",
        role="user",
    )

    monkeypatch.setattr(
        relationship_graph_api,
        "get_current_principal",
        lambda _request: ordinary_principal,
    )

    async def fake_get_agent_for_request(_request: object) -> SimpleNamespace:
        """模拟已通过平台工作区归属校验的普通登录用户。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    monkeypatch.setattr(
        relationship_graph_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    response = await relationship_graph_api.review_xuzhou_relationship_edge(
        edge["edge_id"],
        relationship_graph_api.ReviewRelationshipEdgeRequest.model_validate(
            {
                "decision": "待补证",
                "review_note": "普通用户认为现有公开证据不足以确认关系，需要补充一手材料。",
            }
        ),
        object(),
    )

    assert response["edge"]["review_state"] == "need_evidence"
    assert response["edge"]["review_history"][-1]["reviewer_user_id"] == ordinary_principal.user_id
    assert storage.load_ledger(tmp_path)["evidence"] == evidence_before
    with pytest.raises(ValidationError):
        relationship_graph_api.ReviewRelationshipEdgeRequest.model_validate(
            {
                "decision": "有效",
                "review_note": "试图伪造复核人。",
                "reviewer_user_id": "attacker-user",
            }
        )


def test_relationship_graph_readonly_state_filter_and_summary_do_not_write_ledger(
    tmp_path: Path,
) -> None:
    """图谱查询可按关系复核状态筛选与统计，但完整台账必须保持逐字不变。"""
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt=(
                "示例具身智能企业与徐州状态筛选园区一形成产业链配套，"
                "并与徐州状态筛选园区二开展战略合作。"
            ),
        ),
    )
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为只读状态筛选测试建立目标线索节点。",
        authenticated_user_id="setup-user",
    )["node"]
    local_nodes = [
        relationship_graph.register_relationship_node(
            tmp_path,
            display_name=name,
            node_kind="园区",
            locality="徐州本地",
            linked_lead_id=None,
            evidence_ids=[created["evidence"]["evidence_id"]],
            registration_reason="为只读状态筛选测试建立本地节点。",
            authenticated_user_id="setup-user",
        )["node"]
        for name in ("徐州状态筛选园区一", "徐州状态筛选园区二")
    ]
    first_edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_nodes[0]["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为只读状态筛选测试登记第一条关系。",
        authenticated_user_id="setup-user",
    )["edge"]
    relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_nodes[1]["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="战略合作",
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为只读状态筛选测试登记第二条关系。",
        authenticated_user_id="setup-user",
    )
    relationship_graph.review_relationship_edge(
        tmp_path,
        edge_id=first_edge["edge_id"],
        decision="有效",
        review_note="为验证只读筛选准备一条已人工确认的关系。",
        reviewer_user_id="ordinary-review-user",
    )
    ledger_before_read = copy.deepcopy(storage.load_ledger(tmp_path))

    filtered = relationship_graph.get_relationship_graph(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        review_state="有效",
    )
    pending = relationship_graph.get_relationship_graph(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
        review_state="等待用户复核",
    )

    assert filtered["review_state_filter"] == "valid"
    assert filtered["edge_count"] == 1
    assert filtered["edges"][0]["edge_id"] == first_edge["edge_id"]
    assert filtered["review_state_summary"] == {
        "invalid": 0,
        "need_evidence": 0,
        "pending_user_review": 1,
        "valid": 1,
    }
    assert pending["review_state_filter"] == "pending_user_review"
    assert pending["edge_count"] == 1
    assert pending["edges"][0]["review_state"] == "pending_user_review"
    assert storage.load_ledger(tmp_path) == ledger_before_read
    with pytest.raises(ValueError, match="review_state不合法"):
        relationship_graph.get_relationship_graph(tmp_path, review_state="自动确认")


def test_relationship_review_queue_uses_fixed_rules_and_work_summary_without_writing(
    tmp_path: Path,
) -> None:
    """关系待办队列必须按固定规则排序，并保持图谱、证据和人工状态逐字不变。

    输入是一条关联红色预警线索的待复核关系和一条徐州—外地待补证产业链关系；输出应
    依次为 P0、P1，且明确每条排序原因与原始证据 ID。工作统计只汇总队列数量与关系
    复核审计，不把它们当作招商成果。队列读取不接收用户身份或写入字段，普通用户后续
    仍需经认证复核接口决定状态；本测试逐字比较台账，证明查询不能改写证据链或图谱。
    """
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    reports = _load_plugin_module("reports")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    red_alert = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="红色待办企业",
            source_url="https://example.com/news/red-alert-relationship",
            source_title="红色待办企业风险事项公告",
            claim_type="风险",
            excerpt="红色待办企业与徐州红色待办园区的战略合作公告披露，企业目前资金链断裂。",
        ),
    )
    need_evidence = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="待补证待办企业",
            source_url="https://example.com/news/need-evidence-relationship",
            source_level="线索",
            claim_type="合作",
            evidence_quality=1,
            excerpt="徐州待补证待办园区为待补证待办企业提供上游供应产业链配套合作。",
            xuzhou_association_tags=[],
        ),
    )
    red_local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州红色待办园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[red_alert["evidence"]["evidence_id"]],
        registration_reason="为关系待办固定规则测试登记徐州本地园区。",
        authenticated_user_id="ordinary-queue-user",
    )["node"]
    red_target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="红色待办企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=red_alert["lead"]["lead_id"],
        evidence_ids=[red_alert["evidence"]["evidence_id"]],
        registration_reason="为关系待办固定规则测试登记红色预警企业。",
        authenticated_user_id="ordinary-queue-user",
    )["node"]
    red_edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=red_local_node["node_id"],
        to_node_id=red_target_node["node_id"],
        relationship_type="战略合作",
        evidence_ids=[red_alert["evidence"]["evidence_id"]],
        registration_reason="该关系待普通用户复核，不能由红色预警自动确认。",
        authenticated_user_id="ordinary-queue-user",
    )["edge"]
    need_local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州待补证待办园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[need_evidence["evidence"]["evidence_id"]],
        registration_reason="为关系待办固定规则测试登记待补证本地园区。",
        authenticated_user_id="ordinary-queue-user",
    )["node"]
    need_target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="待补证待办企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=need_evidence["lead"]["lead_id"],
        evidence_ids=[need_evidence["evidence"]["evidence_id"]],
        registration_reason="为关系待办固定规则测试登记待补证企业。",
        authenticated_user_id="ordinary-queue-user",
    )["node"]
    need_edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=need_local_node["node_id"],
        to_node_id=need_target_node["node_id"],
        relationship_type="上游供应",
        evidence_ids=[need_evidence["evidence"]["evidence_id"]],
        registration_reason="该产业链关系需要先补充原始来源再完成业务判断。",
        authenticated_user_id="ordinary-queue-user",
    )["edge"]
    relationship_graph.review_relationship_edge(
        tmp_path,
        edge_id=need_edge["edge_id"],
        decision="待补证",
        review_note="人工确认需要补充合作范围和持续性的可追溯原始来源。",
        reviewer_user_id="ordinary-queue-reviewer",
    )
    ledger_before_read = copy.deepcopy(storage.load_ledger(tmp_path))

    limited_queue = relationship_graph.get_relationship_review_queue(tmp_path, limit=1)
    queue = relationship_graph.get_relationship_review_queue(tmp_path, limit=10)
    work_summary = reports.get_investment_work_summary(tmp_path, window_days=30)

    assert limited_queue["shown_item_count"] == 1
    assert limited_queue["truncated"] is True
    assert limited_queue["items"][0]["edge_id"] == red_edge["edge_id"]
    assert queue["read_only"] is True
    assert queue["summary"] == {
        "queued_item_count": 2,
        "priority_counts": {
            "P0_red_alert_relation_review": 1,
            "P1_xuzhou_external_industry_chain_need_evidence": 1,
        },
        "review_state_counts": {"need_evidence": 1, "pending_user_review": 1},
        "xuzhou_external_industry_chain_queue_item_count": 1,
    }
    assert [item["edge_id"] for item in queue["items"]] == [
        red_edge["edge_id"],
        need_edge["edge_id"],
    ]
    assert queue["items"][0]["priority"] == "P0_red_alert_relation_review"
    assert queue["items"][0]["priority_reasons"] == [
        "关联招商线索已有红色预警，关系边仍需人工复核。"
    ]
    assert queue["items"][0]["evidence_reference_ids"] == [
        red_alert["evidence"]["evidence_id"]
    ]
    assert queue["items"][1]["priority"] == (
        "P1_xuzhou_external_industry_chain_need_evidence"
    )
    assert queue["items"][1]["review_state"] == "need_evidence"
    assert queue["items"][1]["business_flow_direction"] == "from_node_to_to_node"
    assert queue["items"][1]["linked_leads"] == [
        {
            "lead_id": need_evidence["lead"]["lead_id"],
            "entity_name": "待补证待办企业",
            "sector": "embodied_intelligence",
            "review_state": "pending_user_review",
            "red_alert": False,
        }
    ]
    assert work_summary["report_type"] == "xuzhou_investment_work_summary_readonly_v5"
    assert work_summary["current_totals"]["relationship_review_queue_item_count"] == 2
    assert work_summary["current_totals"]["relationship_need_evidence_edge_count"] == 1
    assert work_summary["relationship_review_queue_summary"] == queue["summary"]
    assert any(
        item["relationship_review_actions"] == 1 for item in work_summary["daily_activity"]
    )
    assert storage.load_ledger(tmp_path) == ledger_before_read
    with pytest.raises(ValueError, match="limit必须是1到200之间的整数"):
        relationship_graph.get_relationship_review_queue(tmp_path, limit=201)


def test_action_center_and_outreach_brief_are_evidence_backed_and_readonly(
    tmp_path: Path,
) -> None:
    """行动中心只汇总既有待办，准备卡只引用证据，二者均不得改写台账。

    输入构造同一企业的融资、双来源同日记录、徐州合作关系和较早来源；输出应按固定
    P0--P3 顺序产生人工待办，并基于证据类别生成招商准备动作。测试在读取前后逐字
    比较完整台账，确保待办、准备卡没有将多来源、旧来源或红色预警写成新事实，也没有
    变更原始 URL、摘录、采集时间、完整性链、评分或人工业务状态。
    """
    action_center = _load_plugin_module("action_center")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    financing = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="行动中心企业",
            source_url="https://official.example.com/news/financing",
            source_title="行动中心企业官方融资公告",
            excerpt="行动中心企业在官方公告中披露完成新一轮融资，并计划扩大研发与商业化投入。",
        ),
    )
    second_source = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="行动中心企业",
            source_url="https://media.example.net/news/financing",
            source_title="行动中心企业融资报道",
            source_level="二手",
            evidence_quality=3,
            excerpt="行动中心企业完成新一轮融资，后续将扩大研发和商业化投入。",
            xuzhou_association_tags=[],
        ),
    )
    relationship_evidence = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="行动中心企业",
            source_url="https://cooperation.example.org/news/xuzhou",
            source_title="行动中心企业与徐州行动园区合作公告",
            source_published_at="2025-01-01",
            claim_type="合作",
            excerpt="行动中心企业与徐州行动园区开展产业链配套合作，并讨论后续地方产业资源需求。",
            xuzhou_association_tags=["业务关联"],
        ),
    )
    xuzhou_connection_evidence = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="行动中心企业",
            source_url="https://official.example.com/news/xuzhou-connection",
            source_title="行动中心企业创始团队徐州关联介绍",
            claim_type="徐州关联",
            excerpt="行动中心企业创始团队公开介绍了在徐州的成长经历和后续产业合作关注。",
            xuzhou_association_tags=[],
        ),
    )
    risk_evidence = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="行动中心企业",
            source_url="https://official.example.com/news/risk",
            source_title="行动中心企业风险事项公告",
            claim_type="风险",
            excerpt="行动中心企业在官方公告中披露资金链断裂，正在推进破产重整程序。",
            xuzhou_association_tags=[],
        ),
    )
    assert financing["lead"]["lead_id"] == second_source["lead"]["lead_id"]
    assert financing["lead"]["lead_id"] == relationship_evidence["lead"]["lead_id"]
    assert financing["lead"]["lead_id"] == xuzhou_connection_evidence["lead"]["lead_id"]
    assert financing["lead"]["lead_id"] == risk_evidence["lead"]["lead_id"]
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州行动园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[relationship_evidence["evidence"]["evidence_id"]],
        registration_reason="为行动中心准备卡登记已存在证据支撑的徐州园区节点。",
        authenticated_user_id="setup-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="行动中心企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=financing["lead"]["lead_id"],
        evidence_ids=[relationship_evidence["evidence"]["evidence_id"]],
        registration_reason="为行动中心准备卡登记既有招商线索对应的企业节点。",
        authenticated_user_id="setup-user",
    )["node"]
    edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[relationship_evidence["evidence"]["evidence_id"]],
        registration_reason="行动中心应提示人工复核的徐州产业链合作关系。",
        authenticated_user_id="setup-user",
    )["edge"]
    ledger_before_read = copy.deepcopy(storage.load_ledger(tmp_path))

    center = action_center.get_investment_action_center(tmp_path, limit=100)
    brief = action_center.get_outreach_preparation_brief(
        tmp_path,
        lead_id=financing["lead"]["lead_id"],
    )

    assert center["read_only"] is True
    assert center["summary"]["action_item_count"] == 4
    assert center["summary"]["item_kind_counts"] == {
        "multi_source_event_alignment": 1,
        "red_alert_review": 1,
        "relationship_manual_review": 1,
        "source_freshness_review": 1,
    }
    assert [item["item_kind"] for item in center["items"]] == [
        "red_alert_review",
        "relationship_manual_review",
        "multi_source_event_alignment",
        "source_freshness_review",
    ]
    assert [item["action_priority_rank"] for item in center["items"]] == [0, 1, 2, 3]
    assert center["items"][0]["lead"]["lead_id"] == financing["lead"]["lead_id"]
    assert center["items"][0]["risk"] == {
        "level": "critical",
        "type": "liquidity_or_solvency",
        "status": "open",
        "reason": "资金链或偿债风险：原始来源明确出现“资金链断裂”",
        "trigger_evidence_ids": [risk_evidence["evidence"]["evidence_id"]],
    }
    assert [card["evidence_id"] for card in center["items"][0]["evidence_cards"]] == [
        risk_evidence["evidence"]["evidence_id"]
    ]
    assert center["items"][1]["relationship_item"]["edge_id"] == edge["edge_id"]
    assert center["items"][2]["unique_source_domains"] == [
        "media.example.net",
        "official.example.com",
    ]
    assert {
        card["evidence_id"] for card in center["items"][2]["evidence_cards"]
    } == {
        financing["evidence"]["evidence_id"],
        second_source["evidence"]["evidence_id"],
    }
    assert center["items"][3]["evidence_card"]["evidence_id"] == relationship_evidence[
        "evidence"
    ]["evidence_id"]
    assert center["delivery_boundary"]["external_push_sent"] is False
    assert center["automated_business_conclusion"]["status"] == "not_generated"
    assert all("snapshot_relative_path" not in item for item in center["items"])

    assert brief["read_only"] is True
    assert brief["lead"]["lead_id"] == financing["lead"]["lead_id"]
    assert brief["related_relationship_review_item_count"] == 1
    assert brief["manual_authorization_boundary"] == {
        "external_contact_sent": False,
        "policy_or_incentive_committed": False,
        "reason": "本简报只准备人工核验材料，任何联系、邀约、政策承诺和线索状态变更均须另行由有权人员完成。",
    }
    action_cards_by_key = {item["action_key"]: item for item in brief["action_cards"]}
    assert set(action_cards_by_key) == {
        "review_existing_red_alert",
        "prepare_financing_or_fund_resource_question_list",
            "verify_xuzhou_connection_before_outreach",
            "verify_landing_or_cooperation_requirement",
            "review_counter_signals_before_outreach",
            "resolve_relationship_graph_review_items",
        }
    assert action_cards_by_key["review_counter_signals_before_outreach"]["evidence_ids"] == [
        risk_evidence["evidence"]["evidence_id"]
    ]
    assert action_cards_by_key["verify_xuzhou_connection_before_outreach"]["evidence_ids"] == [
        xuzhou_connection_evidence["evidence"]["evidence_id"]
    ]
    assert action_cards_by_key["prepare_financing_or_fund_resource_question_list"][
        "evidence_ids"
    ] == sorted(
        [financing["evidence"]["evidence_id"], second_source["evidence"]["evidence_id"]]
    )
    assert action_cards_by_key["verify_landing_or_cooperation_requirement"][
        "evidence_ids"
    ] == [relationship_evidence["evidence"]["evidence_id"]]
    assert action_cards_by_key["resolve_relationship_graph_review_items"][
        "relationship_edge_ids"
    ] == [edge["edge_id"]]
    assert brief["manual_authorization_boundary"]["external_contact_sent"] is False
    assert brief["automated_business_conclusion"]["status"] == "not_generated"
    assert storage.load_ledger(tmp_path) == ledger_before_read
    with pytest.raises(ValueError, match="limit必须是1到200之间的整数"):
        action_center.get_investment_action_center(tmp_path, limit=201)
    with pytest.raises(ValueError, match="未找到对应的招商线索"):
        action_center.get_outreach_preparation_brief(tmp_path, lead_id="missing-lead")


@pytest.mark.asyncio
async def test_relationship_graph_read_api_allows_ordinary_user_state_filter_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """认证图谱查询接口允许普通用户按状态读取，且不改变关系或证据。"""
    relationship_graph_api = _load_plugin_module("relationship_graph_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业与徐州只读接口园区建立产业链配套关系。",
        ),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州只读接口园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为只读接口测试建立本地节点。",
        authenticated_user_id="setup-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=created["lead"]["lead_id"],
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为只读接口测试建立目标节点。",
        authenticated_user_id="setup-user",
    )["node"]
    relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[created["evidence"]["evidence_id"]],
        registration_reason="为只读接口测试登记等待复核的关系。",
        authenticated_user_id="setup-user",
    )
    ledger_before_read = copy.deepcopy(storage.load_ledger(tmp_path))
    ordinary_principal = Principal(
        user_id="ordinary-graph-reader",
        username="ordinary-graph-reader",
        role="user",
    )
    monkeypatch.setattr(
        relationship_graph_api,
        "get_current_principal",
        lambda _request: ordinary_principal,
    )

    async def fake_get_agent_for_request(_request: object) -> SimpleNamespace:
        """模拟平台已验证的当前普通用户工作区。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    monkeypatch.setattr(
        relationship_graph_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    response = await relationship_graph_api.get_xuzhou_relationship_graph(
        object(),
        lead_id=created["lead"]["lead_id"],
        review_state="待补证",
    )

    assert response["ok"] is True
    assert response["review_state_filter"] == "need_evidence"
    assert response["edge_count"] == 0
    assert response["review_state_summary"]["pending_user_review"] == 1
    assert storage.load_ledger(tmp_path) == ledger_before_read


def test_relationship_edge_evidence_supplement_is_append_only_and_visible_in_graph(
    tmp_path: Path,
) -> None:
    """关系边补证必须独立追加，既不覆盖原边证据也不改写原始证据链。"""
    core = _load_plugin_module("core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业与徐州补证测试园区开展产业链配套合作。",
        ),
    )
    supplemental_evidence = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/relationship-follow-up",
            source_title="企业官方合作补充公告",
            source_published_at="2026-08-25",
            excerpt=(
                "示例具身智能企业在官方公告中补充披露与徐州补证测试园区开展"
                "产业链配套合作，并继续推进具身智能产品研发。"
            ),
            claim_type="合作",
        ),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州补证测试园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[original["evidence"]["evidence_id"]],
        registration_reason="为关系补证测试建立本地节点。",
        authenticated_user_id="setup-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=original["lead"]["lead_id"],
        evidence_ids=[original["evidence"]["evidence_id"]],
        registration_reason="为关系补证测试建立目标线索节点。",
        authenticated_user_id="setup-user",
    )["node"]
    edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[original["evidence"]["evidence_id"]],
        registration_reason="为关系补证测试登记原始证据化关系。",
        authenticated_user_id="setup-user",
    )["edge"]
    ledger_before = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(ledger_before["evidence"])
    nodes_before = copy.deepcopy(ledger_before["relationship_graph_nodes"])
    edges_before = copy.deepcopy(ledger_before["relationship_graph_edges"])
    audit_before = copy.deepcopy(ledger_before["relationship_graph_audit"])

    appended = relationship_graph.append_relationship_edge_evidence_supplement(
        tmp_path,
        edge_id=edge["edge_id"],
        evidence_ids=[supplemental_evidence["evidence"]["evidence_id"]],
        supplement_note="新增同一目标线索的官方合作公告，作为原关系的补充核验材料。",
        authenticated_user_id="ordinary-supplement-user",
    )["supplement"]

    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == evidence_before
    assert ledger_after["relationship_graph_nodes"] == nodes_before
    assert ledger_after["relationship_graph_edges"] == edges_before
    assert ledger_after["relationship_graph_edges"][0]["evidence_ids"] == [
        original["evidence"]["evidence_id"]
    ]
    assert ledger_after["relationship_graph_edge_evidence_supplements"] == [appended]
    assert appended["supplemented_by_user_id"] == "ordinary-supplement-user"
    assert appended["operation_scope"] == "relationship_graph_evidence_supplement_append_only"
    assert ledger_after["relationship_graph_audit"][:-1] == audit_before
    assert ledger_after["relationship_graph_audit"][-1]["action"] == (
        "relationship_edge_evidence_supplemented"
    )

    graph = relationship_graph.get_relationship_graph(
        tmp_path,
        lead_id=original["lead"]["lead_id"],
    )
    assert storage.load_ledger(tmp_path) == ledger_after
    assert graph["edge_evidence_supplements"] == [appended]
    assert graph["evidence_reference_ids"] == sorted(
        [original["evidence"]["evidence_id"], supplemental_evidence["evidence"]["evidence_id"]]
    )
    edge_analysis = graph["industry_chain_analysis"]["edge_analysis"][0]
    assert edge_analysis["business_flow_direction"] == "not_declared"
    assert edge_analysis["evidence_support"]["original_edge_evidence_ids"] == [
        original["evidence"]["evidence_id"]
    ]
    assert edge_analysis["evidence_support"]["supplement_evidence_ids"] == [
        supplemental_evidence["evidence"]["evidence_id"]
    ]
    assert edge_analysis["evidence_support"]["level"] == "multiple_evidence_references"
    with pytest.raises(ValueError, match="不能重复引用"):
        relationship_graph.append_relationship_edge_evidence_supplement(
            tmp_path,
            edge_id=edge["edge_id"],
            evidence_ids=[original["evidence"]["evidence_id"]],
            supplement_note="故意重复原始关系边证据。",
            authenticated_user_id="ordinary-supplement-user",
        )


@pytest.mark.asyncio
async def test_relationship_edge_evidence_supplement_api_uses_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """关系边补证接口允许普通用户，但请求体不能伪造补证人或替换原始证据。"""
    relationship_graph_api = _load_plugin_module("relationship_graph_api")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    relationship_graph = importlib.import_module(f"{PACKAGE_NAME}.relationship_graph")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    original = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt="示例具身智能企业与徐州接口补证园区开展产业链配套合作。",
        ),
    )
    supplemental_evidence = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            source_url="https://example.com/news/api-relationship-follow-up",
            source_title="接口测试官方合作补充公告",
            source_published_at="2026-08-25",
            excerpt=(
                "示例具身智能企业补充披露与徐州接口补证园区开展产业链配套合作，"
                "并持续推进产品研发。"
            ),
            claim_type="合作",
        ),
    )
    local_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="徐州接口补证园区",
        node_kind="园区",
        locality="徐州本地",
        linked_lead_id=None,
        evidence_ids=[original["evidence"]["evidence_id"]],
        registration_reason="为补证接口测试建立本地节点。",
        authenticated_user_id="setup-user",
    )["node"]
    target_node = relationship_graph.register_relationship_node(
        tmp_path,
        display_name="示例具身智能企业",
        node_kind="企业",
        locality="目标或外地",
        linked_lead_id=original["lead"]["lead_id"],
        evidence_ids=[original["evidence"]["evidence_id"]],
        registration_reason="为补证接口测试建立目标节点。",
        authenticated_user_id="setup-user",
    )["node"]
    edge = relationship_graph.register_relationship_edge(
        tmp_path,
        from_node_id=local_node["node_id"],
        to_node_id=target_node["node_id"],
        relationship_type="产业链",
        evidence_ids=[original["evidence"]["evidence_id"]],
        registration_reason="为补证接口测试登记原始关系。",
        authenticated_user_id="setup-user",
    )["edge"]
    evidence_before = copy.deepcopy(storage.load_ledger(tmp_path)["evidence"])
    ordinary_principal = Principal(
        user_id="ordinary-api-supplement-user",
        username="ordinary-api-supplement-user",
        role="user",
    )
    monkeypatch.setattr(
        relationship_graph_api,
        "get_current_principal",
        lambda _request: ordinary_principal,
    )

    async def fake_get_agent_for_request(_request: object) -> SimpleNamespace:
        """模拟平台已验证工作区归属的普通用户，不注入管理员权限。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    monkeypatch.setattr(
        relationship_graph_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    response = await relationship_graph_api.append_xuzhou_relationship_edge_evidence_supplement(
        edge["edge_id"],
        relationship_graph_api.AppendRelationshipEdgeEvidenceSupplementRequest.model_validate(
            {
                "evidence_ids": [supplemental_evidence["evidence"]["evidence_id"]],
                "supplement_note": "普通用户追加同一目标线索的既有官方合作证据。",
            }
        ),
        object(),
    )

    assert response["supplement"]["supplemented_by_user_id"] == ordinary_principal.user_id
    assert storage.load_ledger(tmp_path)["evidence"] == evidence_before
    assert storage.load_ledger(tmp_path)["relationship_graph_edges"][0]["evidence_ids"] == [
        original["evidence"]["evidence_id"]
    ]
    with pytest.raises(ValidationError):
        relationship_graph_api.AppendRelationshipEdgeEvidenceSupplementRequest.model_validate(
            {
                "evidence_ids": [supplemental_evidence["evidence"]["evidence_id"]],
                "supplement_note": "试图伪造补证操作者。",
                "supplemented_by_user_id": "attacker-user",
            }
        )


def test_ledger_revision_rejects_stale_concurrent_save_without_losing_first_write(
    tmp_path: Path,
) -> None:
    """两个请求基于同一版本保存时，后者必须冲突而不能覆盖先写入的数据。"""
    storage = _load_plugin_module("storage")
    first_request_ledger = storage.load_ledger(tmp_path)
    stale_request_ledger = storage.load_ledger(tmp_path)
    first_request_ledger["leads"].append(
        {
            "lead_id": "lead-concurrency-test",
            "entity_name": "并发测试企业",
            "entity_kind": "enterprise",
            "sector": "embodied_intelligence",
            "review_state": "pending_user_review",
            "red_alert": False,
            "alert_reason": None,
            "created_at": "2026-08-27T00:00:00+00:00",
            "updated_at": "2026-08-27T00:00:00+00:00",
            "evidence_ids": [],
            "status_history": [],
        }
    )

    storage.save_ledger(tmp_path, first_request_ledger)

    with pytest.raises(RuntimeError, match="已被其他操作更新"):
        storage.save_ledger(tmp_path, stale_request_ledger)
    persisted = storage.load_ledger(tmp_path)
    assert persisted["ledger_revision"] == 1
    assert [lead["lead_id"] for lead in persisted["leads"]] == ["lead-concurrency-test"]


def test_industry_metric_drafts_require_signed_evidence_human_confirmation_and_parallel_reporting(
    tmp_path: Path,
) -> None:
    """经营指标只能由已签名证据提取，普通用户确认后仍须按不同口径并列展示。

    测试使用核心受控入证流程准备一条带原始快照的正式证据；自动提取只新增待复核草稿，
    再由普通用户确认其中的营业收入。整个过程必须保持 evidence、线索和草稿原文不变，
    审计身份来自函数的认证参数；报告只包含已确认的同口径序列，绝不把产值或项目投资额
    与营业收入相加。
    """
    industry_metrics = _load_plugin_module("industry_metrics")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    created = core.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            excerpt=(
                "2026年，示例具身智能企业实现营业收入1.5亿元，工业总产值2亿元；"
                "2026年该企业徐州项目总投资3亿元。"
            ),
            claim_type="产业方向",
        ),
    )
    before_generation = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(before_generation["evidence"])
    leads_before = copy.deepcopy(before_generation["leads"])

    generated = industry_metrics.generate_industry_metric_drafts(tmp_path)
    after_generation = storage.load_ledger(tmp_path)
    drafts_before_review = copy.deepcopy(after_generation["industry_metric_drafts"])

    assert generated["run"]["generated_draft_count"] == 3
    assert after_generation["evidence"] == evidence_before
    assert after_generation["leads"] == leads_before
    assert {draft["metric_kind"] for draft in drafts_before_review} == {
        "revenue",
        "output_value",
        "project_investment",
    }
    values_by_kind = {draft["metric_kind"]: draft["value_yuan"] for draft in drafts_before_review}
    assert values_by_kind == {
        "revenue": 150_000_000,
        "output_value": 200_000_000,
        "project_investment": 300_000_000,
    }
    assert all(draft["period_year"] == 2026 for draft in drafts_before_review)
    assert all(draft["state"] == "pending_user_review" for draft in drafts_before_review)
    assert all(draft["evidence_id"] == created["evidence"]["evidence_id"] for draft in drafts_before_review)

    revenue_draft = next(draft for draft in drafts_before_review if draft["metric_kind"] == "revenue")
    reviewed = industry_metrics.review_industry_metric_draft(
        tmp_path,
        metric_draft_id=revenue_draft["metric_draft_id"],
        decision="确认",
        review_note="普通登录用户已核对原始摘录、年份、单位和金额，确认按营业收入口径入表。",
        authenticated_user_id="ordinary-metric-reviewer",
    )
    after_review = storage.load_ledger(tmp_path)
    assert reviewed["metric_draft_review"]["old_state"] == "pending_user_review"
    assert reviewed["metric_draft_review"]["new_state"] == "confirmed"
    assert reviewed["metric_draft_review"]["reviewed_by_user_id"] == "ordinary-metric-reviewer"
    assert reviewed["metric_draft_review"]["operation_scope"] == (
        "authenticated_user_industry_metric_draft_review_only"
    )
    assert reviewed["confirmed_metric"]["value_yuan"] == 150_000_000
    assert after_review["evidence"] == evidence_before
    assert after_review["leads"] == leads_before
    assert after_review["industry_metric_drafts"] == drafts_before_review
    assert len(after_review["industry_metric_draft_reviews"]) == 1
    assert len(after_review["industry_business_metrics"]) == 1

    report = industry_metrics.get_industry_business_metrics_report(tmp_path, window_years=5)
    assert report["mixed_metric_rule"]["mode"] == "parallel_by_metric_kind_and_scope"
    assert report["mixed_metric_rule"]["forbidden_aggregation"] == [
        "revenue",
        "output_value",
        "project_investment",
    ]
    assert report["confirmed_metric_count"] == 1
    assert report["pending_user_review_draft_count"] == 2
    assert report["series"][0]["metric_kind"] == "revenue"
    assert report["series"][0]["records"][0]["value_yuan"] == 150_000_000

    # 标准研判报告只能带入已确认且仍属于当前线索证据链的营业收入记录；尚未复核的产值
    # 与项目投资额不应被展示或相加。读取报告前后台账必须逐字保持不变。
    reports = _load_plugin_module("reports")
    before_assessment = copy.deepcopy(storage.load_ledger(tmp_path))
    assessment = reports.get_investment_assessment_report(
        tmp_path,
        lead_id=created["lead"]["lead_id"],
    )
    metric_section = assessment["industry_business_metrics_assessment"]
    assert assessment["report_type"] == "xuzhou_investment_assessment_readonly_v6"
    assert metric_section["section_key"] == "confirmed_industry_business_metrics"
    assert metric_section["status"] == "evidence_available"
    assert metric_section["recorded_confirmed_metric_count"] == 1
    assert metric_section["metric_kind_counts"] == {"revenue": 1}
    assert metric_section["records"] == [
        {
            "metric_id": reviewed["confirmed_metric"]["metric_id"],
            "metric_draft_id": revenue_draft["metric_draft_id"],
            "evidence_id": created["evidence"]["evidence_id"],
            "metric_kind": "revenue",
            "metric_label": "营业收入",
            "source_metric_label": "营业收入",
            "statistic_scope": "enterprise",
            "period_year": 2026,
            "value_yuan": 150_000_000,
            "original_value_text": "1.5亿元",
            "confirmed_at": reviewed["metric_draft_review"]["reviewed_at"],
        }
    ]
    assert metric_section["evidence_reference_ids"] == [created["evidence"]["evidence_id"]]
    assert metric_section["mixed_metric_rule"]["forbidden_aggregation"] == [
        "revenue",
        "output_value",
        "project_investment",
    ]
    assert storage.load_ledger(tmp_path) == before_assessment

    # 第一批闭环把待复核指标接入行动中心、把草稿/确认/复核活动接入日常统计，但二者均
    # 只能读取已有台账。已确认营收不应再排入指标复核待办，剩余两条草稿需保留正式证据
    # 卡片，且统计不得把三类指标金额相加或表述为招商成果。
    action_center = _load_plugin_module("action_center")
    before_center_and_summary = copy.deepcopy(storage.load_ledger(tmp_path))
    center = action_center.get_investment_action_center(tmp_path, limit=100)
    work_summary = reports.get_investment_work_summary(tmp_path, window_days=30)
    metric_action_items = [
        item
        for item in center["items"]
        if item["item_kind"] == "industry_metric_draft_manual_review"
    ]
    assert center["center_type"] == "xuzhou_investment_action_center_readonly_v4"
    assert center["summary"]["pending_industry_metric_draft_review_count"] == 2
    assert center["summary"]["excluded_invalid_or_untraceable_metric_draft_count"] == 0
    assert {item["metric_draft"]["metric_kind"] for item in metric_action_items} == {
        "output_value",
        "project_investment",
    }
    assert all(
        item["evidence_card"]["evidence_id"] == created["evidence"]["evidence_id"]
        for item in metric_action_items
    )
    assert all(item["automatic_business_conclusion"] == "not_generated" for item in metric_action_items)
    assert work_summary["report_type"] == "xuzhou_investment_work_summary_readonly_v5"
    assert work_summary["current_totals"]["industry_metric_draft_count"] == 3
    assert work_summary["current_totals"]["pending_user_review_industry_metric_draft_count"] == 2
    assert work_summary["current_totals"]["confirmed_industry_business_metric_count"] == 1
    assert work_summary["industry_metric_summary"] == {
        "draft_review_state_counts": {"confirmed": 1, "pending_user_review": 2},
        "pending_user_review_draft_count": 2,
        "confirmed_metric_count": 1,
        "mixed_metric_rule": {
            "mode": "parallel_by_metric_kind_and_scope",
            "forbidden_aggregation": ["revenue", "output_value", "project_investment"],
            "reason": "工作统计只计数，不汇总营业收入、产值和项目投资额，三类口径不得相加。",
        },
        "report_cross_reference": {
            "assessment_report_field": "industry_business_metrics_assessment",
            "reason": "单条线索的已确认指标及其 evidence_id 应在标准研判报告中查看；本汇总不复制或修改原始证据。",
        },
    }
    funnel_stages = {
        item["state"]: item["current_lead_count"]
        for item in work_summary["lead_review_funnel"]["current_state_stages"]
    }
    assert work_summary["lead_review_funnel"]["entry_lead_count"] == 1
    assert funnel_stages["pending_user_review"] == 1
    assert any(
        item["industry_metric_review_actions"] == 1
        and item["confirmed_industry_business_metrics"] == 1
        for item in work_summary["daily_activity"]
    )
    assert storage.load_ledger(tmp_path) == before_center_and_summary

    before_repeat = copy.deepcopy(storage.load_ledger(tmp_path))
    with pytest.raises(ValueError, match="已经完成业务复核"):
        industry_metrics.review_industry_metric_draft(
            tmp_path,
            metric_draft_id=revenue_draft["metric_draft_id"],
            decision="无效",
            review_note="不允许覆盖已经追加的确认审计。",
            authenticated_user_id="ordinary-metric-reviewer-2",
        )
    assert storage.load_ledger(tmp_path) == before_repeat


@pytest.mark.asyncio
async def test_industry_metric_review_api_uses_verified_identity_and_rejects_metric_fact_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """指标复核接口只能接收决定和备注，用户身份与指标事实均不能由客户端伪造。"""
    industry_metrics_api = _load_plugin_module("industry_metrics_api")
    verified_principal = Principal(
        user_id="ordinary-metric-api-user",
        username="ordinary-metric-api-user",
        role="user",
    )
    received: dict[str, object] = {}
    monkeypatch.setattr(
        industry_metrics_api,
        "get_current_principal",
        lambda _request: verified_principal,
    )

    async def _owned_workspace(_request: object) -> SimpleNamespace:
        """模拟通过平台归属验证的普通用户工作区，不访问真实认证或磁盘外路径。"""
        return SimpleNamespace(workspace_dir=tmp_path)

    def _fake_metric_review(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录路由转交的受限字段，验证路由没有接受或拼接客户端事实。"""
        received["workspace_dir"] = workspace_dir
        received.update(kwargs)
        return {"metric_draft_review": {"new_state": "confirmed"}, "confirmed_metric": {}}

    monkeypatch.setattr(
        industry_metrics_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(industry_metrics_api, "review_industry_metric_draft", _fake_metric_review)
    response = await industry_metrics_api.review_xuzhou_industry_metric_draft(
        "industry-metric-draft-api-test",
        industry_metrics_api.ReviewIndustryMetricDraftRequest(
            decision="确认",
            review_note="普通用户只确认既有草稿，不能提交或修改指标事实。",
        ),
        object(),
    )

    assert response["ok"] is True
    assert received["workspace_dir"] == tmp_path
    assert received["authenticated_user_id"] == "ordinary-metric-api-user"
    assert received["decision"] == "确认"
    assert "reviewer_user_id" not in received
    with pytest.raises(ValidationError):
        industry_metrics_api.ReviewIndustryMetricDraftRequest.model_validate(
            {
                "decision": "确认",
                "review_note": "尝试伪造操作者和经营事实。",
                "reviewer_user_id": "attacker-user",
                "authenticated_user_id": "attacker-user",
                "value_yuan": 999_999_999,
                "period_year": 2026,
                "entity_name": "伪造企业",
                "source_url": "https://attacker.example/forged",
                "excerpt": "伪造原始摘录",
            }
        )


def test_key_person_relationship_drafts_are_deterministic_reviewable_and_preserve_evidence_chain(
    core_module, tmp_path: Path
) -> None:
    """公开人员角色只能生成待复核草稿，普通用户复核也不能改写原始证据链。

    输入是一条已由核心服务签名的企业正式证据，摘录在同一句明确出现组织、法人姓名、角色
    和徐州关联措辞；输出验证固定规则生成一个去重草稿、徐州关联仅被标为宽泛候选，且普通
    用户的“可登记图谱”仅追加审计。测试逐字比对线索、证据及草稿内容，确保没有 URL、
    原始摘录、采集时间、评分或自动图谱边被复核操作改变。
    """

    storage = _load_plugin_module("storage")
    relationship_draft = _load_plugin_module("relationship_draft")
    created = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="徐州智造科技有限公司",
            excerpt=(
                "徐州智造科技有限公司公开公告称，法定代表人为张三，张三为徐州籍创业者，"
                "公司将继续开展具身智能研发。"
            ),
        ),
    )
    before_generation = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(before_generation["evidence"])
    leads_before = copy.deepcopy(before_generation["leads"])

    generated = relationship_draft.generate_relationship_drafts_from_signed_evidence(tmp_path)
    after_generation = storage.load_ledger(tmp_path)
    assert generated["run"]["generated_draft_count"] == 1
    assert after_generation["evidence"] == evidence_before
    assert after_generation["leads"] == leads_before
    assert after_generation["relationship_graph_nodes"] == []
    assert after_generation["relationship_graph_edges"] == []
    draft = after_generation["relationship_drafts"][0]
    assert draft["lead_id"] == created["lead"]["lead_id"]
    assert draft["evidence_id"] == created["evidence"]["evidence_id"]
    assert draft["organization_name"] == "徐州智造科技有限公司"
    assert draft["person_display_name"] == "张三"
    assert draft["relationship_type"] == "legal_representative"
    assert draft["person_locality_candidate"] == "xuzhou_related"
    assert draft["matched_xuzhou_term"] == "徐州籍"
    assert draft["state"] == "pending_user_review"

    repeated = relationship_draft.generate_relationship_drafts_from_signed_evidence(tmp_path)
    assert repeated["run"]["generated_draft_count"] == 0
    assert repeated["run"]["skipped_duplicate_count"] == 1

    review = relationship_draft.review_relationship_draft(
        tmp_path,
        relationship_draft_id=draft["relationship_draft_id"],
        decision="可登记图谱",
        review_note="已核对原始公开摘录；后续仍需经图谱接口再次核验后再登记。",
        authenticated_user_id="ordinary-key-person-reviewer",
    )
    after_review = storage.load_ledger(tmp_path)
    assert review["relationship_draft"]["review_state"] == "ready_for_graph_registration"
    assert after_review["evidence"] == evidence_before
    assert after_review["leads"] == leads_before
    assert after_review["relationship_drafts"] == after_generation["relationship_drafts"]
    assert after_review["relationship_graph_nodes"] == []
    assert after_review["relationship_graph_edges"] == []
    audit = after_review["relationship_draft_reviews"][-1]
    assert audit["old_state"] == "pending_user_review"
    assert audit["new_state"] == "ready_for_graph_registration"
    assert audit["reviewed_by_user_id"] == "ordinary-key-person-reviewer"

    # “可登记图谱”是人工明确的下一步，不应在草稿复核时自动执行；行动中心只读显示
    # 单独待办。实际登记时服务器只能使用草稿绑定的既有证据，生成的关系边仍待复核。
    action_center = _load_plugin_module("action_center")
    pending_center = action_center.get_investment_action_center(tmp_path, limit=50)
    assert any(
        item["item_kind"] == "relationship_draft_graph_registration"
        and item["relationship_draft"]["relationship_draft_id"] == draft["relationship_draft_id"]
        for item in pending_center["items"]
    )
    relationship_graph = _load_plugin_module("relationship_graph")
    registered = relationship_graph.register_approved_relationship_draft_to_graph(
        tmp_path,
        relationship_draft_id=draft["relationship_draft_id"],
        authenticated_user_id="ordinary-graph-registration-user",
    )
    after_registration = storage.load_ledger(tmp_path)
    assert registered["organization_node_created"] is True
    assert registered["person_node_created"] is True
    assert registered["edge_created"] is True
    assert registered["edge"]["review_state"] == "pending_user_review"
    assert after_registration["evidence"] == evidence_before
    assert after_registration["leads"] == leads_before
    assert after_registration["relationship_drafts"] == after_generation["relationship_drafts"]
    assert after_registration["relationship_draft_reviews"] == after_review["relationship_draft_reviews"]
    assert after_registration["relationship_graph_audit"][-1]["action"] == (
        "approved_relationship_draft_registered_to_graph"
    )
    assert after_registration["relationship_graph_audit"][-1]["operated_by_user_id"] == (
        "ordinary-graph-registration-user"
    )
    with pytest.raises(ValueError, match="已经登记到图谱"):
        relationship_graph.register_approved_relationship_draft_to_graph(
            tmp_path,
            relationship_draft_id=draft["relationship_draft_id"],
            authenticated_user_id="ordinary-graph-registration-user",
        )

    public_draft = relationship_draft.list_relationship_drafts(tmp_path)["relationship_drafts"][0]
    assert public_draft["graph_registration_state"] == "registered_pending_relationship_review"
    completed_center = action_center.get_investment_action_center(tmp_path, limit=50)
    assert not any(
        item["item_kind"].startswith("relationship_draft_") for item in completed_center["items"]
    )
    assert any(
        item["item_kind"] == "relationship_manual_review" for item in completed_center["items"]
    )
    reports = _load_plugin_module("reports")
    work_summary = reports.get_investment_work_summary(tmp_path, window_days=30)
    assert work_summary["current_totals"]["key_person_relationship_draft_count"] == 1
    assert work_summary["current_totals"]["pending_key_person_relationship_draft_action_count"] == 0
    assert work_summary["current_totals"]["registered_key_person_relationship_draft_count"] == 1
    assert work_summary["key_person_relationship_draft_summary"]["registered_to_graph_draft_count"] == 1


@pytest.mark.asyncio
async def test_key_person_relationship_draft_review_api_uses_verified_identity_and_forbids_claimed_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """关系草稿 API 必须允许普通用户复核，但不能接受客户端伪造的审核人或证据字段。

    输入是认证主体、最小受控请求与归属工作区替身；输出验证核心层只收到服务器
    ``principal.user_id``，不需要管理员角色。额外提交 ``reviewer_user_id``、证据或草稿
    内容会被请求模型拒绝，因此接口不能让模型/前端覆盖原始证据链或自动登记关系边。
    """

    relationship_draft_api = _load_plugin_module("relationship_draft_api")
    verified_principal = SimpleNamespace(user_id="verified-ordinary-user", role="user")
    captured: dict[str, object] = {}

    async def _owned_workspace(_request: object) -> SimpleNamespace:
        """返回当前用户已经由平台授权的临时工作区，不提供跨用户路径。"""

        return SimpleNamespace(workspace_dir=tmp_path)

    def _fake_review(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录 API 传入值，证明身份由认证主体而非客户端请求体决定。"""

        captured["workspace_dir"] = workspace_dir
        captured.update(kwargs)
        return {"relationship_draft": {"review_state": "observe"}, "review": {}}

    monkeypatch.setattr(
        relationship_draft_api, "get_current_principal", lambda _request: verified_principal
    )
    monkeypatch.setattr(
        relationship_draft_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(relationship_draft_api, "review_relationship_draft", _fake_review)
    response = await relationship_draft_api.review_xuzhou_relationship_draft(
        "relationship-draft-1234abcd",
        relationship_draft_api.ReviewRelationshipDraftRequest(
            decision="观察", review_note="普通用户仅追加业务判断。"
        ),
        SimpleNamespace(),
    )
    assert response["ok"] is True
    assert captured["workspace_dir"] == tmp_path.resolve()
    assert captured["authenticated_user_id"] == "verified-ordinary-user"
    assert captured["relationship_draft_id"] == "relationship-draft-1234abcd"
    with pytest.raises(ValidationError):
        relationship_draft_api.ReviewRelationshipDraftRequest.model_validate(
            {
                "decision": "观察",
                "review_note": "攻击者试图伪造审核人并修改证据。",
                "reviewer_user_id": "attacker-user",
                "evidence_id": "evidence-attacker",
            }
        )


@pytest.mark.asyncio
async def test_approved_relationship_draft_graph_registration_api_uses_verified_identity_without_client_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """草稿登记图谱接口只允许认证普通用户提交服务器草稿 ID，不能注入关系事实。

    输入为认证主体与已归属工作区替身；输出验证路由向核心只传递路径中的服务器草稿 ID 和
    ``principal.user_id``。接口没有请求体，因而客户端不能伪造人员、组织、角色、证据、URL、
    摘录或审核人；核心仍会在真实执行时重新校验已签名 evidence 并创建待复核关系边。
    """

    relationship_draft_api = _load_plugin_module("relationship_draft_api")
    verified_principal = SimpleNamespace(user_id="verified-graph-user", role="user")
    received: dict[str, object] = {}

    async def _owned_workspace(_request: object) -> SimpleNamespace:
        """模拟平台已验证的当前用户工作区，不提供跨用户路径。"""

        return SimpleNamespace(workspace_dir=tmp_path)

    def _fake_registration(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录被路由转交的最小字段，验证不存在客户端可控关系事实。"""

        received["workspace_dir"] = workspace_dir
        received.update(kwargs)
        return {"edge": {"review_state": "pending_user_review"}}

    monkeypatch.setattr(
        relationship_draft_api, "get_current_principal", lambda _request: verified_principal
    )
    monkeypatch.setattr(
        relationship_draft_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(
        relationship_draft_api,
        "register_approved_relationship_draft_to_graph",
        _fake_registration,
    )
    response = await relationship_draft_api.register_xuzhou_approved_relationship_draft_to_graph(
        "relationship-draft-graph-api-test", SimpleNamespace()
    )
    assert response["ok"] is True
    assert received == {
        "workspace_dir": tmp_path.resolve(),
        "relationship_draft_id": "relationship-draft-graph-api-test",
        "authenticated_user_id": "verified-graph-user",
    }


def test_co_investment_signals_are_org_level_reviewable_and_preserve_evidence_chain(
    core_module, tmp_path: Path
) -> None:
    """协同投资信号只能来自一手正式证据，普通用户复核也不能形成个人资金池。

    输入是一条同句明确出现目标企业和联合投资文本的一手正式证据；输出验证固定规则只
    生成待复核的组织级草稿，草稿和“可核验对接”审计均不会改写线索、证据、来源、评分
    或建立实际联系。测试还检查行动中心与工作统计只显示人工核验待办，不将其当作资金
    承诺、实际投资金额或招商推进结论。
    """

    storage = _load_plugin_module("storage")
    co_investment_signal = _load_plugin_module("co_investment_signal")
    created = core_module.record_public_evidence(
        tmp_path,
        **_evidence_arguments(
            entity_name="徐州协同资本科技有限公司",
            source_title="徐州协同资本科技有限公司官方公告",
            claim_type="基金募资",
            excerpt=(
                "徐州协同资本科技有限公司官方公告称，公司正与产业方共同设立基金，"
                "用于具身智能项目的长期产业协同。"
            ),
            xuzhou_association_tags=["业务关联"],
        ),
    )
    before_generation = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(before_generation["evidence"])
    leads_before = copy.deepcopy(before_generation["leads"])
    scores_before = copy.deepcopy(before_generation["score_snapshots"])

    generated = co_investment_signal.generate_co_investment_signal_drafts_from_signed_evidence(
        tmp_path
    )
    after_generation = storage.load_ledger(tmp_path)
    assert generated["run"]["generated_draft_count"] == 1
    assert after_generation["evidence"] == evidence_before
    assert after_generation["leads"] == leads_before
    assert after_generation["score_snapshots"] == scores_before
    draft = after_generation["co_investment_signal_drafts"][0]
    assert draft["lead_id"] == created["lead"]["lead_id"]
    assert draft["evidence_id"] == created["evidence"]["evidence_id"]
    assert draft["signal_type"] == "fund_formation"
    assert draft["matched_terms"] == ["共同设立基金"]
    assert draft["state"] == "pending_user_review"
    assert "个人" not in draft
    assert "金额" not in draft

    repeated = co_investment_signal.generate_co_investment_signal_drafts_from_signed_evidence(
        tmp_path
    )
    assert repeated["run"]["generated_draft_count"] == 0
    assert repeated["run"]["skipped_duplicate_count"] == 1

    reviewed = co_investment_signal.review_co_investment_signal_draft(
        tmp_path,
        co_investment_draft_id=draft["co_investment_draft_id"],
        decision="可核验对接",
        review_note="已核对官方原文；仅继续人工核验组织主体和授权联系人。",
        authenticated_user_id="ordinary-co-investment-reviewer",
    )
    after_review = storage.load_ledger(tmp_path)
    assert reviewed["co_investment_signal_draft"]["review_state"] == (
        "ready_for_contact_validation"
    )
    assert after_review["evidence"] == evidence_before
    assert after_review["leads"] == leads_before
    assert after_review["score_snapshots"] == scores_before
    assert after_review["co_investment_signal_drafts"] == after_generation[
        "co_investment_signal_drafts"
    ]
    audit = after_review["co_investment_signal_draft_reviews"][-1]
    assert audit["old_state"] == "pending_user_review"
    assert audit["new_state"] == "ready_for_contact_validation"
    assert audit["reviewed_by_user_id"] == "ordinary-co-investment-reviewer"

    action_center = _load_plugin_module("action_center")
    center = action_center.get_investment_action_center(tmp_path, limit=50)
    signal_items = [
        item
        for item in center["items"]
        if item["item_kind"] == "co_investment_signal_contact_validation"
    ]
    assert len(signal_items) == 1
    assert signal_items[0]["lead"]["lead_id"] == created["lead"]["lead_id"]
    assert signal_items[0]["action"] == (
        "validate_public_organization_contact_and_authorization_manually"
    )
    assert signal_items[0]["automatic_business_conclusion"] == "not_generated"

    reports = _load_plugin_module("reports")
    summary = reports.get_investment_work_summary(tmp_path, window_days=30)
    assert summary["current_totals"]["co_investment_signal_draft_count"] == 1
    assert summary["current_totals"]["pending_co_investment_signal_draft_action_count"] == 1
    assert summary["co_investment_signal_draft_summary"]["draft_review_state_counts"] == {
        "ready_for_contact_validation": 1
    }
    assert storage.load_ledger(tmp_path)["evidence"] == evidence_before


@pytest.mark.asyncio
async def test_co_investment_signal_review_api_uses_verified_identity_and_forbids_financial_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """协同投资信号 API 只能接收人工判断，不能让客户端注入用户或个人金融信息。

    输入是已认证普通用户、最小请求模型与已验证工作区替身；输出验证核心只取得路径中的
    服务器草稿 ID、决定、备注和 ``principal.user_id``。请求模型拒绝用户 ID、个人姓名、
    资产、金额、联系方式及证据字段，故接口不能被用来建立个人资金池或改写原始证据链。
    """

    co_investment_signal_api = _load_plugin_module("co_investment_signal_api")
    verified_principal = SimpleNamespace(user_id="verified-co-investment-user", role="user")
    received: dict[str, object] = {}

    async def _owned_workspace(_request: object) -> SimpleNamespace:
        """返回当前普通用户已获平台授权的临时工作区，不提供跨用户路径。"""

        return SimpleNamespace(workspace_dir=tmp_path)

    def _fake_review(workspace_dir: Path, **kwargs: object) -> dict[str, object]:
        """记录路由转交的受控字段，验证没有拼接客户端金融事实。"""

        received["workspace_dir"] = workspace_dir
        received.update(kwargs)
        return {"co_investment_signal_draft": {"review_state": "observe"}, "review": {}}

    monkeypatch.setattr(
        co_investment_signal_api, "get_current_principal", lambda _request: verified_principal
    )
    monkeypatch.setattr(
        co_investment_signal_api,
        "shared_ledger_dir_for_request",
        lambda _request: tmp_path.resolve(),
    )
    monkeypatch.setattr(
        co_investment_signal_api, "review_co_investment_signal_draft", _fake_review
    )
    response = await co_investment_signal_api.review_xuzhou_co_investment_signal_draft(
        "co-investment-draft-api-test",
        co_investment_signal_api.ReviewCoInvestmentSignalDraftRequest(
            decision="观察", review_note="仅保留组织级公开信号，暂不继续人工核验。"
        ),
        SimpleNamespace(),
    )
    assert response["ok"] is True
    assert received == {
        "workspace_dir": tmp_path.resolve(),
        "co_investment_draft_id": "co-investment-draft-api-test",
        "decision": "观察",
        "review_note": "仅保留组织级公开信号，暂不继续人工核验。",
        "authenticated_user_id": "verified-co-investment-user",
    }
    with pytest.raises(ValidationError):
        co_investment_signal_api.ReviewCoInvestmentSignalDraftRequest.model_validate(
            {
                "decision": "可核验对接",
                "review_note": "攻击者试图写入个人金融信息。",
                "reviewer_user_id": "attacker-user",
                "person_name": "张三",
                "available_capital_yuan": 1_000_000,
                "phone": "13800000000",
                "evidence_id": "forged-evidence",
            }
        )


def test_policy_blocked_domains_do_not_starve_observation_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """被政策禁止的域名不得占用每轮观察名额，较新的合格候选必须轮到首次观察。

    输入是三条发现时间最老、所在域名已被标记为 ``policy_blocked`` 的候选和一条较新的
    正常候选，以及打桩的 robots 与页面响应。输出验证：被封域名不再被选中或尝试（观察
    次数保持为零、不产生快照），新候选完成首次观察并保存不可覆盖快照；正式证据、线索
    与评分逐字不变。回归背景：被封候选永远排在最前，若在选择阶段不排除，每轮三个不同
    域名的预算会被它们占满，其他候选永远观察不到。
    """
    observation = _load_plugin_module("candidate_observation")
    core = importlib.import_module(f"{PACKAGE_NAME}.core")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    core.record_public_evidence(tmp_path, **_evidence_arguments())
    ledger = storage.load_ledger(tmp_path)
    evidence_before = copy.deepcopy(ledger["evidence"])
    leads_before = copy.deepcopy(ledger["leads"])
    scores_before = copy.deepcopy(ledger["score_snapshots"])

    def _candidate(candidate_id: str, url: str, discovered_at: str) -> dict:
        """构造通过范围筛选的候选，以隔离本测试的来源退避排队行为。"""
        return {
            "candidate_id": candidate_id,
            "run_id": "discovery-run-starvation-test",
            "source_url": url,
            "source_title": "服务器发现的候选页面",
            "search_snippet": "搜索摘要不是事实。",
            "sector": "semiconductor",
            "source_catalog_id": "public_early_signal",
            "discovery_query": "服务器生成的测试查询",
            "discovered_at": discovered_at,
            "source_type": "public_search_index",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            "eligibility_assessment": _eligible_candidate_scope_assessment(),
        }

    blocked_domains = (
        "blocked-a.example.com",
        "blocked-b.example.com",
        "blocked-c.example.com",
    )
    ledger["discovery_candidates"] = [
        _candidate(
            f"blocked-candidate-{index}",
            f"https://{domain}/page",
            f"2026-08-27T04:00:0{index}+00:00",
        )
        for index, domain in enumerate(blocked_domains)
    ]
    ledger["discovery_candidates"].append(
        _candidate(
            "eligible-candidate-new",
            "https://eligible.example.com/news",
            "2026-09-04T00:00:00+00:00",
        )
    )
    for domain in blocked_domains:
        ledger["source_health"].append(
            {
                "source_key": f"public_page_domain:{domain}",
                "status": "policy_blocked",
                "consecutive_failures": 1,
                "last_success_at": None,
                "last_failure_at": "2026-09-01T00:00:00+00:00",
                "last_error_kind": "policy_blocked",
                "last_error": "robots_disallow",
                "next_eligible_at": None,
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )
    storage.save_ledger(tmp_path, ledger)

    raw_content = " eligible 企业发布的普通公开新闻。".encode("utf-8")
    monkeypatch.setattr(
        observation,
        "check_robots_access",
        lambda _source_url: {"allowed": True, "reason": "robots_allow"},
    )
    monkeypatch.setattr(
        observation,
        "fetch_public_source_for_observation",
        lambda _source_url: {
            "source_url": "https://eligible.example.com/news",
            "source_title": "合格候选页面",
            "content_type": "text/html",
            "visible_text": " eligible 企业发布的普通公开新闻。",
            "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
            "byte_size": len(raw_content),
            "raw_content": raw_content,
        },
    )

    result = observation.observe_discovery_candidates(tmp_path)

    assert result["run"]["status"] == "completed"
    assert result["run"]["observation_count"] == 1
    ledger_after = storage.load_ledger(tmp_path)
    assert ledger_after["evidence"] == evidence_before
    assert ledger_after["leads"] == leads_before
    assert ledger_after["score_snapshots"] == scores_before
    candidates_after = {
        candidate["candidate_id"]: candidate
        for candidate in ledger_after["discovery_candidates"]
    }
    for index in range(3):
        blocked_candidate = candidates_after[f"blocked-candidate-{index}"]
        assert "observation_attempts" not in blocked_candidate
        assert blocked_candidate["state"] == "discovered"
    eligible_candidate = candidates_after["eligible-candidate-new"]
    assert eligible_candidate["observation_attempts"] == 1
    assert ledger_after["candidate_observations"][0]["state"] == "pending_evidence_verification"


def test_discovery_rejects_foreign_results_without_creating_candidate_or_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """海外无中国语境页面只能留过滤审计，不能进入招商候选或证据链。

    输入同时模拟一个英国英文采购站和一个有徐州、企业与合作信号的中国候选。输出验证前
    者只保存“摘要不足”的审计原因，后者才成为待补证候选；二者均不直接写成企业事实。
    该回归测试覆盖 RS Components 一类页面，防止以后仅因行业搜索词命中而重新入队。
    """
    discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "RS Components | Electronic and Electrical Components",
                "source_url": "https://uk.rs-online.com/components",
                "search_snippet": "UK online catalogue, delivery and payment information.",
            },
            {
                "source_title": "徐州具身智能科技有限公司寻找产业合作伙伴",
                "source_url": "https://example.cn/news/xuzhou-robotics-partnership",
                "search_snippet": "徐州企业披露具身智能机器人产业合作与扩产计划。",
            },
        ],
    )

    result = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=1,
        max_results_per_query=2,
    )

    assert result["run"]["candidate_count"] == 1
    assert result["run"]["rejected_result_count"] == 1
    rejected = result["run"]["rejected_results"][0]
    assert rejected["source_url"] == "https://uk.rs-online.com/components"
    assert "missing_china_or_xuzhou_context" in rejected["eligibility_assessment"][
        "rejection_reasons"
    ]
    ledger = storage.load_ledger(tmp_path)
    assert [candidate["source_url"] for candidate in ledger["discovery_candidates"]] == [
        "https://example.cn/news/xuzhou-robotics-partnership"
    ]


def test_discovery_prefilter_rejects_known_non_business_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """认证与百科域名只留操作审计，正常中国企业页面仍需通过正式资格门槛。"""
    discovery = _load_plugin_module("source_discovery")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    monkeypatch.setattr(
        discovery,
        "fetch_public_search_feed",
        lambda _query: [
            {
                "source_title": "支付宝认证入口",
                "source_url": "https://auth.alipay.com/login",
                "search_snippet": "账户认证与登录服务。",
            },
            {
                "source_title": "具身智能 - 百度百科",
                "source_url": "https://baike.baidu.com/item/robotics",
                "search_snippet": "百科词条。",
            },
            {
                "source_title": "徐州机器人企业完成融资并扩大产线",
                "source_url": "https://example.cn/news/robotics-financing",
                "search_snippet": "中国徐州企业披露融资、扩产与产业合作计划。",
            },
        ],
    )

    result = discovery.discover_public_sources(
        tmp_path,
        sector="具身智能",
        max_queries=1,
        max_results_per_query=3,
    )

    assert result["run"]["candidate_count"] == 1
    assert result["run"]["rejected_result_count"] == 2
    rejected = result["run"]["rejected_results"]
    assert {item["source_url"] for item in rejected} == {
        "https://auth.alipay.com/login",
        "https://baike.baidu.com/item/robotics",
    }
    assert all(
        item["eligibility_assessment"]["rejection_reasons"]
        == ["excluded_known_non_business_or_authentication_source"]
        for item in rejected
    )
    ledger = storage.load_ledger(tmp_path)
    assert [candidate["source_url"] for candidate in ledger["discovery_candidates"]] == [
        "https://example.cn/news/robotics-financing"
    ]
    assert ledger["evidence"] == []
    assert ledger["evidence"] == []


def test_legacy_out_of_scope_snapshot_is_hidden_and_audited_without_mutation(
    tmp_path: Path,
) -> None:
    """旧版无范围评估快照退出队列时，原始候选和哈希必须逐字保留。

    输入是一个没有新门槛评估字段的历史境外候选和已保存原始快照。列表应立即隐藏它，
    后台观察则只追加候选及快照两条范围排除审计；输出逐字比对原候选、观察和快照哈希，
    防止“清理无关项”错误变成删除或覆盖证据材料。
    """
    observation = _load_plugin_module("candidate_observation")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    candidate_id = "legacy-foreign-candidate"
    observation_id = "legacy-foreign-observation"
    raw_content = b"UK catalogue page without China or Xuzhou business context"
    raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    snapshot_path = storage.save_candidate_observation_snapshot(
        tmp_path,
        observation_id=observation_id,
        payload={
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": "https://uk.example.com/catalogue",
            "source_title": "UK catalogue",
            "content_type": "text/html",
            "collected_at": "2026-09-02T00:00:00+00:00",
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_content),
            "visible_text": raw_content.decode("utf-8"),
            "raw_content_base64": base64.b64encode(raw_content).decode("ascii"),
            "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
        },
    )
    ledger = storage.load_ledger(tmp_path)
    legacy_candidate = {
        "candidate_id": candidate_id,
        "run_id": "legacy-run",
        "source_url": "https://uk.example.com/catalogue",
        "source_title": "UK catalogue",
        "search_snippet": "English catalogue page.",
        "sector": "semiconductor",
        "source_catalog_id": "public_early_signal",
        "discovery_query": "legacy query",
        "discovered_at": "2026-09-02T00:00:00+00:00",
        "source_type": "public_search_index",
        "source_level": "lead_only",
        "state": "discovered",
        "capture_attempts": 0,
        "captured_evidence_id": None,
        "last_capture_error": None,
        "last_captured_at": None,
    }
    legacy_observation = {
        "observation_id": observation_id,
        "candidate_id": candidate_id,
        "source_url": "https://uk.example.com/catalogue",
        "source_title": "UK catalogue",
        "content_type": "text/html",
        "collected_at": "2026-09-02T00:00:00+00:00",
        "raw_sha256": raw_sha256,
        "byte_size": len(raw_content),
        "snapshot_relative_path": str(snapshot_path.relative_to(tmp_path)),
        "signals": [],
        "review_priority": "normal_review",
        "state": "pending_evidence_verification",
        "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
    }
    ledger["discovery_candidates"] = [copy.deepcopy(legacy_candidate)]
    ledger["candidate_observations"] = [copy.deepcopy(legacy_observation)]
    storage.save_ledger(tmp_path, ledger)

    # 新代码部署后、下一次后台任务之前也必须立即隐藏，避免前端继续展示旧的无关页面。
    hidden_queue = observation.list_candidate_observations(tmp_path, limit=10)
    assert hidden_queue["count"] == 0
    assert hidden_queue["scope_excluded_count"] == 1
    assert observation.get_candidate_observation(
        tmp_path, observation_id=observation_id
    )["scope_exclusion"]["audit_recorded"] is False

    reconciliation = observation.observe_discovery_candidates(tmp_path)
    ledger_after = storage.load_ledger(tmp_path)
    assert reconciliation["run"]["selected_candidate_count"] == 0
    assert reconciliation["run"]["new_scope_exclusion_audit_count"] == 2
    assert ledger_after["discovery_candidates"] == [legacy_candidate]
    assert ledger_after["candidate_observations"] == [legacy_observation]
    assert len(ledger_after["candidate_scope_exclusions"]) == 2
    assert storage.candidate_observation_snapshot_path(tmp_path, observation_id).is_file()
    detail = observation.get_candidate_observation(tmp_path, observation_id=observation_id)
    assert detail["snapshot"]["raw_sha256"] == raw_sha256
    assert detail["scope_exclusion"]["audit_recorded"] is True


def test_evidence_draft_preflight_explains_no_network_when_fields_or_scope_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """不完整草稿或中国/徐州范围缺口必须在联网前给出可读原因。

    该回归覆盖真实用户遇到的失败模式：旧页面错误展示了“重新核验并入证”，而服务器在
    前置校验阶段正确停止。测试保证响应明确标识“未访问原页面”、审计不宣称已抓取，且
    不会因此新增正式证据或线索。
    """
    confirmation = importlib.import_module(f"{PACKAGE_NAME}.evidence_draft_confirmation")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    candidate_id = "discovery-candidate-preflight-scope-test"
    draft_id = "evidence-draft-preflight-scope-test"
    ledger = storage.empty_ledger()
    ledger["discovery_candidates"] = [
        {
            "candidate_id": candidate_id,
            "source_url": "https://media.example.com/investment/foreign-company",
            "source_title": "境外候选融资报道",
            "state": "discovered",
            "requires_china_business_verification": True,
            "eligibility_assessment": {
                "eligible": True,
                "requires_china_business_verification": True,
            },
        }
    ]
    ledger["evidence_drafts"] = [
        {
            "draft_id": draft_id,
            "observation_id": "candidate-observation-preflight-scope-test",
            "candidate_id": candidate_id,
            "source_url": "https://media.example.com/investment/foreign-company",
            "source_title": "境外候选融资报道",
            "collected_at": "2026-09-08T00:00:00+00:00",
            "raw_sha256": hashlib.sha256(b"preflight snapshot").hexdigest(),
            "entity_name": "示例境外科技",
            "entity_kind": None,
            "source_published_at": "2026-09-08",
            "source_published_at_raw": "2026-09-08",
            "claim_type": "financing",
            "excerpt": "示例境外科技宣布完成融资。",
            "matched_terms": ["融资"],
            "needs_human_confirmation": ["实体类型（企业、基金或创业团队）"],
            "extractor_version": "deterministic-candidate-observation-v1",
            "created_at": "2026-09-08T00:01:00+00:00",
            "state": "pending_human_confirmation",
            "operation_scope": "automatic_deterministic_candidate_observation_draft_only",
        }
    ]
    storage.save_ledger(tmp_path, ledger)
    monkeypatch.setattr(
        confirmation,
        "capture_discovery_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("预检查失败时不得访问原页面")
        ),
    )

    result = confirmation.confirm_evidence_draft(
        tmp_path,
        draft_id=draft_id,
        review_note="草稿缺实体类型且当前没有中国或徐州业务公开依据。",
        authenticated_user_id="ordinary-user-preflight",
    )
    after = storage.load_ledger(tmp_path)

    assert result["confirmed"] is False
    assert result["preflight"] == {
        "passed": False,
        "capture_started": False,
        "missing_confirmation_fields": ["实体类型（企业、基金或创业团队）"],
        "requires_china_business_verification": True,
        "blockers": [
            "证据草稿的entity_kind不可确认",
            "候选的中国业务与徐州关联仍是合并的历史待确认项；请人工作地域判断，或在确认中国业务和企业来源地后选择“无需徐州字段”。",
        ],
    }
    assert result["audit"]["capture_attempted"] is False
    assert result["audit"]["capture_result"] == "draft_fields_incomplete"
    assert result["audit"]["new_state"] == "confirmation_rejected_incomplete_draft"
    assert "服务器未访问原页面" in str(result["message"])
    assert after["evidence"] == []
    assert after["leads"] == []


def test_human_authorized_material_is_audited_and_learns_source_without_becoming_server_score_input(
    tmp_path: Path,
) -> None:
    """人工有权访问的材料可进入人工研判与来源学习，但绝不伪装成服务器抓取事实。

    这覆盖实际业务场景：网页需要登录、动态渲染或拒绝服务器访问时，业务人员仍可把有权
    使用的关键摘录登记进共享台账。测试验证提交人、访问方式与材料来源会被明确保留；
    自动评分只看到零条服务器材料；结果反馈只改变追加式反馈，供来源优先级学习读取。
    """
    _load_plugin_module("__entry__")
    human_evidence = importlib.import_module(f"{PACKAGE_NAME}.human_evidence")
    adaptive_learning = importlib.import_module(f"{PACKAGE_NAME}.adaptive_learning")
    scoring_rules = importlib.import_module(f"{PACKAGE_NAME}.scoring_rules")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")

    created = human_evidence.record_human_attested_evidence(
        tmp_path,
        entity_name="授权访问的示例企业",
        entity_kind="企业",
        sector="具身智能",
        source_url="https://member.example.com/announcements/robotics",
        source_title="成员页面中的项目扩张说明",
        source_published_at="2026-09-10",
        source_level="一手",
        claim_type="扩张",
        evidence_quality=4,
        excerpt="该企业在已授权成员页面披露具身智能产品扩张计划，需由业务人员继续交叉核对。",
        material_text="服务器不使用该成员权限；材料仅供内部招商研判。",
        access_mode="login_or_member_access",
        attestation_confirmed=True,
        xuzhou_association_tags=["产业链"],
        attachment=None,
        authenticated_user_id="business-user-with-authorized-access",
    )
    lead_id = created["lead"]["lead_id"]
    assert created["evidence"]["evidence_origin"] == "human_attested_material"
    assert created["evidence"]["human_attestation"]["server_retrieval"] == "not_performed_at_human_submission"
    public_materials = human_evidence.list_human_evidence_submissions(tmp_path)
    assert public_materials["count"] == 1
    assert public_materials["items"][0]["submitted_by_user_id"] == "business-user-with-authorized-access"
    assert public_materials["items"][0]["access_mode"] == "login_or_member_access"

    # 人工材料可以留在线索材料链中，但绝不能被评分器当成服务器已经读取并核验的原文。
    score = scoring_rules.create_score_snapshot(
        tmp_path,
        lead_id=lead_id,
        authenticated_user_id="business-user-with-authorized-access",
    )["score_snapshot"]
    assert score["evidence_ids"] == []
    assert score["server_captured_evidence_count"] == 0
    assert score["human_attested_material_excluded_count"] == 1

    ledger_before_feedback = storage.load_ledger(tmp_path)
    protected_before = {
        field: copy.deepcopy(ledger_before_feedback[field])
        for field in ("leads", "evidence", "trusted_sources", "adaptive_learning_policy")
        if field in ledger_before_feedback
    }
    feedback = adaptive_learning.record_human_material_source_outcome(
        tmp_path,
        lead_id=lead_id,
        outcome="positive_progress",
        note="业务人员已完成内部研判，进入下一轮人工对接准备。",
        authenticated_user_id="business-user-with-authorized-access",
    )["feedback"]
    assert feedback["feedback_kind"] == "human_material_source_learning_only"
    assert feedback["score_snapshot_id"] is None
    assert feedback["source_evidence_ids"] == [created["evidence"]["evidence_id"]]
    assert feedback["source_domains"] == ["member.example.com"]
    ledger_after_feedback = storage.load_ledger(tmp_path)
    assert {
        field: ledger_after_feedback[field]
        for field in protected_before
    } == protected_before
    status = adaptive_learning.get_adaptive_learning_status(tmp_path)
    signal = next(item for item in status["source_signals"] if item["domain"] == "member.example.com")
    assert signal["human_attested_material_count"] == 1
    assert signal["positive_progress_count"] == 1


def test_human_authorized_material_rejects_private_or_credential_bearing_source_urls(
    tmp_path: Path,
) -> None:
    """人工能打开的页面也不能把内网或可复用访问凭据写进共享台账。"""
    _load_plugin_module("__entry__")
    human_evidence = importlib.import_module(f"{PACKAGE_NAME}.human_evidence")

    common = {
        "entity_name": "受限页面示例企业",
        "entity_kind": "企业",
        "sector": "半导体",
        "source_title": "授权材料页面",
        "source_published_at": "2026-09-10",
        "source_level": "一手",
        "claim_type": "扩张",
        "evidence_quality": 4,
        "excerpt": "这是业务人员有权查看的关键内容摘录。",
        "material_text": None,
        "access_mode": "login_or_member_access",
        "attestation_confirmed": True,
        "xuzhou_association_tags": [],
        "attachment": None,
        "authenticated_user_id": "business-user",
    }
    for unsafe_url in (
        "https://member.example.com/export?access_token=secret-value",
        "https://127.0.0.1/private-document",
        "https://team.local/private-document",
    ):
        with pytest.raises(ValueError):
            human_evidence.record_human_attested_evidence(
                tmp_path,
                source_url=unsafe_url,
                **common,
            )
