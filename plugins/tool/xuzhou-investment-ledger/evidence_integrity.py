"""招商原始证据链的本地完整性签名。

签名密钥与可审查的台账 JSON 分离保存。它用于发现 API、模型或文件编辑器对既有
evidence 的删除、重排或改写；它不是取代操作系统访问控制或异地存证的万能安全边界。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from qwenpaw.security.secret_store import _get_master_key


INTEGRITY_SCHEME = "hmac-sha256-evidence-chain-v2"
LEDGER_INTEGRITY_SCHEME = "hmac-sha256-ledger-v1"
_KEY_DERIVATION_CONTEXT = b"qwenpaw-xuzhou-evidence-chain-v2\x00"
_LEDGER_INTEGRITY_CONTEXT = b"qwenpaw-xuzhou-ledger-v1\x00"


def _workspace_integrity_key(workspace_dir: Path) -> bytes:
    """从 QwenPaw 平台私钥派生当前工作区独立的证据 HMAC 密钥。

    输入是通过平台鉴权隔离的工作区，输出为仅在内存中存在的派生密钥。平台根密钥位于
    QwenPaw ``secret_store`` 的受限安全边界而非工作区，派生时加入固定用途和规范化路径，
    避免不同工作区或其他加密用途共享同一 HMAC 密钥。付费升级方案：若需抵御服务器管理
    员或主机被攻破，应在部署阶段改为 KMS/HSM 或异地不可变存证；该能力不由普通用户、
    模型或前端调用。本函数不写入台账、工作区或日志，也不修改证据。

    ===== 付费升级（暂不启用；仅供后续工程师预算）=====
    若需在服务器安全边界外保护签名根密钥，应改由 KMS/HSM 完成签名或密钥派生，且将迁移
    前后哈希同步至不可变对象存储/第三方时间戳。以阿里云公开价作预算参考：软件 KMS 实例
    约 2,499 元/月、硬件 KMS 实例约 7,699 元/月；对象原文存储另按容量、请求、流量和锁定
    保留策略计费。实际价格、数据地域和合规要求须在采购时复核；禁止把 KMS 凭据、对象存储
    密钥或签名私钥写进工作区、模型输入、前端或本插件配置。
    """
    try:
        platform_master_key = _get_master_key()
    except Exception as exc:
        raise RuntimeError("无法取得平台证据签名根密钥，拒绝读写招商证据") from exc
    if not isinstance(platform_master_key, bytes) or len(platform_master_key) < 32:
        raise RuntimeError("平台证据签名根密钥格式无效，拒绝读写招商证据")
    workspace_context = str(workspace_dir.resolve()).encode("utf-8")
    return hmac.new(
        platform_master_key,
        _KEY_DERIVATION_CONTEXT + workspace_context,
        hashlib.sha256,
    ).digest()


def empty_evidence_integrity() -> dict[str, object]:
    """返回空证据链的可序列化完整性元数据，不创建密钥也不写入磁盘。

    输出明确声明签名算法、零条记录和空链头。空链没有需要签名的事实，因此不提前创建
    密钥；真正首次追加正式证据时才会取得平台私钥的内存派生值并生成第一条签名。
    """
    return {
        "scheme": INTEGRITY_SCHEME,
        "record_count": 0,
        "chain_head_signature": None,
    }


def empty_ledger_integrity() -> dict[str, object]:
    """返回尚未保存的新台账完整性元数据，不取得密钥也不签名。

    输出声明整本台账签名方案和空签名；新工作区首次原子保存时才由 ``sign_ledger_integrity``
    取得平台密钥。它覆盖复核审计、评分规则、来源目录和监控记录等业务数据，却不替代证据
    链对单条原始证据的顺序签名；函数不读写工作区、不修改台账。
    """
    return {"scheme": LEDGER_INTEGRITY_SCHEME, "signature": None}


def _canonical_ledger_bytes(ledger: dict[str, Any]) -> bytes:
    """将整本台账除自身签名元数据外规范化为稳定字节。

    输入是待验证或待保存的台账对象，输出为稳定 UTF-8 JSON 字节；只排除
    ``ledger_integrity``，避免签名递归。其余线索状态历史、评分规则、来源目录、监控审计、
    证据链元数据和修订号都被覆盖，因此直接编辑 JSON 无法伪造业务审计；函数不读写文件。
    """
    unsigned = {key: value for key, value in ledger.items() if key != "ledger_integrity"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verify_ledger_integrity(workspace_dir: Path, ledger: dict[str, Any]) -> None:
    """验证整本台账的 HMAC，阻止复核、规则和监控审计被离线篡改。

    输入是当前工作区和已完成结构校验的台账，成功时无返回值，缺少、格式错误或不匹配的
    签名均失败关闭。平台密钥只在内存派生；普通用户、模型、前端和人工复核均没有重签
    接口。函数不会修复或覆盖可疑业务数据，历史无该签名的台账只能经管理员可信迁移。
    """
    integrity = ledger.get("ledger_integrity")
    if not isinstance(integrity, dict):
        raise RuntimeError("招商台账缺少完整性签名，需管理员完成可信迁移后才能继续使用")
    if integrity.get("scheme") != LEDGER_INTEGRITY_SCHEME:
        raise RuntimeError("招商台账完整性签名方案不受支持")
    actual_signature = integrity.get("signature")
    if not isinstance(actual_signature, str) or len(actual_signature) != 64:
        raise RuntimeError("招商台账完整性签名无效")
    expected_signature = hmac.new(
        _workspace_integrity_key(workspace_dir),
        _LEDGER_INTEGRITY_CONTEXT + _canonical_ledger_bytes(ledger),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise RuntimeError("招商台账完整性校验失败，拒绝使用可能被篡改的业务数据")


def sign_ledger_integrity(workspace_dir: Path, ledger: dict[str, Any]) -> None:
    """在服务器完成一次受控业务保存后，为整本台账写入新的 HMAC。

    输入是已验证旧签名、已更新修订号和业务字段的台账，输出为空并仅更新
    ``ledger_integrity``。调用方只能是固定存储层：它先验证旧台账，再在同一原子写入前
    重签，从而正常复核、评分规则或监控审计更新不会破坏校验；任何前端或模型均不能直接
    提交签名、覆盖原始证据或绕过管理员迁移。
    """
    ledger["ledger_integrity"] = {
        "scheme": LEDGER_INTEGRITY_SCHEME,
        "signature": hmac.new(
            _workspace_integrity_key(workspace_dir),
            _LEDGER_INTEGRITY_CONTEXT + _canonical_ledger_bytes(ledger),
            hashlib.sha256,
        ).hexdigest(),
    }


def _canonical_evidence_bytes(evidence: dict[str, Any]) -> bytes:
    """将一条证据除自身签名外规范化为稳定字节，防止字段顺序影响 HMAC 结果。

    输入是内存中的证据对象，输出为 UTF-8 JSON 字节。``integrity_signature`` 被排除是
    因为它正是待计算结果；其余原始 URL、摘录、采集时间、哈希及评分依据均被覆盖，
    因而任一改写都会改变签名。函数不读写台账、不修改对象、不联网。
    """
    unsigned = {key: value for key, value in evidence.items() if key != "integrity_signature"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_signature(
    key: bytes,
    *,
    record_index: int,
    previous_signature: str | None,
    evidence: dict[str, Any],
) -> str:
    """计算一条证据在链中唯一位置的 HMAC，不接受外部传入的签名值。

    输入是服务器私钥、当前列表下标、上一条签名和待追加证据；输出是十六进制 HMAC。
    记录下标与前一签名一起进入固定分隔的消息，既能发现内容篡改，也能发现删除或重排；
    本函数纯计算，不写台账、密钥或任何业务状态。
    """
    message = _KEY_DERIVATION_CONTEXT
    message += str(record_index).encode("ascii") + b"\x00"
    message += (previous_signature or "").encode("ascii") + b"\x00"
    message += _canonical_evidence_bytes(evidence)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_evidence_integrity(workspace_dir: Path, ledger: dict[str, Any]) -> None:
    """验证整条原始证据 HMAC 链，发现篡改时拒绝继续读取或保存台账。

    输入是当前工作区和已解析的台账，成功时无返回值，失败时抛出明确异常。它覆盖证据
    内容、顺序、数量和链头，任何 API 复核、图谱、评分或监控流程在 ``load_ledger`` /
    ``save_ledger`` 中都会经过该校验，因而不能借业务写入掩盖篡改；函数不会修复、重签
    或覆盖异常数据，避免把攻击后的内容重新视为可信。
    """
    evidence_records = ledger.get("evidence")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    integrity = ledger.get("evidence_integrity")
    if integrity is None:
        if evidence_records:
            raise RuntimeError("历史招商证据尚未签名，需管理员完成可信迁移后才能继续使用")
        return
    if not isinstance(integrity, dict):
        raise RuntimeError("招商证据完整性元数据格式异常")
    if integrity.get("scheme") != INTEGRITY_SCHEME:
        if integrity.get("scheme") == "hmac-sha256-evidence-chain-v1":
            # 迁移前始终失败关闭，普通用户、模型和前端没有任何“信任旧链”或重签入口。
            # 付费升级方案：迁移完成后可将旧链哈希写入 WORM 存储、KMS/HSM 签名服务或
            # 第三方时间戳服务，提供跨主机、跨管理员的独立证明。
            raise RuntimeError(
                "历史招商证据使用已弃用的工作区侧密钥，需管理员完成可信迁移后才能继续使用"
            )
        raise RuntimeError("招商证据完整性签名方案不受支持")
    record_count = integrity.get("record_count")
    chain_head_signature = integrity.get("chain_head_signature")
    if (
        not isinstance(record_count, int)
        or isinstance(record_count, bool)
        or record_count < 0
        or record_count != len(evidence_records)
    ):
        raise RuntimeError("招商证据完整性记录数量不一致")
    if not evidence_records:
        if chain_head_signature is not None:
            raise RuntimeError("空招商证据链不应包含链头签名")
        return
    if not isinstance(chain_head_signature, str) or len(chain_head_signature) != 64:
        raise RuntimeError("招商证据完整性链头签名无效")

    key = _workspace_integrity_key(workspace_dir)
    previous_signature: str | None = None
    for record_index, evidence in enumerate(evidence_records):
        if not isinstance(evidence, dict):
            raise RuntimeError("招商证据记录必须是对象")
        actual_signature = evidence.get("integrity_signature")
        if not isinstance(actual_signature, str) or len(actual_signature) != 64:
            raise RuntimeError("招商证据记录缺少有效完整性签名")
        expected_signature = _record_signature(
            key,
            record_index=record_index,
            previous_signature=previous_signature,
            evidence=evidence,
        )
        if not hmac.compare_digest(actual_signature, expected_signature):
            raise RuntimeError("招商原始证据完整性校验失败，拒绝使用可能被篡改的台账")
        previous_signature = actual_signature
    if not hmac.compare_digest(chain_head_signature, previous_signature or ""):
        raise RuntimeError("招商原始证据链头签名校验失败，拒绝使用可能被篡改的台账")


def seal_verified_legacy_evidence(workspace_dir: Path, ledger: dict[str, Any]) -> list[str]:
    """仅在管理员已核验旧台账后，将既有证据重封为当前 v2 HMAC 链。

    输入是尚未签名或 v1 签名的内存台账，输出按原顺序重封的证据 ID 列表。调用方必须在
    外层完成管理员身份验证、迁移前文件 SHA-256 比对和人工来源核验；本函数只替换每条
    记录的 ``integrity_signature`` 安全元数据并重建链头，随后逐字比较所有非签名字段，
    因而不会改写 URL、摘录、采集时间、原始内容哈希或评分依据。v2 台账、格式异常记录
    和无法列出 ID 的历史数据均拒绝处理，避免普通业务路径借迁移重新信任不明事实。
    """
    evidence_records = ledger.get("evidence")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    integrity = ledger.get("evidence_integrity")
    if integrity is not None:
        if not isinstance(integrity, dict):
            raise RuntimeError("招商证据完整性元数据格式异常")
        scheme = integrity.get("scheme")
        if scheme == INTEGRITY_SCHEME:
            raise RuntimeError("当前招商证据链已经是 v2 签名，不能重复迁移")
        if scheme != "hmac-sha256-evidence-chain-v1":
            raise RuntimeError("历史招商证据完整性签名方案不受支持")

    unsigned_before = json.dumps(
        [
            {key: value for key, value in evidence.items() if key != "integrity_signature"}
            if isinstance(evidence, dict)
            else evidence
            for evidence in evidence_records
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = _workspace_integrity_key(workspace_dir)
    previous_signature: str | None = None
    migrated_evidence_ids: list[str] = []
    for record_index, evidence in enumerate(evidence_records):
        if not isinstance(evidence, dict):
            raise RuntimeError("招商证据记录必须是对象")
        evidence_id = evidence.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise RuntimeError("招商证据记录缺少有效 evidence_id")
        evidence.pop("integrity_signature", None)
        signature = _record_signature(
            key,
            record_index=record_index,
            previous_signature=previous_signature,
            evidence=evidence,
        )
        evidence["integrity_signature"] = signature
        previous_signature = signature
        migrated_evidence_ids.append(evidence_id)
    ledger["evidence_integrity"] = {
        "scheme": INTEGRITY_SCHEME,
        "record_count": len(evidence_records),
        "chain_head_signature": previous_signature,
    }
    unsigned_after = json.dumps(
        [
            {key: value for key, value in evidence.items() if key != "integrity_signature"}
            if isinstance(evidence, dict)
            else evidence
            for evidence in evidence_records
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if unsigned_after != unsigned_before:
        raise RuntimeError("历史证据迁移不得修改原始证据字段")
    verify_evidence_integrity(workspace_dir, ledger)
    return migrated_evidence_ids


def sign_next_evidence(workspace_dir: Path, ledger: dict[str, Any], evidence: dict[str, Any]) -> None:
    """为即将追加的正式证据生成签名并推进链头，禁止对历史记录重新签名。

    输入是通过完整性校验的台账和尚未加入 ``evidence`` 列表的新证据；函数就地补充新证据
    的签名并更新元数据，输出为空。调用方只能在正式受控抓取路径创建证据时调用，不能
    用它修改或重签已有 URL、摘录、采集时间、哈希或评分依据；历史链异常会直接失败。
    """
    verify_evidence_integrity(workspace_dir, ledger)
    evidence_records = ledger.get("evidence")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    if "integrity_signature" in evidence:
        raise ValueError("新招商证据不得提交完整性签名")
    integrity = ledger.get("evidence_integrity")
    if integrity is None:
        integrity = empty_evidence_integrity()
        ledger["evidence_integrity"] = integrity
    if not isinstance(integrity, dict):
        raise RuntimeError("招商证据完整性元数据格式异常")
    previous_signature = integrity.get("chain_head_signature")
    if previous_signature is not None and not isinstance(previous_signature, str):
        raise RuntimeError("招商证据完整性链头签名无效")
    signature = _record_signature(
        _workspace_integrity_key(workspace_dir),
        record_index=len(evidence_records),
        previous_signature=previous_signature,
        evidence=evidence,
    )
    evidence["integrity_signature"] = signature
    integrity["scheme"] = INTEGRITY_SCHEME
    integrity["record_count"] = len(evidence_records) + 1
    integrity["chain_head_signature"] = signature
