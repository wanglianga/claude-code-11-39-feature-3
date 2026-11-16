"""跨主体出口证据链一致性与授权时效审计.

The service chooses a *shared temporal authorization graph plus per-row witness chains*:

  * The graph (party nodes + time-bounded credential edges + role anchors for the invoice /
    title / customs parties) is used to find candidate paths and to propagate the impact of a
    credential change.
  * Every export row nevertheless freezes one concrete, human-verifiable witness path with an
    explicit quantity interval, so its conclusion is explainable and does not drag in
    unrelated relations.

Hard rules implemented here:
  * Eligibility is judged *at the export date*. ``received_time`` never substitutes for the
    export-date state; current party status is never used.
  * A document received after export can still support history when it was issued and effective
    before export, its content is verifiable and it covers the goods line — after review.
  * A document issued after export needs a verifiable retroactive scope; otherwise the row is
    pending evidence.
  * The same title share reached by several paths is counted once (max, not sum). A cycle never
    inflates coverage.
  * A path missing any required authorization scope is only a pending candidate; a truncated
    search may never assert that no valid authorization exists.
  * A partial title transfer splits the row into a restricted-confirmation segment and a broken
    title-chain segment; the row never passes as a whole.
  * A supplementary document that changes a key interval of an already verified chain moves the
    current conclusion to review and pauses it for new dossiers; historical conclusions keep their
    frozen versions and basis.
  * A shared credential change locates every dependent row; only rows whose valid witness is lost
    or whose scope changed are paused, then every row is re-judged on its own goods, region,
    export date and replacement evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..schemas import AuditCaseCreateRequest, AuditEvidenceLinkRequest
from ..util import jsonable, sha256_of
from .audit_registry import credential_view, identity_verified_for_recorded_name, party_state_on

CONFIRMED = "cross_entity_chain_confirmed"
RESTRICTED = "restricted_confirmation"
INSUFFICIENT = "authorization_insufficient"
BROKEN_TITLE = "title_chain_broken"
CONFLICT = "party_conflict"
PENDING = "pending_evidence"

ZERO = Decimal("0")
MAX_PATHS = 64
MAX_DEPTH = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dossier construction
# ---------------------------------------------------------------------------


def build_case(
    db: Session,
    entity: m.Entity,
    req: AuditCaseCreateRequest,
    actor: str,
    *,
    links: list[AuditEvidenceLinkRequest] | None = None,
) -> m.AuditCase:
    export = _resolve_export(db, entity, req)
    invoice_line = db.get(m.InvoiceLine, export.invoice_line_id) if export.invoice_line_id else None
    customs = db.get(m.CustomsLine, export.customs_line_id) if export.customs_line_id else None

    party_codes = {
        "invoice": req.invoice_party_code,
        "title": req.title_party_code,
        "customs": req.customs_party_code,
    }
    parties: dict[str, m.PartyProfile] = {}
    for role, code in party_codes.items():
        party = db.scalar(select(m.PartyProfile).where(m.PartyProfile.code == code))
        if party is None:
            raise HTTPException(422, f"{role} party not found: {code}")
        if party.entity_id is not None and party.entity_id != entity.id:
            raise HTTPException(403, f"{role} party {code} belongs to another entity scope")
        parties[role] = party

    export_date = export.export_date
    if export_date is None:
        raise HTTPException(
            422, "export line has no export date; an export-date judgement cannot be built"
        )

    recorded = dict(req.recorded_names or {})
    conflicts: list[dict] = []
    for role in ("invoice", "title", "customs"):
        recorded_name = recorded.get(role)
        if not recorded_name:
            continue
        ok, detail = identity_verified_for_recorded_name(db, parties[role], recorded_name, export_date)
        if not ok:
            conflicts.append({"role": role, "party_code": parties[role].code, "identity": detail})

    case = db.scalar(select(m.AuditCase).where(m.AuditCase.export_line_id == export.id))
    created = case is None
    if created:
        case = m.AuditCase(
            entity_id=entity.id,
            case_ref=f"AUD-{export.id}",
            export_line_id=export.id,
            invoice_line_id=invoice_line.id if invoice_line else None,
            customs_line_id=customs.id if customs else None,
            invoice_party_id=parties["invoice"].id,
            title_party_id=parties["title"].id,
            customs_party_id=parties["customs"].id,
            product_sku=req.product_sku or export.sku or (customs.sku if customs else None),
            hs_code=req.hs_code or (customs.hs_code if customs else None),
            description=export.description,
            country=req.region,
            quantity=export.declared_quantity,
            amount=export.declared_amount,
            currency=export.currency,
            export_date=export_date,
            recorded_names=jsonable(recorded),
            status="open",
        )
        db.add(case)
        db.flush()
    else:
        if case.status == "closed":
            raise HTTPException(409, f"audit case {case.case_ref} is closed")
        case.invoice_party_id = parties["invoice"].id
        case.title_party_id = parties["title"].id
        case.customs_party_id = parties["customs"].id
        case.product_sku = req.product_sku or case.product_sku
        case.hs_code = req.hs_code or case.hs_code
        case.country = req.region
        case.quantity = export.declared_quantity
        case.amount = export.declared_amount
        case.export_date = export_date
        case.recorded_names = jsonable(recorded)

    _anchor_role_links(db, case, parties, invoice_line, customs, replace=not created)
    for link in links or []:
        add_evidence_link(db, case, link, actor)
    if conflicts:
        _record_party_conflicts(db, case, conflicts)
    db.add(
        m.AuditReviewEvent(
            case_id=case.id,
            kind="case_built",
            actor=actor,
            note=req.note,
            payload=jsonable({"export_date": export_date, "parties": party_codes, "conflicts": conflicts}),
        )
    )
    db.flush()
    return case


def _resolve_export(db: Session, entity: m.Entity, req: AuditCaseCreateRequest) -> m.ExportLine:
    if req.export_line_id:
        export = db.get(m.ExportLine, req.export_line_id)
    elif req.invoice_line_id:
        export = db.scalar(select(m.ExportLine).where(m.ExportLine.invoice_line_id == req.invoice_line_id))
    elif req.customs_line_id:
        export = db.scalar(select(m.ExportLine).where(m.ExportLine.customs_line_id == req.customs_line_id))
    else:
        raise HTTPException(422, "one of export_line_id / invoice_line_id / customs_line_id is required")
    if export is None:
        raise HTTPException(404, "export line not found for the supplied invoice/customs line")
    if export.entity_id != entity.id:
        raise HTTPException(403, "export line belongs to another entity scope")
    return export


def _anchor_role_links(
    db: Session,
    case: m.AuditCase,
    parties: dict[str, m.PartyProfile],
    invoice_line: m.InvoiceLine | None,
    customs: m.CustomsLine | None,
    *,
    replace: bool = False,
) -> None:
    if replace:
        for row in db.scalars(
            select(m.AuditEvidenceLink).where(
                m.AuditEvidenceLink.case_id == case.id,
                m.AuditEvidenceLink.node_kind.in_(("party", "export_row", "source_document")),
            )
        ).all():
            db.delete(row)
        db.flush()
    anchors = [
        ("party", parties["invoice"].code, "invoice_party", {"party_id": parties["invoice"].id}),
        ("party", parties["title"].code, "title_party", {"party_id": parties["title"].id}),
        ("party", parties["customs"].code, "customs_party", {"party_id": parties["customs"].id}),
        ("export_row", f"EXP-{case.export_line_id}", "export_line", {"export_line_id": case.export_line_id}),
    ]
    for kind, ref, role, detail in anchors:
        db.add(
            m.AuditEvidenceLink(
                case_id=case.id,
                node_kind=kind,
                node_ref=ref,
                role=role,
                quantity=case.quantity if kind == "export_row" else None,
                detail=jsonable(detail),
            )
        )
    if invoice_line is not None:
        doc = db.get(m.SourceDocument, invoice_line.document_id)
        db.add(
            m.AuditEvidenceLink(
                case_id=case.id,
                node_kind="source_document",
                node_ref=f"DOC-{doc.id}",
                role="invoice_document",
                source_document_id=doc.id,
                original_location=doc.original_location,
                sha256=doc.sha256,
                detail=jsonable({"event_time": doc.event_time, "received_time": doc.received_time}),
            )
        )
    if customs is not None:
        doc = db.get(m.SourceDocument, customs.document_id)
        db.add(
            m.AuditEvidenceLink(
                case_id=case.id,
                node_kind="source_document",
                node_ref=f"DOC-{doc.id}",
                role="customs_document",
                source_document_id=doc.id,
                original_location=doc.original_location,
                sha256=doc.sha256,
                detail=jsonable({"event_time": doc.event_time, "received_time": doc.received_time}),
            )
        )
    db.flush()


def add_evidence_link(
    db: Session, case: m.AuditCase, req: AuditEvidenceLinkRequest, actor: str
) -> m.AuditEvidenceLink:
    link = m.AuditEvidenceLink(
        case_id=case.id,
        node_kind=req.role,
        node_ref=req.node_ref,
        role=req.role,
        valid_from=req.valid_from,
        valid_to=req.valid_to,
        quantity=req.quantity,
        source_document_id=req.source_document_id,
        original_location=req.original_location,
        sha256=req.sha256 or sha256_of(req.model_dump(mode="json")),
        detail=jsonable(req.detail),
    )
    db.add(link)
    db.flush()
    return link


def _record_party_conflicts(db: Session, case: m.AuditCase, conflicts: list[dict]) -> None:
    existing = {
        c.detail.get("role")
        for c in db.scalars(
            select(m.DifferenceCase).where(
                m.DifferenceCase.kind == "party_identity_conflict",
                m.DifferenceCase.ref_type == "audit_case",
                m.DifferenceCase.ref_id == case.id,
                m.DifferenceCase.state == "open",
            )
        ).all()
    }
    for conflict in conflicts:
        if conflict["role"] in existing:
            continue
        db.add(
            m.DifferenceCase(
                entity_id=case.entity_id,
                kind="party_identity_conflict",
                state="open",
                ref_type="audit_case",
                ref_id=case.id,
                detail=jsonable(
                    {
                        **conflict,
                        "rule": "a recorded name that cannot be tied to the party by USCC or an official "
                        "inheritance document is a party conflict; name similarity alone is not identity",
                    }
                ),
            )
        )


def load_case(db: Session, case_id: int) -> m.AuditCase:
    case = db.get(m.AuditCase, case_id)
    if case is None:
        raise HTTPException(404, "audit case not found")
    return case


def current_conclusion(db: Session, case_id: int) -> m.AuditConclusion | None:
    return db.scalar(
        select(m.AuditConclusion)
        .where(m.AuditConclusion.case_id == case_id, m.AuditConclusion.is_current.is_(True))
        .order_by(m.AuditConclusion.version.desc())
    )


# ---------------------------------------------------------------------------
# Temporal authorization graph
# ---------------------------------------------------------------------------


def _in_interval(when: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and when < start:
        return False
    if end is not None and when > end:
        return False
    return True


def _credential_state_at(
    db: Session, cred: m.AuthorizationCredential, when: datetime
) -> tuple[bool, str, list[str]]:
    """Is the credential effective on ``when``? Returns (ok, blocking_code, reasons).

    ``received_time`` never gates effectiveness. Two mutually exclusive revocations freeze the
    credential upstream (status == "frozen"), and a frozen chain can never be used to produce a
    verified state.
    """
    if cred.status == "frozen":
        return False, "frozen", [f"credential {cred.credential_no} is frozen: {cred.frozen_reason}"]
    if cred.status == "superseded":
        return False, "superseded", [f"credential {cred.credential_no} was replaced by a newer version"]
    if not _in_interval(when, cred.effective_from, cred.effective_to):
        retro_ok = (
            cred.retroactive
            and cred.retroactive_scope_verified
            and cred.retroactive_from is not None
            and cred.retroactive_from <= when
            and (cred.effective_to is None or when <= cred.effective_to)
        )
        if not retro_ok:
            return False, "not_effective", [
                f"credential {cred.credential_no} not effective at {when.isoformat()}: "
                f"effective_from {cred.effective_from.isoformat()}"
            ]
    revs = db.scalars(
        select(m.CredentialRevocation).where(
            m.CredentialRevocation.credential_id == cred.id,
            m.CredentialRevocation.status != "conflict",
        )
    ).all()
    for rev in revs:
        if rev.claimed_effective_from <= when:
            return False, "revoked", [
                f"credential {cred.credential_no} was revoked effectively "
                f"{rev.claimed_effective_from.isoformat()} (revocation {rev.revocation_no})"
            ]
    return True, "effective", []


def _scope_covers(
    cred: m.AuthorizationCredential,
    case: m.AuditCase,
    customs_party: m.PartyProfile,
) -> tuple[bool, list[str]]:
    scope = cred.scope or {}
    missing: list[str] = []
    regions = scope.get("regions") or []
    if case.country and regions and case.country not in regions and "*" not in regions:
        missing.append(f"region {case.country} not in scope {regions}")
    elif not regions and scope.get("global") is not True:
        missing.append("credential scope does not state a covered region")
    skus = scope.get("skus") or []
    if case.product_sku and skus and case.product_sku not in skus and "*" not in skus:
        missing.append(f"sku {case.product_sku} not in scope {skus}")
    hs_codes = scope.get("hs_codes") or []
    if case.hs_code and hs_codes and not any(str(case.hs_code).startswith(h) for h in hs_codes):
        missing.append(f"hs_code {case.hs_code} not covered by {hs_codes}")
    custom_parties = scope.get("customs_party_codes") or []
    if custom_parties and customs_party.code not in custom_parties:
        missing.append(f"customs party {customs_party.code} not in scope {custom_parties}")
    return not missing, missing


def _role_of(cred: m.AuthorizationCredential) -> str:
    if cred.kind in ("entrusted_export", "customs_authorization"):
        return "customs_authorization"
    if cred.kind == "title_transfer":
        return "title"
    return "intragroup"


class Graph:
    """Party nodes and time-bounded credential edges for one entity at one export date."""

    def __init__(self, db: Session, entity_id: int, case: m.AuditCase):
        self.db = db
        self.entity_id = entity_id
        self.case = case
        self.when = case.export_date
        self.customs_party = db.get(m.PartyProfile, case.customs_party_id)
        self.party_by_id: dict[int, m.PartyProfile] = {}
        for p in db.scalars(
            select(m.PartyProfile).where(
                (m.PartyProfile.entity_id == entity_id) | (m.PartyProfile.entity_id.is_(None))
            )
        ).all():
            self.party_by_id[p.id] = p
        self.creds = list(
            db.scalars(
                select(m.AuthorizationCredential)
                .where(
                    m.AuthorizationCredential.entity_id == entity_id,
                )
                .order_by(m.AuthorizationCredential.id)
            ).all()
        )
        for cred in self.creds:
            for pid in (cred.grantor_party_id, cred.grantee_party_id):
                if pid not in self.party_by_id:
                    p = db.get(m.PartyProfile, pid)
                    if p is not None:
                        self.party_by_id[pid] = p
        # step 1: applicability at the export date (time window + revocation/freeze)
        self.applicable: list[tuple[m.AuthorizationCredential, str]] = []
        # step 2: of the applicable ones, those whose scope covers this goods line
        self.edges: dict[int, list[tuple[m.AuthorizationCredential, str]]] = {}
        self.skipped: list[dict] = []
        self.pending_candidates: list[dict] = []
        for cred in self.creds:
            ok, code, reasons = _credential_state_at(db, cred, self.when)
            covers, missing = _scope_covers(cred, case, self.customs_party)
            if not ok:
                self.skipped.append(
                    {
                        "credential_id": cred.id,
                        "credential_no": cred.credential_no,
                        "kind": cred.kind,
                        "blocking": code,
                        "reasons": reasons,
                    }
                )
                # A post-export-issued credential without a verifiable retroactive scope, a frozen
                # chain, or a credential whose scope is missing can only be a pending candidate.
                # A cleanly superseded version or a revocation on its own is decisive, not pending.
                if code == "not_effective" and cred.effective_from > self.when:
                    self.pending_candidates.append(
                        {"credential_id": cred.id, "credential_no": cred.credential_no,
                         "kind": cred.kind, "reason": "issued/effective after export with no verifiable "
                         "retroactive scope"}
                    )
                elif code == "frozen":
                    self.pending_candidates.append(
                        {"credential_id": cred.id, "credential_no": cred.credential_no,
                         "kind": cred.kind, "reason": "chain frozen by mutually exclusive revocations; "
                         "issuance source and sequence must be verified"}
                    )
                elif code == "superseded" and not covers:
                    self.pending_candidates.append(
                        {"credential_id": cred.id, "credential_no": cred.credential_no,
                         "kind": cred.kind, "reason": "superseded version; scope does not cover the line",
                         "missing": missing}
                    )
                elif code not in ("revoked", "superseded") and not covers:
                    self.pending_candidates.append(
                        {"credential_id": cred.id, "credential_no": cred.credential_no,
                         "kind": cred.kind, "reason": "scope does not cover the goods line",
                         "missing": missing}
                    )
                continue
            if not covers:
                self.skipped.append(
                    {
                        "credential_id": cred.id,
                        "credential_no": cred.credential_no,
                        "kind": cred.kind,
                        "blocking": "scope_insufficient",
                        "reasons": missing,
                    }
                )
                self.pending_candidates.append(
                    {"credential_id": cred.id, "credential_no": cred.credential_no,
                     "kind": cred.kind, "reason": "scope does not cover the goods line",
                     "missing": missing}
                )
                continue
            self.applicable.append((cred, _role_of(cred)))
            self.edges.setdefault(cred.grantor_party_id, []).append((cred, _role_of(cred)))
        for pid in self.edges:
            self.edges[pid].sort(key=lambda e: e[0].id)

        self.truncated = False
        self.path_limit_hit = False
        self.paths: dict[str, list[list[dict]]] = {}

    def find_paths(self, start_id: int, roles: tuple[str, ...], target_id: int | None = None) -> list[list[dict]]:
        target_id = self.case.customs_party_id if target_id is None else target_id
        results: list[list[dict]] = []
        self._dfs(start_id, roles, [], {start_id}, results, 0, target_id)
        if len(results) >= MAX_PATHS:
            self.path_limit_hit = True
            self.truncated = True
        self.paths[",".join(roles)] = results
        return results

    def _dfs(self, node_id, roles, acc, visited, results, depth, target_id):
        if len(results) >= MAX_PATHS:
            self.path_limit_hit = True
            self.truncated = True
            return
        if depth >= MAX_DEPTH:
            self.truncated = True
            return
        for cred, role in self.edges.get(node_id, []):
            if role not in roles:
                continue
            step = self._step(cred, role)
            new_acc = acc + [step]
            target = cred.grantee_party_id
            if target in visited:
                # A cycle back to an already-visited party is refused: it would re-use the same
                # title share and could otherwise fake extra coverage.
                continue
            if target == target_id and role == roles[-1]:
                results.append(new_acc)
                if len(results) >= MAX_PATHS:
                    self.path_limit_hit = True
                    self.truncated = True
                    return
                continue
            if target not in self.party_by_id:
                continue
            if target == target_id:
                # reached the target with a non-terminal role: the chain cannot be completed here
                continue
            visited.add(target)
            self._dfs(target, roles, new_acc, visited, results, depth + 1, target_id)
            visited.remove(target)

    def find_partial_paths(
        self, start_id: int, roles: tuple[str, ...], end_id: int
    ) -> list[list[dict]]:
        """Paths that start at ``start_id`` and reach ``end_id`` using the allowed roles."""
        results: list[list[dict]] = []
        self._dfs(start_id, roles, [], {start_id}, results, 0, end_id)
        return results

    def _step(self, cred: m.AuthorizationCredential, role: str) -> dict:
        return {
            "credential_id": cred.id,
            "credential_no": cred.credential_no,
            "kind": cred.kind,
            "role": role,
            "grantor_party_id": cred.grantor_party_id,
            "grantee_party_id": cred.grantee_party_id,
            "intragroup_acts_as": cred.intragroup_acts_as,
            "issued_at": cred.issued_at,
            "received_time": cred.received_time,
            "effective_from": cred.effective_from,
            "effective_to": cred.effective_to,
            "scope": cred.scope,
            "quantity_limit": cred.quantity_limit,
            "version": cred.version,
            "sha256": cred.sha256,
            "post_export_issued": cred.issued_at > self.when,
            "post_export_received": cred.received_time > self.when,
            # these edges survived the export-date validity test, so they are effective at export
            "valid_at_export": True,
        }

    def intragroup_paths(self, start_id: int, end_id: int) -> list[list[dict]]:
        results: list[list[dict]] = []

        def walk(node, visited, chain):
            if len(results) >= MAX_PATHS or len(chain) >= MAX_DEPTH:
                return
            for cred, role in self.edges.get(node, []):
                if role != "intragroup":
                    continue
                step = self._step(cred, "intragroup")
                target = cred.grantee_party_id
                new_chain = chain + [step]
                if target == end_id:
                    results.append(new_chain)
                    continue
                if target in visited or target not in self.party_by_id:
                    continue
                visited.add(target)
                walk(target, visited, new_chain)
                visited.remove(target)

        walk(start_id, {start_id}, [])
        return results


# ---------------------------------------------------------------------------
# Eligibility judgement
# ---------------------------------------------------------------------------


def evaluate_case(
    db: Session,
    case: m.AuditCase,
    *,
    actor: str,
    note: str | None = None,
    kind: str = "evaluation",
) -> m.AuditConclusion:
    parties = {
        "invoice": db.get(m.PartyProfile, case.invoice_party_id),
        "title": db.get(m.PartyProfile, case.title_party_id),
        "customs": db.get(m.PartyProfile, case.customs_party_id),
    }
    graph = Graph(db, case.entity_id, case)

    # 1. All three parties valid on the export date (current status is irrelevant).
    party_reasons: list[str] = []
    party_ok = True
    for role, party in parties.items():
        ok, reasons = party_state_on(party, case.export_date) if party else (False, ["party missing"])
        if not ok:
            party_ok = False
            party_reasons.extend(f"{role}: {r}" for r in reasons)
    if not party_ok:
        return _persist(
            db, case, actor, kind,
            state=CONFLICT,
            quantity_confirmed=ZERO,
            quantity_restricted=ZERO,
            segments=_segments(case, [(CONFLICT, ZERO, case.quantity)]),
            witnesses={},
            graph=graph,
            dependencies=[],
            reasoning={"party_checks": party_reasons, "basis": "export_date"},
            note=note,
        )

    # 2. Customs party identity in the customs archive (old name needs USCC / inheritance link).
    recorded = (case.recorded_names or {}).get("customs")
    ident_ok, ident_detail = identity_verified_for_recorded_name(
        db, parties["customs"], recorded, case.export_date
    )
    if recorded and not ident_ok:
        return _persist(
            db, case, actor, kind,
            state=CONFLICT,
            quantity_confirmed=ZERO,
            quantity_restricted=ZERO,
            segments=_segments(case, [(CONFLICT, ZERO, case.quantity)]),
            witnesses={},
            graph=graph,
            dependencies=[],
            reasoning={
                "party_checks": party_reasons or ["all three parties valid at the export date"],
                "identity": ident_detail,
            },
            note=note,
        )

    quantity = case.quantity

    # 3. Quantity evidence consistency: packing / transport evidence may corroborate; evidence
    #    covering fewer units than the row blocks a whole-row pass.
    qty_issue = _quantity_evidence_issue(db, case)

    # 4. Title chain: invoice party -> title party -> customs party (title transfers only).
    title_paths = graph.find_partial_paths(case.invoice_party_id, ("title",), case.title_party_id)
    paths_to_title = [
        p for p in title_paths if p[-1]["grantee_party_id"] == case.title_party_id
    ]
    paths_to_customs: list[list[dict]] = []
    if case.title_party_id == case.invoice_party_id:
        paths_to_customs.extend(graph.find_paths(case.invoice_party_id, ("title",)))
        paths_to_customs = _dedupe_paths(paths_to_customs)
    else:
        tail_paths = graph.find_paths(case.title_party_id, ("title",))
        for head in paths_to_title:
            for tail in tail_paths:
                if tail[-1]["grantee_party_id"] != case.customs_party_id:
                    continue
                if {s["grantor_party_id"] for s in tail} & {case.invoice_party_id}:
                    continue
                paths_to_customs.append(head + tail)
        if case.customs_party_id != case.title_party_id:
            # a direct path from the invoice party to the customs party is also a title witness
            paths_to_customs.extend(graph.find_paths(case.invoice_party_id, ("title",)))
        paths_to_customs = _dedupe_paths(paths_to_customs)
    # Prefer witnesses that were already received at export: a row can be confirmed on a fully
    # pre-export-received chain even if an alternative chain arrived later.
    paths_to_title = _prefer_received(paths_to_title)
    paths_to_customs = _prefer_received(paths_to_customs)
    title_capacity, title_witnesses = _title_capacity(
        case, paths_to_title, paths_to_customs
    )

    # 5. Customs/export authorization: invoice party or title party -> customs party.
    auth_sources = [case.invoice_party_id]
    if case.title_party_id != case.invoice_party_id:
        auth_sources.append(case.title_party_id)
    customs_paths: list[list[dict]] = []
    for source in auth_sources:
        for p in graph.find_paths(source, ("customs_authorization",)):
            if p not in customs_paths:
                customs_paths.append(p)
    combined = graph.find_paths(case.invoice_party_id, ("intragroup", "customs_authorization"))
    customs_paths = _dedupe_paths(customs_paths + combined)
    customs_paths = _prefer_received(customs_paths)
    intragroup_witnesses = _intragroup_witnesses(db, graph, case, customs_paths)

    state, confirmed, restricted, reason = _decide(
        case,
        parties,
        quantity,
        title_capacity,
        customs_paths,
        qty_issue,
        graph,
    )
    witnesses = {
        "title": title_witnesses,
        "customs_authorization": [
            _path_view(p, graph, case) for p in customs_paths
        ],
        "intragroup_agreement": intragroup_witnesses,
    }
    dependencies = _collect_dependencies(
        case, paths_to_title, paths_to_customs, customs_paths, []
    )
    if state != CONFIRMED:
        # A row that did not reach a full witness also depends on the credentials it considered and
        # rejected: a later change to one of them (freeze → replacement or scope revision) must
        # reach the row so it can be rejudged. A fully confirmed row is not widened by unrelated
        # credentials that merely share an endpoint.
        dependencies.extend(_candidate_dependencies(graph, case))
    return _persist(
        db, case, actor, kind,
        state=state,
        quantity_confirmed=confirmed,
        quantity_restricted=restricted,
        segments=_segments(
            case,
            _segment_parts(state, confirmed, restricted, quantity),
        ),
        witnesses=witnesses,
        graph=graph,
        dependencies=dependencies,
        reasoning={
            "party_checks": party_reasons or ["all three parties valid at the export date"],
            "identity": ident_detail,
            "quantity_evidence": qty_issue,
            "line_quantity": str(quantity),
            "title_capacity": str(title_capacity),
            "path_rule": "the same title share reached by several paths is counted once (max, not sum); "
            "a cycle returning to a visited party is refused",
            "reached_state": reason,
            "path_search": {
                "truncated": graph.truncated,
                "path_limit_hit": graph.path_limit_hit,
                "note": "a truncated search only yields pending candidates; it never asserts that no "
                "valid authorization exists" if graph.truncated
                else "path search exhausted the candidate space",
            },
            "pending_candidates": graph.pending_candidates,
            "skipped_credentials": graph.skipped,
        },
        note=note,
    )


def _quantity_evidence_issue(db: Session, case: m.AuditCase) -> dict:
    links = db.scalars(
        select(m.AuditEvidenceLink).where(
            m.AuditEvidenceLink.case_id == case.id,
            m.AuditEvidenceLink.role.in_(("packing", "transport", "title_document")),
            m.AuditEvidenceLink.status == "active",
        )
    ).all()
    issue: dict = {"links_checked": 0, "mismatch": None, "mismatch_reason": None, "notes": []}
    for link in links:
        if link.quantity is None:
            continue
        issue["links_checked"] += 1
        if link.quantity < case.quantity:
            issue["mismatch"] = "insufficient_quantity_evidence"
            issue["mismatch_reason"] = (
                f"{link.role} evidence {link.node_ref} covers {link.quantity} < row quantity "
                f"{case.quantity}"
            )
        elif link.quantity != case.quantity:
            issue["notes"].append(
                f"{link.role} evidence {link.node_ref} covers {link.quantity} vs row {case.quantity}"
            )
    return issue


def _title_capacity(
    case: m.AuditCase,
    paths_to_title: list[list[dict]],
    paths_to_customs: list[list[dict]],
) -> tuple[Decimal, list[dict]]:
    """Title share at the title/customs party = max over paths, never the sum."""
    capacities: list[dict] = []
    best = ZERO
    if case.title_party_id == case.invoice_party_id:
        capacities.append(
            {
                "route": "invoice_party_holds_title",
                "quantity": str(case.quantity),
                "credential_ids": [],
                "note": "invoice party and title party are the same legal party",
            }
        )
        best = case.quantity
    for label, paths in (("invoice_to_title", paths_to_title), ("invoice_to_customs", paths_to_customs)):
        for path in paths:
            limit = _path_limit(case, path)
            capacities.append(
                {
                    "route": label,
                    "quantity": str(limit),
                    "credential_ids": [s["credential_id"] for s in path],
                    "credential_nos": [s["credential_no"] for s in path],
                }
            )
            best = max(best, limit)
    if not capacities:
        capacities.append(
            {
                "route": "none",
                "quantity": "0",
                "credential_ids": [],
                "note": "no effective title-transfer path reaches the title or customs party at export",
            }
        )
    return best, capacities


def _path_limit(case: m.AuditCase, path: list[dict]) -> Decimal:
    limit = case.quantity
    for step in path:
        if step.get("quantity_limit") is not None:
            limit = min(limit, Decimal(str(step["quantity_limit"])))
    return max(limit, ZERO)


def _dedupe_paths(paths: list[list[dict]]) -> list[list[dict]]:
    seen = set()
    out = []
    for p in paths:
        key = tuple(s["credential_id"] for s in p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _prefer_received(paths: list[list[dict]]) -> list[list[dict]]:
    """Prefer witnesses fully received by the export date.

    A row can rely on a chain that was already on file at export even when an alternative chain
    was received later. Only when *every* candidate was received after export does the review
    requirement attach to the conclusion.
    """
    pre = [p for p in paths if not any(s["post_export_received"] for s in p)]
    return pre or paths


def _intragroup_witnesses(
    db: Session, graph: Graph, case: m.AuditCase, customs_paths: list[list[dict]]
) -> list[dict]:
    """Intragroup agreements that explain why an intermediary may act for the group.

    They are supporting evidence; they never replace a customs authorization.
    """
    seen: set[tuple[int, ...]] = set()
    out = []
    sources = {case.invoice_party_id}
    for path in customs_paths:
        sources.add(path[0]["grantor_party_id"])
    for source in sorted(sources):
        for chain in graph.intragroup_paths(source, case.customs_party_id):
            key = tuple(s["credential_id"] for s in chain)
            if key in seen:
                continue
            seen.add(key)
            out.append(_path_view(chain, graph, case))
    return out


def _decide(
    case: m.AuditCase,
    parties: dict[str, m.PartyProfile],
    quantity: Decimal,
    title_capacity: Decimal,
    customs_paths: list[list[dict]],
    qty_issue: dict,
    graph: Graph,
) -> tuple[str, Decimal, Decimal, str]:
    if not customs_paths:
        # No effective customs/export authorization at the export date.
        if graph.pending_candidates:
            return (
                PENDING,
                ZERO,
                quantity,
                "authorization candidates exist but none is effective on the export date "
                "(post-export issuance without verifiable retroactivity, frozen chain, or missing "
                "scope); the row is pending evidence",
            )
        if graph.truncated:
            return (
                PENDING,
                ZERO,
                quantity,
                "authorization path search was truncated; the row stays a pending candidate and no "
                "assertion is made that no valid authorization exists",
            )
        return (
            INSUFFICIENT,
            ZERO,
            quantity,
            "no customs/export authorization from the invoice or title party to the customs party "
            "is effective on the export date",
        )

    if title_capacity == ZERO and case.title_party_id != case.customs_party_id:
        return (
            BROKEN_TITLE,
            ZERO,
            quantity,
            "authorization exists but the title chain to the title party has no effective covered "
            "quantity at export",
        )
    if title_capacity == ZERO and case.title_party_id == case.customs_party_id:
        title_capacity = quantity

    if qty_issue.get("mismatch"):
        return (
            RESTRICTED,
            ZERO,
            quantity,
            f"quantity evidence does not consistently support the row: {qty_issue['mismatch_reason']}",
        )

    usable = min(quantity, title_capacity)
    if usable >= quantity:
        return (
            CONFIRMED,
            quantity,
            ZERO,
            "one witness chain per row frozen; the identical title share reached by several paths is "
            "counted once",
        )
    remainder = quantity - usable
    return (
        RESTRICTED,
        usable,
        remainder,
        "title transfer covers fewer units than the invoice line and no other continuous title "
        "evidence covers the remainder; the row is split into restricted + broken-title segments",
    )


def _segment_parts(state: str, confirmed: Decimal, restricted: Decimal, quantity: Decimal):
    if state == RESTRICTED and restricted > ZERO and confirmed > ZERO:
        return [(RESTRICTED, confirmed, ZERO), (BROKEN_TITLE, ZERO, restricted)]
    if state == RESTRICTED:
        return [(RESTRICTED, ZERO, restricted or quantity)]
    if state == CONFIRMED:
        return [(CONFIRMED, confirmed, ZERO)]
    return [(state, ZERO, restricted or quantity)]


def _segments(case: m.AuditCase, parts: list[tuple[str, Decimal, Decimal]]) -> list[dict]:
    out = []
    for state, confirmed, restricted in parts:
        out.append(
            {
                "state": state,
                "quantity": str(confirmed + restricted),
                "quantity_confirmed": str(confirmed),
                "quantity_restricted": str(restricted),
            }
        )
    return out


def _step_view(path: list[dict], graph: Graph) -> list[dict]:
    out = []
    for step in path:
        out.append(
            {
                "credential_id": step["credential_id"],
                "credential_no": step["credential_no"],
                "kind": step["kind"],
                "role": step["role"],
                "grantor": _party_ref(graph, step["grantor_party_id"]),
                "grantee": _party_ref(graph, step["grantee_party_id"]),
                "issued_at": step["issued_at"],
                "received_time": step["received_time"],
                "effective_from": step["effective_from"],
                "effective_to": step["effective_to"],
                "quantity_limit": step["quantity_limit"],
                "version": step["version"],
                "sha256": step["sha256"],
                "post_export_issued": step["post_export_issued"],
                "post_export_received": step["post_export_received"],
                "valid_at_export": step.get("valid_at_export", False),
            }
        )
    return out


def _party_ref(graph: Graph, party_id: int) -> dict:
    p = graph.party_by_id.get(party_id)
    return {"id": party_id, "code": getattr(p, "code", None), "legal_name": getattr(p, "legal_name", None)}


def _path_view(path: list[dict], graph: Graph, case: m.AuditCase) -> dict:
    return {
        "steps": _step_view(path, graph),
        "covered_quantity": str(_path_limit(case, path)),
        "issued_before_export": all(s["issued_at"] <= case.export_date for s in path),
        "received_after_export": any(s["post_export_received"] for s in path),
        "post_export_issued_without_retroactivity": any(
            s["post_export_issued"] and s["effective_from"] > case.export_date for s in path
        ),
    }


def _collect_dependencies(
    case: m.AuditCase,
    paths_to_title: list[list[dict]],
    paths_to_customs: list[list[dict]],
    customs_paths: list[list[dict]],
    combined: list[list[dict]],
) -> list[dict]:
    deps: dict[tuple[int, str, int, int], Decimal] = {}
    for group, paths in (
        ("title_path", paths_to_title),
        ("title_to_customs", paths_to_customs),
        ("customs_authorization", customs_paths),
        ("combined_authorization", combined),
    ):
        for pi, path in enumerate(paths):
            limit = _path_limit(case, path)
            for si, step in enumerate(path):
                deps[(step["credential_id"], group, pi, si)] = limit
    return [
        {
            "credential_id": key[0],
            "role": key[1],
            "path_index": key[2],
            "step_index": key[3],
            "counted_quantity": str(value),
        }
        for key, value in deps.items()
    ]


def _candidate_dependencies(graph: Graph, case: m.AuditCase) -> list[dict]:
    """Record the credentials this row genuinely considered but could not use.

    Only credentials whose scope covers this goods/region line are recorded: a credential that
    excludes the row's goods or region is irrelevant to it, and changing that credential must not
    churn the row. A credential blocked by time, freeze or supersession but covering the row *is*
    relevant — replacing or re-scoping it can change the row's judgement.
    """
    relevant = {case.invoice_party_id, case.title_party_id, case.customs_party_id}
    out: list[dict] = []
    seen: set[int] = set()
    for cred in graph.creds:
        if cred.id in seen:
            continue
        if not ({cred.grantor_party_id, cred.grantee_party_id} & relevant):
            continue
        covers, _missing = _scope_covers(cred, case, graph.customs_party)
        if not covers:
            continue
        seen.add(cred.id)
        out.append(
            {
                "credential_id": cred.id,
                "role": "candidate",
                "path_index": None,
                "step_index": None,
                "counted_quantity": "0",
            }
        )
    return out


def _freeze_versions(
    case: m.AuditCase,
    parties: dict[str, m.PartyProfile],
    graph: Graph,
    dependencies: list[dict],
) -> dict:
    party_versions = {
        role: {"party_id": p.id, "code": p.code, "legal_name": p.legal_name, "uscc": p.uscc,
               "version": p.version, "status": p.status, "valid_from": p.valid_from,
               "valid_to": p.valid_to}
        for role, p in parties.items()
        if p is not None
    }
    creds = {}
    for dep in dependencies:
        cred = graph.db.get(m.AuthorizationCredential, dep["credential_id"])
        if cred is not None:
            creds[str(cred.id)] = {
                "credential_no": cred.credential_no,
                "kind": cred.kind,
                "version": cred.version,
                "status": cred.status,
                "sha256": cred.sha256,
                "effective_from": cred.effective_from,
                "effective_to": cred.effective_to,
                "received_time": cred.received_time,
            }
    links = graph.db.scalars(
        select(m.AuditEvidenceLink).where(m.AuditEvidenceLink.case_id == case.id)
    ).all()
    return jsonable(
        {
            "party_versions": party_versions,
            "credential_versions": creds,
            "evidence_versions": [
                {
                    "link_id": l.id,
                    "role": l.role,
                    "node_ref": l.node_ref,
                    "sha256": l.sha256,
                    "source_document_id": l.source_document_id,
                    "status": l.status,
                }
                for l in links
            ],
            "export_date": case.export_date,
            "case_scope": {
                "product_sku": case.product_sku,
                "hs_code": case.hs_code,
                "region": case.country,
                "quantity": str(case.quantity),
                "currency": case.currency,
            },
            "judged_at": _now(),
        }
    )


def _persist(
    db: Session,
    case: m.AuditCase,
    actor: str,
    kind: str,
    *,
    state: str,
    quantity_confirmed: Decimal,
    quantity_restricted: Decimal,
    segments: list[dict],
    witnesses: dict,
    graph: Graph,
    dependencies: list[dict],
    reasoning: dict,
    note: str | None,
) -> m.AuditConclusion:
    version = _next_version(db, case.id)
    prev = current_conclusion(db, case.id)
    if prev is not None:
        prev.is_current = False
        if prev.status == "active":
            prev.status = "superseded"
    post_export_received = _any_post_export_received(witnesses, case)
    # A conclusion based on documents received after export becomes importable only after an
    # explicit review approval; every other effective-at-export chain is importable immediately.
    approved = kind == "review_approved"
    usable = state in (CONFIRMED, RESTRICTED) and (not post_export_received or approved)
    conclusion = m.AuditConclusion(
        case_id=case.id,
        version=version,
        is_current=True,
        status="active",
        state=state,
        quantity_confirmed=quantity_confirmed,
        quantity_restricted=quantity_restricted,
        usable_for_new_dossier=usable,
        review_approved=approved,
        segments=jsonable(segments),
        witnesses=jsonable(witnesses),
        frozen_versions=_freeze_versions(case, _parties(db, case), graph, dependencies),
        reasoning=jsonable(
            {
                **reasoning,
                "review_required_for_post_export_receipt": post_export_received,
                "review_note": note,
                "conclusion_kind": kind,
                "state_vocabulary": [
                    CONFIRMED, RESTRICTED, INSUFFICIENT, BROKEN_TITLE, CONFLICT, PENDING,
                ],
            }
        ),
        basis_cutoff=_now(),
        supersedes_id=prev.id if prev else None,
        created_by=actor,
    )
    db.add(conclusion)
    db.flush()
    for dep in dependencies:
        db.add(
            m.ConclusionCredentialRef(
                conclusion_id=conclusion.id,
                credential_id=dep["credential_id"],
                role=dep["role"],
                path_index=dep.get("path_index"),
                step_index=dep.get("step_index"),
                counted_quantity=Decimal(dep["counted_quantity"]) if dep.get("counted_quantity") else None,
            )
        )
    db.add(
        m.AuditReviewEvent(
            case_id=case.id,
            conclusion_id=conclusion.id,
            kind=kind,
            actor=actor,
            note=note,
            payload=jsonable({"state": state, "version": version, "usable_for_new_dossier": usable}),
        )
    )
    db.flush()
    return conclusion


def _parties(db: Session, case: m.AuditCase) -> dict[str, m.PartyProfile]:
    return {
        "invoice": db.get(m.PartyProfile, case.invoice_party_id),
        "title": db.get(m.PartyProfile, case.title_party_id),
        "customs": db.get(m.PartyProfile, case.customs_party_id),
    }


def _next_version(db: Session, case_id: int) -> int:
    last = db.scalar(
        select(m.AuditConclusion.version)
        .where(m.AuditConclusion.case_id == case_id)
        .order_by(m.AuditConclusion.version.desc())
        .limit(1)
    )
    return (last or 0) + 1


def _any_post_export_received(witnesses: dict, case: m.AuditCase) -> bool:
    for group in witnesses.values():
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            if item.get("received_after_export"):
                return True
            steps = item.get("steps") or item.get("path", {}).get("steps", [])
            for step in steps:
                if step.get("post_export_received"):
                    return True
    return False


# ---------------------------------------------------------------------------
# Shared credential changes and per-row propagation
# ---------------------------------------------------------------------------


def affected_cases(db: Session, credential_id: int) -> list[int]:
    """Every case whose current conclusion cited this credential, plus cases that referenced an
    earlier/later version of the same credential number."""
    cred_ids = {credential_id}
    cred = db.get(m.AuthorizationCredential, credential_id)
    if cred is not None:
        versions = db.scalars(
            select(m.AuthorizationCredential.id).where(
                m.AuthorizationCredential.entity_id == cred.entity_id,
                m.AuthorizationCredential.credential_no == cred.credential_no,
            )
        ).all()
        cred_ids.update(versions)
    rows = db.scalars(
        select(m.ConclusionCredentialRef.conclusion_id)
        .join(m.AuditConclusion, m.AuditConclusion.id == m.ConclusionCredentialRef.conclusion_id)
        .where(
            m.ConclusionCredentialRef.credential_id.in_(cred_ids),
            m.AuditConclusion.is_current.is_(True),
        )
    ).all()
    case_ids = []
    for cid in set(rows):
        conclusion = db.get(m.AuditConclusion, cid)
        if conclusion is not None:
            case_ids.append(conclusion.case_id)
    return sorted(set(case_ids))


def handle_shared_change(
    db: Session,
    credential: m.AuthorizationCredential,
    *,
    kind: str,
    actor: str,
    detail: dict | None = None,
) -> m.AuditSharedChangeEvent:
    """Locate every dependent row, rejudge each row alone, and pause only rows that lose their
    valid witness (or whose scope basis changed).

    Rows that still hold an independent valid witness keep their current conclusion untouched, so
    their historical frozen versions stay exactly as they were.
    """
    case_ids = affected_cases(db, credential.id)
    event = m.AuditSharedChangeEvent(
        entity_id=credential.entity_id,
        credential_id=credential.id,
        kind=kind,
        detail=jsonable({**(detail or {}), "credential_no": credential.credential_no}),
        affected_case_ids=jsonable(case_ids),
        created_by=actor,
    )
    db.add(event)
    db.flush()

    paused: list[int] = []
    unchanged: list[int] = []
    rejudged: list[dict] = []
    affected_ids = _credential_version_ids(db, credential)
    for case_id in case_ids:
        case = db.get(m.AuditCase, case_id)
        prev = current_conclusion(db, case_id)
        if case is None or prev is None:
            continue
        prev_status = prev.status
        prev_usable = prev.usable_for_new_dossier
        prev_state = prev.state
        prev_confirmed = str(prev.quantity_confirmed)
        # Rejudge strictly per row — never copy another row's conclusion.
        new = evaluate_case(
            db,
            case,
            actor=actor,
            note=f"rejudged after shared credential change {credential.credential_no}",
            kind="shared_change_rejudge",
        )
        still_valid = _still_holds_unaffected_witness(prev, new, affected_ids)
        if still_valid and new.state == prev_state and str(new.quantity_confirmed) == prev_confirmed:
            # The row still holds an independent, identical valid witness: withdraw the
            # rejudgement and leave the original conclusion (and its frozen versions) in place.
            _discard_conclusion(db, new)
            prev.is_current = True
            prev.status = prev_status
            prev.usable_for_new_dossier = prev_usable
            unchanged.append(case_id)
            rejudged.append(
                {
                    "case_id": case_id,
                    "previous_conclusion_id": prev.id,
                    "new_conclusion_id": prev.id,
                    "previous_state": prev_state,
                    "new_state": prev_state,
                    "changed": False,
                    "usable_for_new_dossier": prev_usable,
                }
            )
            continue
        # The row's witness was lost or its basis changed: pause the old conclusion for new
        # dossiers and install the per-row rejudgement.
        prev.is_current = False
        prev.status = "under_review"
        prev.usable_for_new_dossier = False
        paused.append(case_id)
        db.add(
            m.AuditReviewEvent(
                case_id=case_id,
                conclusion_id=prev.id,
                kind="shared_credential_change",
                actor=actor,
                note=f"shared credential {credential.credential_no} changed ({kind}); the current "
                "conclusion is paused for new dossiers pending per-row rejudgement",
                payload=jsonable({"credential_id": credential.id, "new_status": credential.status}),
            )
        )
        db.flush()
        rejudged.append(
            {
                "case_id": case_id,
                "previous_conclusion_id": prev.id,
                "new_conclusion_id": new.id,
                "previous_state": prev_state,
                "new_state": new.state,
                "changed": True,
                "usable_for_new_dossier": new.usable_for_new_dossier,
            }
        )
    event.paused_case_ids = jsonable(sorted(set(paused)))
    event.unchanged_case_ids = jsonable(sorted(set(unchanged)))
    event.rejudged = jsonable(rejudged)
    db.flush()
    return event


def _credential_version_ids(db: Session, credential: m.AuthorizationCredential) -> set[int]:
    """All versions of a credential share one dependency basis (same credential number)."""
    ids = db.scalars(
        select(m.AuthorizationCredential.id).where(
            m.AuthorizationCredential.entity_id == credential.entity_id,
            m.AuthorizationCredential.credential_no == credential.credential_no,
        )
    ).all()
    return set(ids) | {credential.id}


def _path_cred_ids(entry: dict) -> tuple[int, ...]:
    steps = entry.get("steps") or entry.get("path", {}).get("steps", [])
    return tuple(s["credential_id"] for s in steps if s.get("credential_id"))


def _still_holds_unaffected_witness(
    prev: m.AuditConclusion, new: m.AuditConclusion, affected_ids: set[int]
) -> bool:
    """Does at least one *effective authorization witness* of the previous conclusion avoid the
    changed credential and survive unchanged?"""
    prev_paths = {
        _path_cred_ids(entry)
        for entry in (prev.witnesses or {}).get("customs_authorization", [])
        if entry.get("steps")
    }
    new_paths = {
        _path_cred_ids(entry)
        for entry in (new.witnesses or {}).get("customs_authorization", [])
        if entry.get("steps")
    }
    unaffected = {p for p in prev_paths if p and not (set(p) & affected_ids)}
    return bool(unaffected & new_paths)


def _discard_conclusion(db: Session, conclusion: m.AuditConclusion) -> None:
    db.flush()
    db.execute(
        m.ConclusionCredentialRef.__table__.delete().where(
            m.ConclusionCredentialRef.conclusion_id == conclusion.id
        )
    )
    db.execute(
        m.AuditReviewEvent.__table__.delete().where(
            m.AuditReviewEvent.conclusion_id == conclusion.id
        )
    )
    db.execute(m.AuditConclusion.__table__.delete().where(m.AuditConclusion.id == conclusion.id))
    db.flush()


# ---------------------------------------------------------------------------
# Review flow
# ---------------------------------------------------------------------------


def add_supplement(
    db: Session, case: m.AuditCase, links: list[AuditEvidenceLinkRequest], actor: str, note: str | None
) -> list[m.AuditEvidenceLink]:
    """Append supplementary material and move the current conclusion to review.

    Historical conclusions keep their frozen versions and basis; only the current one is paused.
    """
    created = [add_evidence_link(db, case, link, actor) for link in links]
    prev = current_conclusion(db, case.id)
    if prev is not None:
        prev.is_current = False
        prev.status = "under_review"
        prev.usable_for_new_dossier = False
    db.add(
        m.AuditReviewEvent(
            case_id=case.id,
            conclusion_id=prev.id if prev else None,
            kind="supplementary_material",
            actor=actor,
            note=note or "supplementary material changes a key interval; current conclusion under review",
            payload=jsonable({"link_ids": [l.id for l in created]}),
        )
    )
    db.flush()
    return created


def approve_review(db: Session, case: m.AuditCase, actor: str, note: str | None) -> m.AuditConclusion:
    conclusion = evaluate_case(
        db, case, actor=actor, note=note or "review approved", kind="review_approved"
    )
    conclusion.review_approved = True
    if conclusion.state in (CONFIRMED, RESTRICTED):
        conclusion.usable_for_new_dossier = True
    else:
        # An approval can clear a review hold on a statement that is still only partial; it can
        # never turn a pending/insufficient/broken/conflicting row into an importable fact.
        conclusion.usable_for_new_dossier = False
    db.flush()
    return conclusion


def reject_review(db: Session, case: m.AuditCase, actor: str, note: str | None) -> m.AuditConclusion:
    conclusion = evaluate_case(
        db, case, actor=actor, note=note or "review rejected", kind="review_rejected"
    )
    # A rejected review can never keep a fully-confirmed state and cannot be imported.
    conclusion.usable_for_new_dossier = False
    if conclusion.state == CONFIRMED:
        qty = conclusion.quantity_confirmed
        conclusion.state = RESTRICTED
        conclusion.segments = jsonable(
            [{"state": RESTRICTED, "quantity": str(qty), "quantity_confirmed": "0",
              "quantity_restricted": str(qty)}]
        )
        conclusion.quantity_restricted = qty
        conclusion.quantity_confirmed = ZERO
    db.flush()
    return conclusion


# ---------------------------------------------------------------------------
# Reconciliation gate
# ---------------------------------------------------------------------------


def hold_for_new_dossier(db: Session, export_line_id: int) -> dict | None:
    """Why a new reconciliation dossier may not reference this export row, if at all.

    Only rows that have an audit case are gated. A row is held when its current conclusion is
    under review (for example after supplementary material or a shared credential change) or when
    the current conclusion is not importable — e.g. it relies on post-export receipt that has not
    been review-approved yet. Rows without an audit case are unaffected.
    """
    case = db.scalar(select(m.AuditCase).where(m.AuditCase.export_line_id == export_line_id))
    if case is None:
        return None
    latest = db.scalar(
        select(m.AuditConclusion)
        .where(m.AuditConclusion.case_id == case.id)
        .order_by(m.AuditConclusion.version.desc())
        .limit(1)
    )
    if latest is None:
        return None
    if latest.status == "under_review" and not latest.is_current:
        return {
            "case_id": case.id,
            "case_ref": case.case_ref,
            "conclusion_id": latest.id,
            "reason": "audit conclusion is under review; new reconciliation dossiers are paused "
            "until the review is adjudicated",
        }
    current = current_conclusion(db, case.id)
    if current is None:
        return {
            "case_id": case.id,
            "case_ref": case.case_ref,
            "conclusion_id": latest.id,
            "reason": "audit case has no current conclusion",
        }
    if not current.usable_for_new_dossier:
        extra = ""
        if current.reasoning.get("review_required_for_post_export_receipt") and not current.review_approved:
            extra = " (documents received after export still need review approval)"
        return {
            "case_id": case.id,
            "case_ref": case.case_ref,
            "conclusion_id": current.id,
            "state": current.state,
            "reason": f"audit conclusion {current.state} is not importable into a new dossier{extra}",
        }
    return None


def conclusion_view(db: Session, conclusion: m.AuditConclusion) -> dict:
    case = db.get(m.AuditCase, conclusion.case_id)
    return jsonable(
        {
            "id": conclusion.id,
            "case_id": conclusion.case_id,
            "case_ref": case.case_ref if case else None,
            "version": conclusion.version,
            "is_current": conclusion.is_current,
            "status": conclusion.status,
            "state": conclusion.state,
            "quantity_confirmed": conclusion.quantity_confirmed,
            "quantity_restricted": conclusion.quantity_restricted,
            "usable_for_new_dossier": conclusion.usable_for_new_dossier,
            "review_approved": conclusion.review_approved,
            "segments": conclusion.segments,
            "witnesses": conclusion.witnesses,
            "frozen_versions": conclusion.frozen_versions,
            "reasoning": conclusion.reasoning,
            "basis_cutoff": conclusion.basis_cutoff,
            "supersedes_id": conclusion.supersedes_id,
            "created_by": conclusion.created_by,
            "created_at": conclusion.created_at,
        }
    )


def case_detail(db: Session, case: m.AuditCase) -> dict:
    parties = _parties(db, case)
    links = db.scalars(
        select(m.AuditEvidenceLink).where(m.AuditEvidenceLink.case_id == case.id).order_by(m.AuditEvidenceLink.id)
    ).all()
    conclusions = db.scalars(
        select(m.AuditConclusion)
        .where(m.AuditConclusion.case_id == case.id)
        .order_by(m.AuditConclusion.version.desc())
    ).all()
    current = current_conclusion(db, case.id)
    reviews = db.scalars(
        select(m.AuditReviewEvent).where(m.AuditReviewEvent.case_id == case.id).order_by(m.AuditReviewEvent.id)
    ).all()
    cred_ids: set[int] = set()
    if current is not None:
        for group in current.witnesses.values():
            if not isinstance(group, list):
                continue
            for item in group:
                steps = item.get("steps") or item.get("path", {}).get("steps", [])
                for step in steps:
                    if step.get("credential_id"):
                        cred_ids.add(step["credential_id"])
    creds = [
        credential_view(db, db.get(m.AuthorizationCredential, cid)) for cid in sorted(cred_ids)
    ]
    from .audit_registry import party_view

    return jsonable(
        {
            "case": {
                "id": case.id,
                "case_ref": case.case_ref,
                "entity_id": case.entity_id,
                "export_line_id": case.export_line_id,
                "invoice_line_id": case.invoice_line_id,
                "customs_line_id": case.customs_line_id,
                "product_sku": case.product_sku,
                "hs_code": case.hs_code,
                "region": case.country,
                "quantity": case.quantity,
                "amount": case.amount,
                "currency": case.currency,
                "export_date": case.export_date,
                "recorded_names": case.recorded_names,
                "status": case.status,
            },
            "parties": {
                role: party_view(db, party) for role, party in parties.items() if party is not None
            },
            "evidence_links": [
                {
                    "id": l.id,
                    "node_kind": l.node_kind,
                    "node_ref": l.node_ref,
                    "role": l.role,
                    "valid_from": l.valid_from,
                    "valid_to": l.valid_to,
                    "quantity": l.quantity,
                    "source_document_id": l.source_document_id,
                    "original_location": l.original_location,
                    "sha256": l.sha256,
                    "status": l.status,
                    "detail": l.detail,
                }
                for l in links
            ],
            "credentials": creds,
            "current_conclusion": conclusion_view(db, current) if current else None,
            "conclusion_history": [
                {
                    "id": c.id,
                    "version": c.version,
                    "state": c.state,
                    "status": c.status,
                    "is_current": c.is_current,
                    "frozen_versions": c.frozen_versions,
                    "review_approved": c.review_approved,
                    "created_at": c.created_at,
                }
                for c in conclusions
            ],
            "review_history": [
                {
                    "id": r.id,
                    "conclusion_id": r.conclusion_id,
                    "kind": r.kind,
                    "actor": r.actor,
                    "note": r.note,
                    "payload": r.payload,
                    "created_at": r.created_at,
                }
                for r in reviews
            ],
        }
    )
