"""Seed demo data: entities, users/API keys, contracts, invoices, customs, tax receipts, bank flows.

Idempotent: re-running does nothing unless --reset is given.
Prints the API keys so the README instructions can be followed verbatim.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app import models as m
from app.config import settings
from app.db import SessionLocal, ensure_database, init_db
from app.schemas import IntakeLine, IntakeRequest
from app.services import audit, audit_registry, balance, facts, ledgers, resolver
from app.services.intake import ingest_document
from app.util import key_hash

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
EXPORT_DATE = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
VALUE_DATE = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
DECL = "DECL-2026-100"
INV = "INV-2026-001"

USERS = [
    ("admin", "admin", "demo-admin-key", None),
    ("analyst1", "analyst", "demo-analyst-1-key", ["ENT-A"]),
    ("analyst2", "analyst", "demo-analyst-2-key", ["ENT-A"]),
    ("entity-b-analyst", "analyst", "demo-analyst-b-key", ["ENT-B"]),
    ("viewer", "viewer", "demo-viewer-key", ["ENT-A", "ENT-B"]),
]

LINES = [
    dict(line_no=1, sku="SKU-AA-100", description="Widget A", quantity=Decimal("100"), amount=Decimal("1200.00")),
    dict(line_no=2, sku="SKU-BB-200", description="Widget B", quantity=Decimal("50"), amount=Decimal("1800.00")),
    dict(line_no=3, sku="SKU-CC-300", description="Widget C", quantity=Decimal("20"), amount=Decimal("2000.00")),
]


def _req(**kw) -> IntakeRequest:
    base = dict(original_location="seed://placeholder", event_time=T0, received_time=T0)
    base.update(kw)
    return IntakeRequest(**base)


def seed(reset: bool = False) -> None:
    ensure_database(settings.database_url, settings.admin_database_url)
    init_db()

    db = SessionLocal()
    try:
        if reset:
            # Rebuild the schema so option/column changes in this codebase are always picked up;
            # the demo database is disposable and rebuilt from scratch.
            m.Base.metadata.drop_all(bind=db.get_bind())
            m.Base.metadata.create_all(bind=db.get_bind())
            db.commit()
        if db.scalar(select(m.Entity.id).limit(1)) is not None:
            print("database already seeded — use --reset to rebuild")
            return

        ents = {
            "ENT-A": m.Entity(code="ENT-A", name="辰星跨境贸易有限公司", country="CN"),
            "ENT-B": m.Entity(code="ENT-B", name="海岳国际供应链有限公司", country="CN"),
        }
        db.add_all(ents.values())
        db.flush()

        for name, role, key, codes in USERS:
            user = m.User(name=name, role=role, api_key_hash=key_hash(key))
            db.add(user)
            db.flush()
            for code in codes or []:
                db.add(m.UserEntityGrant(user_id=user.id, entity_id=ents[code].id))
        db.flush()

        actor = "seed"

        # contract
        ingest_document(
            db,
            _req(
                doc_type="contract",
                entity_code="ENT-A",
                external_no="C-2026-001",
                original_location="s3://demo/contracts/C-2026-001.pdf",
                currency="USD",
                amount=Decimal("5000.00"),
                lines=[IntakeLine(contract_no="C-2026-001", buyer_name="Global Buyer LLC", currency="USD",
                                  amount=Decimal("5000.00"))],
            ),
            actor,
        )

        # invoice (3 lines) — each line becomes an export line
        ingest_document(
            db,
            _req(
                doc_type="invoice",
                entity_code="ENT-A",
                external_no=INV,
                original_location=f"s3://demo/invoices/{INV}.pdf",
                currency="USD",
                amount=Decimal("5000.00"),
                lines=[
                    IntakeLine(
                        line_no=l["line_no"],
                        sku=l["sku"],
                        description=l["description"],
                        quantity=l["quantity"],
                        amount=l["amount"],
                        currency="USD",
                        invoice_no=INV,
                        contract_no="C-2026-001",
                        declaration_no=DECL,
                        export_date=EXPORT_DATE,
                    )
                    for l in LINES
                ],
            ),
            actor,
        )

        # customs archive (original basis)
        ingest_document(
            db,
            _req(
                doc_type="customs_archive",
                entity_code="ENT-A",
                external_no=DECL,
                original_location=f"s3://demo/customs/{DECL}.xml",
                lines=[
                    IntakeLine(
                        line_no=l["line_no"],
                        sku=l["sku"],
                        description=l["description"],
                        quantity=l["quantity"],
                        amount=l["amount"],
                        currency="USD",
                        hs_code="8471.30",
                        declaration_no=DECL,
                    )
                    for l in LINES
                ],
            ),
            actor,
        )

        # tax receipt (batch 1)
        ingest_document(
            db,
            _req(
                doc_type="tax_receipt",
                entity_code="ENT-A",
                external_no="B-2026-09-01",
                original_location="s3://demo/tax/B-2026-09-01.json",
                lines=[
                    IntakeLine(
                        line_no=l["line_no"],
                        batch_no="B-2026-09-01",
                        declaration_no=DECL,
                        review_status="approved",
                        quantity=l["quantity"],
                        amount=l["amount"],
                        currency="USD",
                        issue_time=T0,
                        sequence_no=1,
                    )
                    for l in LINES
                ],
            ),
            actor,
        )
        db.commit()

        # declaration states: submitted + verified for all three lines
        exports = db.scalars(
            select(m.ExportLine).where(m.ExportLine.entity_id == ents["ENT-A"].id).order_by(m.ExportLine.id)
        ).all()
        for e in exports:
            ledgers.apply_declaration_event(db, e, "submitted", actor=actor, reference="DECL-SUBMIT-2026-09")
            ledgers.apply_declaration_event(db, e, "verified", actor=actor, reference="TAX-REVIEW-2026-09")
        db.commit()

        # bank flows:
        #  1) unique SKU reference, partial (1000 against 1200)
        #  2) combined remittance with no line key (2500 for lines 2+3 = 3800) -> candidates only
        ingest_document(
            db,
            _req(
                doc_type="bank_statement",
                entity_code="ENT-A",
                external_no="STMT-2026-10-A",
                original_location="s3://demo/bank/2026-10-a.csv",
                event_time=VALUE_DATE,
                received_time=VALUE_DATE,
                currency="USD",
                amount=Decimal("1000.00"),
                lines=[
                    IntakeLine(
                        txn_ref="TXN-2026-10-A",
                        payer_name="Global Buyer LLC",
                        currency="USD",
                        amount=Decimal("1000.00"),
                        value_date=VALUE_DATE,
                        remittance_info="ORDER SETTLEMENT SKU-AA-100",
                    )
                ],
            ),
            actor,
        )
        ingest_document(
            db,
            _req(
                doc_type="bank_statement",
                entity_code="ENT-A",
                external_no="STMT-2026-10-B",
                original_location="s3://demo/bank/2026-10-b.csv",
                event_time=VALUE_DATE,
                received_time=VALUE_DATE,
                currency="USD",
                amount=Decimal("2500.00"),
                lines=[
                    IntakeLine(
                        txn_ref="TXN-2026-10-B",
                        payer_name="Global Buyer LLC",
                        currency="USD",
                        amount=Decimal("2500.00"),
                        value_date=VALUE_DATE,
                        remittance_info="OCTOBER CONSOLIDATED PAYMENT",
                    )
                ],
            ),
            actor,
        )
        db.commit()

        resolver.resolve_all(db, ents["ENT-A"].id)
        db.commit()
        balance.run_reconciliation_pass(db, entity_id=ents["ENT-A"].id, actor="seed")
        db.commit()
        run = facts.compile_facts(db, entity_id=ents["ENT-A"].id, actor="seed")
        db.commit()
        audit_summary = seed_audit(db, ents, exports, actor)
        db.commit()

        # The seeded pending/review conclusions legitimately block *new* dossiers. Re-run the
        # resolver so the hold becomes visible in the demo data from the start.
        resolver.resolve_all(db, ents["ENT-A"].id)
        db.commit()
        audit_summary["held_lines"] = sorted(
            {
                case.export_line_id
                for case in db.scalars(select(m.AuditCase)).all()
                if audit.hold_for_new_dossier(db, case.export_line_id) is not None
            }
        )

        print("seeded: entities=%d users=%d export_lines=%d facts=%d unallocated=%d differences=%d"
              % (len(ents), len(USERS), len(exports), run.facts_count, run.unallocated_count, run.differences_count))
        print("audit: cases=%d conclusions=%s held_export_lines=%s"
              % (len(audit_summary["cases"]), audit_summary["states"], audit_summary["held_lines"]))
        print()
        print("API keys (X-API-Key):")
        for name, role, key, codes in USERS:
            print(f"  {name:<18} role={role:<8} key={key:<24} entities={','.join(codes or ['*'])}")
        print()
        print("start: uvicorn app.main:app --host 0.0.0.0 --port 8000")
        print("swagger: http://127.0.0.1:8000/docs")
    finally:
        db.close()


def seed_audit(db, ents, exports, actor: str) -> dict:
    """Party archive, credential registry and per-row audit conclusions for the demo rows.

    Row 1: title chain + customs authorization both effective before export but the authorization
    was *received* after export (still confirmed, flagged for review).
    Row 2: authorization issued after export with no verifiable retroactive scope -> pending.
    Row 3: title transfer covers only part of the row -> restricted + broken-title segments.
    """
    from app.schemas import (
        AuditCaseCreateRequest,
        CredentialCreateRequest,
        PartyCreateRequest,
    )

    parties = {}
    for code, name, uscc in (
        ("P-INV", "辰星跨境贸易有限公司", "91310000MA1FL00123"),
        ("P-TIT", "辰星国际供应链有限公司", "91310000MA1FL00456"),
        ("P-CUS", "辰星系报关服务有限公司", "91310000MA1FL00789"),
    ):
        parties[code] = audit_registry.create_party(
            db,
            PartyCreateRequest(
                code=code,
                legal_name=name,
                uscc=uscc,
                country="CN",
                entity_code="ENT-A",
                valid_from=T0,
            ),
            actor,
        )

    def cred(no, kind, grantor, grantee, limit, *, issued=T0, received=T0, effective=T0,
             scope=None, retroactive=False, retro_verified=False, retro_from=None):
        return audit_registry.create_credential(
            db,
            CredentialCreateRequest(
                entity_code="ENT-A",
                credential_no=no,
                kind=kind,
                grantor_party_code=grantor,
                grantee_party_code=grantee,
                issued_at=issued,
                received_time=received,
                effective_from=effective,
                scope=scope or {"regions": ["CN"]},
                quantity_limit=Decimal(limit),
                currency="USD",
                original_location=f"s3://demo/audit/{no}.pdf",
                retroactive=retroactive,
                retroactive_scope_verified=retro_verified,
                retroactive_from=retro_from,
            ),
            actor,
        )

    received_late = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    issued_late = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    cases = []
    states = []

    def build(export, *, title_qty, auth_issued, auth_received, sku):
        audit_registry.create_credential(
            db,
            CredentialCreateRequest(
                entity_code="ENT-A",
                credential_no=f"TT-{export.id}",
                kind="title_transfer",
                grantor_party_code="P-INV",
                grantee_party_code="P-TIT",
                issued_at=T0,
                received_time=T0,
                effective_from=T0,
                scope={"regions": ["CN"], "skus": [sku]},
                quantity_limit=Decimal(title_qty),
                original_location=f"s3://demo/audit/TT-{export.id}.pdf",
            ),
            actor,
        )
        cred(
            f"CA-{export.id}",
            "customs_authorization",
            "P-TIT",
            "P-CUS",
            "200",
            issued=auth_issued,
            received=auth_received,
            effective=auth_issued,
            scope={"regions": ["CN"], "skus": [sku], "customs_party_codes": ["P-CUS"]},
        )
        case = audit.build_case(
            db,
            ents["ENT-A"],
            AuditCaseCreateRequest(
                export_line_id=export.id,
                invoice_party_code="P-INV",
                title_party_code="P-TIT",
                customs_party_code="P-CUS",
                region="CN",
                product_sku=sku,
            ),
            actor,
        )
        conclusion = audit.evaluate_case(db, case, actor=actor, kind="initial")
        cases.append(case.id)
        states.append(conclusion.state)

    build(exports[0], title_qty="100", auth_issued=T0, auth_received=received_late, sku="SKU-AA-100")
    build(exports[1], title_qty="50", auth_issued=issued_late, auth_received=issued_late, sku="SKU-BB-200")
    build(exports[2], title_qty="8", auth_issued=T0, auth_received=T0, sku="SKU-CC-300")
    return {"cases": cases, "states": states}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--if-empty", action="store_true")
    args = ap.parse_args()
    if args.if_empty:
        db = SessionLocal()
        try:
            if db.scalar(select(m.Entity.id).limit(1)) is not None:
                print("seed skipped: data present")
                return 0
        finally:
            db.close()
    seed(reset=args.reset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
