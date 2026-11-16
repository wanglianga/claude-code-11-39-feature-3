"""跨主体出口证据链一致性与授权时效审计 endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import Principal, get_principal, require_writer
from ..db import get_db
from ..schemas import (
    AuditCaseCreateRequest,
    AuditEvaluateRequest,
    AuditEvidenceLinkRequest,
    AuditReviewDecisionRequest,
    AuditSupplementRequest,
    CredentialCreateRequest,
    CredentialReplacementRequest,
    CredentialRevocationRequest,
    CredentialScopeRevisionRequest,
    PartyControlRelationRequest,
    PartyCreateRequest,
    PartyNameChangeRequest,
)
from ..services import audit, audit_registry
from ..util import jsonable

router = APIRouter(prefix="/api/v1", tags=["audit"])


def _entity(db: Session, code: str) -> m.Entity:
    ent = db.scalar(select(m.Entity).where(m.Entity.code == code))
    if ent is None:
        raise HTTPException(404, f"entity not found: {code}")
    return ent


def _case_scoped(db: Session, case_id: int, principal: Principal) -> m.AuditCase:
    case = audit.load_case(db, case_id)
    principal.require_entity(case.entity_id)
    return case


# ---------------------------------------------------------------------------
# Party registry
# ---------------------------------------------------------------------------


@router.post("/audit/parties", status_code=201, tags=["audit-registry"])
def register_party(
    body: PartyCreateRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记法人主体/分支机构档案：统一社会标识、有效期、控制关系与名称变更。有效期用于按出口日判定主体资格，不使用当前状态。"""
    if body.entity_code:
        ent = _entity(db, body.entity_code)
        principal.require_entity(ent.id)
    party = audit_registry.create_party(db, body, principal.name)
    db.commit()
    return audit_registry.party_view(db, party)


@router.get("/audit/parties", tags=["audit-registry"])
def list_parties(
    entity_code: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出主体档案（含名称变更史与控制关系）。"""
    stmt = select(m.PartyProfile).order_by(m.PartyProfile.code)
    rows = db.scalars(stmt).all()
    out = []
    for p in rows:
        if p.entity_id is not None and not principal.can_act_on(p.entity_id):
            continue
        out.append(audit_registry.party_view(db, p))
    return out


@router.get("/audit/parties/{party_code}", tags=["audit-registry"])
def get_party(
    party_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """主体档案详情：法人名称、统一社会标识、有效期、状态、名称变更与控制关系。"""
    party = audit_registry.get_party(db, party_code)
    if party.entity_id is not None:
        principal.require_entity(party.entity_id)
    return audit_registry.party_view(db, party)


@router.post("/audit/parties/{party_code}/name-changes", status_code=201, tags=["audit-registry"])
def add_name_change(
    party_code: str,
    body: PartyNameChangeRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记名称变更。必须由统一社会标识或官方继承文件核验；仅凭名称相似不得归并同一主体。"""
    party = audit_registry.get_party(db, party_code)
    if party.entity_id is not None:
        principal.require_entity(party.entity_id)
    change = audit_registry.add_name_change(db, party_code, body, principal.name)
    db.commit()
    return {
        "id": change.id,
        "party_id": change.party_id,
        "old_name": change.old_name,
        "new_name": change.new_name,
        "effective_from": change.effective_from,
        "uscc_verified": change.uscc_verified,
        "inheritance_doc_no": change.inheritance_doc_no,
    }


@router.post("/audit/parties/{party_code}/control-relations", status_code=201, tags=["audit-registry"])
def add_control_relation(
    party_code: str,
    body: PartyControlRelationRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记控制关系/分支机构关系（带有效区间），用于集团内部关系与主体资格判定。"""
    party = audit_registry.get_party(db, party_code)
    if party.entity_id is not None:
        principal.require_entity(party.entity_id)
    rel = audit_registry.add_control_relation(db, party_code, body, principal.name)
    db.commit()
    return {
        "id": rel.id,
        "parent_party_id": rel.parent_party_id,
        "child_party_id": rel.child_party_id,
        "relation_type": rel.relation_type,
        "valid_from": rel.valid_from,
        "valid_to": rel.valid_to,
    }


# ---------------------------------------------------------------------------
# Credential registry
# ---------------------------------------------------------------------------


@router.post("/entities/{entity_code}/credentials", status_code=201, tags=["audit-credentials"])
def create_credential(
    entity_code: str,
    body: CredentialCreateRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记授权凭证（委托出口/报关授权/货权转让/集团内部协议）：签发方、授权范围、商品与地区限制、生效终止日、接收时间、原文哈希与替代关系。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    if body.entity_code != entity_code:
        raise HTTPException(422, "entity_code in the body must match the path")
    cred = audit_registry.create_credential(db, body, principal.name)
    db.commit()
    return audit_registry.credential_view(db, cred)


@router.get("/entities/{entity_code}/credentials", tags=["audit-credentials"])
def list_credentials(
    entity_code: str,
    kind: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出授权凭证及其版本、撤销通知与状态。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    rows = db.scalars(audit_registry.credentials_query(db, ent.id, kind=kind)).all()
    return [audit_registry.credential_view(db, c) for c in rows]


@router.get("/credentials/{credential_id}", tags=["audit-credentials"])
def get_credential(
    credential_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """授权凭证详情：范围、生效/接收时间、原文哈希、替代关系、撤销通知。"""
    cred = audit_registry.get_credential(db, credential_id)
    principal.require_entity(cred.entity_id)
    return audit_registry.credential_view(db, cred)


@router.post("/credentials/{credential_id}/revocations", status_code=201, tags=["audit-credentials"])
def add_revocation(
    credential_id: int,
    body: CredentialRevocationRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记撤销通知。两份互斥且都声称在出口日前生效的撤销会使授权链冻结并要求核对签发来源与序号。"""
    cred = audit_registry.get_credential(db, credential_id)
    principal.require_entity(cred.entity_id)
    rev = audit_registry.add_revocation(db, cred, body, principal.name)
    event = audit.handle_shared_change(
        db,
        cred,
        kind="revocation_recorded",
        actor=principal.name,
        detail={"revocation_id": rev.id, "revocation_no": rev.revocation_no,
                "claimed_effective_from": rev.claimed_effective_from},
    )
    db.commit()
    return {
        "revocation_id": rev.id,
        "credential_id": cred.id,
        "credential_status": cred.status,
        "frozen_reason": cred.frozen_reason,
        "shared_change_event": _event_view(event),
    }


@router.post("/credentials/{credential_id}/scope-revision", status_code=201, tags=["audit-credentials"])
def revise_scope(
    credential_id: int,
    body: CredentialScopeRevisionRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """按版本追加授权范围修订（不覆盖旧版本），并触发共享凭证变动传播。"""
    cred = audit_registry.get_credential(db, credential_id)
    principal.require_entity(cred.entity_id)
    new = audit_registry.scope_revision(db, cred, body, principal.name)
    event = audit.handle_shared_change(
        db,
        cred,
        kind="scope_revision",
        actor=principal.name,
        detail={"revision_credential_id": new.id, "new_version": new.version,
                "scope": new.scope, "quantity_limit": new.quantity_limit},
    )
    db.commit()
    return {
        "credential": audit_registry.credential_view(db, new),
        "previous_credential_id": cred.id,
        "shared_change_event": _event_view(event),
    }


@router.post("/credentials/{credential_id}/replace", status_code=201, tags=["audit-credentials"])
def replace_credential(
    credential_id: int,
    body: CredentialReplacementRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """替换授权凭证：旧凭证标记为被替代，新凭证通过 supersedes 关系继承依赖关系，并触发共享变动传播。"""
    cred = audit_registry.get_credential(db, credential_id)
    principal.require_entity(cred.entity_id)
    if db.scalar(
        select(m.AuthorizationCredential.id).where(
            m.AuthorizationCredential.entity_id == cred.entity_id,
            m.AuthorizationCredential.credential_no == body.credential_no,
            m.AuthorizationCredential.version == 1,
        )
    ):
        raise HTTPException(409, f"credential {body.credential_no} already exists")
    replacement = m.AuthorizationCredential(
        entity_id=cred.entity_id,
        credential_no=body.credential_no,
        kind=cred.kind,
        grantor_party_id=cred.grantor_party_id,
        grantee_party_id=cred.grantee_party_id,
        issued_at=body.issued_at,
        received_time=body.received_time,
        effective_from=body.effective_from,
        effective_to=body.effective_to if body.effective_to is not None else cred.effective_to,
        scope=jsonable(audit_registry._normalise_scope(body.scope or cred.scope)),
        quantity_limit=body.quantity_limit if body.quantity_limit is not None else cred.quantity_limit,
        currency=body.currency or cred.currency,
        original_location=body.original_location,
        sha256=body.sha256 or audit_registry.credential_sha(body.model_dump(mode="json")),
        supersedes_id=cred.id,
        intragroup_acts_as=cred.intragroup_acts_as,
        source_document_id=body.source_document_id,
        created_by=principal.name,
    )
    db.add(replacement)
    db.flush()
    cred.status = "superseded"
    db.flush()
    # Rows depended on the old credential; the replacement is inherited through the same
    # (grantor, grantee, kind) edge so each row can be rejudged against the new scope.
    event = audit.handle_shared_change(
        db,
        cred,
        kind="replacement",
        actor=principal.name,
        detail={
            "replacement_credential_id": replacement.id,
            "replacement_credential_no": replacement.credential_no,
            "effective_from": replacement.effective_from,
            "scope": replacement.scope,
        },
    )
    db.commit()
    return {
        "replacement_credential": audit_registry.credential_view(db, replacement),
        "superseded_credential_id": cred.id,
        "shared_change_event": _event_view(event),
    }


# ---------------------------------------------------------------------------
# Audit cases
# ---------------------------------------------------------------------------


@router.post("/entities/{entity_code}/audit-cases", status_code=201, tags=["audit-cases"])
def create_audit_case(
    entity_code: str,
    body: AuditCaseCreateRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """从发票行与报关商品行建立审计案卷：登记发票主体/货权主体/报关主体、商品、地区与出口日，并锚定主体与证据节点。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    case = audit.build_case(db, ent, body, principal.name)
    conclusion = audit.evaluate_case(db, case, actor=principal.name, note=body.note, kind="initial")
    db.commit()
    return {
        "case_id": case.id,
        "case_ref": case.case_ref,
        "current_conclusion": audit.conclusion_view(db, conclusion),
    }


@router.get("/entities/{entity_code}/audit-cases", tags=["audit-cases"])
def list_audit_cases(
    entity_code: str,
    state: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出主体的审计案卷及当前结论状态（可按结论状态过滤）。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    cases = db.scalars(
        select(m.AuditCase).where(m.AuditCase.entity_id == ent.id).order_by(m.AuditCase.id)
    ).all()
    out = []
    for case in cases:
        current = audit.current_conclusion(db, case.id)
        if state and (current is None or current.state != state):
            continue
        out.append(
            jsonable(
                {
                    "case_id": case.id,
                    "case_ref": case.case_ref,
                    "export_line_id": case.export_line_id,
                    "product_sku": case.product_sku,
                    "region": case.country,
                    "quantity": case.quantity,
                    "currency": case.currency,
                    "export_date": case.export_date,
                    "current_state": current.state if current else None,
                    "current_conclusion_id": current.id if current else None,
                    "version": current.version if current else None,
                    "usable_for_new_dossier": current.usable_for_new_dossier if current else False,
                }
            )
        )
    return out


@router.get("/audit-cases/{case_id}", tags=["audit-cases"])
def get_audit_case(
    case_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """审计案卷全展开：主体档案、授权文本、生效与接收时间、货权/运输证据、商品地区范围、冻结版本与复审历史。"""
    case = _case_scoped(db, case_id, principal)
    return audit.case_detail(db, case)


@router.post("/audit-cases/{case_id}/evidence", status_code=201, tags=["audit-cases"])
def add_evidence(
    case_id: int,
    body: list[AuditEvidenceLinkRequest],
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """追加证据关系图主张（合同、装箱单、运输凭证、货权凭证等），带有效区间与数量。"""
    case = _case_scoped(db, case_id, principal)
    created = [audit.add_evidence_link(db, case, link, principal.name) for link in body]
    db.commit()
    return {"case_id": case.id, "link_ids": [l.id for l in created]}


@router.post("/audit-cases/{case_id}/evaluate", tags=["audit-cases"])
def evaluate(
    case_id: int,
    body: AuditEvaluateRequest | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """在出口日逐段判定主体有效、授权有效、货权链连续、商品范围覆盖与数量证据一致，并冻结支撑版本。"""
    case = _case_scoped(db, case_id, principal)
    conclusion = audit.evaluate_case(
        db, case, actor=principal.name, note=body.note if body else None
    )
    db.commit()
    return audit.conclusion_view(db, conclusion)


@router.post("/audit-cases/{case_id}/supplements", status_code=201, tags=["audit-cases"])
def add_supplement(
    case_id: int,
    body: AuditSupplementRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """补传材料（可含后补授权/货权/运输证据）。改变关键时间区间时当前结论置为复审中并暂停供新案卷引用；历史结论保留冻结版本。"""
    case = _case_scoped(db, case_id, principal)
    links = audit.add_supplement(db, case, body.links, principal.name, body.note)
    db.commit()
    return {
        "case_id": case.id,
        "supplement_link_ids": [l.id for l in links],
        "current_conclusion": audit.conclusion_view(db, audit.current_conclusion(db, case.id))
        if audit.current_conclusion(db, case.id)
        else None,
    }


@router.post("/audit-cases/{case_id}/review", tags=["audit-cases"])
def review_decision(
    case_id: int,
    body: AuditReviewDecisionRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """复审核准或撤销。核准可签发修订结论并恢复引用；撤销或覆盖不足则转为受限确认/断链，不得把数量转移给其他主体。"""
    case = _case_scoped(db, case_id, principal)
    if body.action == "approve":
        conclusion = audit.approve_review(db, case, principal.name, body.note)
    else:
        conclusion = audit.reject_review(db, case, principal.name, body.note)
    db.commit()
    return audit.conclusion_view(db, conclusion)


@router.get("/audit-cases/{case_id}/conclusions", tags=["audit-cases"])
def conclusion_history(
    case_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出该案卷的全部结论版本（含历史冻结版本与当时依据），历史结论文本不被改写。"""
    case = _case_scoped(db, case_id, principal)
    rows = db.scalars(
        select(m.AuditConclusion)
        .where(m.AuditConclusion.case_id == case.id)
        .order_by(m.AuditConclusion.version.desc())
    ).all()
    return [audit.conclusion_view(db, c) for c in rows]


@router.get("/credentials/{credential_id}/shared-changes", tags=["audit-credentials"])
def credential_shared_changes(
    credential_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出某共享凭证的变动事件：受影响的出口行、被暂停的行、逐行重新判定的结果。"""
    cred = audit_registry.get_credential(db, credential_id)
    principal.require_entity(cred.entity_id)
    rows = db.scalars(
        select(m.AuditSharedChangeEvent)
        .where(m.AuditSharedChangeEvent.credential_id == cred.id)
        .order_by(m.AuditSharedChangeEvent.id)
    ).all()
    return [_event_view(e) for e in rows]


@router.get("/entities/{entity_code}/audit-conclusions", tags=["audit-cases"])
def latest_conclusions(
    entity_code: str,
    usable_only: bool = False,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出主体每个出口行的当前审计结论（可只返回可供新对账案卷引用的结论）。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    cases = db.scalars(
        select(m.AuditCase).where(m.AuditCase.entity_id == ent.id).order_by(m.AuditCase.id)
    ).all()
    out = []
    for case in cases:
        current = audit.current_conclusion(db, case.id)
        if current is None:
            continue
        if usable_only and not current.usable_for_new_dossier:
            continue
        out.append(audit.conclusion_view(db, current))
    return out


def _event_view(event: m.AuditSharedChangeEvent) -> dict:
    return jsonable(
        {
            "id": event.id,
            "entity_id": event.entity_id,
            "credential_id": event.credential_id,
            "kind": event.kind,
            "detected_at": event.detected_at,
            "detail": event.detail,
            "affected_case_ids": event.affected_case_ids,
            "paused_case_ids": event.paused_case_ids,
            "unchanged_case_ids": event.unchanged_case_ids,
            "rejudged": event.rejudged,
            "created_by": event.created_by,
        }
    )
