"""Schema-true goAML report XML rendering + REAL-XSD validation (compliance side).

:mod:`bdpay.compliance.goaml` owns the PII-free payload dicts handed to the
``goaml_reporter_v1`` connector. This module owns the next certainty level:
rendering those payloads into goAML report XML that validates against the
**official goAML XSD artifact** pinned under
``tests/fixtures/official/goaml/`` (provenance in that directory's
SOURCES.md), and the typed failure modes for everything that can go wrong.

Boundary discipline (spec/06 + spec/12 §F):

- The payload dict stays PII-free. Clear values (names, narrative text,
  party blocks) enter ONLY here, supplied by the caller as a *render
  context* resolved from the object-store pointers at render time.
- Money crosses paisa -> decimal-string at this render boundary only, via
  integer string arithmetic (no floats anywhere; floats in context are
  refused).
- ``submission_date`` is sourced from an injected-clock value (the STR
  payload's ``filing_at``; for CTR the context's ``filing_at`` — spec/12
  binds submission_date to the injectable clock, and the spec/06 CTR
  payload carries no timestamp; see SPEC_ERRATA-LANE-B).
- Validation error messages carry element paths only — never element
  values (a value could be a name; spec/00 §4 messages are PII-free).

The XSD is registry config (spec/12 ``goaml_xsd_ref``): every entry point
takes an explicit ``xsd_path``. Nothing here hardcodes a fixture location;
the certification tests pass the stored official artifact. A missing or
unloadable XSD fails CLOSED (:class:`GoamlSchemaUnavailableError`) — a
filing payload is never declared schema-valid by default.

The official FIU-SL artifact carries 23 ``xs:pattern`` facets written in
.NET regex lookahead syntax that is not legal W3C XML Schema regex; the
loader builds in ``validation='lax'`` mode, which skips exactly those
facets and enforces all structure, ordering, required elements,
enumerations and value types (pinned by tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from bdpay.compliance.goaml import validate_ctr_payload, validate_str_payload
from bdpay.platform.errors import InternalError, InvalidRequestError

__all__ = [
    "CERTIFICATION_PROFILE",
    "FUNDS_CODE_BY_METHOD",
    "GoamlProfile",
    "GoamlRenderContextError",
    "GoamlSchemaUnavailableError",
    "GoamlSchemaViolationError",
    "TRANSMODE_BY_METHOD",
    "load_goaml_schema",
    "paisa_to_decimal_text",
    "render_ctr_xml",
    "render_str_xml",
    "validate_goaml_xml",
]


class GoamlSchemaUnavailableError(InternalError):
    """The configured goAML XSD artifact is missing or unloadable (fail-closed)."""

    default_code = "goaml_schema_unavailable"


class GoamlRenderContextError(InvalidRequestError):
    """The render context is missing, inconsistent with the payload, or malformed."""

    default_code = "goaml_render_context_invalid"


class GoamlSchemaViolationError(InvalidRequestError):
    """Rendered goAML XML failed validation against the official XSD."""

    default_code = "goaml_schema_violation"


# ---------------------------------------------------------------------------
# Profile (per-FIU lookup codes — registry config, BD profile awaited)
# ---------------------------------------------------------------------------

#: Method -> goAML ``transmode_code`` against the PINNED official 4.x
#: enumeration. spec/12 §F's illustrative codes (MO/CT/CC) exist in no public
#: official goAML XSD enumeration — the schema wins (SPEC_ERRATA-LANE-B).
TRANSMODE_BY_METHOD: dict[str, str] = {
    "BKASH": "MBWL",
    "NAGAD": "MBWL",
    "ROCKET": "MBWL",
    "UPAY": "MBWL",
    "BANGLA_QR": "MOAP",
    "NPSB_IBFT": "INTR",
    "BEFTN_CREDIT": "INTR",
    "RTGS": "INTR",
    "CARD": "IPG",
    "CASH": "AGNT",
    "CASH_AGENT": "AGNT",
    "CASH_BRANCH": "BRCH",
    "ATM": "ATM",
}

#: Method -> goAML ``funds_type`` code (same pinned-enumeration rule).
FUNDS_CODE_BY_METHOD: dict[str, str] = {
    "BKASH": "MOBL",
    "NAGAD": "MOBL",
    "ROCKET": "MOBL",
    "UPAY": "MOBL",
    "BANGLA_QR": "MOBL",
    "NPSB_IBFT": "ELEC",
    "BEFTN_CREDIT": "ELEC",
    "RTGS": "ELEC",
    "CARD": "CDEB",
    "CASH": "CASH",
    "CASH_AGENT": "CASH",
    "CASH_BRANCH": "CASH",
    "ATM": "CASH",
}


@dataclass(frozen=True)
class GoamlProfile:
    """FIU-profile lookup codes (hot-swappable config, spec/12 ``goaml_xsd_ref``).

    ``rentity_id`` is the FIU-assigned NUMERIC reporting-entity id — the real
    schema types it ``xs:int`` (spec/12's example "BFIU-RE-0000" cannot
    validate; SPEC_ERRATA-LANE-B).
    """

    profile_ref: str
    rentity_id: int
    currency_code: str = "BDT"
    str_indicators: tuple[str, ...] = ("OTH",)
    ctr_indicators: tuple[str, ...] = ("CTR",)
    transmode_by_method: dict[str, str] = field(default_factory=lambda: dict(TRANSMODE_BY_METHOD))
    funds_code_by_method: dict[str, str] = field(
        default_factory=lambda: dict(FUNDS_CODE_BY_METHOD)
    )

    def __post_init__(self) -> None:
        if isinstance(self.rentity_id, bool) or not isinstance(self.rentity_id, int):
            raise GoamlRenderContextError("profile rentity_id must be an integer")
        if self.rentity_id < 1:
            raise GoamlRenderContextError("profile rentity_id must be >= 1 (xs:int, FIU-assigned)")


#: Default profile valid against the pinned official 4.x artifact. The
#: rentity_id uses the fixture value 35 (within the artifact's 1..999 constraint) —
#: production value is BFIU-assigned config (OpenBao), never hardcoded.
CERTIFICATION_PROFILE = GoamlProfile(profile_ref="goaml_4x_official_pin", rentity_id=35)


# ---------------------------------------------------------------------------
# Scalar render helpers (no floats, no wall clock)
# ---------------------------------------------------------------------------


def paisa_to_decimal_text(amount_minor: int) -> str:
    """Integer paisa -> goAML ``xs:decimal`` text. Render boundary ONLY."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise GoamlRenderContextError("amounts must be integer paisa (floats are rejected)")
    if amount_minor < 0:
        raise GoamlRenderContextError("amounts must be >= 0 (xs:decimal minInclusive 0)")
    return f"{amount_minor // 100}.{amount_minor % 100:02d}"


def _sql_datetime(value: Any, where: str) -> str:
    """Render a tz-aware datetime (or ISO string / date) as goAML ``sql_date``.

    Emitted as UTC-naive ``YYYY-MM-DDTHH:MM:SS`` — the form every goAML
    profile's ``xs:dateTime``-based ``sql_date`` accepts deterministically
    (offset forms are only partially ordered against the type's untimezoned
    ``minInclusive`` facet).
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise GoamlRenderContextError(f"{where} is not an ISO-8601 timestamp") from exc
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise GoamlRenderContextError(f"{where} must be timezone-aware (conventions spec/00)")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00"
    raise GoamlRenderContextError(f"{where} must be a datetime, date, or ISO-8601 string")


def _scalar_text(value: Any, where: str) -> str:
    if isinstance(value, float):
        raise GoamlRenderContextError(f"{where} is a float (money is integer paisa; no floats)")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return _sql_datetime(value, where)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value:
            raise GoamlRenderContextError(f"{where} is empty")
        return value
    raise GoamlRenderContextError(f"{where} has unsupported type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Party block rendering (field order = the schema's declared sequences)
# ---------------------------------------------------------------------------

_PERSON_FIELDS = (
    "gender",
    "title",
    "first_name",
    "middle_name",
    "prefix",
    "last_name",
    "birthdate",
    "birth_place",
    "mothers_name",
    "alias",
    "ssn",
    "passport_number",
    "passport_country",
    "id_number",
    "nationality1",
    "nationality2",
    "nationality3",
    "residence",
    "addresses",
    "email",
    "occupation",
    "employer_name",
    "tax_number",
    "source_of_wealth",
    "comments",
)

_ENTITY_FIELDS = (
    "name",
    "commercial_name",
    "incorporation_legal_form",
    "incorporation_number",
    "business",
    "addresses",
    "email",
    "url",
    "incorporation_state",
    "incorporation_country_code",
    "director_id",
    "incorporation_date",
    "tax_number",
    "tax_reg_number",
    "comments",
)

_ACCOUNT_FIELDS = (
    "institution_name",
    "institution_code",
    "swift",
    "non_bank_institution",
    "branch",
    "account",
    "currency_code",
    "account_name",
    "iban",
    "client_number",
    "personal_account_type",
    "opened",
    "closed",
    "status_code",
    "comments",
)

_ADDRESS_FIELDS = (
    "address_type",
    "address",
    "town",
    "city",
    "zip",
    "country_code",
    "state",
    "comments",
)

_FIELDS_BY_KIND = {"person": _PERSON_FIELDS, "entity": _ENTITY_FIELDS, "account": _ACCOUNT_FIELDS}
_DATE_FIELDS = frozenset({"birthdate", "incorporation_date", "opened", "closed"})


def _render_addresses(parent: Element, addresses: Any, where: str) -> None:
    if not isinstance(addresses, list | tuple) or not addresses:
        raise GoamlRenderContextError(f"{where}.addresses must be a non-empty list")
    wrapper = SubElement(parent, "addresses")
    for index, address in enumerate(addresses):
        if not isinstance(address, dict):
            raise GoamlRenderContextError(f"{where}.addresses[{index}] must be a dict")
        unknown = set(address) - set(_ADDRESS_FIELDS)
        if unknown:
            raise GoamlRenderContextError(
                f"{where}.addresses[{index}] has unknown fields {sorted(unknown)}"
            )
        node = SubElement(wrapper, "address")
        for fieldname in _ADDRESS_FIELDS:
            if fieldname in address:
                SubElement(node, fieldname).text = _scalar_text(
                    address[fieldname], f"{where}.addresses[{index}].{fieldname}"
                )


def _render_fields(node: Element, fields: dict, ordered: tuple[str, ...], where: str) -> None:
    unknown = set(fields) - set(ordered)
    if unknown:
        raise GoamlRenderContextError(f"{where} has unknown fields {sorted(unknown)}")
    for fieldname in ordered:
        if fieldname not in fields:
            continue
        if fieldname == "addresses":
            _render_addresses(node, fields[fieldname], where)
        elif fieldname == "director_id":
            _render_directors(node, fields[fieldname], where)
        elif fieldname in _DATE_FIELDS:
            SubElement(node, fieldname).text = _sql_datetime(
                fields[fieldname], f"{where}.{fieldname}"
            )
        else:
            SubElement(node, fieldname).text = _scalar_text(
                fields[fieldname], f"{where}.{fieldname}"
            )


def _render_directors(parent: Element, directors: Any, where: str) -> None:
    """``director_id`` is a t_person extension plus a ``role`` code."""
    if not isinstance(directors, list | tuple) or not directors:
        raise GoamlRenderContextError(f"{where}.director_id must be a non-empty list")
    for index, director in enumerate(directors):
        if not isinstance(director, dict) or "role" not in director:
            raise GoamlRenderContextError(
                f"{where}.director_id[{index}] must be a dict of person fields plus role"
            )
        person_fields = {key: value for key, value in director.items() if key != "role"}
        node = SubElement(parent, "director_id")
        _render_fields(node, person_fields, _PERSON_FIELDS, f"{where}.director_id[{index}]")
        SubElement(node, "role").text = _scalar_text(
            director["role"], f"{where}.director_id[{index}].role"
        )


def _render_party_block(parent: Element, tag: str, block: dict, where: str) -> None:
    """Render one person/entity/account block under ``parent`` as ``tag``."""
    kind = block.get("kind")
    if kind not in _FIELDS_BY_KIND:
        raise GoamlRenderContextError(f"{where}.kind must be person, entity, or account")
    fields = block.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise GoamlRenderContextError(f"{where}.fields must be a non-empty dict")
    node = SubElement(parent, tag)
    _render_fields(node, fields, _FIELDS_BY_KIND[kind], f"{where}.fields")


def _render_leg(
    parent: Element, direction: str, party: Any, method: str, profile: GoamlProfile, where: str
) -> None:
    """Render ``t_from[_my_client]`` / ``t_to[_my_client]`` for one transaction."""
    if not isinstance(party, dict):
        raise GoamlRenderContextError(f"{where} must be a dict party block")
    my_client = party.get("my_client")
    if not isinstance(my_client, bool):
        raise GoamlRenderContextError(f"{where}.my_client must be a bool")
    funds_code = party.get("funds_code") or profile.funds_code_by_method.get(method)
    if not funds_code:
        raise GoamlRenderContextError(f"{where}: no funds_code and method {method!r} unmapped")
    country = party.get("country")
    if not isinstance(country, str) or len(country) != 2:
        raise GoamlRenderContextError(f"{where}.country must be an ISO-3166 alpha-2 code")
    kind = party.get("kind")
    if kind not in _FIELDS_BY_KIND:
        raise GoamlRenderContextError(f"{where}.kind must be person, entity, or account")
    wrapper_tag = f"t_{direction}_my_client" if my_client else f"t_{direction}"
    wrapper = SubElement(parent, wrapper_tag)
    SubElement(wrapper, f"{direction}_funds_code").text = funds_code
    _render_party_block(wrapper, f"{direction}_{kind}", party, where)
    SubElement(wrapper, f"{direction}_country").text = country


def _render_transaction(
    parent: Element, txn: dict, profile: GoamlProfile, where: str
) -> tuple[str, int]:
    """Render one ``transaction`` element; returns (transaction_id, amount_minor)."""
    if not isinstance(txn, dict):
        raise GoamlRenderContextError(f"{where} must be a dict")
    txn_id = txn.get("transaction_id")
    if not isinstance(txn_id, str) or not txn_id:
        raise GoamlRenderContextError(f"{where}.transaction_id must be a non-empty string")
    if len(txn_id) > 50:
        raise GoamlRenderContextError(
            f"{where}.transaction_id exceeds the schema's 50-char transactionnumber limit"
        )
    description = txn.get("description")
    if not isinstance(description, str) or not description:
        raise GoamlRenderContextError(
            f"{where}.description is required (schema transaction_description is mandatory)"
        )
    method = txn.get("method")
    if not isinstance(method, str) or not method:
        raise GoamlRenderContextError(f"{where}.method must be a non-empty string")
    transmode = txn.get("transmode_code") or profile.transmode_by_method.get(method)
    if not transmode:
        raise GoamlRenderContextError(
            f"{where}: method {method!r} has no transmode_code mapping in the profile"
        )
    amount_minor = txn.get("amount_minor")
    amount_text = paisa_to_decimal_text(amount_minor)

    node = SubElement(parent, "transaction")
    SubElement(node, "transactionnumber").text = txn_id
    SubElement(node, "transaction_description").text = description
    SubElement(node, "date_transaction").text = _sql_datetime(
        txn.get("occurred_at"), f"{where}.occurred_at"
    )
    SubElement(node, "transmode_code").text = transmode
    SubElement(node, "amount_local").text = amount_text
    _render_leg(node, "from", txn.get("from_party"), method, profile, f"{where}.from_party")
    _render_leg(node, "to", txn.get("to_party"), method, profile, f"{where}.to_party")
    return txn_id, amount_minor


def _render_report_head(
    report: Element, *, profile: GoamlProfile, report_code: str, entity_reference: str,
    submission_at: Any, reporting_person: Any, reason: str | None,
) -> None:
    SubElement(report, "rentity_id").text = str(profile.rentity_id)
    SubElement(report, "submission_code").text = "E"
    SubElement(report, "report_code").text = report_code
    SubElement(report, "entity_reference").text = entity_reference
    SubElement(report, "submission_date").text = _sql_datetime(submission_at, "submission_date")
    SubElement(report, "currency_code_local").text = profile.currency_code
    if reporting_person is not None:
        if not isinstance(reporting_person, dict):
            raise GoamlRenderContextError("reporting_person must be a dict person block")
        _render_party_block(
            report,
            "reporting_person",
            {"kind": "person", "fields": reporting_person},
            "reporting_person",
        )
    if reason is not None:
        SubElement(report, "reason").text = reason


def _render_indicators(report: Element, indicators: tuple[str, ...]) -> None:
    if not indicators:
        raise GoamlRenderContextError(
            "profile must define at least one report indicator (schema report_indicators"
            " is mandatory; codes are FIU-profile lookups)"
        )
    wrapper = SubElement(report, "report_indicators")
    for code in indicators:
        SubElement(wrapper, "indicator").text = _scalar_text(code, "report_indicators.indicator")


# ---------------------------------------------------------------------------
# STR / CTR renderers
# ---------------------------------------------------------------------------

_SUBJECT_KIND = {"INDIVIDUAL": "person", "ENTITY": "entity"}


def render_str_xml(
    payload: dict, context: dict, *, profile: GoamlProfile = CERTIFICATION_PROFILE
) -> bytes:
    """Render a validated STR payload + render context to goAML report XML.

    The context carries the clear values the PII-free payload only points
    at: ``narrative`` (resolved from ``suspicion_narrative_pointer``),
    ``transactions`` (one entry per payload transaction id, each with both
    party legs), optional ``reporting_person`` (resolved CAMLCO identity).
    """
    validate_str_payload(payload)
    if not isinstance(context, dict):
        raise GoamlRenderContextError("render context must be a dict")
    narrative = context.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        raise GoamlRenderContextError(
            "context.narrative (resolved suspicion narrative) is required for an STR"
        )
    txns = context.get("transactions")
    if not isinstance(txns, list) or not txns:
        raise GoamlRenderContextError(
            "an STR renders as a transaction report and requires >= 1 transaction;"
            " activity-only STRs are not renderable in v1 (SPEC_ERRATA-LANE-B)"
        )
    report = Element("report")
    _render_report_head(
        report,
        profile=profile,
        report_code="STR",
        entity_reference=payload["report_ref"],
        submission_at=payload["filing_at"],
        reporting_person=context.get("reporting_person"),
        reason=narrative,
    )
    rendered_ids: list[str] = []
    subject_kind = _SUBJECT_KIND[payload["subject_type"]]
    subject_seen = False
    for index, txn in enumerate(txns):
        txn_id, _ = _render_transaction(report, txn, profile, f"transactions[{index}]")
        rendered_ids.append(txn_id)
        for leg in ("from_party", "to_party"):
            party = txn.get(leg) or {}
            if party.get("is_subject"):
                if party.get("kind") != subject_kind:
                    raise GoamlRenderContextError(
                        f"transactions[{index}].{leg} is marked is_subject but its kind does"
                        f" not match payload subject_type {payload['subject_type']}"
                    )
                subject_seen = True
    if sorted(rendered_ids) != sorted(payload["transaction_ids"]):
        raise GoamlRenderContextError(
            "context transactions must cover exactly the payload transaction_ids"
        )
    if not subject_seen:
        raise GoamlRenderContextError(
            "no context party is marked is_subject matching the payload subject_type"
        )
    _render_indicators(report, profile.str_indicators)
    return tostring(report, encoding="utf-8", xml_declaration=True)


def render_ctr_xml(
    payload: dict, context: dict, *, profile: GoamlProfile = CERTIFICATION_PROFILE
) -> bytes:
    """Render a validated CTR payload + render context to goAML report XML.

    The real goAML schema has NO aggregate-total fields: a CTR is a report
    listing each contributing cash transaction (SPEC_ERRATA-LANE-B). The
    payload's aggregates stay the cross-check: transaction count and the
    per-direction paisa sums must reconcile exactly or rendering refuses.
    ``context.filing_at`` is the injected-clock submission timestamp
    (the spec/06 CTR payload carries none).
    """
    validate_ctr_payload(payload)
    if not isinstance(context, dict):
        raise GoamlRenderContextError("render context must be a dict")
    if "filing_at" not in context:
        raise GoamlRenderContextError(
            "context.filing_at (injected-clock submission timestamp) is required for a CTR"
        )
    txns = context.get("transactions")
    if not isinstance(txns, list) or not txns:
        raise GoamlRenderContextError("a CTR requires the contributing cash transactions")
    report = Element("report")
    _render_report_head(
        report,
        profile=profile,
        report_code="CTR",
        entity_reference=payload["report_ref"],
        submission_at=context["filing_at"],
        reporting_person=context.get("reporting_person"),
        reason=None,
    )
    aggregation_date = date.fromisoformat(payload["aggregation_date"])
    sums = {"cash_in": 0, "cash_out": 0}
    for index, txn in enumerate(txns):
        where = f"transactions[{index}]"
        direction = txn.get("direction") if isinstance(txn, dict) else None
        if direction not in sums:
            raise GoamlRenderContextError(f"{where}.direction must be cash_in or cash_out")
        _, amount_minor = _render_transaction(report, txn, profile, where)
        occurred_at = txn["occurred_at"]
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)
        if isinstance(occurred_at, datetime):
            if occurred_at.tzinfo is None:
                raise GoamlRenderContextError(f"{where}.occurred_at must be timezone-aware")
            occurred_on = occurred_at.astimezone(UTC).date()
        else:
            occurred_on = occurred_at
        if occurred_on != aggregation_date:
            raise GoamlRenderContextError(
                f"{where} occurred outside the payload aggregation_date (daily aggregation)"
            )
        sums[direction] += amount_minor
    if len(txns) != payload["contributing_txn_count"]:
        raise GoamlRenderContextError(
            "context transaction count does not equal payload contributing_txn_count"
        )
    if sums["cash_in"] != payload["total_cash_in_minor"]:
        raise GoamlRenderContextError(
            "context cash-in paisa sum does not equal payload total_cash_in_minor"
        )
    if sums["cash_out"] != payload["total_cash_out_minor"]:
        raise GoamlRenderContextError(
            "context cash-out paisa sum does not equal payload total_cash_out_minor"
        )
    _render_indicators(report, profile.ctr_indicators)
    return tostring(report, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Official-XSD validation (xmlschema; XSD is registry config)
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[str, Any] = {}


def load_goaml_schema(xsd_path: str | Path) -> Any:
    """Load (and cache) the official goAML XSD. Missing/unloadable = fail-closed."""
    import xmlschema

    resolved = str(Path(xsd_path).resolve())
    cached = _SCHEMA_CACHE.get(resolved)
    if cached is not None:
        return cached
    if not Path(resolved).is_file():
        raise GoamlSchemaUnavailableError(
            "configured goAML XSD artifact not found; filings cannot be schema-checked"
        )
    try:
        # validation='lax': skips exactly the official artifact's
        # non-W3C-regex pattern facets; everything else stays enforced.
        schema = xmlschema.XMLSchema10(resolved, validation="lax")
    except Exception as exc:
        raise GoamlSchemaUnavailableError(
            "configured goAML XSD artifact failed to load as XML Schema 1.0"
        ) from exc
    _SCHEMA_CACHE[resolved] = schema
    return schema


def validate_goaml_xml(xml_bytes: bytes, *, xsd_path: str | Path) -> None:
    """Validate rendered report XML against the official XSD; typed refusal.

    Raises :class:`GoamlSchemaViolationError` carrying element PATHS only
    (values may be PII; spec/00 §4 messages are PII-free).
    """
    schema = load_goaml_schema(xsd_path)
    if not isinstance(xml_bytes, bytes):
        raise GoamlSchemaViolationError("rendered report must be bytes")
    try:
        document = xml_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GoamlSchemaViolationError("rendered report is not valid UTF-8") from exc
    errors = list(schema.iter_errors(document))
    if errors:
        paths = sorted({error.path or "/" for error in errors})
        raise GoamlSchemaViolationError(
            f"goAML XML failed official-XSD validation at {len(errors)} point(s);"
            f" element paths: {', '.join(paths[:10])}"
        )
