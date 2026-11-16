"""Acceptance tests for the cross-entity export evidence chain / authorization timeliness audit."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from helpers import EXPORT_DATE, seed_contract, seed_customs, seed_invoice, seed_tax

T_EXPORT = EXPORT_DATE
T_BEFORE = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
T_AFTER = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
T_RECEIVED_LATE = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def audit_env(client, bootstrap, db):
    """Three parties under ENT-A with the standard 3-line export."""
    a = bootstrap["analyst1"]
    seed_contract(client, a)
    seed_invoice(client, a)
    seed_customs(client, a)
    seed_tax(client, a)
    parties = {}
    for code, name, uscc in (
        ("P-INV", "辰星跨境贸易有限公司", "91310000MA1FL00123"),
        ("P-TIT", "辰星国际供应链有限公司", "91310000MA1FL00456"),
        ("P-CUS", "辰星系报关服务有限公司", "91310000MA1FL00789"),
    ):
        r = client.post(
            "/api/v1/audit/parties",
            json={
                "code": code,
                "legal_name": name,
                "uscc": uscc,
                "country": "CN",
                "entity_code": "ENT-A",
                "valid_from": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
            },
            headers=a,
        )
        assert r.status_code == 201, r.text
        parties[code] = r.json()
    dossiers = client.get("/api/v1/entities/ENT-A/dossiers", headers=a)
    assert dossiers.status_code == 200
    r = client.post("/api/v1/entities/ENT-A/resolve", headers=a)
    assert r.status_code == 201, r.text
    rows = client.get("/api/v1/entities/ENT-A/dossiers", headers=a).json()
    export_ids = sorted(x["export_line_id"] for x in rows if x["export_line_id"])
    return {"headers": a, "parties": parties, "export_ids": export_ids}


def _credential(client, env, **kw):
    body = {
        "entity_code": "ENT-A",
        "credential_no": kw.pop("credential_no"),
        "kind": kw.pop("kind"),
        "grantor_party_code": kw.pop("grantor"),
        "grantee_party_code": kw.pop("grantee"),
        "issued_at": T_BEFORE.isoformat(),
        "received_time": T_BEFORE.isoformat(),
        "effective_from": T_BEFORE.isoformat(),
        "original_location": "s3://audit/cred.pdf",
        "scope": kw.pop("scope", {"regions": ["CN"], "skus": ["SKU-AA-100"]}),
    }
    body.update(kw)
    r = client.post("/api/v1/entities/ENT-A/credentials", json=body, headers=env["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def _case(client, env, export_id, *, invoice="P-INV", title="P-TIT", customs="P-CUS",
          region="CN", names=None, sku="SKU-AA-100"):
    r = client.post(
        "/api/v1/entities/ENT-A/audit-cases",
        json={
            "export_line_id": export_id,
            "invoice_party_code": invoice,
            "title_party_code": title,
            "customs_party_code": customs,
            "region": region,
            "product_sku": sku,
            "recorded_names": names or {},
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _current(client, env, case_id):
    detail = client.get(f"/api/v1/audit-cases/{case_id}", headers=env["headers"]).json()
    return detail["current_conclusion"]


def _base_chain(client, env, export_id, *, title_qty="100", auth_qty="120",
                received=T_BEFORE, issued=T_BEFORE):
    _credential(
        client, env,
        credential_no=f"TT-{export_id}", kind="title_transfer",
        grantor="P-INV", grantee="P-TIT", quantity_limit=title_qty,
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{export_id}", kind="customs_authorization",
        grantor="P-TIT", grantee="P-CUS", quantity_limit=auth_qty,
        issued_at=issued.isoformat(), received_time=received.isoformat(),
        effective_from=T_BEFORE.isoformat(),
    )


# ---------------------------------------------------------------------------
# 1. Post-export receipt, effective before export -> confirmed after review
# ---------------------------------------------------------------------------


def test_post_export_receipt_with_export_date_effect_is_confirmed_after_review(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="100", effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="120", issued_at=T_BEFORE.isoformat(), received_time=T_RECEIVED_LATE.isoformat(),
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "cross_entity_chain_confirmed"
    # received after export, so it still needs review before being importable
    assert cur["usable_for_new_dossier"] is False
    assert cur["reasoning"]["review_required_for_post_export_receipt"] is True
    auth_steps = cur["witnesses"]["customs_authorization"][0]["steps"]
    assert auth_steps[0]["post_export_received"] is True
    assert auth_steps[0]["issued_at"] <= EXPORT_DATE.isoformat()

    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/review",
        json={"action": "approve", "note": "content verified against the signed original"},
        headers=env["headers"],
    )
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["usable_for_new_dossier"] is True
    assert approved["review_approved"] is True


# ---------------------------------------------------------------------------
# 2. Issued after export with no retroactive scope -> pending, never back-filled
# ---------------------------------------------------------------------------


def test_post_export_issued_without_retroactive_scope_is_pending_evidence(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="100", effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="120", issued_at=T_AFTER.isoformat(), received_time=T_AFTER.isoformat(),
        effective_from=T_AFTER.isoformat(),
    )
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "pending_evidence"
    assert cur["usable_for_new_dossier"] is False
    assert cur["quantity_confirmed"] in ("0", "0.0000", "0.00")
    assert any(
        c.get("reason") and "after export" in c["reason"]
        for c in cur["reasoning"]["pending_candidates"]
    )
    # review approval may not silently back-fill export-date eligibility
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/review",
        json={"action": "approve", "note": "approved anyway"},
        headers=env["headers"],
    )
    assert r.status_code == 200
    assert r.json()["state"] == "pending_evidence"
    assert r.json()["usable_for_new_dossier"] is False


def test_retroactive_scope_is_accepted_when_verified(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="100", effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="120", issued_at=T_AFTER.isoformat(), received_time=T_AFTER.isoformat(),
        effective_from=T_AFTER.isoformat(),
        retroactive=True,
        retroactive_scope_verified=True,
        retroactive_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "cross_entity_chain_confirmed"
    assert cur["usable_for_new_dossier"] is False  # still needs review: issued and received after export


def test_retroactive_flag_without_verified_scope_is_rejected(client, audit_env):
    env = audit_env
    r = client.post(
        "/api/v1/entities/ENT-A/credentials",
        json={
            "entity_code": "ENT-A",
            "credential_no": "CA-BAD",
            "kind": "customs_authorization",
            "grantor_party_code": "P-TIT",
            "grantee_party_code": "P-CUS",
            "issued_at": T_AFTER.isoformat(),
            "received_time": T_AFTER.isoformat(),
            "effective_from": T_AFTER.isoformat(),
            "original_location": "s3://audit/bad.pdf",
            "retroactive": True,
            "retroactive_scope_verified": False,
        },
        headers=env["headers"],
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 3. Partial title coverage -> restricted + broken-title segments
# ---------------------------------------------------------------------------


def test_partial_title_transfer_splits_row_into_restricted_and_broken_segments(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="60", effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="120", effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "restricted_confirmation"
    assert cur["quantity_confirmed"] in ("60", "60.0000", "60.00")
    assert cur["quantity_restricted"] in ("40", "40.0000", "40.00")
    states = {s["state"]: s for s in cur["segments"]}
    assert states["restricted_confirmation"]["quantity_confirmed"] in ("60", "60.0000", "60.00")
    assert states["title_chain_broken"]["quantity_restricted"] in ("40", "40.0000", "40.00")


def test_packing_evidence_covering_less_than_row_blocks_whole_row_pass(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _base_chain(client, env, eid)
    case = _case(client, env, eid)
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/evidence",
        json=[
            {
                "role": "packing",
                "node_ref": "PL-2026-001",
                "quantity": "80",
                "valid_from": T_BEFORE.isoformat(),
            }
        ],
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/audit-cases/{case['case_id']}/evaluate", json={}, headers=env["headers"])
    assert r.status_code == 200
    cur = r.json()
    assert cur["state"] == "restricted_confirmation"
    assert "quantity evidence" in cur["reasoning"]["reached_state"]


# ---------------------------------------------------------------------------
# 4. Name change: old name resolves through verified USCC, not through similarity
# ---------------------------------------------------------------------------


def test_old_name_in_customs_archive_must_resolve_through_uscc(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    # unverified name change -> party conflict (similarity alone is not identity)
    r = client.post(
        "/api/v1/audit/parties/P-CUS/name-changes",
        json={
            "old_name": "辰星系报关服务有限公司",
            "new_name": "辰星系报关服务股份有限公司",
            "effective_from": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
            "uscc_verified": False,
        },
        headers=env["headers"],
    )
    assert r.status_code == 422, r.text
    _base_chain(client, env, eid)
    case = _case(
        client, env, eid, names={"customs": "辰星系报关服务股份有限公司"}
    )
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "party_conflict"
    assert cur["reasoning"]["identity"]["matched"] == "none"


def test_name_change_verified_by_uscc_resolves_old_customs_name(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    r = client.post(
        "/api/v1/audit/parties/P-CUS/name-changes",
        json={
            "old_name": "辰星系报关服务有限公司",
            "new_name": "辰星系报关服务股份有限公司",
            "effective_from": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
            "uscc_verified": True,
            "inheritance_doc_no": "USCC-INHERIT-2026-001",
            "inheritance_location": "s3://audit/uscc-inherit.pdf",
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    _base_chain(client, env, eid)
    # the customs archive records the OLD name; identity is resolved through USCC/inheritance
    case = _case(
        client, env, eid, names={"customs": "辰星系报关服务有限公司"}
    )
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "cross_entity_chain_confirmed"
    party = client.get("/api/v1/audit/parties/P-CUS", headers=env["headers"]).json()
    assert party["name_changes"][0]["uscc_verified"] is True
    assert party["name_changes"][0]["inheritance_doc_no"] == "USCC-INHERIT-2026-001"


# ---------------------------------------------------------------------------
# 5. Mutually exclusive revocations freeze the chain
# ---------------------------------------------------------------------------


def test_mutually_exclusive_revocations_freeze_chain_and_yield_no_verified_state(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _base_chain(client, env, eid)
    case = _case(client, env, eid)
    assert _current(client, env, case["case_id"])["state"] == "cross_entity_chain_confirmed"

    creds = client.get("/api/v1/entities/ENT-A/credentials", headers=env["headers"]).json()
    ca = next(c for c in creds if c["kind"] == "customs_authorization")
    claim = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc).isoformat()
    for no, iss in (("REV-A", "P-INV"), ("REV-B", "P-TIT")):
        r = client.post(
            f"/api/v1/credentials/{ca['id']}/revocations",
            json={
                "revocation_no": no,
                "issuer_party_code": iss,
                "sequence_no": 1,
                "claimed_effective_from": claim,
                "signed_at": claim,
                "received_time": claim,
                "original_location": f"s3://audit/{no}.pdf",
            },
            headers=env["headers"],
        )
        assert r.status_code == 201, r.text
    cred = client.get(f"/api/v1/credentials/{ca['id']}", headers=env["headers"]).json()
    assert cred["status"] == "frozen"
    assert "mutually exclusive" in cred["frozen_reason"]
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "pending_evidence"
    assert cur["usable_for_new_dossier"] is False


# ---------------------------------------------------------------------------
# 6. Two paths to the same customs party count one share once; cycle does not inflate
# ---------------------------------------------------------------------------


def test_two_title_paths_to_same_customs_party_count_one_share_once(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]  # row quantity is 100
    sku = "SKU-AA-100"
    # two routes: INV->TIT->CUS (60 units) and INV->CUS (50 units). Summed they would look
    # like 110 — more than the row — but the same title share can never be counted twice.
    _credential(
        client, env, credential_no=f"TT-A-{eid}", kind="title_transfer",
        grantor="P-INV", grantee="P-TIT", quantity_limit="60", scope={"regions": ["CN"], "skus": [sku]},
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env, credential_no=f"TT-B-{eid}", kind="title_transfer",
        grantor="P-TIT", grantee="P-CUS", quantity_limit="60", scope={"regions": ["CN"], "skus": [sku]},
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env, credential_no=f"TT-C-{eid}", kind="title_transfer",
        grantor="P-INV", grantee="P-CUS", quantity_limit="50", scope={"regions": ["CN"], "skus": [sku]},
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env, credential_no=f"CA-{eid}", kind="customs_authorization",
        grantor="P-INV", grantee="P-CUS", quantity_limit="200",
        scope={"regions": ["CN"], "skus": [sku]},
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid, sku=sku)
    cur = _current(client, env, case["case_id"])
    capacities = cur["witnesses"]["title"]
    route_two_path = [c for c in capacities if c["credential_nos"] == [f"TT-A-{eid}", f"TT-B-{eid}"]]
    route_direct = [c for c in capacities if c["credential_nos"] == [f"TT-C-{eid}"]]
    # exactly two distinct routes reach the customs party; the share is never summed
    assert route_two_path and route_two_path[0]["quantity"] in ("60", "60.0000", "60.00")
    assert route_direct and route_direct[0]["quantity"] in ("50", "50.0000", "50.00")
    # the two-title-head route and the direct route land on the same customs party, so the
    # covered title share is max(60, 50) = 60 — not 110 — and the rest is broken title chain.
    assert cur["quantity_confirmed"] in ("60", "60.0000", "60.00")
    assert cur["quantity_restricted"] in ("40", "40.0000", "40.00")
    assert cur["state"] == "restricted_confirmation"


def test_cycle_back_to_original_party_does_not_inflate_coverage(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env, credential_no=f"TT-1-{eid}", kind="title_transfer",
        grantor="P-INV", grantee="P-TIT", quantity_limit="100",
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env, credential_no=f"TT-2-{eid}", kind="title_transfer",
        grantor="P-TIT", grantee="P-CUS", quantity_limit="100",
        effective_from=T_BEFORE.isoformat(),
    )
    # a cycle: CUS back to INV. It must not contribute extra title coverage.
    _credential(
        client, env, credential_no=f"TT-3-{eid}", kind="title_transfer",
        grantor="P-CUS", grantee="P-INV", quantity_limit="100",
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env, credential_no=f"CA-{eid}", kind="customs_authorization",
        grantor="P-INV", grantee="P-CUS", quantity_limit="200",
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["quantity_confirmed"] in ("100", "100.0000", "100.00")
    # the cycle cannot create more than one row's worth of confirmed quantity
    assert float(cur["quantity_confirmed"]) <= 100.0


# ---------------------------------------------------------------------------
# 7. Supplementary material moves the current conclusion to review
# ---------------------------------------------------------------------------


def test_supplementary_material_triggers_review_and_pauses_new_dossiers(client, audit_env):
    env = audit_env
    eid = env["export_ids"][1]  # row quantity is 50
    sku = "SKU-BB-200"
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="30", scope={"regions": ["CN"], "skus": [sku]},
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="50", scope={"regions": ["CN"], "skus": [sku]},
        issued_at=T_BEFORE.isoformat(), received_time=T_BEFORE.isoformat(),
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid, sku=sku)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "restricted_confirmation"
    confirmed = float(cur["quantity_confirmed"])
    assert confirmed == 30.0

    # declare the row as confirmed BEFORE the supplementary material arrives
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/review",
        json={"action": "approve", "note": "stable chain"},
        headers=env["headers"],
    )
    assert r.status_code == 200
    stable = r.json()
    assert stable["state"] == "restricted_confirmation"

    # supplementary material changes the title interval (post-export material)
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/supplements",
        json={
            "note": "late title transfer re-dates the chain",
            "links": [
                {
                    "role": "title_document",
                    "node_ref": "TT-REDATED-2026",
                    "valid_from": T_AFTER.isoformat(),
                    "quantity": str(confirmed),
                }
            ],
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    detail = client.get(f"/api/v1/audit-cases/{case['case_id']}", headers=env["headers"]).json()
    assert detail["current_conclusion"] is None  # current conclusion is paused for new dossiers
    reviews = [r for r in detail["review_history"] if r["kind"] == "supplementary_material"]
    assert reviews
    # frozen version for the historical conclusion is retained
    history = detail["conclusion_history"]
    assert history[0]["state"] == "restricted_confirmation"
    assert history[0]["status"] == "under_review"
    assert history[0]["frozen_versions"]["credential_versions"]

    # new dossiers can no longer be built referencing this row
    r = client.post("/api/v1/entities/ENT-A/resolve", headers=env["headers"])
    assert r.status_code == 201
    diff = client.get("/api/v1/entities/ENT-A/difference-cases", headers=env["headers"]).json()
    assert [d for d in diff if d["kind"] == "audit_hold_new_dossier" and d["ref_id"] == eid]


# ---------------------------------------------------------------------------
# 8. Shared credential change: only rows that lose a valid witness are paused;
#    every remaining affected row is re-judged individually.
# ---------------------------------------------------------------------------


def test_supplement_review_can_confirm_new_chain_and_restore_importability(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="100", scope={"regions": ["CN"], "skus": ["SKU-AA-100"]},
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    assert _current(client, env, case["case_id"])["state"] == "authorization_insufficient"

    # supplementary evidence: a customs authorization signed and effective before export but
    # received late. The current conclusion moves to review and cannot enter new dossiers.
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/supplements",
        json={
            "note": "late customs authorization found in the archive",
            "links": [
                {
                    "role": "customs_document",
                    "node_ref": "CA-LATE-RECEIVED",
                    "valid_from": T_BEFORE.isoformat(),
                    "quantity": "100",
                    "detail": {"kind_hint": "customs_authorization"},
                }
            ],
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    assert _current(client, env, case["case_id"]) is None

    # the reviewer registers the credential itself and approves the new chain
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="120", issued_at=T_BEFORE.isoformat(), received_time=T_RECEIVED_LATE.isoformat(),
        effective_from=T_BEFORE.isoformat(),
    )
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/review",
        json={"action": "approve", "note": "late authorization verified as effective before export"},
        headers=env["headers"],
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "cross_entity_chain_confirmed"
    assert out["review_approved"] is True
    assert out["usable_for_new_dossier"] is True
    hist = client.get(
        f"/api/v1/audit-cases/{case['case_id']}/conclusions", headers=env["headers"]
    ).json()
    assert any(h["state"] == "authorization_insufficient" for h in hist)


def test_supplement_review_rejection_downgrades_to_restricted(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _base_chain(client, env, eid)
    case = _case(client, env, eid)
    assert _current(client, env, case["case_id"])["state"] == "cross_entity_chain_confirmed"
    client.post(
        f"/api/v1/audit-cases/{case['case_id']}/supplements",
        json={
            "links": [
                {
                    "role": "transport",
                    "node_ref": "BOL-VOID-ATTEMPT",
                    "valid_from": T_AFTER.isoformat(),
                    "quantity": "100",
                }
            ]
        },
        headers=env["headers"],
    )
    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/review",
        json={"action": "reject", "note": "transport voucher contradicts the export window"},
        headers=env["headers"],
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "restricted_confirmation"
    assert out["usable_for_new_dossier"] is False
    assert out["quantity_restricted"] in ("100", "100.0000", "100.00")


def test_shared_credential_change_pauses_only_rows_losing_valid_witness(client, audit_env):
    env = audit_env
    eid1, eid2 = env["export_ids"][0], env["export_ids"][1]
    # shared customs authorization covering both SKU-AA-100 and SKU-BB-200
    _credential(
        client, env,
        credential_no="CA-SHARED", kind="customs_authorization",
        grantor="P-INV", grantee="P-CUS", quantity_limit="200",
        scope={"regions": ["CN"], "skus": ["SKU-AA-100", "SKU-BB-200"]},
        effective_from=T_BEFORE.isoformat(),
    )
    for eid in (eid1, eid2):
        for no, grantor, grantee in (
            (f"TT-{eid}", "P-INV", "P-TIT"),
            (f"TT2-{eid}", "P-TIT", "P-CUS"),
        ):
            _credential(
                client, env,
                credential_no=no, kind="title_transfer",
                grantor=grantor, grantee=grantee, quantity_limit="100",
                scope={"regions": ["CN"], "skus": ["SKU-AA-100", "SKU-BB-200"]},
                effective_from=T_BEFORE.isoformat(),
            )
    case1 = _case(client, env, eid1, sku="SKU-AA-100")
    case2 = _case(client, env, eid2, sku="SKU-BB-200")
    assert _current(client, env, case1["case_id"])["state"] == "cross_entity_chain_confirmed"
    assert _current(client, env, case2["case_id"])["state"] == "cross_entity_chain_confirmed"

    creds = client.get("/api/v1/entities/ENT-A/credentials", headers=env["headers"]).json()
    shared = next(c for c in creds if c["credential_no"] == "CA-SHARED")
    # revise scope: only SKU-AA-100 is covered now
    r = client.post(
        f"/api/v1/credentials/{shared['id']}/scope-revision",
        json={
            "scope": {"regions": ["CN"], "skus": ["SKU-AA-100"]},
            "quantity_limit": "120",
            "note": "goods scope narrowed",
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    event = r.json()["shared_change_event"]
    assert sorted(event["affected_case_ids"]) == sorted([case1["case_id"], case2["case_id"]])
    # A scope revision changes the basis of BOTH rows (both cite the same credential), so both are
    # paused for new dossiers — yet each is still rejudged on its own goods, region and date:
    # row 1 (SKU-AA-100) keeps a confirmed state, row 2 (SKU-BB-200) loses coverage.
    rejudged = {e["case_id"]: e for e in event["rejudged"]}
    assert rejudged[case1["case_id"]]["new_state"] == "cross_entity_chain_confirmed"
    assert rejudged[case2["case_id"]]["new_state"] != "cross_entity_chain_confirmed"
    assert sorted(event["paused_case_ids"]) == sorted([case1["case_id"], case2["case_id"]])

    # historical conclusions are each preserved with their original frozen basis
    for case in (case1, case2):
        hist = client.get(
            f"/api/v1/audit-cases/{case['case_id']}/conclusions", headers=env["headers"]
        ).json()
        assert any(h["state"] == "cross_entity_chain_confirmed"
                   and h["frozen_versions"]["credential_versions"]
                   for h in hist), hist
    # row 1 is re-confirmed against the revised scope; row 2 is not importable
    assert _current(client, env, case1["case_id"])["state"] == "cross_entity_chain_confirmed"
    assert _current(client, env, case2["case_id"])["usable_for_new_dossier"] is False


def test_shared_revocation_does_not_pause_a_row_with_an_independent_witness(client, audit_env):
    env = audit_env
    eid1, eid2 = env["export_ids"][0], env["export_ids"][1]
    skus = ["SKU-AA-100", "SKU-BB-200"]
    _credential(
        client, env,
        credential_no="CA-SHARED", kind="customs_authorization",
        grantor="P-INV", grantee="P-CUS", quantity_limit="200",
        scope={"regions": ["CN"], "skus": skus},
        effective_from=T_BEFORE.isoformat(),
    )
    # row 1 also has an independent authorization, so it keeps a valid witness
    _credential(
        client, env,
        credential_no="CA-INDEPENDENT", kind="customs_authorization",
        grantor="P-INV", grantee="P-CUS", quantity_limit="100",
        scope={"regions": ["CN"], "skus": ["SKU-AA-100"]},
        effective_from=T_BEFORE.isoformat(),
    )
    case1 = _case(client, env, eid1, invoice="P-INV", title="P-CUS", customs="P-CUS", sku="SKU-AA-100")
    case2 = _case(client, env, eid2, invoice="P-INV", title="P-CUS", customs="P-CUS", sku="SKU-BB-200")
    assert _current(client, env, case1["case_id"])["state"] == "cross_entity_chain_confirmed"
    assert _current(client, env, case2["case_id"])["state"] == "cross_entity_chain_confirmed"
    creds = client.get("/api/v1/entities/ENT-A/credentials", headers=env["headers"]).json()
    shared = next(c for c in creds if c["credential_no"] == "CA-SHARED")
    r = client.post(
        f"/api/v1/credentials/{shared['id']}/revocations",
        json={
            "revocation_no": "REV-SHARED",
            "issuer_party_code": "P-INV",
            "claimed_effective_from": T_BEFORE.isoformat(),
            "signed_at": T_BEFORE.isoformat(),
            "received_time": T_RECEIVED_LATE.isoformat(),
            "original_location": "s3://audit/REV-SHARED.pdf",
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    event = r.json()["shared_change_event"]
    assert sorted(event["affected_case_ids"]) == sorted([case1["case_id"], case2["case_id"]])
    assert event["paused_case_ids"] == [case2["case_id"]]
    assert event["unchanged_case_ids"] == [case1["case_id"]]
    assert _current(client, env, case1["case_id"])["state"] == "cross_entity_chain_confirmed"
    assert _current(client, env, case2["case_id"])["state"] != "cross_entity_chain_confirmed"
    assert _current(client, env, case2["case_id"])["usable_for_new_dossier"] is False
# ---------------------------------------------------------------------------
# 9. Full drilldown: parties, credential text, times, title/transport evidence,
#    goods/region scope, frozen versions and review history all expandable.
# ---------------------------------------------------------------------------


def test_shared_change_to_out_of_scope_credential_does_not_touch_other_rows(client, audit_env):
    """A credential whose scope excludes a row's goods is not that row's dependency."""
    env = audit_env
    eid1, eid2 = env["export_ids"][0], env["export_ids"][1]
    sku1, sku2 = "SKU-AA-100", "SKU-BB-200"
    case1 = _case(client, env, eid1, invoice="P-INV", title="P-CUS", customs="P-CUS", sku=sku1)
    case2 = _case(client, env, eid2, invoice="P-INV", title="P-CUS", customs="P-CUS", sku=sku2)
    # no credential yet -> authorization insufficient; a scope-excluded credential would only be a
    # pending candidate, never proof of a valid chain.
    assert _current(client, env, case1["case_id"])["state"] == "authorization_insufficient"
    assert _current(client, env, case2["case_id"])["state"] == "authorization_insufficient"
    # an authorization covering only SKU-AA-100: it depends on row 1, not row 2
    auth = _credential(
        client, env,
        credential_no="CA-SKU1-ONLY", kind="customs_authorization",
        grantor="P-INV", grantee="P-CUS", quantity_limit="100",
        scope={"regions": ["CN"], "skus": [sku1]},
        effective_from=T_BEFORE.isoformat(),
    )
    client.post(f"/api/v1/audit-cases/{case1['case_id']}/evaluate", json={}, headers=env["headers"])
    client.post(f"/api/v1/audit-cases/{case2['case_id']}/evaluate", json={}, headers=env["headers"])
    assert _current(client, env, case1["case_id"])["state"] == "cross_entity_chain_confirmed"
    r = client.post(
        f"/api/v1/credentials/{auth['id']}/scope-revision",
        json={"scope": {"regions": ["CN"], "skus": [sku1]}, "quantity_limit": "90"},
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    event = r.json()["shared_change_event"]
    assert event["affected_case_ids"] == [case1["case_id"]]
    assert case2["case_id"] not in event["affected_case_ids"]
    # row 2 never became usable: the SKU-AA-100-only authorization is at best a pending candidate
    # for it, so it is not a dependency whose change has to churn the row.
    row2 = _current(client, env, case2["case_id"])
    assert row2["state"] in ("authorization_insufficient", "pending_evidence")
    assert row2["usable_for_new_dossier"] is False


def test_review_approval_lifts_new_dossier_hold(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-TIT",
        quantity_limit="100", scope={"regions": ["CN"], "skus": ["SKU-AA-100"]},
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-TIT", grantee="P-CUS",
        quantity_limit="120", issued_at=T_BEFORE.isoformat(), received_time=T_RECEIVED_LATE.isoformat(),
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    assert _current(client, env, case["case_id"])["usable_for_new_dossier"] is False
    client.post("/api/v1/entities/ENT-A/resolve", headers=env["headers"])
    holds = client.get("/api/v1/entities/ENT-A/difference-cases", headers=env["headers"]).json()
    assert any(d["kind"] == "audit_hold_new_dossier" and d["ref_id"] == eid for d in holds)

    r = client.post(
        f"/api/v1/audit-cases/{case['case_id']}/review",
        json={"action": "approve", "note": "signed original verified"},
        headers=env["headers"],
    )
    assert r.status_code == 200 and r.json()["usable_for_new_dossier"] is True

    client.post("/api/v1/entities/ENT-A/resolve", headers=env["headers"])
    open_holds = client.get("/api/v1/entities/ENT-A/difference-cases", headers=env["headers"]).json()
    assert not any(d["kind"] == "audit_hold_new_dossier" and d["ref_id"] == eid for d in open_holds)
    resolved = client.get(
        "/api/v1/entities/ENT-A/difference-cases?state=resolved", headers=env["headers"]
    ).json()
    assert any(d["kind"] == "audit_hold_new_dossier" and d["ref_id"] == eid for d in resolved)


def test_conclusion_drilldown_exposes_frozen_basis(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _base_chain(client, env, eid)
    case = _case(client, env, eid)
    detail = client.get(f"/api/v1/audit-cases/{case['case_id']}", headers=env["headers"]).json()
    cur = detail["current_conclusion"]
    assert cur["state"] == "cross_entity_chain_confirmed"
    assert detail["parties"]["invoice"]["uscc"] == "91310000MA1FL00123"
    assert detail["parties"]["customs"]["name_changes"] == []
    steps = cur["witnesses"]["customs_authorization"][0]["steps"]
    assert steps[0]["issued_at"] <= EXPORT_DATE.isoformat()
    assert steps[0]["received_time"] <= EXPORT_DATE.isoformat()
    assert "effective_from" in steps[0]
    assert steps[0]["quantity_limit"] is not None
    frozen = cur["frozen_versions"]
    assert frozen["export_date"]
    assert frozen["party_versions"]["customs"]["uscc"] == "91310000MA1FL00789"
    assert frozen["party_versions"]["customs"]["version"] >= 1
    assert frozen["credential_versions"]
    assert frozen["evidence_versions"]
    assert detail["review_history"]


def test_case_scoping_prevents_cross_entity_reads(client, audit_env, bootstrap):
    env = audit_env
    eid = env["export_ids"][0]
    _base_chain(client, env, eid)
    case = _case(client, env, eid)
    r = client.get(f"/api/v1/audit-cases/{case['case_id']}", headers=bootstrap["outsider"])
    assert r.status_code == 403
    r = client.get("/api/v1/entities/ENT-A/audit-cases", headers=bootstrap["outsider"])
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 10. Party validity evaluated at the export date, not at current status
# ---------------------------------------------------------------------------


def test_party_validity_is_judged_at_export_date(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    # party was dissolved AFTER export — the export date is still valid
    r = client.post(
        "/api/v1/audit/parties",
        json={
            "code": "P-LATE-DISSOLVED",
            "legal_name": "辰星物流（已注销）",
            "uscc": "91310000MA1FL00999",
            "country": "CN",
            "entity_code": "ENT-A",
            "valid_from": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
            "valid_to": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
            "status": "dissolved",
        },
        headers=env["headers"],
    )
    assert r.status_code == 201, r.text
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer", grantor="P-INV", grantee="P-LATE-DISSOLVED",
        quantity_limit="100",
        scope={"global": True},
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization", grantor="P-LATE-DISSOLVED",
        grantee="P-CUS", quantity_limit="150",
        scope={"global": True},
    )
    case = _case(client, env, eid, title="P-LATE-DISSOLVED")
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "cross_entity_chain_confirmed"
    assert cur["reasoning"]["party_checks"] == ["all three parties valid at the export date"]


def test_party_invalid_at_export_is_conflict(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    r = client.post(
        "/api/v1/audit/parties",
        json={
            "code": "P-NOT-YET",
            "legal_name": "辰星新设公司",
            "uscc": "91310000MA1FL00888",
            "country": "CN",
            "entity_code": "ENT-A",
            "valid_from": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
        },
        headers=env["headers"],
    )
    assert r.status_code == 201
    case = _case(client, env, eid, customs="P-NOT-YET")
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "party_conflict"
    assert cur["usable_for_new_dossier"] is False


# ---------------------------------------------------------------------------
# 11. Deterministic no-authorization case
# ---------------------------------------------------------------------------


def test_no_authorization_yields_authorization_insufficient(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "authorization_insufficient"
    assert cur["usable_for_new_dossier"] is False
    assert "no customs/export authorization" in cur["reasoning"]["reached_state"]


def test_truncated_search_yields_pending_not_absence(client, audit_env, monkeypatch):
    """A truncated path search may never assert that no valid authorization exists."""
    env = audit_env
    eid = env["export_ids"][0]
    _base_chain(client, env, eid)
    import app.services.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_DEPTH", 0)
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "pending_evidence"
    assert cur["reasoning"]["path_search"]["truncated"] is True
    assert "truncated" in cur["reasoning"]["reached_state"]
    assert cur["usable_for_new_dossier"] is False


def test_missing_scope_is_pending_not_insufficient(client, audit_env):
    env = audit_env
    eid = env["export_ids"][0]
    _credential(
        client, env,
        credential_no=f"TT-{eid}", kind="title_transfer",
        grantor="P-INV", grantee="P-TIT", quantity_limit="100",
        effective_from=T_BEFORE.isoformat(),
    )
    _credential(
        client, env,
        credential_no=f"CA-{eid}", kind="customs_authorization",
        grantor="P-TIT", grantee="P-CUS", quantity_limit="120",
        scope={"regions": ["US"], "skus": ["SKU-AA-100"]},  # wrong region
        effective_from=T_BEFORE.isoformat(),
    )
    case = _case(client, env, eid)
    cur = _current(client, env, case["case_id"])
    assert cur["state"] == "pending_evidence"
    cand = cur["reasoning"]["pending_candidates"]
    assert any("scope does not cover" in c["reason"] for c in cand)
