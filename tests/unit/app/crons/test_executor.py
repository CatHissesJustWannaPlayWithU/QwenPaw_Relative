# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.crons.executor import CronExecutor
from qwenpaw.app.crons.models import (
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    ScheduleSpec,
)
from qwenpaw.runtime.tool_registry import ToolDescriptor, ToolRegistry
from tests.unit.app.conftest import make_cron_job_spec


class _Workspace:
    chat_manager = None

    def __init__(self) -> None:
        self.events_consumed = 0

    async def stream_query(self, _request):
        for event in ("first", "second"):
            self.events_consumed += 1
            yield event


@pytest.mark.asyncio
async def test_silent_agent_job_runs_without_channel_delivery(monkeypatch):
    workspace = _Workspace()
    channel_manager = AsyncMock()
    job = make_cron_job_spec(job_id="silent-job")
    job.dispatch = DispatchSpec(
        target=DispatchTarget(user_id="u1", session_id="console:u1"),
        silent=True,
    )

    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.read_session_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.create_trace",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.append_trace_from_session_delta",
        AsyncMock(),
    )
    finalize_trace = AsyncMock()
    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.finalize_trace",
        finalize_trace,
    )

    result = await CronExecutor(
        workspace=workspace,
        channel_manager=channel_manager,
    ).execute(job)

    assert workspace.events_consumed == 2
    channel_manager.send_event.assert_not_awaited()
    assert result["delivery_status"] == "suppressed"
    finalize_trace.assert_awaited_once_with(result["run_id"], status="success")


@pytest.mark.asyncio
async def test_agent_job_still_delivers_by_default(monkeypatch):
    workspace = _Workspace()
    channel_manager = AsyncMock()
    job = make_cron_job_spec(job_id="normal-job")

    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.read_session_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.create_trace",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.append_trace_from_session_delta",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "qwenpaw.app.crons.executor.finalize_trace",
        AsyncMock(),
    )

    result = await CronExecutor(
        workspace=workspace,
        channel_manager=channel_manager,
    ).execute(job)

    assert workspace.events_consumed == 2
    assert channel_manager.send_event.await_count == 2
    assert result["delivery_status"] == "success"


@pytest.mark.asyncio
async def test_tool_job_runs_only_an_enabled_cron_safe_tool(tmp_path):
    invoked = {}

    async def collect(limit: int):
        invoked["limit"] = limit
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="collect",
            func=collect,
            metadata={"cron_safe": True},
        ),
    )
    workspace = SimpleNamespace(
        agent_id="xhs",
        workspace_dir=tmp_path,
        plugins=SimpleNamespace(tool_registry=registry),
        config=SimpleNamespace(
            tools=SimpleNamespace(
                builtin_tools={"collect": SimpleNamespace(enabled=True)},
            ),
        ),
    )
    job = CronJobSpec.model_validate(
        {
            "name": "Collect hotspots",
            "schedule": ScheduleSpec(type="cron", cron="0 9 * * *"),
            "task_type": "tool",
            "tool": {"name": "collect", "arguments": {"limit": 10}},
            "dispatch": {
                "target": {"user_id": "admin", "session_id": "xhs-daily"},
                "silent": True,
            },
        },
    )

    result = await CronExecutor(
        workspace=workspace,
        channel_manager=AsyncMock(),
    ).execute(job)

    assert invoked == {"limit": 10}
    assert result["task_type"] == "tool"
    assert result["tool_name"] == "collect"


@pytest.mark.asyncio
async def test_cron_only_delivery_tool_sends_its_explicit_text_and_records_result(tmp_path):
    recorded = []

    async def deliver():
        return {
            "ok": True,
            "cron_delivery": {"text": "日报计数和标题"},
        }

    async def record_delivery(*, result, status, error):
        recorded.append((result, status, error))

    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="daily-digest",
            func=deliver,
            metadata={
                "cron_safe": True,
                "cron_only": True,
                "cron_deliver_result": True,
                "cron_delivery_result_handler": record_delivery,
            },
        ),
    )
    workspace = SimpleNamespace(
        agent_id="xuzhou",
        workspace_dir=tmp_path,
        plugins=SimpleNamespace(tool_registry=registry),
        # Cron-only tools intentionally have no ordinary agent tool config.
        config=SimpleNamespace(tools=SimpleNamespace(builtin_tools={})),
    )
    job = CronJobSpec.model_validate(
        {
            "name": "Daily digest",
            "schedule": ScheduleSpec(type="cron", cron="0 9 * * *"),
            "task_type": "tool",
            "tool": {"name": "daily-digest", "arguments": {}},
            "dispatch": {
                "channel": "dingtalk",
                "target": {"user_id": "owner", "session_id": "bound-session"},
                "silent": False,
            },
        },
    )
    channel_manager = AsyncMock()

    result = await CronExecutor(workspace=workspace, channel_manager=channel_manager).execute(job)

    assert result["delivery_status"] == "success"
    channel_manager.send_text.assert_awaited_once()
    assert channel_manager.send_text.await_args.kwargs["text"] == "日报计数和标题"
    assert recorded == [
        (
            {"ok": True, "cron_delivery": {"text": "日报计数和标题"}},
            "sent",
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_cron_delivery_tool_records_failure_and_surfaces_job_error(tmp_path):
    recorded = []

    async def deliver():
        return {"cron_delivery": {"text": "日报"}}

    async def record_delivery(*, result, status, error):
        recorded.append((result, status, error))

    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="daily-digest",
            func=deliver,
            metadata={
                "cron_safe": True,
                "cron_only": True,
                "cron_deliver_result": True,
                "cron_delivery_result_handler": record_delivery,
            },
        ),
    )
    workspace = SimpleNamespace(
        agent_id="xuzhou",
        workspace_dir=tmp_path,
        plugins=SimpleNamespace(tool_registry=registry),
        config=SimpleNamespace(tools=SimpleNamespace(builtin_tools={})),
    )
    job = CronJobSpec.model_validate(
        {
            "name": "Daily digest",
            "schedule": ScheduleSpec(type="cron", cron="0 9 * * *"),
            "task_type": "tool",
            "tool": {"name": "daily-digest", "arguments": {}},
            "dispatch": {
                "channel": "dingtalk",
                "target": {"user_id": "owner", "session_id": "bound-session"},
                "silent": False,
            },
        },
    )
    channel_manager = AsyncMock()
    channel_manager.send_text.side_effect = RuntimeError("delivery down")

    with pytest.raises(RuntimeError, match="delivery failed"):
        await CronExecutor(workspace=workspace, channel_manager=channel_manager).execute(job)

    assert recorded[0][1] == "failed"
    assert "delivery down" in (recorded[0][2] or "")


@pytest.mark.asyncio
async def test_tool_job_rejects_a_tool_without_cron_safe_marker(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolDescriptor(name="unsafe", func=lambda: {"ok": True}))
    workspace = SimpleNamespace(
        agent_id="xhs",
        workspace_dir=tmp_path,
        plugins=SimpleNamespace(tool_registry=registry),
        config=SimpleNamespace(
            tools=SimpleNamespace(
                builtin_tools={"unsafe": SimpleNamespace(enabled=True)},
            ),
        ),
    )
    job = CronJobSpec.model_validate(
        {
            "name": "Unsafe tool",
            "schedule": ScheduleSpec(type="cron", cron="0 9 * * *"),
            "task_type": "tool",
            "tool": {"name": "unsafe", "arguments": {}},
            "dispatch": {
                "target": {"user_id": "admin", "session_id": "xhs-daily"},
                "silent": True,
            },
        },
    )

    with pytest.raises(ValueError, match="not explicitly marked cron_safe"):
        await CronExecutor(workspace=workspace, channel_manager=AsyncMock()).execute(job)


@pytest.mark.asyncio
async def test_tool_job_rejects_a_cron_safe_tool_that_is_not_enabled(tmp_path):
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="collect",
            func=lambda: {"ok": True},
            metadata={"cron_safe": True},
        ),
    )
    workspace = SimpleNamespace(
        agent_id="xhs",
        workspace_dir=tmp_path,
        plugins=SimpleNamespace(tool_registry=registry),
        config=SimpleNamespace(
            tools=SimpleNamespace(
                builtin_tools={"collect": SimpleNamespace(enabled=False)},
            ),
        ),
    )
    job = CronJobSpec.model_validate(
        {
            "name": "Disabled tool",
            "schedule": ScheduleSpec(type="cron", cron="0 9 * * *"),
            "task_type": "tool",
            "tool": {"name": "collect", "arguments": {}},
            "dispatch": {
                "target": {"user_id": "admin", "session_id": "xhs-daily"},
                "silent": True,
            },
        },
    )

    with pytest.raises(ValueError, match="not enabled for this agent"):
        await CronExecutor(workspace=workspace, channel_manager=AsyncMock()).execute(job)


@pytest.mark.asyncio
async def test_tool_job_rejects_arguments_outside_its_fixed_policy(tmp_path):
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="collect",
            func=lambda **_kwargs: {"ok": True},
            metadata={
                "cron_safe": True,
                "cron_fixed_arguments": {"limit": 10, "allow_paid_source": False},
            },
        ),
    )
    workspace = SimpleNamespace(
        agent_id="xhs",
        workspace_dir=tmp_path,
        plugins=SimpleNamespace(tool_registry=registry),
        config=SimpleNamespace(
            tools=SimpleNamespace(
                builtin_tools={"collect": SimpleNamespace(enabled=True)},
            ),
        ),
    )
    job = CronJobSpec.model_validate(
        {
            "name": "Collect hotspots",
            "schedule": ScheduleSpec(type="cron", cron="0 9 * * *"),
            "task_type": "tool",
            "tool": {
                "name": "collect",
                "arguments": {"limit": 10, "allow_paid_source": True},
            },
            "dispatch": {
                "target": {"user_id": "admin", "session_id": "xhs-daily"},
                "silent": True,
            },
        },
    )

    with pytest.raises(ValueError, match="do not match its fixed policy"):
        await CronExecutor(workspace=workspace, channel_manager=AsyncMock()).execute(job)
