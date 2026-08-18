# -*- coding: utf-8 -*-
"""多智能体管理 API。

提供管理多个智能体实例的 RESTful API。
"""

import json
import logging
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, field_validator

from qwenpaw.exceptions import (
    AppBaseException,
)

from ...agents.utils.file_handling import read_text_file_with_encoding_fallback
from ..utils import schedule_agent_reload
from ...config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    ModelSlotConfig,
    load_agent_config,
    save_agent_config,
    generate_short_agent_id,
    sanitize_agent_id,
    validate_agent_id,
)
from ...config.utils import load_config, save_config
from ...agents.utils import copy_workspace_md_files, normalize_agent_language
from ...agents.skill_system import SkillPoolService, get_workspace_skills_dir
from ..agent_startup import AgentStartupStatus
from ..multi_agent_manager import MultiAgentManager
from ...constant import WORKING_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentSummary(BaseModel):
    """智能体摘要信息。"""

    id: str
    name: str
    description: str
    workspace_dir: str
    enabled: bool
    pinned: bool
    startup_status: AgentStartupStatus
    active_model: ModelSlotConfig | None = None


class AgentListResponse(BaseModel):
    """智能体列表响应。"""

    agents: list[AgentSummary]


class ReorderAgentsRequest(BaseModel):
    """保存智能体排序的请求模型。"""

    agent_ids: list[str]


class CreateAgentRequest(BaseModel):
    """创建新智能体的请求模型。

    ``id`` 字段可选。提供时，服务端会在清理后将其作为智能体标识；
    未提供时，服务端会自动生成一个随机的短 UUID。
    """

    id: str | None = None
    name: str
    description: str = ""
    workspace_dir: str | None = None
    language: str | None = None
    skill_names: list[str] | None = None
    active_model: ModelSlotConfig | None = None

    @field_validator("id", mode="before")
    @classmethod
    def sanitize_id(cls, value: str | None) -> str | None:
        """去除自定义 ID 首尾的空白字符。"""
        if value is None:
            return None
        if isinstance(value, str):
            sanitized = sanitize_agent_id(value)
            return sanitized if sanitized else None
        return value

    @field_validator("workspace_dir", mode="before")
    @classmethod
    def strip_workspace_dir(cls, value: str | None) -> str | None:
        """去除工作区路径中误输入的空白字符。"""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return value


def _get_multi_agent_manager(request: Request) -> MultiAgentManager:
    """从应用状态中获取多智能体管理器。"""
    if not hasattr(request.app.state, "multi_agent_manager"):
        raise HTTPException(
            status_code=500,
            detail="MultiAgentManager not initialized",
        )
    return request.app.state.multi_agent_manager


def _normalized_agent_order(config) -> list[str]:
    """返回去重后覆盖全部已配置智能体的排序列表。"""
    profile_ids = list(config.agents.profiles.keys())
    ordered_ids: list[str] = []

    for agent_id in config.agents.agent_order:
        if agent_id in config.agents.profiles and agent_id not in ordered_ids:
            ordered_ids.append(agent_id)

    for agent_id in profile_ids:
        if agent_id not in ordered_ids:
            ordered_ids.append(agent_id)

    return ordered_ids


def _group_agent_order(config, ordered_ids: list[str]) -> list[str]:
    """将完整排序按默认、置顶、普通智能体分组。"""
    pinned_ids = [
        agent_id
        for agent_id in ordered_ids
        if agent_id != "default"
        and getattr(config.agents.profiles[agent_id], "pinned", False)
    ]
    regular_ids = [
        agent_id
        for agent_id in ordered_ids
        if agent_id != "default" and agent_id not in pinned_ids
    ]
    default_ids = ["default"] if "default" in ordered_ids else []
    return [*default_ids, *pinned_ids, *regular_ids]


def _display_agent_order(config) -> list[str]:
    """返回按默认、置顶、普通智能体分组后的显示顺序。"""
    return _group_agent_order(config, _normalized_agent_order(config))


def _is_valid_display_order(config, agent_ids: list[str]) -> bool:
    """判断排序是否符合默认智能体和置顶智能体的分组规则。"""
    return _group_agent_order(config, agent_ids) == agent_ids


def _read_profile_description(workspace_dir: str) -> str:
    """存在 PROFILE.md 时，从中读取智能体描述。"""
    try:
        profile_path = Path(workspace_dir) / "PROFILE.md"
        if not profile_path.exists():
            return ""

        content = read_text_file_with_encoding_fallback(profile_path).strip()
        lines = []
        in_identity = False

        for line in content.split("\n"):
            if line.strip().startswith("## 身份") or line.strip().startswith(
                "## Identity",
            ):
                in_identity = True
                continue
            if in_identity:
                if line.strip().startswith("##"):
                    break
                if line.strip() and not line.strip().startswith("#"):
                    lines.append(line.strip())

        return " ".join(lines)[:200] if lines else ""
    except Exception:  # noqa: E722
        return ""


@router.get(
    "",
    response_model=AgentListResponse,
    summary="List all agents",
    description="Get list of all configured agents",
)
async def list_agents(request: Request) -> AgentListResponse:
    """列出全部已配置智能体。"""
    # 登录模式下身份来自认证中间件验证后的 Token，而不是前端参数。
    # 未启用认证的单机部署没有 principal，因此保持原先可见全部智能体的行为。
    principal = getattr(request.state, "principal", None)
    current_user_id = principal.user_id if principal is not None else None

    config = load_config()
    manager = (
        _get_multi_agent_manager(request) if request is not None else None
    )
    ordered_agent_ids = _display_agent_order(config)

    agents = []
    for agent_id in ordered_agent_ids:
        agent_ref = config.agents.profiles[agent_id]

        # 已登录时只返回当前用户拥有的智能体。
        # owner_user_id 为 None 的旧智能体暂时不对任何已登录用户显示。
        if (
            current_user_id is not None
            and agent_ref.owner_user_id != current_user_id
        ):
            continue
        enabled = getattr(agent_ref, "enabled", True)
        pinned = agent_id == "default" or getattr(
            agent_ref,
            "pinned",
            False,
        )
        startup_status = (
            manager.get_agent_startup_status(agent_id, enabled=enabled)
            if manager is not None
            else (
                AgentStartupStatus.PENDING
                if enabled
                else AgentStartupStatus.DISABLED
            )
        )
        try:
            agent_config = load_agent_config(agent_id)
            description = agent_config.description or ""

            profile_desc = _read_profile_description(agent_ref.workspace_dir)
            if profile_desc:
                if description.strip():
                    description = f"{description.strip()} | {profile_desc}"
                else:
                    description = profile_desc

            active_model = agent_config.active_model

            agents.append(
                AgentSummary(
                    id=agent_id,
                    name=agent_config.name,
                    description=description,
                    workspace_dir=agent_ref.workspace_dir,
                    enabled=enabled,
                    pinned=pinned,
                    startup_status=startup_status,
                    active_model=active_model,
                ),
            )
        except Exception:  # noqa: E722
            agents.append(
                AgentSummary(
                    id=agent_id,
                    name=agent_id.title(),
                    description="",
                    workspace_dir=agent_ref.workspace_dir,
                    enabled=enabled,
                    pinned=pinned,
                    startup_status=startup_status,
                ),
            )

    return AgentListResponse(agents=agents)


@router.put(
    "/order",
    summary="Persist agent order",
    description="Save the full ordered list of configured agent IDs",
)
async def reorder_agents(
    reorder_request: ReorderAgentsRequest = Body(...),
    request: Request = None,
) -> dict:
    """保存完整的智能体 ID 排序列表。"""
    config = load_config()
    principal = getattr(getattr(request, "state", None), "principal", None)
    current_user_id = principal.user_id if principal is not None else None
    configured_ids = [
        agent_id
        for agent_id, agent_ref in config.agents.profiles.items()
        if (
            current_user_id is None
            or agent_ref.owner_user_id == current_user_id
        )
    ]

    if len(reorder_request.agent_ids) != len(set(reorder_request.agent_ids)):
        raise HTTPException(
            status_code=400,
            detail="Each configured agent ID must appear exactly once.",
        )

    if set(reorder_request.agent_ids) != set(configured_ids):
        raise HTTPException(
            status_code=400,
            detail="Each configured agent ID must appear exactly once.",
        )

    if not _is_valid_display_order(config, reorder_request.agent_ids):
        raise HTTPException(
            status_code=400,
            detail=(
                "Agent order must keep default first and pinned agents "
                "before unpinned agents."
            ),
        )

    # agent_order 是历史遗留的全局配置。只替换当前用户拥有的位置，
    # 其他用户的顺序保留，防止一个用户覆盖整份全局顺序。
    requested_ids = iter(reorder_request.agent_ids)
    owned_ids = set(configured_ids)
    config.agents.agent_order = [
        (
            next(requested_ids)
            if agent_id in owned_ids
            else agent_id
        )
        for agent_id in _normalized_agent_order(config)
    ]
    save_config(config)

    return {"success": True, "agent_ids": reorder_request.agent_ids}


@router.patch(
    "/{agentId}/pin",
    summary="Pin or unpin an agent",
    description="Persist an agent's pinned state in agent selectors",
)
async def set_agent_pinned(
    agentId: str = PathParam(...),
    pinned: bool = Body(..., embed=True),
    request: Request = None,
) -> dict:
    """保存智能体的置顶状态，不改变启用状态。"""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    agent_ref = config.agents.profiles[agentId]
    principal = getattr(getattr(request, "state", None), "principal", None)
    if (
        principal is not None
        and agent_ref.owner_user_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="Agent not found")

    if agentId == "default" and not pinned:
        raise HTTPException(
            status_code=400,
            detail="Cannot unpin the default agent",
        )

    if agentId != "default":
        agent_ref.pinned = pinned
        config.agents.agent_order = _display_agent_order(config)
        save_config(config)

    return {
        "success": True,
        "agent_id": agentId,
        "pinned": True if agentId == "default" else pinned,
    }


@router.get(
    "/{agentId}",
    response_model=AgentProfileConfig,
    summary="Get agent details",
    description="Get complete configuration for a specific agent",
)
async def get_agent(
    agentId: str = PathParam(...),
    request: Request = None,
) -> AgentProfileConfig:
    """获取指定智能体的完整配置。"""
    config = load_config()
    agent_ref = config.agents.profiles.get(agentId)
    if agent_ref is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    principal = getattr(getattr(request, "state", None), "principal", None)
    if (
        principal is not None
        and agent_ref.owner_user_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        agent_config = load_agent_config(agentId)
        return agent_config
    except (ValueError, AppBaseException) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _generate_unique_id(existing_ids: set[str]) -> str:
    """生成不与已有 ID 重复的随机短智能体 ID。

    抛出：
        HTTPException：连续尝试后仍无法生成唯一 ID 时抛出。
    """
    max_attempts = 10
    for _ in range(max_attempts):
        candidate_id = generate_short_agent_id()
        if candidate_id not in existing_ids:
            return candidate_id
    raise HTTPException(
        status_code=500,
        detail="Failed to generate unique agent ID after 10 attempts",
    )


@router.post(
    "",
    response_model=AgentProfileRef,
    status_code=201,
    summary="Create new agent",
    description="Create a new agent with optional custom ID",
)
async def create_agent(
    request: CreateAgentRequest = Body(...),
    http_request: Request = None,
) -> AgentProfileRef:
    """创建新智能体。

    提供 ``request.id`` 时，将其作为智能体标识，并校验 URL 安全字符、
    长度、保留字和唯一性；未提供时生成随机短 UUID。
    """
    # 所属用户 ID 必须从已验证 Token 对应的当前用户中取得，
    # 不能相信前端请求体传来的用户 ID。未启用认证时保持旧的单机行为。
    principal = getattr(getattr(http_request, "state", None), "principal", None)
    current_user_id = principal.user_id if principal is not None else None

    config = load_config()
    existing_ids = set(config.agents.profiles.keys())

    if request.id:
        try:
            validate_agent_id(request.id, existing_ids)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=str(e),
            ) from e
        new_id = request.id
    else:
        new_id = _generate_unique_id(existing_ids)

    if request.workspace_dir and current_user_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Custom workspace directories are unavailable when "
                "user isolation is enabled."
            ),
        )

    workspace_dir = Path(
        request.workspace_dir or WORKING_DIR / "workspaces" / new_id,
    ).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    from ...config.config import (
        ChannelConfig,
        MCPConfig,
        HeartbeatConfig,
        ToolsConfig,
    )

    language = normalize_agent_language(
        request.language or config.agents.language or "en",
    )

    active_model = request.active_model
    if not active_model or not active_model.provider_id:
        try:
            from ...providers import ProviderManager

            global_model = ProviderManager.get_instance().get_active_model()
            if global_model and global_model.provider_id:
                active_model = global_model
        except Exception:
            pass

    agent_config = AgentProfileConfig(
        id=new_id,
        name=request.name,
        description=request.description,
        workspace_dir=str(workspace_dir),
        language=language,
        channels=ChannelConfig(),
        mcp=MCPConfig(),
        heartbeat=HeartbeatConfig(),
        tools=ToolsConfig(),
        active_model=active_model,
    )

    _initialize_agent_workspace(
        workspace_dir,
        skill_names=(
            request.skill_names if request.skill_names is not None else []
        ),
        language=language,
    )

    agent_ref = AgentProfileRef(
        id=new_id,
        workspace_dir=str(workspace_dir),
        owner_user_id=current_user_id,
        enabled=True,
    )

    config.agents.profiles[new_id] = agent_ref
    config.agents.agent_order = _normalized_agent_order(config)
    save_config(config)
    save_agent_config(new_id, agent_config)

    logger.info(f"Created new agent: {new_id} (name={request.name})")

    if http_request is not None:
        manager = _get_multi_agent_manager(http_request)
        manager.schedule_agent_startup(new_id)

    return agent_ref


@router.put(
    "/{agentId}",
    response_model=AgentProfileConfig,
    summary="Update agent",
    description="Update agent configuration and trigger reload",
)
async def update_agent(
    agentId: str = PathParam(...),
    agent_config: AgentProfileConfig = Body(...),
    request: Request = None,
) -> AgentProfileConfig:
    """更新智能体配置。"""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    agent_ref = config.agents.profiles[agentId]
    principal = getattr(getattr(request, "state", None), "principal", None)
    if (
        principal is not None
        and agent_ref.owner_user_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="Agent not found")

    existing_config = load_agent_config(agentId)

    update_data = agent_config.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if key != "id":
            setattr(existing_config, key, value)

    existing_config.id = agentId
    save_agent_config(agentId, existing_config)
    schedule_agent_reload(request, agentId)

    return agent_config


@router.delete(
    "/{agentId}",
    summary="Delete agent",
    description="Delete agent and workspace (cannot delete default agent)",
)
async def delete_agent(
    agentId: str = PathParam(...),
    request: Request = None,
) -> dict:
    """删除智能体。"""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    agent_ref = config.agents.profiles[agentId]
    principal = getattr(getattr(request, "state", None), "principal", None)
    if (
        principal is not None
        and agent_ref.owner_user_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="Agent not found")

    if agentId == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the default agent",
        )

    manager = _get_multi_agent_manager(request)
    if manager.is_agent_startup_in_progress(agentId):
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{agentId}' cannot be deleted while starting",
        )
    await manager.stop_agent(agentId)

    del config.agents.profiles[agentId]
    config.agents.agent_order = _normalized_agent_order(config)
    save_config(config)

    return {"success": True, "agent_id": agentId}


@router.patch(
    "/{agentId}/toggle",
    summary="Toggle agent enabled state",
    description="Enable or disable an agent (cannot disable default agent)",
)
async def toggle_agent_enabled(
    agentId: str = PathParam(...),
    enabled: bool = Body(..., embed=True),
    request: Request = None,
) -> dict:
    """切换智能体的启用状态。"""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    agent_ref = config.agents.profiles[agentId]
    principal = getattr(getattr(request, "state", None), "principal", None)
    if (
        principal is not None
        and agent_ref.owner_user_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="Agent not found")

    if agentId == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot disable the default agent",
        )

    manager = _get_multi_agent_manager(request)

    if not enabled and manager.is_agent_startup_in_progress(agentId):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent '{agentId}' is still starting and cannot be "
                f"disabled yet"
            ),
        )

    if not enabled and getattr(agent_ref, "enabled", True):
        await manager.stop_agent(agentId)

    agent_ref.enabled = enabled
    save_config(config)

    if enabled:
        manager.schedule_agent_startup(agentId)

    return {
        "success": True,
        "agent_id": agentId,
        "enabled": enabled,
    }


def _apply_workspace_md_templates(
    workspace_dir: Path,
    language: str,
    *,
    md_template_id: str | None,
) -> None:
    """为工作区复制通用和模板专用的 Markdown 文件。"""
    copy_workspace_md_files(
        language,
        workspace_dir,
        md_template_id=md_template_id,
    )


def _ensure_heartbeat_file(workspace_dir: Path, language: str) -> None:
    """缺少 HEARTBEAT.md 时创建默认文件。"""
    heartbeat_file = workspace_dir / "HEARTBEAT.md"
    if heartbeat_file.exists():
        return

    default_heartbeat_mds = {
        "zh": """# Heartbeat checklist
- 扫描收件箱紧急邮件
- 查看未来 2h 的日历
- 检查待办是否卡住
- 若安静超过 8h，轻量 check-in
""",
        "en": """# Heartbeat checklist
- Scan inbox for urgent email
- Check calendar for next 2h
- Check tasks for blockers
- Light check-in if quiet for 8h
""",
        "ru": """# Heartbeat checklist
- Проверить входящие на срочные письма
- Просмотреть календарь на ближайшие 2 часа
- Проверить задачи на наличие блокировок
- Лёгкая проверка при отсутствии активности более 8 часов
""",
    }
    heartbeat_content = default_heartbeat_mds.get(
        language,
        default_heartbeat_mds["en"],
    )
    with open(heartbeat_file, "w", encoding="utf-8") as file:
        file.write(heartbeat_content.strip())


def _install_initial_skills(
    workspace_dir: Path,
    skill_names: list[str] | None,
) -> None:
    """从技能池安装请求指定的初始技能。"""
    if not skill_names:
        return

    pool_service = SkillPoolService()
    for skill_name in skill_names:
        try:
            result = pool_service.download_to_workspace(
                skill_name=skill_name,
                workspace_dir=workspace_dir,
                overwrite=False,
            )
            if result.get("success"):
                continue
            logger.warning(
                "Failed to install initial skill %s for %s: %s",
                skill_name,
                workspace_dir,
                result.get("reason", "unknown"),
            )
        except Exception as e:
            logger.warning(
                "Failed to install initial skill %s for %s: %s",
                skill_name,
                workspace_dir,
                e,
            )


def _initialize_agent_workspace(
    workspace_dir: Path,
    skill_names: list[str] | None = None,
    md_template_id: str | None = None,
    language: str | None = None,
) -> None:
    """初始化智能体工作区，仅安装明确请求的技能。"""
    from ...config import load_config as load_global_config

    (workspace_dir / "sessions").mkdir(exist_ok=True)
    (workspace_dir / "memory").mkdir(exist_ok=True)
    get_workspace_skills_dir(workspace_dir).mkdir(exist_ok=True)

    config = load_global_config()
    if not language:
        language = config.agents.language or "zh"

    _apply_workspace_md_templates(
        workspace_dir,
        language,
        md_template_id=md_template_id,
    )
    _ensure_heartbeat_file(workspace_dir, language)
    _install_initial_skills(workspace_dir, skill_names)

    jobs_file = workspace_dir / "jobs.json"
    if not jobs_file.exists():
        with open(jobs_file, "w", encoding="utf-8") as file:
            json.dump(
                {"version": 1, "jobs": []},
                file,
                ensure_ascii=False,
                indent=2,
            )

    chats_file = workspace_dir / "chats.json"
    if not chats_file.exists():
        with open(chats_file, "w", encoding="utf-8") as file:
            json.dump(
                {"version": 1, "chats": []},
                file,
                ensure_ascii=False,
                indent=2,
            )
