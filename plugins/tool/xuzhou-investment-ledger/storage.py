"""招商线索台账的本地存储实现。

台账保存在当前智能体工作区，而不是项目源码目录。这样每个智能体拥有独立数据，
部署时只需要持久化工作区目录即可保留线索与审计历史。
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .evidence_integrity import (
    INTEGRITY_SCHEME,
    LEDGER_INTEGRITY_SCHEME,
    empty_evidence_integrity,
    empty_ledger_integrity,
    seal_verified_legacy_evidence,
    sign_ledger_integrity,
    verify_evidence_integrity,
    verify_ledger_integrity,
)
from .models import LedgerDocument


SCHEMA_VERSION = 1
LEDGER_RELATIVE_PATH = Path("investment_attraction") / "lead_evidence_ledger.json"
CANDIDATE_OBSERVATION_SNAPSHOTS_RELATIVE_DIR = (
    Path("investment_attraction") / "candidate_observation_snapshots"
)
# 第二来源补证快照与原候选页面快照分目录保存。它们来自候选页面中明确标记的官网链接，
# 仍只是待人工核验材料，绝不能覆盖或替代候选原始页面、正式 evidence 或其完整性签名。
CANDIDATE_CORROBORATION_SNAPSHOTS_RELATIVE_DIR = (
    Path("investment_attraction") / "candidate_corroboration_snapshots"
)
EVIDENCE_SNAPSHOTS_RELATIVE_DIR = Path("investment_attraction") / "evidence_snapshots"
MAX_EVIDENCE_SNAPSHOT_RECORDS = 1_000
MAX_EVIDENCE_SNAPSHOT_STORAGE_BYTES = 256 * 1024 * 1024
# 这五条固定配方与 ``source_discovery.QUERY_RECIPES`` 一一对应。来源学习只能重排这一
# 完整集合，不能删除早期信号、徐州关联、校友、合作落地或经营指标入口；产业总称与所有
# 细分方向仍由发现器源码固定保留，普通用户、模型和网页内容都不能通过规则配置注入词表。
SOURCE_RECIPE_BASELINE_ORDER = (
    "public_early_signal",
    "xuzhou_connection",
    "university_alumni",
    "cooperation_and_landing",
    "industry_business_metrics",
)


def _initial_trusted_sources(created_at: str) -> list[dict[str, object]]:
    """返回可审查的初始可信来源目录，不把目录本身视为网页事实。

    输入是创建台账的 UTC 时间，输出是徐州市政府、重点高校及国家统计机构公开域名的
    受控目录配置。清华、北大域名仅可支持校友、合作和产业方向类事实；徐州市政府域名
    仅可支持徐州关联、合作、扩张和产业方向；国家统计局与国家数据平台仅可支持产业/扩张
    背景，均不能因为域名匹配而证明单个企业融资、个人资金能力或招商意愿。目录后续可由
    普通登录用户以新记录替换或停用；本函数不联网、不抓取页面、不写入线索，也不改变任
    何证据。
    """
    common_fields = {
        "status": "active",
        "created_at": created_at,
        "created_by_user_id": "system-default",
        "change_reason": "徐州招商智能体初始可信公开来源目录",
    }
    return [
        {
            "source_id": "trusted-xuzhou-government",
            "domain": "xz.gov.cn",
            "source_type": "government_investment_portal",
            "source_level": "primary",
            "evidence_quality": 4,
            "allowed_claim_types": [
                "xuzhou_connection",
                "cooperation",
                "expansion",
                "industry_focus",
            ],
            **common_fields,
        },
        {
            "source_id": "trusted-tsinghua-university",
            "domain": "tsinghua.edu.cn",
            "source_type": "university_alumni",
            "source_level": "primary",
            "evidence_quality": 4,
            "allowed_claim_types": ["xuzhou_connection", "cooperation", "industry_focus"],
            **common_fields,
        },
        {
            "source_id": "trusted-peking-university",
            "domain": "pku.edu.cn",
            "source_type": "university_alumni",
            "source_level": "primary",
            "evidence_quality": 4,
            "allowed_claim_types": ["xuzhou_connection", "cooperation", "industry_focus"],
            **common_fields,
        },
        # 以下两项是免费的官方统计来源种子。它们只提升未来经 robots 与原始快照校验后
        # 的来源可追溯性，不会自动抓取、入库数据或把宏观统计混同于某企业经营事实。
        {
            "source_id": "trusted-national-bureau-statistics",
            "domain": "stats.gov.cn",
            "source_type": "official_statistics",
            "source_level": "primary",
            "evidence_quality": 5,
            "allowed_claim_types": ["expansion", "industry_focus"],
            **common_fields,
        },
        {
            "source_id": "trusted-national-data-platform",
            "domain": "data.stats.gov.cn",
            "source_type": "official_statistics",
            "source_level": "primary",
            "evidence_quality": 5,
            "allowed_claim_types": ["expansion", "industry_focus"],
            **common_fields,
        },
        # 省级公开招商入口用于扩大与本地门户不同的发现覆盖面；目录只表示未来页面的
        # 来源分级，实际抓取仍逐次遵守 robots 和来源健康退避，绝不自动确认证据事实。
        {
            "source_id": "trusted-jiangsu-commerce-investment",
            "domain": "doc.jiangsu.gov.cn",
            "source_type": "government_investment_portal",
            "source_level": "primary",
            "evidence_quality": 4,
            "allowed_claim_types": ["xuzhou_connection", "cooperation", "expansion", "industry_focus"],
            **common_fields,
        },
        {
            "source_id": "trusted-jiangsu-international-investment-promotion",
            "domain": "iinvest.org.cn",
            "source_type": "government_investment_portal",
            "source_level": "primary",
            "evidence_quality": 4,
            "allowed_claim_types": ["xuzhou_connection", "cooperation", "expansion", "industry_focus"],
            **common_fields,
        },
    ]


def _initial_scoring_rule_set(created_at: str) -> dict[str, object]:
    """创建一版仅供人工后续调整的初始招商评分规则。

    输入是新建台账时的 UTC 时间，输出是带版本、创建来源、正向维度和降权规则的
    JSON 可序列化规则集。它把落地意愿、徐州关联、产业匹配、证据质量与时效分别
    保存，避免总分掩盖业务判断依据；直接竞品、阶段不匹配和证据风险则独立降权。
    规则是系统初始化的业务基线，不来自网页或模型输出，也不在本函数中计算分数、
    修改线索或自动学习；后续变更必须创建可审计的新版本。
    """
    return {
        "rule_set_id": "xuzhou-initial-rules",
        "version": 1,
        "status": "active",
        "dimensions": [
            {
                "key": "landing_intent",
                "label": "落地与合作意愿",
                "weight": 35,
                "description": "是否存在可追溯的投资、落地、扩产、合作或资源需求信号。",
                "enabled": True,
            },
            {
                "key": "xuzhou_association",
                "label": "徐州关联",
                "weight": 25,
                "description": "祖籍、成长经历、团队、业务、供应链或合作与徐州的证据化关联。",
                "enabled": True,
            },
            {
                "key": "industry_fit",
                "label": "产业与载体匹配",
                "weight": 20,
                "description": "与具身智能、半导体、医药及其细分方向和徐州产业基础的匹配程度。",
                "enabled": True,
            },
            {
                "key": "evidence_quality_recency",
                "label": "证据质量与时效",
                "weight": 20,
                "description": "来源是否一手、可追溯、相互印证且仍具时效。",
                "enabled": True,
            },
        ],
        "penalty_rules": [
            {
                "key": "direct_competitor",
                "label": "已布局直接竞品",
                "score_delta": -25,
                "description": "已投资或深度合作直接竞品时，降低招商匹配优先级。",
                "enabled": True,
            },
            {
                "key": "stage_or_scale_mismatch",
                "label": "阶段或规模不匹配",
                "score_delta": -15,
                "description": "投资阶段、资金规模或目标载体明显不匹配时降权。",
                "enabled": True,
            },
            {
                "key": "weak_or_conflicting_evidence",
                "label": "证据弱或相互矛盾",
                "score_delta": -20,
                "description": "来源无法追溯、证据强度不足或内容矛盾时降权。",
                "enabled": True,
            },
            {
                "key": "signal_stale",
                "label": "关键信号过期",
                "score_delta": -10,
                "description": "关键意向信号超过 180 天且未获更新时降权。",
                "enabled": True,
            },
            {
                "key": "fund_lifecycle_or_lack_of_recent_cases",
                "label": "基金生命周期或近期案例不足",
                "score_delta": -15,
                "description": "基金临近生命周期末期或缺少近期可验证投资案例时降权。",
                "enabled": True,
            },
        ],
        "created_at": created_at,
        "created_by_user_id": "system-default",
        "change_reason": "徐州招商智能体初始业务规则",
    }


def _initial_source_recipe_rule_set(created_at: str) -> dict[str, object]:
    """创建一版仅定义固定来源配方顺序的初始规则，不改变查询内容或招商事实。

    输入是新建台账的服务器 UTC 时间，输出为版本化的完整五配方顺序。规则只决定在调用
    方主动限制查询数、或免费搜索源提前触发访问退避时，哪条既有配方先执行；它不删除
    任一配方、不改变三条产业主线和细分方向、不写入 URL/关键词，也不把来源数量解释为
    企业意愿。后续调整必须由学习建议经认证普通用户批准后追加新版本，不能由网页、模型
    或定时任务直接修改。
    """
    return {
        "rule_set_id": "xuzhou-initial-source-recipe-rules",
        "version": 1,
        "status": "active",
        "recipe_order": list(SOURCE_RECIPE_BASELINE_ORDER),
        "created_at": created_at,
        "created_by_user_id": "system-default",
        "change_reason": "徐州招商智能体初始免费公开来源配方顺序",
        "based_on_proposal_id": None,
    }


def _utc_now() -> str:
    """返回统一使用的 UTC 时间字符串，避免不同服务器时区影响审计时间。"""

    return datetime.now(timezone.utc).isoformat()


def _ensure_private_workspace_directory(workspace_dir: Path, relative_directory: Path) -> Path:
    """创建并验证工作区内固定私有目录，拒绝任何目录级符号链接逃逸。

    输入是平台隔离工作区和服务器常量定义的相对目录，输出为已存在或新建的 0700 普通
    目录。逐层检查 ``investment_attraction`` 等父目录，不能让台账、锁或原始快照因为
    工作区内预置的符号链接写入其他位置；目录名从不接受 URL、模型或前端输入，函数也
    不读取、修改或删除任何线索、证据与业务状态。
    """
    if relative_directory.is_absolute() or any(part in {"", ".", ".."} for part in relative_directory.parts):
        raise ValueError("工作区私有目录必须是安全的相对路径")
    if workspace_dir.is_symlink():
        raise ValueError("工作区目录不能是符号链接")
    if workspace_dir.exists() and not workspace_dir.is_dir():
        raise ValueError("工作区路径必须是目录")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    current = workspace_dir
    for component in relative_directory.parts:
        current = current / component
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValueError("招商台账私有目录不能是符号链接或普通文件")
        else:
            current.mkdir(mode=0o700)
        os.chmod(current, 0o700)
    return current


def ledger_path(workspace_dir: Path) -> Path:
    """计算当前智能体工作区中的台账文件路径。"""

    return workspace_dir / LEDGER_RELATIVE_PATH


def ledger_lock_path(workspace_dir: Path) -> Path:
    """返回同一工作区台账的固定锁文件路径，不由调用方提供文件名。

    输入是已由平台隔离的工作区目录，输出与台账同目录的锁文件。锁只协调保存顺序，
    不承载业务数据、不替代访问控制，也不会读取、修改或删除线索、证据或人工判断。
    """
    return ledger_path(workspace_dir).with_suffix(".lock")


@contextmanager
def _exclusive_ledger_lock(workspace_dir: Path):
    """在比较版本并保存的临界区持有排他锁，防止两个进程同时覆盖台账。

    输入是工作区目录，输出为 ``with`` 范围内有效的服务器固定路径文件锁。锁文件仅
    当前系统账号可读写；本函数不锁网络采集的整个过程，具体业务仍须用
    ``ledger_revision`` 判断是否基于最新台账保存，不能凭锁猜测冲突应保留哪一方。

    付费升级方案：若部署为多服务器实例，应使用托管 PostgreSQL 或具备事务语义的托管
    数据库，在单个事务中执行 ``SELECT ... FOR UPDATE`` 或 ``UPDATE ... WHERE revision =
    :expected``，并让快照索引与原始内容使用同一持久化事务；不要依赖 NFS/共享盘锁语义。
    该迁移涉及全局访问控制，必须由安全管理员确认后实施，普通业务用户和模型不能切换
    存储后端。

    ===== 付费升级（暂不启用；仅供后续工程师预算）=====
    托管 PostgreSQL 的实例价格随地域、高可用、存储与备份策略变化，可按小规模生产实例
    约 100–1,000 元/月、双可用区或合规高可用约 1,000–5,000+ 元/月作预算占位；采购前必须
    用目标云的选型页重新核价。证据原文异地留存可使用对象存储，阿里云 OSS 中国内地标准
    本地冗余存储公开价为约 0.12 元/GB/月，另计请求和公网流量；对象锁定与跨地域副本须按
    实际套餐核价。迁移必须保持工作区隔离、事务版本校验和最小权限数据库账号。
    """
    lock_path = ledger_lock_path(workspace_dir)
    _ensure_private_workspace_directory(workspace_dir, LEDGER_RELATIVE_PATH.parent)
    if lock_path.is_symlink():
        raise ValueError("招商台账锁文件不能是符号链接")
    lock_flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        lock_flags |= nofollow
    descriptor = os.open(lock_path, lock_flags, 0o600)
    lock_file = None
    try:
        os.chmod(lock_path, 0o600)
        lock_file = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if lock_file is not None:
            lock_file.close()
        elif descriptor >= 0:
            os.close(descriptor)


class ScheduledMonitorAlreadyRunningError(RuntimeError):
    """同一工作区已有后台来源监控时的受控跳过信号。

    该异常仅表示非阻塞执行锁未取得，调用方应返回“已跳过”而不是重试、等待或绕过锁。
    它不携带 URL、企业信息、证据内容或用户身份，也不改变任何台账、快照和人工业务数据。
    """


@contextmanager
def exclusive_scheduled_monitor_lock(workspace_dir: Path):
    """在整次后台来源监控期间持有同工作区的非阻塞排他锁。

    输入是平台隔离工作区，输出是在 ``with`` 范围内有效的固定锁。锁文件始终位于招商
    台账私有目录、权限为 0600，文件名不接受调用方输入；已有任务持锁时立即抛出
    ``ScheduledMonitorAlreadyRunningError``，避免两个 Cron 同时联网、重复发现或竞争保存。
    进程异常退出后操作系统会释放 ``flock``，不需要以不可靠的时间戳删除“陈旧锁”。
    本函数不读取网页、不写证据、不修改线索、评分、人工复核或任何业务判断。
    """
    private_directory = _ensure_private_workspace_directory(
        workspace_dir, LEDGER_RELATIVE_PATH.parent
    )
    lock_path = private_directory / ".scheduled_public_discovery.lock"
    if lock_path.is_symlink():
        raise ValueError("定时来源监控锁文件不能是符号链接")
    lock_flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        lock_flags |= nofollow
    descriptor = os.open(lock_path, lock_flags, 0o600)
    lock_file = None
    locked = False
    try:
        os.chmod(lock_path, 0o600)
        lock_file = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScheduledMonitorAlreadyRunningError("后台来源监控正在运行，本次已安全跳过") from exc
        locked = True
        yield
    finally:
        if lock_file is not None:
            if locked:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _read_persisted_ledger_revision(path: Path) -> int:
    """在保存锁内读取磁盘版本，兼容旧台账并拒绝损坏版本字段。

    输入是固定台账路径，输出当前持久化的非负整数版本；不存在的文件和旧版缺失字段
    均按版本 0 处理。JSON 或版本格式异常会明确失败，不会把异常数据误作可覆盖台账。
    """
    if path.is_symlink():
        raise ValueError("招商线索台账文件不能是符号链接")
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"招商线索台账不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("招商线索台账根节点必须是对象")
    revision = payload.get("ledger_revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("招商线索台账中的 ledger_revision 必须是非负整数")
    return revision


def candidate_observation_snapshot_path(workspace_dir: Path, observation_id: str) -> Path:
    """返回候选观察快照的受限工作区路径，拒绝路径穿越或客户端文件名。

    输入是服务器生成的观察 ID，输出是当前智能体工作区内唯一的 JSON 快照文件路径。
    快照文件名只允许字母、数字和连字符，避免候选 URL、模型文本或任何调用方内容影响
    本地路径。函数不创建目录、不读取网络、不修改台账或证据链；实际写入由下面的
    ``save_candidate_observation_snapshot`` 使用原子替换完成。
    """
    if (
        not isinstance(observation_id, str)
        or not observation_id
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in observation_id)
    ):
        raise ValueError("候选观察快照 ID 格式不合法")
    return workspace_dir / CANDIDATE_OBSERVATION_SNAPSHOTS_RELATIVE_DIR / f"{observation_id}.json"


def save_candidate_observation_snapshot(
    workspace_dir: Path,
    *,
    observation_id: str,
    payload: dict[str, object],
) -> Path:
    """原子保存一份只追加的公开候选页面原始快照。

    输入是服务器生成的观察 ID 与包含 URL、采集时间、哈希、可见文本及 Base64 原始字节
    的快照载荷，输出是工作区中的新快照路径。若同 ID 文件已经存在则显式失败，防止
    后续任务覆盖历史原文；文件和临时文件均限制为当前系统账号可读写。此函数不接收
    人工编辑的证据字段、不修改台账，也不把快照中的词语解释为招商事实。
    """
    if not isinstance(payload, dict):
        raise ValueError("候选观察快照必须是对象")
    path = candidate_observation_snapshot_path(workspace_dir, observation_id)
    _ensure_private_workspace_directory(
        workspace_dir,
        CANDIDATE_OBSERVATION_SNAPSHOTS_RELATIVE_DIR,
    )
    if path.is_symlink():
        raise ValueError("候选观察快照不能是符号链接")
    if path.exists():
        raise ValueError("候选观察快照已存在，禁止覆盖历史原始内容")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".candidate_observation_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


def candidate_corroboration_snapshot_path(workspace_dir: Path, corroboration_id: str) -> Path:
    """返回第二来源补证快照的固定受限路径，拒绝任意文件名或路径穿越。

    输入为服务器从候选 ID、父快照哈希和抓取时间生成的补证审计 ID，输出为当前工作区内的
    唯一 JSON 文件路径。该 ID 不接受前端、模型或网页传入，因而网页中的 URL、标题和文字
    无法控制服务器文件路径；函数不创建目录、不访问网络、不写台账或修改候选/证据链。
    """
    if (
        not isinstance(corroboration_id, str)
        or not corroboration_id
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in corroboration_id)
    ):
        raise ValueError("候选第二来源补证快照 ID 格式不合法")
    return (
        workspace_dir
        / CANDIDATE_CORROBORATION_SNAPSHOTS_RELATIVE_DIR
        / f"{corroboration_id}.json"
    )


def save_candidate_corroboration_snapshot(
    workspace_dir: Path,
    *,
    corroboration_id: str,
    payload: dict[str, object],
) -> Path:
    """原子追加保存一份第二来源候选快照，禁止覆盖任何历史材料。

    输入是服务器生成的补证审计 ID 与已校验公网抓取结果，输出为仅当前系统账号可读写的
    新快照路径。调用方只能保存从已验证候选页面中明确“官网”链接得到的页面，且必须先
    通过 robots 与公网校验；保存内容仍只是待人工核验材料，不能自动写 evidence、创建
    lead、确认中国/徐州关系或改变原候选、原始快照和评分。若同 ID 已存在即失败关闭。
    """
    if not isinstance(payload, dict):
        raise ValueError("候选第二来源补证快照必须是对象")
    path = candidate_corroboration_snapshot_path(workspace_dir, corroboration_id)
    _ensure_private_workspace_directory(
        workspace_dir,
        CANDIDATE_CORROBORATION_SNAPSHOTS_RELATIVE_DIR,
    )
    if path.is_symlink():
        raise ValueError("候选第二来源补证快照不能是符号链接")
    if path.exists():
        raise ValueError("候选第二来源补证快照已存在，禁止覆盖历史原始内容")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".candidate_corroboration_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


def evidence_snapshot_path(workspace_dir: Path, evidence_id: str) -> Path:
    """返回正式证据原始响应快照的固定路径，拒绝外部指定文件名。

    输入是服务器依据已核验字段生成的 evidence ID，输出是工作区内唯一的 JSON 快照路径。
    ID 只允许小写字母、数字和连字符，因此 URL、模型文本和前端参数都不能影响本地路径；
    函数不创建文件、不读取网络、不改写台账或证据，实际只追加写入由
    ``save_evidence_snapshot`` 完成。快照路径随后作为已签名证据字段保存，人工复核不能
    替换其指向的原文。
    """
    if (
        not isinstance(evidence_id, str)
        or not evidence_id
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in evidence_id)
    ):
        raise ValueError("正式证据快照 ID 格式不合法")
    return workspace_dir / EVIDENCE_SNAPSHOTS_RELATIVE_DIR / f"{evidence_id}.json"


def _ensure_evidence_snapshot_capacity(directory: Path, *, new_snapshot_bytes: int) -> None:
    """在新增正式快照前核对本地条数和真实磁盘占用，阻止后台任务无限写盘。

    输入是已通过私有目录校验的固定快照目录和即将写入 JSON 的 UTF-8 字节数，成功时无
    返回值，超出条数或容量立即失败。目录中若出现符号链接或非普通 JSON 文件同样失败，
    以免攻击者让容量统计漏记；函数只读目录元数据，不删除历史原文、不改写台账或证据。
    """
    snapshot_count = 0
    storage_bytes = 0
    for item in directory.iterdir():
        if item.is_symlink() or not item.is_file() or item.suffix != ".json":
            raise ValueError("正式证据快照目录包含非预期文件，拒绝继续写入")
        snapshot_count += 1
        storage_bytes += item.stat().st_size
    if snapshot_count >= MAX_EVIDENCE_SNAPSHOT_RECORDS:
        raise RuntimeError("正式证据原始快照数量已达到安全上限")
    if storage_bytes + new_snapshot_bytes > MAX_EVIDENCE_SNAPSHOT_STORAGE_BYTES:
        raise RuntimeError("正式证据原始快照存储空间已达到安全上限")


def _verify_evidence_snapshot(workspace_dir: Path, evidence: dict[str, object]) -> None:
    """验证一条已签名正式证据所指快照仍存在且原始字节未被替换。

    输入是当前工作区和已通过 HMAC 链校验的证据对象，成功时无返回值，任何路径、文件类型、
    JSON 结构、Base64、长度或 SHA-256 不一致都会失败关闭。没有快照引用的经管理员迁移
    历史记录暂不伪造原文；一旦存在引用，本函数保证 URL、采集时间、原始字节与已签名证据
    一致。它不补写、不修复、不删除文件，也不接受人工复核内容。付费升级方案：对历史上
    未留存的原文，只能在确认授权、引用范围和采集时间后采购合规网页存证/档案服务或联系
    原发布方补取；补取内容必须作为新的补充证据登记，不能伪装为旧证据的原始快照。
    """
    snapshot_relative_path = evidence.get("raw_snapshot_relative_path")
    if snapshot_relative_path is None:
        return
    evidence_id = evidence.get("evidence_id")
    if not isinstance(evidence_id, str):
        raise RuntimeError("正式证据快照缺少有效 evidence_id")
    expected_path = evidence_snapshot_path(workspace_dir, evidence_id)
    if snapshot_relative_path != str(expected_path.relative_to(workspace_dir)):
        raise RuntimeError("正式证据快照路径与服务器规则不一致")
    if (
        expected_path.parent.is_symlink()
        or expected_path.is_symlink()
        or not expected_path.is_file()
    ):
        raise RuntimeError("正式证据原始快照缺失或不是普通文件")
    if expected_path.stat().st_size > MAX_EVIDENCE_SNAPSHOT_STORAGE_BYTES:
        raise RuntimeError("正式证据原始快照文件大小异常")
    try:
        with expected_path.open("r", encoding="utf-8") as file:
            snapshot = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("正式证据原始快照无法解析") from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError("正式证据原始快照必须是对象")
    encoded_raw_content = snapshot.get("raw_content_base64")
    try:
        raw_content = base64.b64decode(encoded_raw_content, validate=True)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("正式证据原始快照 Base64 内容无效") from exc
    expected_byte_size = evidence.get("raw_snapshot_byte_size")
    if not isinstance(expected_byte_size, int) or isinstance(expected_byte_size, bool):
        raise RuntimeError("正式证据快照缺少有效原始字节数")
    if (
        snapshot.get("evidence_id") != evidence_id
        or snapshot.get("source_url") != evidence.get("source_url")
        or snapshot.get("collected_at") != evidence.get("created_at")
        or snapshot.get("raw_sha256") != evidence.get("raw_sha256")
        or len(raw_content) != expected_byte_size
        or hashlib.sha256(raw_content).hexdigest() != evidence.get("raw_sha256")
    ):
        raise RuntimeError("正式证据原始快照与已签名证据不一致")


def save_evidence_snapshot(
    workspace_dir: Path,
    *,
    evidence_id: str,
    payload: dict[str, object],
) -> Path:
    """原子保存一份只追加的正式证据原始响应快照。

    输入是服务器生成的 evidence ID，及由核心证据登记函数构造的 URL、采集时间、内容哈希
    和 Base64 原始字节；输出是新快照的固定路径。相同 ID、符号链接或非私有目录均失败，
    防止后续任务覆盖已签名证据对应的原文。此函数不接受人工复核数据、不解释网页事实、
    不修改台账；付费升级方案：如需跨主机防篡改和合规长期留存，可将此只追加对象同步至
    带对象锁定（WORM）的对象存储，并把对象版本 ID 写入独立审计库。
    """
    if not isinstance(payload, dict):
        raise ValueError("正式证据快照必须是对象")
    path = evidence_snapshot_path(workspace_dir, evidence_id)
    _ensure_private_workspace_directory(workspace_dir, EVIDENCE_SNAPSHOTS_RELATIVE_DIR)
    if path.is_symlink():
        raise ValueError("正式证据快照不能是符号链接")
    if path.exists():
        raise ValueError("正式证据快照已存在，禁止覆盖历史原始内容")

    serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _ensure_evidence_snapshot_capacity(
        path.parent,
        new_snapshot_bytes=len(serialized_payload.encode("utf-8")),
    )

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".evidence_snapshot_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(serialized_payload)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


def empty_ledger() -> LedgerDocument:
    """创建带初始评分规则和可信来源目录但不含线索、证据和反馈的新台账。

    返回值把规则集、可信来源目录、未来评分快照和人工结果反馈与不可变证据分开保存。
    新台账仅写入系统默认业务配置，不对任何企业或基金作评分、事实判断或预警；因此
    初始化不会把预设权重或可信域名误写成真实招商结论，也不会影响后续证据链。
    """
    created_at = _utc_now()

    return {
        "schema_version": SCHEMA_VERSION,
        # 每次成功保存递增，用于拒绝两个操作基于同一旧台账发生的丢更新。
        "ledger_revision": 0,
        "updated_at": created_at,
        # 台账按录入顺序保存。后续展示、人工复核和导出时都需要保留这个顺序，
        # 因此使用列表而不是以 ID 为键的对象。
        "leads": [],
        "evidence": [],
        # 原始 evidence 的链式 HMAC 元数据不含密钥。密钥由受限侧文件保存，读写台账时
        # 统一校验，避免人工复核、图谱等其他功能可静默改写或删减历史证据。
        "evidence_integrity": empty_evidence_integrity(),
        # 整本台账的 HMAC 覆盖复核审计、规则与来源治理；首次保存时才会生成实际签名。
        "ledger_integrity": empty_ledger_integrity(),
        # 仅管理员迁移历史未签名台账时追加安全审计；普通业务操作绝不写入此列表。
        "evidence_integrity_migrations": [],
        # 仅管理员可对“无正式事实但整本 HMAC 已不匹配”的历史空台账追加恢复审计。
        # 它不替代证据迁移；只要已有任一线索或证据，恢复函数即拒绝执行。
        "ledger_integrity_reseals": [],
        # 仅认证用户启用/暂停固定后台来源监控时追加的业务审计。它不包含工具可写参数、
        # 访问目标或证据内容；整本 HMAC 仍覆盖该审计，防止计划开关被静默改写。
        "automation_schedule_audit": [],
        # 钉钉日报订阅和每次投递结果与证据链完全分离。订阅仅保存服务器从已存在钉钉
        # 会话解析出的受限投递目标；日报只汇总已在台账中的公开信息，绝不发送给外部主体。
        "dingtalk_daily_digest_subscriptions": [],
        "dingtalk_daily_digest_delivery_audit": [],
        "scoring_rule_sets": [_initial_scoring_rule_set(created_at)],
        # 来源配方规则与可信域名目录分开：前者只能排列固定免费查询配方，后者决定未来
        # 抓取后的来源分级。两者都不能改写历史 evidence 或让配置本身成为网页事实。
        "source_recipe_rule_sets": [_initial_source_recipe_rule_set(created_at)],
        # 自动学习只能把由既有候选、观察和已签名证据导出的建议追加到这里；建议原文
        # 永不被审批覆盖，审批结论和操作者另存审计，确保普通用户能复盘为何调整顺序。
        "source_recipe_learning_proposals": [],
        "source_recipe_learning_reviews": [],
        "score_snapshots": [],
        "outcome_feedback": [],
        # 自动发现的搜索批次和候选链接不是原始证据；二者单独保存，避免候选状态
        # 或重试记录污染已采集的来源链接、原始摘录和内容哈希。
        "discovery_runs": [],
        "discovery_candidates": [],
        # 候选观察只保存原文快照的不可覆盖索引和“待补证”词表命中。原始字节在单独的
        # 0600 快照文件中，避免台账状态、人工判断或评分功能改写采集当时的页面内容。
        "candidate_observations": [],
        # 第二来源补证只允许从已验证候选页面中明确标记“官网”的跨域公开链接得到。每次
        # 尝试和（如成功）独立原文快照均追加在此；它不是“官网已证实”或中国/徐州业务事实，
        # 更不能覆盖原候选、原始观察、正式 evidence 或任何人工业务判断。
        "candidate_corroboration_attempts": [],
        # 自动范围排除审计只追加“为何不再自动观察/展示”的固定规则结果，绝不删除或覆盖
        # discovery_candidates、候选原始快照或正式 evidence；例如无法证实中国/徐州业务
        # 语境的历史海外页面会退出工作队列，但仍保留哈希和采集记录供审计。
        "candidate_scope_exclusions": [],
        # 发现候选的人工业务判断与候选链接、原始快照和正式证据分表保存。判断只记录
        # “待补证、观察或无效”及认证操作者，不能借状态按钮覆盖来源 URL、页面摘要、
        # 采集时间、内容哈希或既有证据链。
        "candidate_business_reviews": [],
        # 草稿只引用已校验的候选快照，并保存固定规则的抽取候选。它独立于正式 evidence，
        # 不能用于覆盖 URL、原始摘录、采集时间、哈希、评分或线索状态；全台账 HMAC 会覆盖
        # 该列表，防止有人静默改写人工待确认内容。
        "evidence_drafts": [],
        # 草稿确认审计与草稿分表追加保存。审计只记录服务器认证操作者与受控抓取结果，
        # 不能反向修改草稿字段、候选原始快照或已经签名的正式 evidence。
        "evidence_draft_confirmation_audit": [],
        # 信息不完整草稿的业务判断只追加在此处；它不能被当作来源事实，也不能覆盖草稿、
        # 候选快照或正式证据。整本台账 HMAC 同样覆盖该审计，便于后续追溯责任。
        "evidence_draft_business_review_audit": [],
        # 正式证据之后的招商跟进审批卡与证据链分表保存。它只记录由谁基于哪些既有证据
        # 决定进入跟进池、负责人和下一步，绝不将计划写成企业意愿、签约或落地事实，也
        # 不允许审批操作改写 URL、摘录、采集时间、原始快照或评分。
        "lead_followup_approval_audit": [],
        # 经营指标草稿从已签名正式证据的摘录中以固定规则得出，但仍须由普通登录用户确认。
        # 三类指标与原始 evidence 完全分表：复核不能覆盖 URL、摘录、采集时间、签名或评分
        # 依据；营业收入、产值和项目投资额也会在后续报告中按口径并列，不能相加。
        "industry_metric_drafts": [],
        # 指标复核审计只追加服务器认证的操作者、旧新状态和备注，草稿原文与原始证据保持
        # 不可覆盖。确认后的派生数据独立存入下一列表，供只读统计使用。
        "industry_metric_draft_reviews": [],
        "industry_business_metrics": [],
        # 人工授权材料与服务器抓取证据共用不可变快照和签名链，但提交审计独立保存，
        # 便于页面清楚标明“人工提交”而非伪装成服务器已抓取。
        "human_evidence_submissions": [],
        # 自适应学习默认只动态计算队列优先级；只有业务成员明确变更策略时才写入策略和
        # 追加审计，绝不让自动学习改写来源、证据或招商结论。
        "adaptive_learning_policy_audit": [],
        # 来源健康度和后台任务审计只控制未来访问频率与覆盖观察。它们绝不保存、覆盖
        # 或推断网页事实，因而不能改写 evidence 中的 URL、摘录、采集时间或哈希。
        "source_health": [],
        "monitoring_runs": [],
        # robots、RSS 和 Sitemap 发现审计与搜索批次分开保存。它只记录可信目录来源
        # 的访问结果和候选数量，不能把链接或摘要误写成不可变 evidence。
        "source_feed_runs": [],
        # 目录是可审计业务配置，不与网页证据混存。修改目录只能影响未来抓取的分级，
        # 不能改写已经入库的来源、摘录、采集时间或当时使用的目录快照字段。
        "trusted_sources": _initial_trusted_sources(created_at),
        "trusted_source_audit": [],
        # 关系图谱只保存节点、边及其审计对既有证据 ID 的引用。它不复制页面事实，
        # 更不能覆盖 evidence 中的 URL、原始摘录、采集时间、哈希或评分依据。
        "relationship_graph_nodes": [],
        "relationship_graph_edges": [],
        # 新证据不能覆盖关系边首次登记的依据，后续补证用独立只追加记录保存，便于
        # 复盘“何时、由谁、为何新增何条既有证据引用”。
        "relationship_graph_edge_evidence_supplements": [],
        "relationship_graph_audit": [],
        # 关键人员关系草稿只能从已经签名的正式 evidence 中按固定规则产生，独立于图谱
        # 节点和边。草稿及其人工判断均由整本 HMAC 覆盖；它们不能被用来覆盖原始 URL、
        # 摘录、采集时间、内容哈希或评分依据，也不会自动创建人员节点或关系边。
        "relationship_drafts": [],
        "relationship_draft_reviews": [],
        # 协同投资信号草稿只保存已签名公开证据中的组织级信号及人工判断。它不建立
        # “个人资金池”、不记录自然人资产或联系方式，也不能把中间人背书推断成资金承诺；
        # 草稿和审计受整本 HMAC 覆盖，不能借复核覆盖来源 URL、摘录、采集时间或评分。
        "co_investment_signal_drafts": [],
        "co_investment_signal_draft_reviews": [],
    }


def load_ledger(workspace_dir: Path) -> LedgerDocument:
    """读取并兼容补全台账，不改写已有线索和原始证据。

    输入是当前智能体工作区，输出是满足当前规则字段要求的内存台账。文件不存在时
    返回带初始规则和可信目录的新台账；已有 schema v1 台账若缺少规则、自动发现、
    可信目录、评分快照或人工反馈列表，只在内存中补入安全默认值，待后续正常业务
    写入时才原子保存。该兼容步骤不修改 ``leads``、``evidence``、来源 URL、原始
    摘录或状态历史，避免迁移过程破坏既有证据链；字段已存在但类型异常时则直接报错，
    而非猜测或覆盖数据。
    """

    path = ledger_path(workspace_dir)
    private_directory = workspace_dir / LEDGER_RELATIVE_PATH.parent
    if workspace_dir.is_symlink() or (
        private_directory.exists()
        and (private_directory.is_symlink() or not private_directory.is_dir())
    ):
        raise ValueError("招商台账私有目录不能是符号链接或普通文件")
    if path.is_symlink():
        raise ValueError("招商线索台账文件不能是符号链接")
    if not path.exists():
        return empty_ledger()

    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"招商线索台账不是有效 JSON：{path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("招商线索台账根节点必须是对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"不支持的招商线索台账版本：{payload.get('schema_version')!r}"
        )
    if not isinstance(payload.get("leads"), list):
        raise ValueError("招商线索台账中的 leads 必须是列表")
    if not isinstance(payload.get("evidence"), list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    if "evidence_integrity" not in payload:
        if payload["evidence"]:
            # 旧台账有事实记录却没有外部可信签名时，不能在普通业务写入中擅自补签。
            # 付费升级方案：管理员迁移成功后可将迁移前 SHA-256 与审计记录写入 WORM 存储
            # 或第三方时间戳服务，提供本机安全边界之外的留存证明。
            raise RuntimeError("历史招商证据尚未签名，需管理员完成可信迁移后才能继续使用")
        payload["evidence_integrity"] = empty_evidence_integrity()
    verify_evidence_integrity(workspace_dir, payload)
    for evidence in payload["evidence"]:
        if not isinstance(evidence, dict):
            raise ValueError("招商证据记录必须是对象")
        _verify_evidence_snapshot(workspace_dir, evidence)
    if "ledger_revision" not in payload:
        # 历史 JSON 首次按版本 0 读取，正常保存时才补齐此元数据，不改写事实记录。
        payload["ledger_revision"] = 0
    elif (
        not isinstance(payload["ledger_revision"], int)
        or isinstance(payload["ledger_revision"], bool)
        or payload["ledger_revision"] < 0
    ):
        raise ValueError("招商线索台账中的 ledger_revision 必须是非负整数")

    # 评分规则与可信来源目录都被整本 HMAC 覆盖。旧版可信台账可能还没有这些后来新增的
    # 业务配置，必须先按旧字段集合验签，再在内存补默认值；在验签前补入会改变 HMAC 输入。
    has_scoring_rule_sets = "scoring_rule_sets" in payload
    if has_scoring_rule_sets and not isinstance(payload["scoring_rule_sets"], list):
        raise ValueError("招商线索台账中的 scoring_rule_sets 必须是列表")

    # 来源配方规则与学习建议同样受整本 HMAC 覆盖。旧版可信台账必须先按创建当时的字段
    # 集合验签；仅在成功后补初始顺序和空审计，不能在验签前把新字段误判为历史篡改。
    has_source_recipe_rule_sets = "source_recipe_rule_sets" in payload
    if has_source_recipe_rule_sets and not isinstance(payload["source_recipe_rule_sets"], list):
        raise ValueError("招商线索台账中的 source_recipe_rule_sets 必须是列表")

    # 先记录旧版台账是否还没有草稿列表：它已由整本台账 HMAC 覆盖，必须先按当时的字段
    # 集合完成验签，不能为了升级在验签前擅自补字段而误把可信旧台账判为篡改。
    has_evidence_drafts = "evidence_drafts" in payload
    if has_evidence_drafts and not isinstance(payload["evidence_drafts"], list):
        raise ValueError("招商线索台账中的 evidence_drafts 必须是列表")
    # 与草稿列表相同，确认审计是新增的整本台账签名字段；旧版可信台账必须先按原字段
    # 集合验签，再在内存补齐空列表，不能为了兼容而在验签前篡改待验证载荷。
    has_evidence_draft_confirmation_audit = "evidence_draft_confirmation_audit" in payload
    if has_evidence_draft_confirmation_audit and not isinstance(
        payload["evidence_draft_confirmation_audit"], list
    ):
        raise ValueError("招商线索台账中的 evidence_draft_confirmation_audit 必须是列表")
    # 业务判断审计同样是新增签名字段。必须验证旧载荷之后才能在内存补空列表，否则会把
    # 可信旧签名误报为篡改，也不能凭空生成任何人工判断记录。
    has_evidence_draft_business_review_audit = "evidence_draft_business_review_audit" in payload
    if has_evidence_draft_business_review_audit and not isinstance(
        payload["evidence_draft_business_review_audit"], list
    ):
        raise ValueError("招商线索台账中的 evidence_draft_business_review_audit 必须是列表")
    # 招商跟进审批审计是后续版本新增的整本台账签名字段。旧版可信台账必须先按原始
    # 字段集合完成 HMAC 校验，再在内存补空列表；不能在验签前把新字段混入旧签名载荷。
    has_lead_followup_approval_audit = "lead_followup_approval_audit" in payload
    if has_lead_followup_approval_audit and not isinstance(
        payload["lead_followup_approval_audit"], list
    ):
        raise ValueError("招商线索台账中的 lead_followup_approval_audit 必须是列表")
    # 以下三个经营指标字段同样由整本台账 HMAC 覆盖。旧版可信台账必须先按旧字段集合验签，
    # 再在内存补空列表；若在验签前补字段，会把没有被旧签名覆盖的内容误当成篡改。
    has_industry_metric_drafts = "industry_metric_drafts" in payload
    if has_industry_metric_drafts and not isinstance(payload["industry_metric_drafts"], list):
        raise ValueError("招商线索台账中的 industry_metric_drafts 必须是列表")
    has_industry_metric_draft_reviews = "industry_metric_draft_reviews" in payload
    if has_industry_metric_draft_reviews and not isinstance(
        payload["industry_metric_draft_reviews"], list
    ):
        raise ValueError("招商线索台账中的 industry_metric_draft_reviews 必须是列表")
    has_industry_business_metrics = "industry_business_metrics" in payload
    if has_industry_business_metrics and not isinstance(payload["industry_business_metrics"], list):
        raise ValueError("招商线索台账中的 industry_business_metrics 必须是列表")
    # 人工材料提交审计与自适应学习策略审计均为后续新增的签名字段。旧台账必须先按
    # 原有字段验证 HMAC，随后才在内存补空列表，不能在验签前制造“历史已有”的假象。
    has_human_evidence_submissions = "human_evidence_submissions" in payload
    if has_human_evidence_submissions and not isinstance(payload["human_evidence_submissions"], list):
        raise ValueError("招商线索台账中的 human_evidence_submissions 必须是列表")
    has_adaptive_learning_policy_audit = "adaptive_learning_policy_audit" in payload
    if has_adaptive_learning_policy_audit and not isinstance(payload["adaptive_learning_policy_audit"], list):
        raise ValueError("招商线索台账中的 adaptive_learning_policy_audit 必须是列表")
    # 整本签名恢复审计是又一项新增且被 HMAC 覆盖的安全字段。旧版可信台账必须先按旧
    # 字段集合验证整本签名，随后才可在内存补空列表，不能在验签前擅自加入恢复记录。
    has_ledger_integrity_reseals = "ledger_integrity_reseals" in payload
    if has_ledger_integrity_reseals and not isinstance(payload["ledger_integrity_reseals"], list):
        raise ValueError("招商线索台账中的 ledger_integrity_reseals 必须是列表")
    # 后台计划审计也是整本台账签名新增字段。必须先按旧字段集合验证可信历史载荷，才能
    # 在内存补空列表；否则既会误报旧签名，又可能让未被旧 HMAC 覆盖的审计混入验证内容。
    has_automation_schedule_audit = "automation_schedule_audit" in payload
    if has_automation_schedule_audit and not isinstance(payload["automation_schedule_audit"], list):
        raise ValueError("招商线索台账中的 automation_schedule_audit 必须是列表")

    # 这些字段均为历史版本之后新增、但也受整本 HMAC 覆盖的列表。此前若直接在验签前
    # ``payload[field_name] = []``，便会把签名时不存在的关系图谱等字段纳入校验，导致
    # 已迁移且未被篡改的旧台账被误判为篡改。先只记录缺失字段，验签通过后再补内存默认值。
    signed_list_compatibility_fields = (
        "score_snapshots",
        "outcome_feedback",
        "discovery_runs",
        "discovery_candidates",
        "candidate_observations",
        "candidate_corroboration_attempts",
        "candidate_scope_exclusions",
        "candidate_business_reviews",
        "lead_followup_approval_audit",
        "source_health",
        "monitoring_runs",
        "source_feed_runs",
        "trusted_source_audit",
        "relationship_graph_nodes",
        "relationship_graph_edges",
        "relationship_graph_edge_evidence_supplements",
        "relationship_graph_audit",
        "evidence_integrity_migrations",
        "source_recipe_learning_proposals",
        "source_recipe_learning_reviews",
        "relationship_drafts",
        "relationship_draft_reviews",
        "co_investment_signal_drafts",
        "co_investment_signal_draft_reviews",
        "dingtalk_daily_digest_subscriptions",
        "dingtalk_daily_digest_delivery_audit",
    )
    missing_signed_list_compatibility_fields: list[str] = []
    for field_name in signed_list_compatibility_fields:
        if field_name not in payload:
            missing_signed_list_compatibility_fields.append(field_name)
        elif not isinstance(payload[field_name], list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")

    has_trusted_sources = "trusted_sources" in payload
    if has_trusted_sources and not isinstance(payload["trusted_sources"], list):
        raise ValueError("招商线索台账中的 trusted_sources 必须是列表")

    verify_ledger_integrity(workspace_dir, payload) #验证HMAC
    if not has_scoring_rule_sets:
        # 旧签名验证成功后才补初始规则。它只影响未来评分配置；下次受控保存时会与旧字段
        # 一起重新签名，不生成任何历史评分、结论或证据。
        payload["scoring_rule_sets"] = [_initial_scoring_rule_set(_utc_now())]
    if not has_source_recipe_rule_sets:
        # 初始规则只提供固定五配方的完整默认顺序。它不表示历史发生过学习、审批或搜索，
        # 正常受控保存时才会连同已验证旧数据重签，既有候选和正式证据不会被重排或改写。
        payload["source_recipe_rule_sets"] = [_initial_source_recipe_rule_set(_utc_now())]
    if not has_trusted_sources:
        # 来源目录是未来抓取的治理配置，不是历史网页事实。只在可信旧台账验签后补默认值，
        # 绝不能在验签前把它当作已被旧签名覆盖的数据。
        payload["trusted_sources"] = _initial_trusted_sources(_utc_now())
    for field_name in missing_signed_list_compatibility_fields:
        # 每个缺失列表仅在旧 HMAC 已验证后于内存补空；空值不表示历史曾发生相应业务操作。
        # 后续真实受控保存会统一重签，且不会改写 leads/evidence 的 URL、摘录、采集时间或
        # 评分依据。
        payload[field_name] = []
    if not has_evidence_drafts:
        # 仅在内存添加空列表；调用方后续经受控保存成功后才会以新结构重签，旧证据、候选
        # 与人工记录均不被改写。若旧签名不可信，上面的验证已失败，绝不会执行此兼容步骤。
        payload["evidence_drafts"] = []
    if not has_evidence_draft_confirmation_audit:
        # 只在旧签名验证成功后补空审计列表；下次受控保存会统一重签，绝不自动补造任何
        # 复核人、确认结论或正式证据。
        payload["evidence_draft_confirmation_audit"] = []
    if not has_evidence_draft_business_review_audit:
        # 仅给已验证旧台账补内存默认值；只有后续受控业务保存时才重签新结构。
        payload["evidence_draft_business_review_audit"] = []
    if not has_lead_followup_approval_audit:
        # 空列表只表示历史版本尚未具备审批卡，不补造任何历史业务决定或负责人记录。
        payload["lead_followup_approval_audit"] = []
    if not has_industry_metric_drafts:
        # 旧台账验签成功后才补空草稿列表；不生成、推测或确认任何历史经营指标。
        payload["industry_metric_drafts"] = []
    if not has_industry_metric_draft_reviews:
        # 审计默认值不代表曾发生人工判断，后续真正复核时才由服务器认证身份追加记录。
        payload["industry_metric_draft_reviews"] = []
    if not has_industry_business_metrics:
        # 派生指标为空不改变历史证据事实；后续受控保存会把新结构连同既有字段重新签名。
        payload["industry_business_metrics"] = []
    if not has_human_evidence_submissions:
        # 空审计只表示旧版本尚未提供人工授权材料入口，不补造任何提交人、摘录或来源。
        payload["human_evidence_submissions"] = []
    if not has_adaptive_learning_policy_audit:
        # 空审计只表示旧版本尚未允许显式选择自适应策略；默认策略仍由读取函数安全给出。
        payload["adaptive_learning_policy_audit"] = []
    if not has_ledger_integrity_reseals:
        # 仅在既有整本签名验证成功后补空恢复审计；空列表不表示曾发生安全恢复，实际恢复
        # 只能由管理员接口在 SHA-256 比对和“空台账”限制下追加。
        payload["ledger_integrity_reseals"] = []
    if not has_automation_schedule_audit:
        # 可信旧台账验签后仅在内存补空计划审计；不会凭空表示后台曾运行或被用户启停。
        # 只有认证路由经受控保存成功后，才会追加真实操作人与服务器时间并重新签名。
        payload["automation_schedule_audit"] = []

    return payload  # type: ignore[return-value]


def migrate_verified_legacy_evidence_integrity(
    workspace_dir: Path,
    *,
    expected_legacy_ledger_sha256: str,
    verification_note: str,
    authenticated_admin_user_id: str,
) -> dict[str, object]:
    """由已认证管理员为已核验旧台账补齐当前完整性签名并追加安全审计。

    输入是服务器验证的管理员 ID、迁移前台账 SHA-256 与核验备注；输出为迁移审计、已重封
    证据数量和台账路径。函数不接受证据正文、URL、摘录、采集时间或任何调用方签名，只
    读取服务器固定台账；保存锁内再次比对 SHA-256，避免管理员核验后文件被悄然替换。
    对未签名或 v1 证据链，它仅改动每条记录的 ``integrity_signature``、链元数据、修订号、
    全台账签名和追加式迁移审计；对已验证的 v2 证据链则不重签任何证据，只补全台账签名。
    保存前逐字确认所有原始证据字段不变。管理员身份必须由 HTTP 认证层验证，普通用户、
    前端和模型不得直接调用此安全操作。
    """
    normalized_expected_sha256 = expected_legacy_ledger_sha256.strip().casefold()
    normalized_note = verification_note.strip()
    normalized_admin_user_id = authenticated_admin_user_id.strip()
    if (
        len(normalized_expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in normalized_expected_sha256)
    ):
        raise ValueError("expected_legacy_ledger_sha256必须是64位十六进制SHA-256值")
    if not normalized_note or len(normalized_note) > 2_000:
        raise ValueError("verification_note必须是1到2000个字符")
    if not normalized_admin_user_id or len(normalized_admin_user_id) > 100:
        raise ValueError("authenticated_admin_user_id必须是1到100个字符")

    path = ledger_path(workspace_dir)
    _ensure_private_workspace_directory(workspace_dir, LEDGER_RELATIVE_PATH.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError("历史招商线索台账必须是固定位置的普通文件")
    with path.open("rb") as file:
        legacy_bytes = file.read()
    actual_legacy_sha256 = hashlib.sha256(legacy_bytes).hexdigest()
    if actual_legacy_sha256 != normalized_expected_sha256:
        raise RuntimeError("历史招商线索台账SHA-256与管理员核验值不一致，拒绝迁移")
    try:
        legacy_ledger = json.loads(legacy_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("历史招商线索台账不是有效 UTF-8 JSON") from exc
    if not isinstance(legacy_ledger, dict) or legacy_ledger.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("历史招商线索台账版本不受支持")
    if not isinstance(legacy_ledger.get("leads"), list) or not isinstance(
        legacy_ledger.get("evidence"), list
    ):
        raise ValueError("历史招商线索台账缺少有效的 leads 或 evidence 列表")
    legacy_integrity = legacy_ledger.get("evidence_integrity")
    evidence_chain_already_current = False
    if legacy_integrity is not None:
        if not isinstance(legacy_integrity, dict):
            raise ValueError("历史招商证据完整性元数据格式异常")
        if legacy_integrity.get("scheme") == INTEGRITY_SCHEME:
            verify_evidence_integrity(workspace_dir, legacy_ledger)
            evidence_chain_already_current = True
        elif legacy_integrity.get("scheme") != "hmac-sha256-evidence-chain-v1":
            raise ValueError("历史招商证据完整性签名方案不受支持")

    evidence_before = json.dumps(
        [
            {key: value for key, value in evidence.items() if key != "integrity_signature"}
            if isinstance(evidence, dict)
            else evidence
            for evidence in legacy_ledger["evidence"]
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    migrated_evidence_ids = (
        []
        if evidence_chain_already_current
        else seal_verified_legacy_evidence(workspace_dir, legacy_ledger)
    )
    evidence_after = json.dumps(
        [
            {key: value for key, value in evidence.items() if key != "integrity_signature"}
            if isinstance(evidence, dict)
            else evidence
            for evidence in legacy_ledger["evidence"]
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if evidence_after != evidence_before:
        raise RuntimeError("历史证据可信迁移不得修改原始证据字段")
    migrations = legacy_ledger.get("evidence_integrity_migrations")
    if migrations is None:
        migrations = []
        legacy_ledger["evidence_integrity_migrations"] = migrations
    if not isinstance(migrations, list):
        raise ValueError("招商证据完整性迁移审计必须是列表")
    migrated_at = _utc_now()
    migration_id = "evidence-integrity-migration-" + hashlib.sha256(
        f"{actual_legacy_sha256}\x00{normalized_admin_user_id}\x00{migrated_at}".encode("utf-8")
    ).hexdigest()[:16]
    migration_audit = {
        "migration_id": migration_id,
        "migrated_at": migrated_at,
        "migrated_by_admin_user_id": normalized_admin_user_id,
        "legacy_ledger_sha256": actual_legacy_sha256,
        "legacy_integrity_scheme": (
            legacy_integrity.get("scheme") if isinstance(legacy_integrity, dict) else "unsigned"
        ),
        "migrated_evidence_ids": migrated_evidence_ids,
        "verification_note": normalized_note,
        "operation_scope": "admin_verified_legacy_evidence_integrity_migration_only",
    }
    migrations.append(migration_audit)
    revision = legacy_ledger.get("ledger_revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("历史招商线索台账中的 ledger_revision 必须是非负整数")
    legacy_ledger["ledger_revision"] = revision
    saved_path = save_ledger(
        workspace_dir,
        legacy_ledger,  # type: ignore[arg-type]
        expected_persisted_sha256=actual_legacy_sha256,
    )
    return {
        "migration": migration_audit,
        "migrated_evidence_count": len(migrated_evidence_ids),
        "ledger_path": str(saved_path),
    }


def reseal_verified_empty_ledger_integrity(
    workspace_dir: Path,
    *,
    expected_ledger_sha256: str,
    verification_note: str,
    authenticated_admin_user_id: str,
) -> dict[str, object]:
    """仅管理员可重签“无正式事实且整本签名不匹配”的当前空台账。

    输入是管理员已离线核验的当前文件 SHA-256、核验说明及仅可由 HTTP 认证层提供的
    管理员 ID；输出为只追加的恢复审计和台账路径。函数直接读取服务器固定工作区文件，
    在保存锁内二次比对 SHA-256，且要求 ``leads``、``evidence`` 均为空、证据链是当前
    空链格式、整本签名方案正确但验证确实失败。它仅变更修订号、服务器时间、整本 HMAC
    和 ``ledger_integrity_reseals`` 审计；所有其他字段保存前后逐字比对。普通用户、前端
    和模型不能提交管理员 ID、签名、证据、URL、摘录或采集时间，也不能借本函数迁移有
    正式事实的台账。

    付费升级方案：若未来需在多机或更高安全边界下处理这类密钥轮换/恢复，应使用 KMS/HSM
    记录密钥版本、对恢复审批进行双人复核，并将迁移前 SHA-256 与审计写入 WORM 或第三方
    时间戳服务；不要在客户端、插件配置或普通业务接口中保存旧主密钥或提供任意重签能力。
    """
    normalized_expected_sha256 = expected_ledger_sha256.strip().casefold()
    normalized_note = verification_note.strip()
    normalized_admin_user_id = authenticated_admin_user_id.strip()
    if (
        len(normalized_expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in normalized_expected_sha256)
    ):
        raise ValueError("expected_ledger_sha256必须是64位十六进制SHA-256值")
    if not normalized_note or len(normalized_note) > 2_000:
        raise ValueError("verification_note必须是1到2000个字符")
    if not normalized_admin_user_id or len(normalized_admin_user_id) > 100:
        raise ValueError("authenticated_admin_user_id必须是1到100个字符")

    path = ledger_path(workspace_dir)
    _ensure_private_workspace_directory(workspace_dir, LEDGER_RELATIVE_PATH.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError("待恢复招商台账必须是固定位置的普通文件")
    with path.open("rb") as file:
        persisted_bytes = file.read()
    actual_sha256 = hashlib.sha256(persisted_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, normalized_expected_sha256):
        raise RuntimeError("招商台账SHA-256与管理员核验值不一致，拒绝恢复")
    try:
        ledger = json.loads(persisted_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("待恢复招商台账不是有效 UTF-8 JSON") from exc
    if not isinstance(ledger, dict) or ledger.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("待恢复招商台账版本不受支持")
    leads = ledger.get("leads")
    evidence = ledger.get("evidence")
    if not isinstance(leads, list) or not isinstance(evidence, list):
        raise ValueError("待恢复招商台账缺少有效的 leads 或 evidence 列表")
    if leads or evidence:
        raise ValueError("整本签名恢复仅允许没有正式线索和正式证据的空台账")
    evidence_integrity = ledger.get("evidence_integrity")
    if (
        not isinstance(evidence_integrity, dict)
        or evidence_integrity.get("scheme") != INTEGRITY_SCHEME
        or evidence_integrity.get("record_count") != 0
        or evidence_integrity.get("chain_head_signature") is not None
    ):
        raise ValueError("待恢复台账必须具有当前格式的空证据链完整性元数据")
    # 即使无正式证据，也要让既有校验器复核空链结构；恢复从不放宽证据链格式规则。
    verify_evidence_integrity(workspace_dir, ledger)
    previous_ledger_integrity = ledger.get("ledger_integrity")
    previous_signature = (
        previous_ledger_integrity.get("signature")
        if isinstance(previous_ledger_integrity, dict)
        else None
    )
    if (
        not isinstance(previous_ledger_integrity, dict)
        or previous_ledger_integrity.get("scheme") != LEDGER_INTEGRITY_SCHEME
        or not isinstance(previous_signature, str)
        or len(previous_signature) != 64
        or any(character not in "0123456789abcdef" for character in previous_signature)
    ):
        raise ValueError("待恢复台账必须具有当前格式的整本完整性元数据")
    try:
        verify_ledger_integrity(workspace_dir, ledger)
    except RuntimeError as exc:
        if "完整性校验失败" not in str(exc):
            raise
    else:
        raise ValueError("当前招商台账完整性校验已通过，无需执行恢复")
    revision = ledger.get("ledger_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("待恢复招商台账缺少有效 ledger_revision")
    reseals = ledger.get("ledger_integrity_reseals")
    if reseals is None:
        reseals = []
        ledger["ledger_integrity_reseals"] = reseals
    if not isinstance(reseals, list):
        raise ValueError("招商线索台账中的 ledger_integrity_reseals 必须是列表")
    protected_before = json.dumps(
        {
            key: value
            for key, value in ledger.items()
            if key
            not in {
                "ledger_integrity",
                "ledger_revision",
                "updated_at",
                "ledger_integrity_reseals",
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    resealed_at = _utc_now()
    reseal_audit = {
        "reseal_id": "ledger-integrity-reseal-"
        + hashlib.sha256(
            f"{actual_sha256}\x00{normalized_admin_user_id}\x00{resealed_at}".encode("utf-8")
        ).hexdigest()[:16],
        "resealed_at": resealed_at,
        "resealed_by_admin_user_id": normalized_admin_user_id,
        "pre_reseal_ledger_sha256": actual_sha256,
        "previous_ledger_integrity_scheme": previous_ledger_integrity["scheme"],
        "verification_note": normalized_note,
        "operation_scope": "admin_verified_empty_ledger_integrity_reseal_only",
    }
    reseals.append(reseal_audit)
    protected_after = json.dumps(
        {
            key: value
            for key, value in ledger.items()
            if key
            not in {
                "ledger_integrity",
                "ledger_revision",
                "updated_at",
                "ledger_integrity_reseals",
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if protected_after != protected_before:
        raise RuntimeError("整本签名恢复不得修改空台账的其他业务记录")
    saved_path = save_ledger(
        workspace_dir,
        ledger,  # type: ignore[arg-type]
        expected_persisted_sha256=actual_sha256,
    )
    return {"reseal": reseal_audit, "ledger_path": str(saved_path)}


def save_ledger(
    workspace_dir: Path,
    ledger: LedgerDocument,
    *,
    expected_persisted_sha256: str | None = None,
) -> Path:
    """以版本比较、排他锁和原子替换保存台账，拒绝并发覆盖。

    输入是由 ``load_ledger`` 读取并完成单次业务变更的台账，输出正式文件路径。保存时
    在固定锁内核对磁盘 ``ledger_revision`` 是否仍等于读取版本；另一请求若已保存则
    明确报冲突，绝不以旧副本覆盖对方的线索、证据、复核或监控审计。核对通过后递增
    版本、写入临时文件并原子替换。管理员可信迁移可额外传入已核验的当前文件 SHA-256，
    锁内会再次比对后才允许写入；普通业务调用不能传入该安全参数。函数不自动合并冲突，
    也不猜测应该保留哪方判断。
    """
    verify_evidence_integrity(workspace_dir, ledger)
    expected_revision = ledger.get("ledger_revision")
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise ValueError("待保存招商线索台账缺少有效 ledger_revision")
    if expected_persisted_sha256 is not None and (
        not isinstance(expected_persisted_sha256, str)
        or len(expected_persisted_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_persisted_sha256)
    ):
        raise ValueError("expected_persisted_sha256必须是64位十六进制SHA-256值")
    path = ledger_path(workspace_dir)
    if path.is_symlink():
        raise ValueError("招商线索台账文件不能是符号链接")
    _ensure_private_workspace_directory(workspace_dir, LEDGER_RELATIVE_PATH.parent)
    with _exclusive_ledger_lock(workspace_dir):
        if expected_persisted_sha256 is not None:
            if not path.is_file():
                raise RuntimeError("历史招商线索台账在迁移保存前不存在，拒绝继续")
            with path.open("rb") as file:
                current_bytes = file.read()
            if not hmac.compare_digest(
                hashlib.sha256(current_bytes).hexdigest(),
                expected_persisted_sha256,
            ):
                raise RuntimeError("历史招商线索台账在迁移保存前已变化，拒绝继续")
        elif path.exists():
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("招商线索台账不是可验证的普通文件")
            try:
                with path.open("r", encoding="utf-8") as file:
                    persisted_ledger = json.load(file)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("招商线索台账无法读取或解析，拒绝覆盖") from exc
            if not isinstance(persisted_ledger, dict):
                raise RuntimeError("招商线索台账根节点必须是对象")
            # 业务函数传入的是已经修改过的内存副本；因此必须在锁内验证磁盘旧版本，而非
            # 错把待保存的新状态当作篡改。快照也同时复验，避免被替换的原文随业务写入掩盖。
            verify_evidence_integrity(workspace_dir, persisted_ledger)
            for persisted_evidence in persisted_ledger.get("evidence", []):
                if not isinstance(persisted_evidence, dict):
                    raise RuntimeError("招商证据记录必须是对象")
                _verify_evidence_snapshot(workspace_dir, persisted_evidence)
            verify_ledger_integrity(workspace_dir, persisted_ledger)
        else:
            integrity = ledger.get("ledger_integrity")
            if not isinstance(integrity, dict) or integrity.get("signature") is not None:
                raise RuntimeError("新招商台账完整性元数据无效")
        current_revision = _read_persisted_ledger_revision(path)
        if current_revision != expected_revision:
            raise RuntimeError("招商线索台账已被其他操作更新，请重新读取后再提交")
        ledger["ledger_revision"] = expected_revision + 1
        ledger["updated_at"] = _utc_now()
        sign_ledger_integrity(workspace_dir, ledger)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".lead_evidence_",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(ledger, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    return path
