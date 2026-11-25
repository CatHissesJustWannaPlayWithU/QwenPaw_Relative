"""徐州招商结果反馈规则学习计划的已认证业务接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from qwenpaw.app.agent_context import get_agent_for_request
from qwenpaw.app.principal import get_current_principal

from .automation_schedule import (
    get_outcome_learning_schedule_status,
    set_outcome_learning_schedule_enabled,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


@router.get("/", status_code=status.HTTP_200_OK)
async def get_xuzhou_outcome_learning_schedule(request: Request) -> dict[str, Any]:
    """读取每日结果学习计划状态；普通登录用户可查看，且不会执行学习或访问网页。

    输入只有服务器已认证请求；输出是固定频率、启用状态与脱敏运行状态。请求不接受用户
    ID、Cron、规则、权重、样本、工具参数、URL、线索或证据，当前身份还必须通过团队名册
    校验。Cron 调度器仍属于当前运行实例，查询不会创建规则版本、修改人工反馈、评分、
    线索状态或任何原始证据链字段。
    """
    # 服务器侧团队授权解析共享目录；本实例没有计划时据此补充“托管在别处”的脱敏提示。
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(await get_outcome_learning_schedule_status(workspace, ledger_dir=ledger_dir)),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/enable", status_code=status.HTTP_200_OK)
async def enable_xuzhou_outcome_learning_schedule(request: Request) -> dict[str, Any]:
    """以当前认证普通用户启用唯一固定的每日结果反馈规则学习计划。

    输入没有请求体；操作者只取 ``Principal.user_id``，时间、工具与固定范围标记由服务端
    常量决定。输出为脱敏计划状态与只追加审计。计划不运行模型、不更新 Skill，只有在日后
    已积累足量人工最终反馈时，底层受限工具才可能创建可追溯的新规则版本；绝不改原始
    证据、线索、人工复核和反馈本身，也不能通过此接口提交权重或自动推进招商项目。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(
                await set_outcome_learning_schedule_enabled(
                    workspace,
                    ledger_dir=ledger_dir,
                    authenticated_user_id=principal.user_id,
                    enabled=True,
                )
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/pause", status_code=status.HTTP_200_OK)
async def pause_xuzhou_outcome_learning_schedule(request: Request) -> dict[str, Any]:
    """以当前认证普通用户持久暂停固定学习计划，不删除历史规则、反馈或审计。

    请求不接受客户端身份、Cron、样本、规则或工具参数；服务端只会把本模块验证过的固定
    任务设为 disabled 并追加审计。暂停后服务重启仍保持暂停；若同名任务不是受控学习
    计划则失败关闭。操作不会联网、运行学习、修改评分、线索或任何原始证据字段。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(
                await set_outcome_learning_schedule_enabled(
                    workspace,
                    ledger_dir=ledger_dir,
                    authenticated_user_id=principal.user_id,
                    enabled=False,
                )
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
