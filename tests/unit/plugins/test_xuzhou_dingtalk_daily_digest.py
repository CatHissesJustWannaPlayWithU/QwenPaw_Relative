"""钉钉日报的内容最小化、投递去重与固定收件人绑定测试。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import qwenpaw.security.secret_store as secret_store


PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "tool" / "xuzhou-investment-ledger"
PACKAGE_NAME = "test_xuzhou_dingtalk_daily_digest"


@pytest.fixture(autouse=True)
def _isolate_secret_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_store, "_cached_master_key", bytes.fromhex("7c" * 32))


def _load_module(module_name: str):
    sys.modules.pop(PACKAGE_NAME, None)
    for key in list(sys.modules):
        if key.startswith(f"{PACKAGE_NAME}."):
            sys.modules.pop(key, None)
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "xuzhou_investment_ledger.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    return importlib.import_module(f"{PACKAGE_NAME}.{module_name}")


def test_daily_digest_contains_only_counts_and_at_most_five_source_titles() -> None:
    digest = _load_module("dingtalk_daily_digest")
    start = datetime(2026, 9, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 8, 1, tzinfo=timezone.utc)
    ledger = {
        "evidence": [
            {"created_at": "2026-09-07T02:00:00+00:00", "source_title": f"证据标题{i}"}
            for i in range(4)
        ],
        "discovery_candidates": [
            {"discovered_at": "2026-09-07T03:00:00+00:00", "source_title": f"候选标题{i}"}
            for i in range(4)
        ],
        "candidate_observations": [
            {"collected_at": "2026-09-07T04:00:00+00:00", "source_title": "观察标题"},
        ],
        "evidence_drafts": [
            {"created_at": "2026-09-07T05:00:00+00:00", "source_title": "草稿标题"},
        ],
    }

    text, counts = digest._digest_text(ledger, start=start, end=end, report_date="2026-09-08")

    assert counts == {
        "evidence": 4,
        "discovery_candidates": 4,
        "candidate_observations": 1,
        "evidence_drafts": 1,
    }
    assert text.count("\n- ") == 5
    assert "https://" not in text
    assert "entity_name" not in text
    assert "证据标题0" in text
    assert "候选标题3" not in text


@pytest.mark.asyncio
async def test_daily_digest_writes_audit_and_never_sends_twice_for_same_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    digest = _load_module("dingtalk_daily_digest")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    monkeypatch.setenv("XUZHOU_INVESTMENT_LEDGER_SHARED_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(digest, "get_current_user_id", lambda: "dingtalk-owner")
    monkeypatch.setattr(
        digest,
        "_daily_window",
        lambda: (
            datetime(2026, 9, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 8, 1, tzinfo=timezone.utc),
            "2026-09-08",
        ),
    )
    ledger_dir = tmp_path / "ledger"
    ledger = storage.load_ledger(ledger_dir)
    ledger["dingtalk_daily_digest_subscriptions"].append(
        {
            "subscription_id": "sub-1",
            "channel": "dingtalk",
            "recipient_user_id": "dingtalk-owner",
            "recipient_session_id": "session-1",
            "enabled": True,
            "operation_scope": digest.DAILY_DIGEST_OPERATION_SCOPE,
        }
    )
    storage.save_ledger(ledger_dir, ledger)

    first = await digest.deliver_xuzhou_dingtalk_daily_digest(digest.DAILY_DIGEST_SCOPE)
    assert first["ok"] is True
    assert isinstance(first["cron_delivery"], dict)
    delivery_id = first["cron_delivery"]["delivery_id"]
    await digest.record_xuzhou_dingtalk_daily_digest_delivery(
        result=first,
        status="sent",
        error=None,
    )
    second = await digest.deliver_xuzhou_dingtalk_daily_digest(digest.DAILY_DIGEST_SCOPE)

    assert second["cron_delivery"] is None
    assert second["status"] == "already_sent"
    persisted = storage.load_ledger(ledger_dir)
    audit = persisted["dingtalk_daily_digest_delivery_audit"]
    assert audit[0]["delivery_id"] == delivery_id
    assert audit[0]["status"] == "sent"
    assert audit[0]["summary_counts"] == {
        "evidence": 0,
        "discovery_candidates": 0,
        "candidate_observations": 0,
        "evidence_drafts": 0,
    }


@pytest.mark.asyncio
async def test_subscription_uses_only_deployment_bound_chat_and_creates_two_fixed_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    digest = _load_module("dingtalk_daily_digest")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    monkeypatch.setenv("XUZHOU_INVESTMENT_LEDGER_SHARED_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv(digest.DAILY_DIGEST_CHAT_ID_ENV, "chat-owner")

    class _Chats:
        async def get_chat(self, chat_id: str):
            assert chat_id == "chat-owner"
            return SimpleNamespace(
                id="chat-owner",
                channel="dingtalk",
                user_id="dingtalk-owner",
                session_id="bound-session",
            )

    class _Cron:
        def __init__(self) -> None:
            self.jobs = {}

        async def get_job(self, job_id: str):
            return self.jobs.get(job_id)

        async def create_or_replace_job(self, job):
            self.jobs[job.id] = job

    cron = _Cron()
    workspace = SimpleNamespace(agent_id="default", chat_manager=_Chats(), cron_manager=cron)
    ledger_dir = tmp_path / "ledger"
    result = await digest.enable_dingtalk_daily_digest_subscription(
        workspace,
        ledger_dir=ledger_dir,
        authenticated_admin_user_id="admin-1",
    )

    assert result["enabled"] is True
    assert len(cron.jobs) == 2
    primary = next(job for job in cron.jobs.values() if job.schedule.cron == "0 9 * * *")
    retry = next(job for job in cron.jobs.values() if job.schedule.cron == "5 9 * * *")
    assert primary.dispatch.target.user_id == "dingtalk-owner"
    assert primary.dispatch.target.session_id == "bound-session"
    assert retry.dispatch.target == primary.dispatch.target
    assert primary.tool.arguments == {"run_scope": digest.DAILY_DIGEST_SCOPE}
