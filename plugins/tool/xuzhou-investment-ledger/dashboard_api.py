"""徐州招商工作台的只读已认证 HTTP 接口。

页面只能通过本模块读取经服务器生成的线索、证据引用、预警待办与统计报告；所有会改变
业务状态的操作仍分别走既有复核、评分、来源目录和图谱接口，避免“看板接口”逐渐变成
可绕过证据链或身份校验的万能写入口。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from .action_center import get_investment_action_center, get_outreach_preparation_brief
from .candidate_observation import get_candidate_observation, list_candidate_observations
from .candidate_corroboration import get_candidate_official_link_corroboration
from .candidate_review import list_discovery_candidates
from .core import get_investment_lead, list_investment_leads
from .industry_metrics import get_industry_business_metrics_report
from .lead_followup import get_lead_followup_card
from .relationship_graph import get_relationship_review_queue
from .reports import (
    get_industry_subdirection_coverage_report,
    get_investment_assessment_report,
    get_investment_work_summary,
)
from .shared_workspace import shared_ledger_dir_for_request
from .source_monitoring import get_source_coverage_report


router = APIRouter()

# 这些字段只用于服务器磁盘存储、HMAC 链或原始页面字节核验，招商人员在工作台完成
# 业务判断不需要看到它们。统一剥离可避免把宿主文件结构和不可用于人工阅读的密钥材料
# 暴露给浏览器；来源 URL、标题、摘录、采集时间和证据 ID 则继续保留。
_INTERNAL_RESPONSE_FIELDS = frozenset(
    {
        "ledger_path",
        "raw_snapshot_relative_path",
        "snapshot_relative_path",
        "raw_content_base64",
        "integrity_signature",
    }
)


def _remove_internal_storage_fields(value: object) -> object:
    """深拷贝并移除只对服务器有意义的存储路径、原文和完整性字段。

    输入是核心只读服务的嵌套返回值，输出是可安全发送到浏览器的独立副本；该过程只删除
    本机路径、Base64 原文和 HMAC 载荷，绝不增删来源 URL、原始摘录、采集时间、证据 ID
    或业务状态。函数不读取台账、不联网、不依据页面输入推断事实，因此不能影响证据链。
    """
    if isinstance(value, dict):
        return {
            key: _remove_internal_storage_fields(item)
            for key, item in value.items()
            if key not in _INTERNAL_RESPONSE_FIELDS
        }
    if isinstance(value, list):
        return [_remove_internal_storage_fields(item) for item in value]
    return copy.deepcopy(value)


def _read_dashboard_payload(reader: Callable[..., dict[str, object]], /, **kwargs: object) -> dict[str, object]:
    """调用既有只读服务并生成不会泄露内部存储字段的工作台响应。

    输入是受本模块固定的核心读取函数和已校验查询参数，输出为 ``ok=True`` 的脱敏副本。
    该辅助函数不接收客户端用户 ID、证据写入字段或业务结论；工作区权限由每个端点先用
    ``shared_ledger_dir_for_request`` 校验。读取失败继续由端点转换为标准 HTTP 错误。
    """
    result = reader(**kwargs)
    public_result = _remove_internal_storage_fields(result)
    if not isinstance(public_result, dict):
        raise RuntimeError("招商工作台只读服务返回格式异常")
    return {"ok": True, **public_result}


def _dashboard_error(exc: Exception, *, operation: str) -> HTTPException:
    """把只读服务的可预期错误转换为不暴露服务器细节的 HTTP 响应。

    输入是核心服务抛出的受控异常和当前读取操作名称，输出为供端点直接抛出的异常对象；
    参数、用户身份和原始证据均不会写回或包含在响应中。校验错误返回 422，台账完整性或
    运行时冲突返回 409，磁盘读取失败只返回通用 500 文案，避免浏览器获知内部路径。
    """
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"无法读取{operation}",
    )


@router.get("/leads", status_code=status.HTTP_200_OK)
async def list_xuzhou_dashboard_leads(
    request: Request,
    sector: str | None = None,
    review_state: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """读取招商线索列表，供工作台按产业和人工状态筛选。

    输入是认证请求与受限筛选项，输出为当前团队共享台账的线索摘要。函数先用服务端会话
    确认用户可访问共享台账，普通成员无需管理员角色；不接受用户 ID、评分、URL、证据
    内容或状态写入字段，也不会触发采集、自动推进、复核或修改证据链。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            list_investment_leads,
            workspace_dir=workspace_dir,
            sector=sector,
            review_state=review_state,
            limit=limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商线索列表") from exc


@router.get("/leads/{lead_id}", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_lead(lead_id: str, request: Request) -> dict[str, object]:
    """读取一条线索及完整可审查证据链，供人工查看而非修改事实。

    输入仅为服务器已有线索 ID 和认证请求，输出保留来源 URL、标题、摘录、采集日期、服务
    器分类与人工状态，但不返回本机快照路径或原始字节。用户身份和团队访问权限只来自
    服务端会话；函数不接收复核人、事实标签或任何证据覆盖字段，且完全不写台账。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_investment_lead,
            workspace_dir=workspace_dir,
            lead_id=lead_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商线索详情") from exc


@router.get("/leads/{lead_id}/followup-card", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_lead_followup_card(
    lead_id: str,
    request: Request,
) -> dict[str, object]:
    """读取“正式证据后由谁做什么”的招商跟进审批卡。

    输出只汇总服务器已有正式证据数量、当前风险提示、人工必填检查项及历史审批；不读取
    外网、不创建任务、不发送钉钉、不接收负责人或招商决定。真正的“纳入跟进池”必须
    由独立认证写接口提交完整检查项后才会追加审计。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_lead_followup_card,
            workspace_dir=workspace_dir,
            lead_id=lead_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商跟进审批卡") from exc


@router.get("/action-center", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_action_center(
    request: Request,
    limit: int = 100,
) -> dict[str, object]:
    """读取风险、待补证与关系待办的人工行动中心，不发送外部通知。

    输入为认证请求和展示上限，输出是服务器固定规则生成的只读队列及其证据引用。红色项
    只表示需要人工核验的企业风险等级，绝不代表自动事实结论或自动淘汰；端点不接受用户
    ID、风险字段或状态字段，不改变预警、线索、评分、关系或不可变证据。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_investment_action_center,
            workspace_dir=workspace_dir,
            limit=limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商行动中心") from exc


@router.get("/leads/{lead_id}/outreach", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_outreach_brief(
    lead_id: str,
    request: Request,
) -> dict[str, object]:
    """读取单线索招商准备卡，明确其仅为人工后续核验参考。

    输入是既有线索 ID 与认证请求，输出为按已有证据归档的准备动作和待补证提示。函数不
    联系企业、不发送消息、不承诺政策、不接受联系人、用户 ID 或推进决定，也不修改原始
    证据、风险状态和评分；真实业务推进仍必须调用独立人工复核接口。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_outreach_preparation_brief,
            workspace_dir=workspace_dir,
            lead_id=lead_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商准备卡") from exc


@router.get("/leads/{lead_id}/assessment", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_assessment_report(
    lead_id: str,
    request: Request,
) -> dict[str, object]:
    """读取标准研判报告，展示证据覆盖而非自动生成招商结论。

    输入是既有线索 ID 与认证请求，输出包含标准章节、证据引用、跨来源待对齐提示和完整度
    矩阵。报告中的分类已由服务器原文规则保护，但仍须人工核对；端点不接收任何模型结论、
    评分、用户 ID 或证据字段，且不会修改台账、复核状态或风险预警。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_investment_assessment_report,
            workspace_dir=workspace_dir,
            lead_id=lead_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商研判报告") from exc


@router.get("/work-summary", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_work_summary(
    request: Request,
    window_days: int = 30,
) -> dict[str, object]:
    """读取招商日常工作统计，避免把计数误写为招商成果或企业事实。

    输入为认证请求和 1–90 天统计窗口，输出线索、证据、复核和待补证观察的既有计数。所有
    数据均来自共享台账的只读汇总；端点不能接收人员绩效结论、用户 ID、来源页面或写入字段，
    不会自动评价人员、企业意愿或修改任何业务记录。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_investment_work_summary,
            workspace_dir=workspace_dir,
            window_days=window_days,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="招商工作统计") from exc


@router.get("/source-coverage", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_source_coverage(
    request: Request,
    window_days: int = 30,
) -> dict[str, object]:
    """读取免费公开来源覆盖、退避和待补证统计，不触发新的网络访问。

    输入为认证请求和统计窗口，输出已有监控审计和来源覆盖缺口。函数不接收 URL、搜索词、
    抓取参数、用户 ID 或来源评级写入字段，不运行监控、不重试被退避站点、不修改候选快照、
    证据链、评分或人工状态。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_source_coverage_report,
            workspace_dir=workspace_dir,
            window_days=window_days,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="来源覆盖统计") from exc


@router.get("/industry-metrics", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_industry_metrics(
    request: Request,
    window_years: int = 5,
) -> dict[str, object]:
    """读取营业收入、产值和项目投资额的并列统计，不混合三类口径。

    输入是认证请求与 1–10 年窗口，输出仅为已人工确认的指标时序及待复核草稿数。函数不会
    接收金额、企业、年份、用户 ID、证据或指标确认结论；三类指标始终并列展示，绝不相加、
    自动确认或改变原始证据和线索状态。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_industry_business_metrics_report,
            workspace_dir=workspace_dir,
            window_years=window_years,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="产业经营指标") from exc


@router.get("/subdirection-coverage", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_subdirection_coverage(
    request: Request,
    window_days: int = 30,
) -> dict[str, object]:
    """读取三条重点产业细分方向的现有证据覆盖，不把词命中当作产业事实。

    输入是认证请求和统计窗口，输出固定细分方向的已见材料和待补证缺口。词命中只用于帮助
    人工发现阅读范围，不能改变企业产业分类、评分、风险、复核或招商推进；端点不联网、
    不接收搜索词和用户 ID，也不写入台账。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_industry_subdirection_coverage_report,
            workspace_dir=workspace_dir,
            window_days=window_days,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="产业细分覆盖") from exc


@router.get("/candidate-observations", status_code=status.HTTP_200_OK)
async def list_xuzhou_dashboard_candidate_observations(
    request: Request,
    review_priority: str | None = None,
    content_changed_only: bool = False,
    limit: int = 100,
) -> dict[str, object]:
    """读取自动监控产生的待补证候选队列，不重新抓取或确认网页事实。

    输入为认证请求及有限筛选条件，输出候选来源、哈希状态、固定词命中和人工优先级。候选
    页面只是待核验材料，不能因工作台展示而自动成为企业融资、合作、风险或投资意愿事实；
    端点不接收 URL、实体、摘录、用户 ID 或写入字段，也不改任何原始快照和证据链。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            list_candidate_observations,
            workspace_dir=workspace_dir,
            review_priority=review_priority,
            content_changed_only=content_changed_only,
            limit=limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="待补证候选队列") from exc


@router.get("/discovery-candidates", status_code=status.HTTP_200_OK)
async def list_xuzhou_dashboard_discovery_candidates(
    request: Request,
    sector: str | None = None,
    review_state: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """读取刚发现的候选链接，使人工无需等待快照即可看到发现结果。

    输入是认证请求与有限筛选条件，输出仅为服务器发现的链接摘要及其追加式业务判断。接口
    不访问候选 URL、不创建快照、证据或线索，也不接收来源、摘要、用户 ID 或复核写入字段；
    因此展示刚发现候选不会绕过证据链或把页面标题升级为企业事实。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            list_discovery_candidates,
            workspace_dir=workspace_dir,
            sector=sector,
            review_state=review_state,
            limit=limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="已发现候选链接") from exc


@router.get(
    "/discovery-candidates/{candidate_id}/corroboration",
    status_code=status.HTTP_200_OK,
)
async def get_xuzhou_dashboard_candidate_corroboration(
    candidate_id: str,
    request: Request,
) -> dict[str, object]:
    """读取候选已保存的第二来源快照，供人工核验而不确认任何企业事实。

    输入仅为服务器候选 ID 和认证请求，输出是经哈希校验的跨域官网链接候选快照及审计说明。
    端点不接收 URL、来源正文、用户 ID、业务判断或证据字段，不联网、不创建 evidence/lead，
    也不认定官网归属、中国业务、徐州关联、融资真实性或招商意愿；内部快照路径和 Base64
    原始字节仍会在响应前剥离，避免浏览器借查看功能改写或外泄原始证据材料。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_candidate_official_link_corroboration,
            workspace_dir=workspace_dir,
            candidate_id=candidate_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="候选第二来源补证快照") from exc


@router.get("/candidate-observations/{observation_id}", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_candidate_observation(
    observation_id: str,
    request: Request,
) -> dict[str, object]:
    """读取一个候选快照及其待确认草稿，供人工决定是否确认入正式证据。

    输入只包含服务器生成的候选观察 ID 与认证请求，输出为可读的页面标题、可展示文本、
    内容哈希摘要和关联证据草稿；浏览器不能在本接口提交 URL、摘录、事实类别、采集时间、
    用户 ID 或确认结论。端点先按服务器会话确认共享台账访问权，再统一剥离原始 Base64
    页面、快照路径和完整性签名；它完全不抓取网页、不生成草稿、不确认事实、不改变候选、
    正式 evidence、评分、预警或人工状态。真正的确认仍须调用独立的受限确认接口，后端
    会重新读取并校验快照，因而前端详情页不能借展示操作改写原始证据链。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_candidate_observation,
            workspace_dir=workspace_dir,
            observation_id=observation_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="待补证候选详情") from exc


@router.get("/relationship-review-queue", status_code=status.HTTP_200_OK)
async def get_xuzhou_dashboard_relationship_review_queue(
    request: Request,
    limit: int = 100,
) -> dict[str, object]:
    """读取关系图谱待复核队列，不自动登记节点、边或个人信息。

    输入为认证请求与展示上限，输出已有关系边的状态、原因和证据引用。该端点不接收人员
    联系方式、资产、用户 ID、关系事实或证据正文；它不推断关系、不创建图谱边、不改变
    招商线索或原始证据，关系复核仍必须走独立受限写接口。
    """
    workspace_dir = shared_ledger_dir_for_request(request)
    try:
        return _read_dashboard_payload(
            get_relationship_review_queue,
            workspace_dir=workspace_dir,
            limit=limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _dashboard_error(exc, operation="关系复核队列") from exc
