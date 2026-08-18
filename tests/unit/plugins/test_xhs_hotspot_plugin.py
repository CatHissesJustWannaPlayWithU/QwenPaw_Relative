"""小红书热点插件第一阶段的核心单元测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile

import httpx
import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "tool" / "xhs-hotspot"
PACKAGE_NAME = "xhs_hotspot_test_plugin"


def _load_package() -> ModuleType:
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "xhs_hotspot.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    return package


def _module(name: str) -> ModuleType:
    _load_package()
    return __import__(f"{PACKAGE_NAME}.{name}", fromlist=[name])


def _response(items: list[dict[str, object]]) -> object:
    models = _module("models")
    return models.ProviderResponse(
        payload={"data": {"items": items}, "cached": False},
        provider_name="test_provider",
        source_url="https://api.example.test/xhs?refresh=true",
        fetched_at="2026-08-10T01:02:03+00:00",
        status_code=200,
        response_headers={"content-type": "application/json"},
    )


def _items() -> list[dict[str, object]]:
    return [
        {
            "rank": rank,
            "title": f"热点 {rank}",
            "keyword": f"关键词 {rank}",
            "hot_value": rank * 100,
            "url": f"https://www.xiaohongshu.com/search_result/{rank}",
        }
        for rank in range(1, 11)
    ]


def _ai_items() -> list[dict[str, object]]:
    items = _items()
    items[0]["title"] = "AI 智能体正在改变办公方式"
    items[1]["title"] = "明星综艺新动态"
    return items


def test_normalize_snapshot_returns_verified_top_ten() -> None:
    core = _module("core")
    snapshot = core.normalize_snapshot(_response(_items()))

    assert [item.rank for item in snapshot.items] == list(range(1, 11))
    assert [item.title for item in snapshot.items] == [f"热点 {rank}" for rank in range(1, 11)]
    assert len(snapshot.snapshot_fingerprint) == 64
    assert snapshot.provider_name == "test_provider"


def test_normalize_snapshot_rejects_duplicate_titles() -> None:
    core = _module("core")
    items = _items()
    items[1]["title"] = "热点 1"

    with pytest.raises(core.SnapshotValidationError, match="标题重复"):
        core.normalize_snapshot(_response(items))


def test_normalize_snapshot_rejects_non_continuous_ranks() -> None:
    core = _module("core")
    items = _items()
    items[4]["rank"] = 6

    with pytest.raises(core.SnapshotValidationError, match="排名不连续"):
        core.normalize_snapshot(_response(items))


def test_save_snapshot_keeps_raw_and_normalized_files(tmp_path: Path) -> None:
    core = _module("core")
    storage = _module("storage")
    source_response = _response(_items())
    snapshot = core.normalize_snapshot(source_response)

    stored = storage.save_snapshot(
        workspace_dir=tmp_path,
        snapshot=snapshot,
        provider_response=source_response,
    )

    raw_content = Path(stored.raw_path).read_bytes()
    snapshot_content = Path(stored.snapshot_path).read_bytes()
    assert hashlib.sha256(raw_content).hexdigest() == stored.raw_sha256
    assert hashlib.sha256(snapshot_content).hexdigest() == stored.snapshot_sha256
    assert "Authorization" not in raw_content.decode("utf-8")
    assert json.loads(raw_content)["provider_name"] == "test_provider"
    assert json.loads(snapshot_content)["items"][0]["rank"] == 1


def test_preference_profile_keeps_only_structured_topics(tmp_path: Path) -> None:
    reporting = _module("reporting")

    profile, path = reporting.save_preferences_from_memory(
        workspace_dir=tmp_path,
        memory_summary="用户长期优先关注 AI 行业和智能体，对娱乐八卦较少关注。",
        topic_weights={"AI行业": 5, "娱乐八卦": 1},
    )

    stored = json.loads(Path(path).read_text(encoding="utf-8"))
    weights = {topic["topic"]: topic["weight"] for topic in profile["topics"]}
    assert weights == {"AI行业": 5, "娱乐八卦": 1}
    assert "memory_summary" not in stored
    assert "用户长期优先关注" not in Path(path).read_text(encoding="utf-8")


def test_preference_profile_derives_weights_from_natural_language() -> None:
    """用户只需自然表达兴趣，内部才负责换算排序权重。"""
    reporting = _module("reporting")

    profile = reporting.build_preference_profile(
        memory_summary="我更关注旅行和美食，娱乐八卦不太关心。",
    )

    weights = {topic["topic"]: topic["weight"] for topic in profile["topics"]}
    assert weights == {"旅行生活": 4, "娱乐八卦": 1}


def test_default_profile_is_general_and_does_not_contain_memory() -> None:
    """首次生成简报可以使用综合版，不强迫用户先填写内部权重。"""
    reporting = _module("reporting")

    profile = reporting.build_default_preference_profile()

    assert profile["source"] == "general_default"
    assert {topic["weight"] for topic in profile["topics"]} == {3}
    assert all(not topic["matched_memory_keywords"] for topic in profile["topics"])


def test_personalized_report_prioritizes_matching_real_snapshot_titles(tmp_path: Path) -> None:
    core = _module("core")
    reporting = _module("reporting")
    storage = _module("storage")
    source_response = _response(_ai_items())
    snapshot = core.normalize_snapshot(source_response)
    storage.save_snapshot(
        workspace_dir=tmp_path,
        snapshot=snapshot,
        provider_response=source_response,
    )
    reporting.save_preferences_from_memory(
        workspace_dir=tmp_path,
        memory_summary="用户更关注 AI 行业和智能体，较少关注娱乐八卦。",
        topic_weights={"AI行业": 5, "娱乐八卦": 1},
    )

    report, markdown, stored = reporting.generate_personalized_report(
        workspace_dir=tmp_path,
        lookback_days=7,
    )

    assert report["available_days"] == 1
    assert report["coverage_complete"] is False
    assert report["priority_items"][0]["title"] == "AI 智能体正在改变办公方式"
    assert report["priority_items"][0]["personalization_score"] == 5
    assert Path(stored.report_path).is_file()
    assert Path(stored.markdown_path).is_file()
    assert Path(stored.workbook_path).is_file()
    with ZipFile(stored.workbook_path) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()
        assert "xl/worksheets/sheet2.xml" in workbook.namelist()
        assert "小红书热点简报" in workbook.read("xl/worksheets/sheet1.xml").decode(
            "utf-8",
        )
    assert "历史数据尚未积累完整" in markdown
    assert "权重" not in markdown
    assert "个性化分数" not in markdown


@pytest.mark.asyncio
async def test_create_briefing_uses_general_mode_without_saved_preference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通用户只请求简报时，应得到综合版而不是内部参数报错。"""
    core = _module("core")
    storage = _module("storage")
    tools = _module("tools")
    source_response = _response(_ai_items())
    snapshot = core.normalize_snapshot(source_response)
    storage.save_snapshot(
        workspace_dir=tmp_path,
        snapshot=snapshot,
        provider_response=source_response,
    )
    monkeypatch.setattr(tools, "_workspace_dir", lambda: tmp_path)

    result = await tools.create_xhs_briefing()

    assert result["ok"] is True
    assert result["briefing_mode"] == "综合版"
    assert result["preference_status"] == "general_without_saved_preference"
    assert result["preference_path"] is None
    assert Path(str(result["report_file_path"])).is_file()
    assert str(result["report_file_name"]).endswith(".xlsx")
    assert "权重" not in result["briefing_markdown"]
    assert "个性化分数" not in result["briefing_markdown"]


@pytest.mark.asyncio
async def test_create_briefing_updates_preference_from_natural_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户用自然语言表达关注方向时，工具自动保存内部偏好。"""
    core = _module("core")
    storage = _module("storage")
    tools = _module("tools")
    source_response = _response(_ai_items())
    snapshot = core.normalize_snapshot(source_response)
    storage.save_snapshot(
        workspace_dir=tmp_path,
        snapshot=snapshot,
        provider_response=source_response,
    )
    monkeypatch.setattr(tools, "_workspace_dir", lambda: tmp_path)

    result = await tools.create_xhs_briefing(
        preference_hint="我更关注 AI 和智能体，娱乐八卦不太关心。",
    )

    assert result["ok"] is True
    assert result["briefing_mode"] == "个性化"
    assert result["preference_status"] == "updated_from_confirmed_request"
    assert Path(str(result["preference_path"])).is_file()
    assert result["priority_items"][0]["title"] == "AI 智能体正在改变办公方式"


def test_smtp_settings_require_encrypted_transport_and_valid_sender() -> None:
    delivery = _module("email_delivery")

    settings = delivery.smtp_settings_from_credential(
        public={
            "host": "smtp.example.test",
            "port": 465,
            "from_email": "reports@example.test",
            "use_ssl": True,
            "use_starttls": False,
        },
        secrets={"username": "reports@example.test", "password": "test-password"},
    )

    assert settings.use_ssl is True
    with pytest.raises(delivery.EmailDeliveryError, match="必须且只能"):
        delivery.smtp_settings_from_credential(
            public={
                "host": "smtp.example.test",
                "from_email": "reports@example.test",
                "use_ssl": False,
                "use_starttls": False,
            },
            secrets={"username": "reports@example.test", "password": "test-password"},
        )


@pytest.mark.parametrize("recipient", ["test@example.com", "demo@sample.test"])
def test_recipient_email_rejects_reserved_example_domains(recipient: str) -> None:
    """示例地址不能进入 SMTP 投递流程，避免产生无意义退信。"""
    delivery = _module("email_delivery")

    with pytest.raises(delivery.EmailDeliveryError, match="示例域名"):
        delivery._validate_recipient_email(recipient)  # pylint: disable=protected-access


def test_report_message_has_standard_date_and_message_id() -> None:
    """发送邮件必须带 Date 和 Message-ID，保证邮箱客户端正确显示时间并可追踪。"""
    delivery = _module("email_delivery")
    settings = delivery.SmtpSettings(
        host="smtp.example.test",
        port=465,
        username="reports@example.test",
        password="test-password",
        from_email="reports@example.test",
        from_name="小红书热点简报",
        use_ssl=True,
        use_starttls=False,
    )

    message = delivery._build_report_message(  # pylint: disable=protected-access
        settings=settings,
        recipient="receiver@real-mail.example",
        markdown="# 测试报告",
        workbook=b"PK\\x03\\x04test workbook",
        workbook_filename="xhs-report-test.xlsx",
    )

    assert message["Date"]
    assert message["Message-ID"].endswith("@example.test>")
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert body.get_content().strip() == "# 测试报告"
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "xhs-report-test.xlsx"
    assert attachments[0].get_content_type() == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert attachments[0].get_payload(decode=True) == b"PK\\x03\\x04test workbook"


@pytest.mark.asyncio
async def test_email_delivery_attaches_the_saved_excel_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """邮件发送必须附带与已保存报告完全一致的 Excel 文件。"""
    core = _module("core")
    reporting = _module("reporting")
    storage = _module("storage")
    delivery = _module("email_delivery")
    snapshot = core.normalize_snapshot(_response(_ai_items()))
    storage.save_snapshot(
        workspace_dir=tmp_path,
        snapshot=snapshot,
        provider_response=_response(_ai_items()),
    )
    report, _markdown, stored = reporting.generate_personalized_report(
        workspace_dir=tmp_path,
        lookback_days=7,
        preference_profile=reporting.build_default_preference_profile(),
    )
    settings = delivery.SmtpSettings(
        host="smtp.example.test",
        port=465,
        username="reports@example.test",
        password="test-password",
        from_email="reports@example.test",
        from_name="小红书热点简报",
        use_ssl=True,
        use_starttls=False,
    )
    sent: dict[str, object] = {}

    async def fake_load_smtp_settings(**_kwargs: object) -> object:
        return settings

    def fake_send_message(*, settings: object, message: object) -> None:
        sent["settings"] = settings
        sent["message"] = message

    monkeypatch.setattr(delivery, "load_smtp_settings", fake_load_smtp_settings)
    monkeypatch.setattr(delivery, "_send_message", fake_send_message)

    result = await delivery.send_personalized_report_email(
        workspace_dir=tmp_path,
        report_id=report["report_id"],
        recipient_email="receiver@real-mail.cn",
        credential_ref="xhs_report_smtp",
    )

    message = sent["message"]
    attachments = list(message.iter_attachments())
    assert result["ok"] is True
    assert result["attachment_file_name"] == Path(stored.workbook_path).name
    assert attachments[0].get_filename() == Path(stored.workbook_path).name
    assert attachments[0].get_payload(decode=True) == Path(stored.workbook_path).read_bytes()
    receipt = json.loads(Path(str(result["receipt_path"])).read_text(encoding="utf-8"))
    assert receipt["attachment_filename"] == Path(stored.workbook_path).name
    assert receipt["attachment_sha256"] == hashlib.sha256(
        Path(stored.workbook_path).read_bytes(),
    ).hexdigest()


@pytest.mark.asyncio
async def test_client_sends_bearer_key_and_refresh_without_storing_key() -> None:
    provider = _module("provider")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["refresh"] = request.url.params["refresh"]
        return httpx.Response(
            200,
            json={"data": {"items": _items()}},
            headers={"Authorization": "should-not-be-stored", "Content-Type": "application/json"},
        )

    client = provider.AidataHotspotClient(
        api_key="secret-key",
        base_url="https://api.example.test/xhs",
        transport=httpx.MockTransport(handler),
    )
    response = await client.fetch(force_refresh=True)

    assert seen == {"authorization": "Bearer secret-key", "refresh": "true"}
    assert "authorization" not in {key.lower() for key in response.response_headers}


@pytest.mark.asyncio
async def test_free_client_accepts_uapi_shape_without_authorization() -> None:
    provider = _module("provider")
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(
            200,
            json={
                "type": "xiaohongshu",
                "update_time": "2026-08-10T12:57:42.125Z",
                "list": [
                    {
                        "index": rank,
                        "title": f"免费热点 {rank}",
                        "hot_value": f"{1000 - rank}.5w",
                        "extra": {"type": "热"},
                        "url": f"https://www.xiaohongshu.com/search_result/{rank}",
                    }
                    for rank in range(1, 11)
                ],
            },
        )

    client = provider.UapiHotspotClient(
        base_url="https://api.example.test/hotboard?type=xiaohongshu",
        transport=httpx.MockTransport(handler),
    )
    response = await client.fetch()
    snapshot = _module("core").normalize_snapshot(response)

    assert "authorization" not in {key.lower() for key in seen_headers}
    assert response.provider_name == "uapi_free"
    assert snapshot.items[0].hot_value == 9_995_000
    assert snapshot.items[0].hot_text == "999.5w"
    assert snapshot.items[0].trend_label == "热"
    assert snapshot.source_updated_at == "2026-08-10T12:57:42.125Z"


def test_legacy_60s_configuration_is_rejected_explicitly() -> None:
    provider = _module("provider")
    tools = _module("tools")

    with pytest.raises(provider.HotspotProviderError, match="已停止使用"):
        tools._build_provider_client({"source": "60s_free"})


def test_paid_source_requires_explicit_per_call_confirmation() -> None:
    provider = _module("provider")
    tools = _module("tools")

    with pytest.raises(provider.HotspotProviderError, match="显式传入"):
        tools._build_provider_client(
            {"source": "aidata_paid", "aidata_api_key": "test-key"},
        )

    client = tools._build_provider_client(
        {"source": "aidata_paid", "aidata_api_key": "test-key"},
        allow_paid_source=True,
    )
    assert isinstance(client, provider.AidataHotspotClient)


def test_daily_cron_payload_is_valid_and_defaults_to_disabled() -> None:
    from qwenpaw.app.crons.models import CronJobSpec

    schedule = _module("schedule")
    payload = schedule.build_daily_collection_job_payload(
        user_id="user-10086",
        session_id="console:user-10086",
    )
    spec = CronJobSpec.model_validate(payload)

    assert spec.enabled is False
    assert spec.task_type == "tool"
    assert spec.tool is not None
    assert spec.tool.name == "collect_xhs_hotspots"
    assert spec.tool.arguments["limit"] == 10
    assert spec.tool.arguments["allow_paid_source"] is False
    assert spec.schedule.cron == "0 9 * * *"
    assert spec.schedule.timezone == "Asia/Shanghai"
    assert spec.dispatch.target.user_id == "user-10086"
    assert spec.dispatch.silent is True
    assert spec.runtime.share_session is False
    assert "allow_paid_source=false" in schedule.DAILY_COLLECTION_PROMPT
    assert spec.request is None
    assert spec.dispatch.target.user_id == "user-10086"


def test_daily_cron_payload_rejects_missing_target_identity() -> None:
    schedule = _module("schedule")

    with pytest.raises(ValueError, match="user_id"):
        schedule.build_daily_collection_job_payload(
            user_id=" ",
            session_id="console:user-10086",
        )


def test_plugin_registers_report_and_email_tools() -> None:
    plugin_module = _module("xhs_hotspot")
    registered: list[str] = []

    class FakeApi:
        def register_tool(
            self,
            *,
            tool_name: str,
            tool_func: object,
            description: str,
            icon: str,
            cron_safe: bool = False,
            cron_fixed_arguments: dict | None = None,
        ) -> None:
            assert callable(tool_func)
            assert description
            assert icon
            assert cron_safe is (tool_name == "collect_xhs_hotspots")
            if tool_name == "collect_xhs_hotspots":
                assert cron_fixed_arguments == {
                    "limit": 10,
                    "force_refresh": True,
                    "allow_paid_source": False,
                }
            else:
                assert cron_fixed_arguments is None
            registered.append(tool_name)

    plugin_module.XiaohongshuHotspotPlugin().register(FakeApi())
    assert registered == [
        "collect_xhs_hotspots",
        "list_xhs_hotspot_snapshots",
        "build_xhs_daily_collection_job",
        "save_xhs_report_preferences",
        "create_xhs_briefing",
        "generate_xhs_personalized_report",
        "list_xhs_personalized_reports",
        "send_xhs_personalized_report_email",
    ]


def test_plugin_registers_governance_metadata() -> None:
    """热点插件工具必须先登记治理类型，不能被按未知工具拒绝。"""
    from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY

    plugin_module = _module("xhs_hotspot")
    old_types = dict(DEFAULT_REGISTRY._types)  # pylint: disable=protected-access
    old_targets = dict(  # pylint: disable=protected-access
        DEFAULT_REGISTRY._target_params,
    )
    old_names = dict(  # pylint: disable=protected-access
        DEFAULT_REGISTRY._python_name_map,
    )
    try:
        plugin_module._register_governance_tools()

        assert DEFAULT_REGISTRY.get_type("CollectXhsHotspots") == "network"
        assert (
            DEFAULT_REGISTRY.python_to_policy_name("collect_xhs_hotspots")
            == "CollectXhsHotspots"
        )
        assert (
            DEFAULT_REGISTRY.get_type("GenerateXhsPersonalizedReport")
            == "internal"
        )
        assert DEFAULT_REGISTRY.get_type("CreateXhsBriefing") == "internal"
        assert (
            DEFAULT_REGISTRY.get_type("SendXhsPersonalizedReportEmail")
            == "network"
        )
    finally:
        DEFAULT_REGISTRY._types = old_types  # pylint: disable=protected-access
        DEFAULT_REGISTRY._target_params = old_targets  # pylint: disable=protected-access
        DEFAULT_REGISTRY._python_name_map = old_names  # pylint: disable=protected-access


def test_manifest_matches_qwenpaw_plugin_loader() -> None:
    """插件清单必须能被 QwenPaw 的 Pydantic 模型正确解析。"""
    from qwenpaw.plugins.architecture import PluginManifest

    manifest = PluginManifest.from_dict(
        json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    )

    assert manifest.plugin_type.value == "tool"
    assert manifest.entry.backend == "xhs_hotspot.py"
    assert manifest.meta["tools"][0]["name"] == "collect_xhs_hotspots"
    assert manifest.meta["tools"][0]["requires_config"] is False
    assert manifest.meta["tools"][0]["config_fields"][0]["default"] == "uapi_free"
    email_tool = manifest.meta["tools"][-1]
    assert email_tool["name"] == "send_xhs_personalized_report_email"
    assert email_tool["config_fields"][0]["name"] == "smtp_credential_ref"
