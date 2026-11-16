"""主体档案册与授权凭证册.

Rules enforced here:
  * A party's validity on the export date is decided by its registered validity interval and
    status — never by its current status.
  * A name change is only accepted as identity evidence when verified by the unified social
    credit identifier (USCC) or an official inheritance document. Name similarity alone is
    never enough to merge two parties.
  * A credential records issued_at / received_time / effective_from / effective_to separately.
    Received time is metadata for review, never a substitute for the export-date state.
  * Two mutually exclusive revocations both claiming to take effect before the export date
    freeze the credential; the judger may not pick one of them to produce a "verified" state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from .. import models as m
from ..schemas import (
    CredentialCreateRequest,
    CredentialRevocationRequest,
    CredentialScopeRevisionRequest,
    PartyControlRelationRequest,
    PartyCreateRequest,
    PartyNameChangeRequest,
)
from ..util import jsonable, sha256_of

CREDENTIAL_ROLES = {
    "entrusted_export": "export_authorization",
    "customs_authorization": "customs_authorization",
    "title_transfer": "title",
    "intragroup_agreement": "intragroup",
    "other": "other",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Party registry
# ---------------------------------------------------------------------------


def get_party(db: Session, code: str) -> m.PartyProfile:
    party = db.scalar(select(m.PartyProfile).where(m.PartyProfile.code == code))
    if party is None:
        raise HTTPException(404, f"party not found: {code}")
    return party


def create_party(db: Session, req: PartyCreateRequest, actor: str) -> m.PartyProfile:
    if db.scalar(select(m.PartyProfile.id).where(m.PartyProfile.code == req.code)):
        raise HTTPException(409, f"party {req.code} already exists")
    head = get_party(db, req.head_office_code) if req.head_office_code else None
    entity = None
    if req.entity_code:
        entity = db.scalar(select(m.Entity).where(m.Entity.code == req.entity_code))
        if entity is None:
            raise HTTPException(422, f"entity_code not found: {req.entity_code}")
    if head is not None and head.uscc and req.uscc and head.uscc != req.uscc and req.is_branch:
        raise HTTPException(
            422,
            "a branch may not carry a USCC that contradicts its head office without an official "
            "inheritance document",
        )
    party = m.PartyProfile(
        entity_id=entity.id if entity else None,
        code=req.code,
        legal_name=req.legal_name,
        uscc=req.uscc,
        country=req.country,
        is_branch=req.is_branch,
        head_office_id=head.id if head else None,
        valid_from=req.valid_from,
        valid_to=req.valid_to,
        status=req.status,
        source_document_id=req.source_document_id,
    )
    db.add(party)
    db.flush()
    if head is not None:
        db.add(
            m.PartyControlRelation(
                parent_party_id=head.id,
                child_party_id=party.id,
                relation_type="branch" if req.is_branch else "control",
                valid_from=req.valid_from,
                valid_to=req.valid_to,
                source_document_id=req.source_document_id,
            )
        )
    db.add(
        m.AuditLog(
            entity_id=entity.id if entity else None,
            actor=actor,
            action="audit.party.register",
            object_type="party_profile",
            object_id=party.id,
            payload=jsonable({"code": party.code, "legal_name": party.legal_name, "uscc": party.uscc}),
        )
    )
    db.flush()
    return party


def add_name_change(db: Session, party_code: str, req: PartyNameChangeRequest, actor: str) -> m.PartyNameChange:
    party = get_party(db, party_code)
    if req.old_name.strip() != party.legal_name.strip():
        raise HTTPException(
            422,
            f"old_name '{req.old_name}' does not match the registered legal name "
            f"'{party.legal_name}' of party {party.code}",
        )
    if not req.uscc_verified and not (req.inheritance_doc_no and req.inheritance_location):
        raise HTTPException(
            422,
            "a name change must be verified by the unified social credit identifier or an official "
            "inheritance document; name similarity alone is not identity evidence",
        )
    change = m.PartyNameChange(
        party_id=party.id,
        old_name=req.old_name,
        new_name=req.new_name,
        effective_from=req.effective_from,
        uscc_verified=req.uscc_verified,
        inheritance_doc_no=req.inheritance_doc_no,
        inheritance_location=req.inheritance_location,
        inheritance_sha256=req.inheritance_sha256,
        source_document_id=req.source_document_id,
    )
    db.add(change)
    party.legal_name = req.new_name
    party.version += 1
    db.add(
        m.AuditLog(
            entity_id=party.entity_id,
            actor=actor,
            action="audit.party.name_change",
            object_type="party_profile",
            object_id=party.id,
            payload=jsonable(
                {
                    "old_name": req.old_name,
                    "new_name": req.new_name,
                    "effective_from": req.effective_from,
                    "uscc_verified": req.uscc_verified,
                    "inheritance_doc_no": req.inheritance_doc_no,
                }
            ),
        )
    )
    db.flush()
    return change


def add_control_relation(
    db: Session, parent_code: str, req: PartyControlRelationRequest, actor: str
) -> m.PartyControlRelation:
    parent = get_party(db, parent_code)
    child = get_party(db, req.child_party_code)
    if parent.id == child.id:
        raise HTTPException(422, "a party cannot control itself")
    rel = m.PartyControlRelation(
        parent_party_id=parent.id,
        child_party_id=child.id,
        relation_type=req.relation_type,
        valid_from=req.valid_from,
        valid_to=req.valid_to,
        source_document_id=req.source_document_id,
    )
    db.add(rel)
    db.add(
        m.AuditLog(
            entity_id=parent.entity_id,
            actor=actor,
            action="audit.party.control_relation",
            object_type="party_control_relation",
            object_id=None,
            payload=jsonable(
                {"parent": parent_code, "child": req.child_party_code, "relation_type": req.relation_type}
            ),
        )
    )
    db.flush()
    return rel


def party_view(db: Session, party: m.PartyProfile) -> dict:
    changes = db.scalars(
        select(m.PartyNameChange)
        .where(m.PartyNameChange.party_id == party.id)
        .order_by(m.PartyNameChange.effective_from, m.PartyNameChange.id)
    ).all()
    relations = db.execute(
        select(m.PartyControlRelation, m.PartyProfile)
        .join(m.PartyProfile, m.PartyProfile.id == m.PartyControlRelation.parent_party_id)
        .where(m.PartyControlRelation.child_party_id == party.id)
    ).all()
    head = db.get(m.PartyProfile, party.head_office_id) if party.head_office_id else None
    return jsonable(
        {
            "id": party.id,
            "code": party.code,
            "legal_name": party.legal_name,
            "uscc": party.uscc,
            "country": party.country,
            "is_branch": party.is_branch,
            "head_office": {"id": head.id, "code": head.code, "legal_name": head.legal_name} if head else None,
            "valid_from": party.valid_from,
            "valid_to": party.valid_to,
            "status": party.status,
            "version": party.version,
            "entity_id": party.entity_id,
            "name_changes": [
                {
                    "old_name": c.old_name,
                    "new_name": c.new_name,
                    "effective_from": c.effective_from,
                    "uscc_verified": c.uscc_verified,
                    "inheritance_doc_no": c.inheritance_doc_no,
                    "inheritance_location": c.inheritance_location,
                    "source_document_id": c.source_document_id,
                }
                for c in changes
            ],
            "control_relations": [
                {
                    "parent_party_id": rel.parent_party_id,
                    "parent_code": parent.code,
                    "relation_type": rel.relation_type,
                    "valid_from": rel.valid_from,
                    "valid_to": rel.valid_to,
                }
                for rel, parent in relations
            ],
        }
    )


def party_state_on(party: m.PartyProfile, when: datetime) -> tuple[bool, list[str]]:
    """Whether the party was a valid legal person/branch at ``when``. Current status is NOT used."""
    reasons: list[str] = []
    ok = True
    if party.valid_from is not None and when < party.valid_from:
        reasons.append(f"party {party.code} not yet valid at {when.isoformat()} (valid_from {party.valid_from.isoformat()})")
        ok = False
    if party.valid_to is not None and when > party.valid_to:
        reasons.append(f"party {party.code} no longer valid at {when.isoformat()} (valid_to {party.valid_to.isoformat()})")
        ok = False
    return ok, reasons


def name_state_at(db: Session, party: m.PartyProfile, when: datetime) -> str:
    """The registered name of the party at ``when``, derived from verified name changes."""
    changes = db.scalars(
        select(m.PartyNameChange)
        .where(
            m.PartyNameChange.party_id == party.id,
            m.PartyNameChange.effective_from <= when,
        )
        .order_by(m.PartyNameChange.effective_from.desc(), m.PartyNameChange.id.desc())
    ).all()
    if changes:
        return changes[0].new_name
    if party.legal_name != (changes[0].new_name if changes else party.legal_name):
        return party.legal_name
    return party.legal_name


def identity_verified_for_recorded_name(
    db: Session, party: m.PartyProfile, recorded_name: str | None, when: datetime
) -> tuple[bool, dict]:
    """Resolve a name found in a customs archive to a party, verifying identity properly.

    Same name at ``when`` is direct evidence. An old name is accepted only when a name change is
    backed by USCC verification or an official inheritance document — never by similarity.
    """
    if not recorded_name:
        return False, {"reason": "no recorded name to check"}
    current_at = name_state_at(db, party, when)
    if recorded_name.strip() == current_at.strip():
        return True, {"matched": "name_at_export_date", "name": current_at}
    changes = db.scalars(
        select(m.PartyNameChange)
        .where(m.PartyNameChange.party_id == party.id)
        .order_by(m.PartyNameChange.effective_from.desc(), m.PartyNameChange.id.desc())
    ).all()
    seen = {current_at.strip()}
    for change in changes:
        candidates = {change.new_name.strip(), change.old_name.strip()}
        if recorded_name.strip() in candidates:
            if change.uscc_verified or (change.inheritance_doc_no and change.inheritance_location):
                return True, {
                    "matched": "verified_name_change",
                    "recorded_name": recorded_name,
                    "party_code": party.code,
                    "uscc": party.uscc,
                    "uscc_verified": change.uscc_verified,
                    "inheritance_doc_no": change.inheritance_doc_no,
                    "change_effective_from": change.effective_from,
                }
            return False, {
                "matched": "unverified_name_change",
                "recorded_name": recorded_name,
                "reason": "only an unverified name change links this recorded name; name similarity "
                "alone cannot merge identities",
            }
        seen.update(candidates)
    return False, {
        "matched": "none",
        "recorded_name": recorded_name,
        "registered_name": current_at,
        "reason": "recorded name is not the party's export-date name and no verified USCC / "
        "inheritance link resolves it",
    }


# ---------------------------------------------------------------------------
# Credential registry
# ---------------------------------------------------------------------------


def get_credential(db: Session, credential_id: int) -> m.AuthorizationCredential:
    cred = db.get(m.AuthorizationCredential, credential_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    return cred


def credential_sha(payload) -> str:
    return sha256_of(payload)


def create_credential(
    db: Session, req: CredentialCreateRequest, actor: str
) -> m.AuthorizationCredential:
    entity = db.scalar(select(m.Entity).where(m.Entity.code == req.entity_code))
    if entity is None:
        raise HTTPException(422, f"entity_code not found: {req.entity_code}")
    grantor = get_party(db, req.grantor_party_code)
    grantee = get_party(db, req.grantee_party_code)
    if grantor.id == grantee.id and req.kind != "other":
        raise HTTPException(
            422,
            "grantor and grantee are the same party; a self-loop cannot prove a cross-party relation",
        )
    if db.scalar(
        select(m.AuthorizationCredential.id).where(
            m.AuthorizationCredential.entity_id == entity.id,
            m.AuthorizationCredential.credential_no == req.credential_no,
            m.AuthorizationCredential.version == 1,
        )
    ):
        raise HTTPException(409, f"credential {req.credential_no} already exists for entity {entity.code}")
    if req.effective_to is not None and req.effective_to < req.effective_from:
        raise HTTPException(422, "effective_to precedes effective_from")
    if req.retroactive and not req.retroactive_scope_verified:
        raise HTTPException(
            422,
            "a retroactive credential needs a verifiable retroactive scope statement; without it the "
            "document can only support a pending-evidence conclusion",
        )
    supersedes_id = None
    if req.supersedes_credential_no:
        old = db.scalar(
            select(m.AuthorizationCredential).where(
                m.AuthorizationCredential.entity_id == entity.id,
                m.AuthorizationCredential.credential_no == req.supersedes_credential_no,
            )
        )
        if old is None:
            raise HTTPException(422, f"superseded credential not found: {req.supersedes_credential_no}")
        supersedes_id = old.id
    scope = _normalise_scope(req.scope)
    cred = m.AuthorizationCredential(
        entity_id=entity.id,
        credential_no=req.credential_no,
        kind=req.kind,
        grantor_party_id=grantor.id,
        grantee_party_id=grantee.id,
        issued_at=req.issued_at,
        received_time=req.received_time,
        effective_from=req.effective_from,
        effective_to=req.effective_to,
        scope=jsonable(scope),
        quantity_limit=req.quantity_limit,
        currency=req.currency,
        original_location=req.original_location,
        sha256=req.sha256 or credential_sha(req.model_dump(mode="json")),
        version=1,
        supersedes_id=supersedes_id,
        intragroup_acts_as=req.intragroup_acts_as,
        retroactive=req.retroactive,
        retroactive_scope_verified=req.retroactive_scope_verified,
        retroactive_from=req.retroactive_from,
        source_document_id=req.source_document_id,
        created_by=actor,
    )
    db.add(cred)
    db.flush()
    db.add(
        m.AuditLog(
            entity_id=entity.id,
            actor=actor,
            action="audit.credential.issue",
            object_type="authorization_credential",
            object_id=cred.id,
            payload=jsonable(
                {
                    "credential_no": cred.credential_no,
                    "kind": cred.kind,
                    "issued_at": cred.issued_at,
                    "received_time": cred.received_time,
                    "effective_from": cred.effective_from,
                }
            ),
        )
    )
    db.flush()
    return cred


def _normalise_scope(scope: dict) -> dict:
    out = dict(scope or {})
    for key in ("skus", "hs_codes", "regions", "customs_party_codes", "product_keywords"):
        value = out.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [value]
        out[key] = [str(v) for v in value]
    return out


def _next_credential_version(db: Session, credential: m.AuthorizationCredential) -> int:
    last = db.scalar(
        select(m.AuthorizationCredential.version)
        .where(
            m.AuthorizationCredential.entity_id == credential.entity_id,
            m.AuthorizationCredential.credential_no == credential.credential_no,
        )
        .order_by(m.AuthorizationCredential.version.desc())
        .limit(1)
    )
    return (last or credential.version) + 1


def scope_revision(
    db: Session, credential: m.AuthorizationCredential, req: CredentialScopeRevisionRequest, actor: str
) -> m.AuthorizationCredential:
    """Append a scope revision as a new credential version linked by supersedes_id."""
    new_version = _next_credential_version(db, credential)
    new = m.AuthorizationCredential(
        entity_id=credential.entity_id,
        credential_no=credential.credential_no,
        kind=credential.kind,
        grantor_party_id=credential.grantor_party_id,
        grantee_party_id=credential.grantee_party_id,
        issued_at=credential.issued_at,
        received_time=_now(),
        effective_from=req.effective_from or credential.effective_from,
        effective_to=req.effective_to if req.effective_to is not None else credential.effective_to,
        scope=jsonable(_normalise_scope(req.scope if req.scope else credential.scope)),
        quantity_limit=req.quantity_limit if req.quantity_limit is not None else credential.quantity_limit,
        currency=credential.currency,
        original_location=credential.original_location,
        sha256=credential_sha(
            {"credential_no": credential.credential_no, "scope": req.scope, "limit": str(req.quantity_limit)}
        ),
        version=new_version,
        supersedes_id=credential.id,
        intragroup_acts_as=credential.intragroup_acts_as,
        retroactive=credential.retroactive,
        retroactive_scope_verified=credential.retroactive_scope_verified,
        retroactive_from=credential.retroactive_from,
        source_document_id=None,
        created_by=actor,
    )
    db.add(new)
    db.flush()
    credential.status = "superseded"
    db.add(
        m.AuditLog(
            entity_id=credential.entity_id,
            actor=actor,
            action="audit.credential.scope_revision",
            object_type="authorization_credential",
            object_id=new.id,
            payload=jsonable(
                {
                    "credential_no": credential.credential_no,
                    "previous_credential_id": credential.id,
                    "new_version": new.version,
                    "note": getattr(req, "note", None),
                }
            ),
        )
    )
    db.flush()
    return new


def add_revocation(
    db: Session, credential: m.AuthorizationCredential, req: CredentialRevocationRequest, actor: str
) -> m.CredentialRevocation:
    issuer = get_party(db, req.issuer_party_code)
    if db.scalar(
        select(m.CredentialRevocation.id).where(
            m.CredentialRevocation.credential_id == credential.id,
            m.CredentialRevocation.revocation_no == req.revocation_no,
        )
    ):
        raise HTTPException(409, f"revocation {req.revocation_no} already recorded for this credential")
    rev = m.CredentialRevocation(
        credential_id=credential.id,
        revocation_no=req.revocation_no,
        issuer_party_id=issuer.id,
        sequence_no=req.sequence_no,
        claimed_effective_from=req.claimed_effective_from,
        signed_at=req.signed_at,
        received_time=req.received_time,
        original_location=req.original_location,
        sha256=req.sha256 or credential_sha(req.model_dump(mode="json")),
        source_document_id=req.source_document_id,
    )
    db.add(rev)
    db.flush()
    _recompute_revocation_freeze(db, credential, actor)
    db.flush()
    return rev


def _recompute_revocation_freeze(
    db: Session, credential: m.AuthorizationCredential, actor: str
) -> None:
    """Freeze a credential when two revocations are mutually exclusive on their claimed dates.

    Two revocations are mutually exclusive when they claim the same effective date but were not
    issued by the same verifiable sequence context. When both claim to take effect before the
    export date, the credential cannot be trusted to any one of them and is frozen for verification.
    """
    revs = db.scalars(
        select(m.CredentialRevocation)
        .where(m.CredentialRevocation.credential_id == credential.id)
        .order_by(m.CredentialRevocation.id)
    ).all()
    if len(revs) < 2:
        return
    conflict = False
    for i, a in enumerate(revs):
        for b in revs[i + 1:]:
            if a.issuer_party_id == b.issuer_party_id:
                continue
            if a.claimed_effective_from == b.claimed_effective_from:
                conflict = True
            elif a.sequence_no is not None and b.sequence_no is not None and a.sequence_no == b.sequence_no:
                conflict = True
    if conflict and credential.status != "frozen":
        credential.status = "frozen"
        credential.frozen_reason = (
            "mutually exclusive revocations claim the same effective date or sequence; the "
            "authorization chain is frozen until issuance source and sequence are verified"
        )
        credential.version += 1
        for rev in revs:
            rev.status = "conflict"
        db.add(
            m.AuditLog(
                entity_id=credential.entity_id,
                actor=actor,
                action="audit.credential.frozen_conflicting_revocations",
                object_type="authorization_credential",
                object_id=credential.id,
                payload=jsonable(
                    {
                        "credential_no": credential.credential_no,
                        "revocations": [
                            {
                                "revocation_no": r.revocation_no,
                                "issuer_party_id": r.issuer_party_id,
                                "claimed_effective_from": r.claimed_effective_from,
                                "sequence_no": r.sequence_no,
                            }
                            for r in revs
                        ],
                    }
                ),
            )
        )


def credential_view(db: Session, cred: m.AuthorizationCredential) -> dict:
    grantor = db.get(m.PartyProfile, cred.grantor_party_id)
    grantee = db.get(m.PartyProfile, cred.grantee_party_id)
    revs = db.scalars(
        select(m.CredentialRevocation)
        .where(m.CredentialRevocation.credential_id == cred.id)
        .order_by(m.CredentialRevocation.id)
    ).all()
    return jsonable(
        {
            "id": cred.id,
            "entity_id": cred.entity_id,
            "credential_no": cred.credential_no,
            "kind": cred.kind,
            "grantor": {"id": grantor.id, "code": grantor.code, "legal_name": grantor.legal_name} if grantor else None,
            "grantee": {"id": grantee.id, "code": grantee.code, "legal_name": grantee.legal_name} if grantee else None,
            "issued_at": cred.issued_at,
            "received_time": cred.received_time,
            "effective_from": cred.effective_from,
            "effective_to": cred.effective_to,
            "scope": cred.scope,
            "quantity_limit": cred.quantity_limit,
            "currency": cred.currency,
            "original_location": cred.original_location,
            "sha256": cred.sha256,
            "status": cred.status,
            "frozen_reason": cred.frozen_reason,
            "version": cred.version,
            "supersedes_id": cred.supersedes_id,
            "intragroup_acts_as": cred.intragroup_acts_as,
            "retroactive": cred.retroactive,
            "retroactive_scope_verified": cred.retroactive_scope_verified,
            "retroactive_from": cred.retroactive_from,
            "revocations": [
                {
                    "id": r.id,
                    "revocation_no": r.revocation_no,
                    "issuer_party_id": r.issuer_party_id,
                    "sequence_no": r.sequence_no,
                    "claimed_effective_from": r.claimed_effective_from,
                    "signed_at": r.signed_at,
                    "received_time": r.received_time,
                    "original_location": r.original_location,
                    "sha256": r.sha256,
                    "status": r.status,
                }
                for r in revs
            ],
        }
    )


def credentials_query(db: Session, entity_id: int, *, kind: str | None = None) -> Select:
    stmt = select(m.AuthorizationCredential).where(m.AuthorizationCredential.entity_id == entity_id)
    if kind:
        stmt = stmt.where(m.AuthorizationCredential.kind == kind)
    return stmt.order_by(m.AuthorizationCredential.id)


def ZERO_QTY() -> Decimal:
    return Decimal("0")
