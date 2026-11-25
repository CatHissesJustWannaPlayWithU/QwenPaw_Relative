"""徐州招商后台来源监控计划的已认证业务接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from qwenpaw.app.agent_context import get_agent_for_request
from qwenpaw.app.principal import get_current_principal

from .automation_schedule import (
    get_source_monitor_schedule_status,
    set_source_monitor_schedule_enabled,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


@router.get("/", status_code=status.HTTP_200_OK)
async def get_xuzhou_source_monitor_schedule(request: Request) -> dict[str, Any]:
    """读取当前实例固定来源监控计划；已授权团队成员可查看，且不会触发任务。

    输入只有服务器已认证请求；输出是固定频率、启用状态及脱敏运行状态。函数不接受
    用户 ID、Cron、时区、工具参数、URL、证据或线索，当前用户身份仅用于平台校验其是否
    已加入团队名册。Cron 调度器仍属于当前运行实例，业务数据始终属于共享台账；它不联网、
    不登记计划、不改变自动学习、线索、评分或原始证据链。
    """
    # 显式取得主体而非只依赖工作区，确保认证未启用/上下文异常时失败关闭，也不信任前端
    # 或模型声称的身份。计划状态本身不包含其他用户投递目标或会话标识。
    # 服务器侧团队授权解析共享目录；本实例没有计划时据此补充“托管在别处”的脱敏提示。
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(await get_source_monitor_schedule_status(workspace, ledger_dir=ledger_dir)),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post("/enable", status_code=status.HTTP_200_OK)
async def enable_xuzhou_source_monitor_schedule(request: Request) -> dict[str, Any]:
    """以当前认证普通用户启用唯一固定的六小时公开来源监控计划。

    输入不含请求体：操作者只取服务器 ``Principal.user_id``，Cron、工具名、固定范围标记、
    时区、投递会话和访问频率均由服务端常量决定。输出为脱敏计划状态与追加审计。用户只能
    启动当前实例的受控监控且必须有共享团队资格，不能借接口调用任意工具或改写证据、来源 URL、原始摘录、采集
    时间、评分、人工复核或招商状态；计划运行后同样只生成候选/待补证审计，不自动推进。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(
                await set_source_monitor_schedule_enabled(
                    workspace,
                    ledger_dir=ledger_dir,
                    authenticated_user_id=principal.user_id,
                    enabled=True,
                )
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post("/pause", status_code=status.HTTP_200_OK)
async def pause_xuzhou_source_monitor_schedule(request: Request) -> dict[str, Any]:
    """以当前认证普通用户持久暂停固定监控，并追加审计但不删除历史计划或证据。

    输入同样没有客户端身份、Cron 或工具字段；输出为脱敏状态和服务器生成的暂停审计。暂停
    只将本模块已经验证的固定计划设为 disabled，服务重启后仍保持暂停；若同名计划不是本
    模块创建，核心服务会拒绝覆盖。操作不联网、不删除候选、审计或报告，更不能改写原始
    证据链；恢复时只能调用本模块的固定启用入口。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(
                await set_source_monitor_schedule_enabled(
                    workspace,
                    ledger_dir=ledger_dir,
                    authenticated_user_id=principal.user_id,
                    enabled=False,
                )
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
