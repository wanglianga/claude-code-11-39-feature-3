from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOC_TYPES = Literal[
    "invoice",
    "packing_list",
    "customs_archive",
    "customs_correction",
    "tax_receipt",
    "bank_statement",
    "contract",
    "entity_directory",
    "return_document",
    "other",
]

DECLARATION_EVENTS = Literal["submitted", "verified", "rejected", "draft_recorded"]


def _money(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))


def _qty(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.0001"))


class IntakeLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int | None = None
    sku: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None
    # invoice / contract linkage
    invoice_no: str | None = None
    invoice_line_no: int | None = None
    contract_no: str | None = None
    buyer_name: str | None = None
    # customs
    hs_code: str | None = None
    declaration_no: str | None = None
    export_date: datetime | None = None
    revises_line_no: int | None = None
    # tax receipt
    batch_no: str | None = None
    review_status: Literal["approved", "rejected", "pending", "corrected"] | None = None
    issue_time: datetime | None = None
    sequence_no: int | None = None
    # bank statement
    txn_ref: str | None = None
    payer_name: str | None = None
    value_date: datetime | None = None
    remittance_info: str | None = None
    # entity directory / contract
    code: str | None = None
    name: str | None = None
    country: str | None = None
    effective_date: datetime | None = None
    # return document
    transport_shipped_date: datetime | None = None
    transport_returned_date: datetime | None = None
    # packing list
    packages: int | None = None
    net_weight: Decimal | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    _money = field_validator("amount", mode="before")(_money)
    _qty = field_validator("quantity", "net_weight", mode="before")(_qty)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type: DOC_TYPES
    entity_code: str | None = None
    external_no: str | None = None
    original_location: str
    event_time: datetime | None = None
    received_time: datetime | None = None
    currency: str | None = None
    amount: Decimal | None = None
    quantity: Decimal | None = None
    sha256: str | None = None
    source_version: int = 1
    supersedes_document_id: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    lines: list[IntakeLine] = Field(default_factory=list)

    _money = field_validator("amount", mode="before")(_money)
    _qty = field_validator("quantity", mode="before")(_qty)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class DeclarationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: DECLARATION_EVENTS
    at: datetime | None = None
    reference: str | None = None
    note: str | None = None
    source_document_id: int | None = None


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: int | None = None
    bank_transaction_id: int | None = None
    export_line_id: int | None = None
    amount: Decimal | None = None
    note: str | None = None

    _money = field_validator("amount", mode="before")(_money)


class VoidRequest(BaseModel):
    reason: str


class ReassignRequest(BaseModel):
    target_export_line_id: int
    reason: str


class CompileRequest(BaseModel):
    entity_code: str | None = None


class UserCreateRequest(BaseModel):
    name: str
    role: Literal["admin", "analyst", "viewer"]
    api_key: str
    entity_codes: list[str] = Field(default_factory=list)


class EntityCreateRequest(BaseModel):
    code: str
    name: str
    country: str | None = None


# ---------------------------------------------------------------------------
# Cross-entity evidence chain / authorization timeliness audit
# ---------------------------------------------------------------------------

CREDENTIAL_KINDS = Literal[
    "entrusted_export",
    "customs_authorization",
    "title_transfer",
    "intragroup_agreement",
    "other",
]

PARTY_STATUSES = Literal["active", "suspended", "dissolved"]


class PartyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    legal_name: str
    uscc: str | None = None
    country: str | None = None
    is_branch: bool = False
    head_office_code: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: PARTY_STATUSES = "active"
    entity_code: str | None = None
    source_document_id: int | None = None
    original_location: str | None = None
    sha256: str | None = None


class PartyNameChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_name: str
    new_name: str
    effective_from: datetime
    uscc_verified: bool = False
    inheritance_doc_no: str | None = None
    inheritance_location: str | None = None
    inheritance_sha256: str | None = None
    source_document_id: int | None = None


class PartyControlRelationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_party_code: str
    relation_type: Literal["control", "branch"] = "control"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_document_id: int | None = None


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_code: str
    credential_no: str
    kind: CREDENTIAL_KINDS
    grantor_party_code: str
    grantee_party_code: str
    issued_at: datetime
    received_time: datetime
    effective_from: datetime
    effective_to: datetime | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    quantity_limit: Decimal | None = None
    currency: str | None = None
    original_location: str
    sha256: str | None = None
    supersedes_credential_no: str | None = None
    intragroup_acts_as: Literal["title_holder", "declarant"] | None = None
    retroactive: bool = False
    retroactive_scope_verified: bool = False
    retroactive_from: datetime | None = None
    source_document_id: int | None = None

    _qty = field_validator("quantity_limit", mode="before")(_qty)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class CredentialRevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revocation_no: str
    issuer_party_code: str
    sequence_no: int | None = None
    claimed_effective_from: datetime
    signed_at: datetime
    received_time: datetime
    original_location: str
    sha256: str | None = None
    source_document_id: int | None = None


class CredentialScopeRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: dict[str, Any] = Field(default_factory=dict)
    quantity_limit: Decimal | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    note: str | None = None

    _qty = field_validator("quantity_limit", mode="before")(_qty)


class CredentialReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_no: str
    issued_at: datetime
    received_time: datetime
    effective_from: datetime
    effective_to: datetime | None = None
    scope: dict[str, Any] | None = None
    quantity_limit: Decimal | None = None
    currency: str | None = None
    original_location: str
    sha256: str | None = None
    source_document_id: int | None = None

    _qty = field_validator("quantity_limit", mode="before")(_qty)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class AuditCaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export_line_id: int | None = None
    invoice_line_id: int | None = None
    customs_line_id: int | None = None
    invoice_party_code: str
    title_party_code: str
    customs_party_code: str
    region: str
    product_sku: str | None = None
    hs_code: str | None = None
    recorded_names: dict[str, str] = Field(default_factory=dict)
    note: str | None = None


class AuditEvidenceLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["contract", "packing", "transport", "title_document", "invoice_document", "customs_document", "other"]
    node_ref: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    quantity: Decimal | None = None
    source_document_id: int | None = None
    original_location: str | None = None
    sha256: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    _qty = field_validator("quantity", mode="before")(_qty)


class AuditEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None
    allow_truncated_search: bool = False


class AuditSupplementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None
    links: list[AuditEvidenceLinkRequest] = Field(default_factory=list)


class AuditReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    note: str | None = None
