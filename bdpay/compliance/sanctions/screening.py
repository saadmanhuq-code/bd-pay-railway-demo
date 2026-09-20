"""SanctionsScreener — list versioning, per-screen audit, born-frozen hit FSM.

spec/07 semantics over the ported dataroom matcher:

- **List versioning:** every ingestion creates an immutable
  ``SanctionsListVersion`` + entries with normalized names precomputed; the
  EXACT versions used are recorded on every screening row, making every
  historical screen reproducible.
- **Fail-closed:** an empty active list REFUSES to screen (raises — the
  dataroom anti-false-cleared guard); screener unavailability is a decline
  signal, never an allow.
- **Decision thresholds (thresholds artifact):** ``score >= T_hit`` or an
  identifier-exact match => HIT (born frozen); ``T_review <= score < T_hit``
  => POTENTIAL_MATCH (onboarding holds, payment declines fail-closed);
  below => CLEAR.
- **Born-frozen rule (ATA 2009):** there is NO unfrozen pre-state. The freeze
  call happens BEFORE any hit row persists; if it raises, the whole operation
  aborts — a screened-hit-but-unfrozen state cannot exist.
- **Determinism:** matches order by (score DESC, list_priority ASC, entry_id
  ASC); the ordered match array is canonical-hashed into ``result_hash``;
  scores are Decimal (floats are banned).
- **Whitelist:** anchored to ``entry_content_hash`` — any change to the
  underlying entry invalidates the suppression automatically. Suppression
  applies only at hit creation; the match is still recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.ids_ext import make_compliance_id
from bdpay.compliance.sanctions.matcher import match_score, normalize_name
from bdpay.compliance.sanctions.translit import translit_version
from bdpay.compliance.thresholds import ComplianceThresholds
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
    SanctionsBlockError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "ALGORITHM_VERSION",
    "LIST_PRIORITY",
    "InMemorySanctionsStore",
    "ListEntryInput",
    "SanctionsHit",
    "SanctionsListEntry",
    "SanctionsListVersion",
    "SanctionsScreener",
    "SanctionsStore",
    "ScreeningRecord",
    "WhitelistRow",
]

PRODUCER = "sanctions-screener@1.0.0"

# match_algo_v2 (COMP-04 / errata E45, revised): scoring is the fail-closed
# max(jaro_winkler, token_sort_ratio, token_set_ratio) and normalization gained
# spec/07 §3 equivalence folding. Bumped from match_algo_v1 so historical v1
# screenings keep their original result_hash and are never silently re-derived
# under a changed algorithm (spec/07 §7 replay contract). Per spec/07
# §Re-screen triggers, an algorithm version change forces a FULL_BOOK_FULL_LIST
# rescreen on activation.
ALGORITHM_VERSION = "match_algo_v2"

#: spec/07 deterministic tie-break priorities.
LIST_PRIORITY: dict[str, int] = {"UN_1267": 1, "UN_1373": 2, "BFIU_DOMESTIC": 3}

_HIT_STATES = ("DETECTED_FROZEN", "UNDER_REVIEW", "CONFIRMED", "CLEARED_FALSE_POSITIVE")
_CONTEXTS = ("ONBOARDING", "PAYMENT", "RESCREEN", "MANUAL", "PERIODIC")
_SUBJECT_TYPES = ("Customer", "Merchant", "Participant", "UBO", "EXTERNAL_PARTY")
_FOUR_DP = Decimal("0.0001")
_WHITELIST_MAX_MONTHS = 24


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SanctionsListVersion:
    """One sanctions_list_versions row (immutable, append-only)."""

    list_version_id: str
    list_code: str
    source_artifact_sha256: str
    source_artifact_pointer: str
    entry_count: int
    status: str  # ACTIVE | SUPERSEDED | REJECTED
    ingested_by: str
    ingested_at: datetime
    added_count: int = 0
    changed_count: int = 0
    removed_count: int = 0
    connector_result_id: str | None = None
    superseded_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class SanctionsListEntry:
    """One sanctions_list_entries row (normalized at ingestion)."""

    entry_id: str
    list_version_id: str
    list_code: str
    entry_reference: str
    entry_content_hash: str
    entity_kind: str  # INDIVIDUAL | ENTITY | VESSEL | AIRCRAFT
    primary_name: str
    primary_name_norm: str
    dossier_pointer: str
    aliases: tuple[str, ...] = ()
    aliases_norm: tuple[str, ...] = ()
    dob: date | None = None
    nationalities: tuple[str, ...] = ()
    identifiers: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]
    designated_at: date | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.identifiers is None:
            object.__setattr__(self, "identifiers", {})

    def all_norms(self) -> tuple[str, ...]:
        return (self.primary_name_norm, *self.aliases_norm)


@dataclass(frozen=True)
class ListEntryInput:
    """Raw entry as supplied by the feed connector / manual upload."""

    entry_reference: str
    entity_kind: str
    primary_name: str
    aliases: tuple[str, ...] = ()
    dob: date | None = None
    nationalities: tuple[str, ...] = ()
    identifiers: Mapping[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class ScreeningRecord:
    """One sanctions_screenings row — EVERY screen is recorded, incl. CLEAR."""

    screening_id: str
    subject_type: str
    subject_id: str | None
    context: str
    names_screened: tuple[str, ...]
    names_norm: tuple[str, ...]
    identifiers_hash: str
    list_version_ids: tuple[str, ...]
    algorithm_version: str
    translit_version: str
    thresholds_version: str
    decision: str  # CLEAR | POTENTIAL_MATCH | HIT
    match_count: int
    result_hash: str
    result_pointer: str
    duration_ms: int
    screened_at: datetime
    top_score: Decimal | None = None
    payment_intent_id: str | None = None
    sanctions_hit_id: str | None = None
    rescreen_run_id: str | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class SanctionsHit:
    """One sanctions_hits row. Born in DETECTED_FROZEN — never unfrozen-new."""

    sanc_id: str
    screening_id: str
    entry_id: str
    entry_content_hash: str
    list_code: str
    list_version_id: str
    subject_type: str
    subject_id: str | None
    status: str
    score: Decimal
    matched_alias: str
    detected_at: datetime
    payment_intent_id: str | None = None
    freeze_applied: bool = True
    freeze_released_at: datetime | None = None
    aml_alert_id: str | None = None
    str_report_id: str | None = None
    approval_request_id: str | None = None
    reviewed_by: str | None = None
    confirmed_by: str | None = None
    rationale_pointer: str | None = None
    review_started_at: datetime | None = None
    resolved_at: datetime | None = None
    escalation_count: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in _HIT_STATES:
            raise ValueError(f"status must be one of {_HIT_STATES}, got {self.status!r}")


@dataclass(frozen=True)
class WhitelistRow:
    """One screening_whitelist row (false-positive suppression)."""

    wl_id: str
    subject_type: str
    subject_id: str
    entry_content_hash: str
    list_code: str
    source_sanc_id: str
    approval_request_id: str
    approved_by: str
    rationale_pointer: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    schema_version: int = 1

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@runtime_checkable
class SanctionsStore(Protocol):
    """Storage contract for the sanctions tables (migrations 0074-0076)."""

    def insert_version(self, version: SanctionsListVersion) -> None: ...

    def save_version(self, version: SanctionsListVersion) -> None: ...

    def version_by_artifact(
        self, list_code: str, artifact_sha256: str
    ) -> SanctionsListVersion | None: ...

    def active_version(self, list_code: str) -> SanctionsListVersion | None: ...

    def active_versions(self) -> list[SanctionsListVersion]: ...

    def insert_entry(self, entry: SanctionsListEntry) -> None: ...

    def entries_for_version(self, list_version_id: str) -> list[SanctionsListEntry]: ...

    def insert_screening(self, record: ScreeningRecord) -> None: ...

    def get_screening(self, screening_id: str) -> ScreeningRecord | None: ...

    def insert_hit(self, hit: SanctionsHit) -> None: ...

    def save_hit(self, hit: SanctionsHit) -> None: ...

    def get_hit(self, sanc_id: str) -> SanctionsHit | None: ...

    def hits_by_subject(self, subject_type: str, subject_id: str) -> list[SanctionsHit]: ...

    def hits_by_status(self, status: str) -> list[SanctionsHit]: ...

    def insert_whitelist(self, row: WhitelistRow) -> None: ...

    def whitelist_for_subject(
        self, subject_type: str, subject_id: str
    ) -> list[WhitelistRow]: ...


class InMemorySanctionsStore:
    """Deterministic in-memory sanctions store for unit tests."""

    def __init__(self) -> None:
        self._versions: dict[str, SanctionsListVersion] = {}
        self._version_order: list[str] = []
        self._entries: dict[str, list[SanctionsListEntry]] = {}
        self._screenings: dict[str, ScreeningRecord] = {}
        self._hits: dict[str, SanctionsHit] = {}
        self._hit_order: list[str] = []
        self._whitelist: dict[str, WhitelistRow] = {}

    def insert_version(self, version: SanctionsListVersion) -> None:
        if version.list_version_id in self._versions:
            raise ConflictError(f"list version {version.list_version_id} exists")
        self._versions[version.list_version_id] = version
        self._version_order.append(version.list_version_id)

    def save_version(self, version: SanctionsListVersion) -> None:
        if version.list_version_id not in self._versions:
            raise ConflictError(f"unknown list version {version.list_version_id}")
        self._versions[version.list_version_id] = version

    def version_by_artifact(
        self, list_code: str, artifact_sha256: str
    ) -> SanctionsListVersion | None:
        for vid in self._version_order:
            v = self._versions[vid]
            if v.list_code == list_code and v.source_artifact_sha256 == artifact_sha256:
                return v
        return None

    def active_version(self, list_code: str) -> SanctionsListVersion | None:
        for vid in self._version_order:
            v = self._versions[vid]
            if v.list_code == list_code and v.status == "ACTIVE":
                return v
        return None

    def active_versions(self) -> list[SanctionsListVersion]:
        return [
            self._versions[vid]
            for vid in self._version_order
            if self._versions[vid].status == "ACTIVE"
        ]

    def insert_entry(self, entry: SanctionsListEntry) -> None:
        self._entries.setdefault(entry.list_version_id, []).append(entry)

    def entries_for_version(self, list_version_id: str) -> list[SanctionsListEntry]:
        return list(self._entries.get(list_version_id, []))

    def insert_screening(self, record: ScreeningRecord) -> None:
        self._screenings[record.screening_id] = record

    def get_screening(self, screening_id: str) -> ScreeningRecord | None:
        return self._screenings.get(screening_id)

    def insert_hit(self, hit: SanctionsHit) -> None:
        if hit.sanc_id in self._hits:
            raise ConflictError(f"hit {hit.sanc_id} already exists")
        self._hits[hit.sanc_id] = hit
        self._hit_order.append(hit.sanc_id)

    def save_hit(self, hit: SanctionsHit) -> None:
        if hit.sanc_id not in self._hits:
            raise ConflictError(f"unknown hit {hit.sanc_id}")
        self._hits[hit.sanc_id] = hit

    def get_hit(self, sanc_id: str) -> SanctionsHit | None:
        return self._hits.get(sanc_id)

    def hits_by_subject(self, subject_type: str, subject_id: str) -> list[SanctionsHit]:
        return [
            self._hits[h]
            for h in self._hit_order
            if self._hits[h].subject_type == subject_type
            and self._hits[h].subject_id == subject_id
        ]

    def hits_by_status(self, status: str) -> list[SanctionsHit]:
        return [self._hits[h] for h in self._hit_order if self._hits[h].status == status]

    def insert_whitelist(self, row: WhitelistRow) -> None:
        if row.wl_id in self._whitelist:
            raise ConflictError(f"whitelist row {row.wl_id} already exists")
        self._whitelist[row.wl_id] = row

    def whitelist_for_subject(
        self, subject_type: str, subject_id: str
    ) -> list[WhitelistRow]:
        return [
            w
            for w in self._whitelist.values()
            if w.subject_type == subject_type and w.subject_id == subject_id
        ]


# ---------------------------------------------------------------------------
# Screener service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Match:
    entry: SanctionsListEntry
    score: Decimal
    matched_alias: str
    identifier_exact: bool
    suppressed_by_wl_id: str | None = None


class SanctionsScreener:
    """Synchronous, fail-closed screening + the SanctionsHit FSM (spec/07)."""

    def __init__(
        self,
        store: SanctionsStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        thresholds: ComplianceThresholds,
        subject_control: Any,  # SubjectControlPort (alerts module)
        approvals: Any | None = None,  # ApprovalPort, required for clearance
        object_store_prefix: str = "objstore://sanctions/",
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._thresholds = thresholds
        self._control = subject_control
        self._approvals = approvals
        self._objstore_prefix = object_store_prefix

    # -- list ingestion -----------------------------------------------------------

    def ingest_list(
        self,
        *,
        list_code: str,
        entries: Sequence[ListEntryInput],
        artifact_sha256: str,
        artifact_pointer: str,
        ingested_by: str,
        connector_result_id: str | None = None,
        conn: Any | None = None,
    ) -> SanctionsListVersion:
        """Create + activate an immutable list version (idempotent on artifact).

        Names are normalized at ingestion (steps precomputed and stored);
        the previous ACTIVE version is SUPERSEDED in the same operation, so
        no window exists where new business screens against a stale list.
        """
        if list_code not in LIST_PRIORITY:
            raise InvalidRequestError(
                f"unknown list_code {list_code!r}; registry is closed "
                f"({sorted(LIST_PRIORITY)})",
                code="unknown_list_code",
            )
        existing = self._store.version_by_artifact(list_code, artifact_sha256)
        if existing is not None:
            return existing  # idempotent ingestion (UNIQUE list_code+artifact)
        now = self._clock.now()
        list_version_id = make_compliance_id(
            "slist", {"list_code": list_code, "source_artifact_sha256": artifact_sha256}
        )
        previous = self._store.active_version(list_code)
        previous_hashes: set[str] = set()
        previous_refs: dict[str, str] = {}
        if previous is not None:
            for entry in self._store.entries_for_version(previous.list_version_id):
                previous_hashes.add(entry.entry_content_hash)
                previous_refs[entry.entry_reference] = entry.entry_content_hash

        new_entries: list[SanctionsListEntry] = []
        added = changed = 0
        seen_refs: set[str] = set()
        for raw in entries:
            content_hash = sha256_canonical(
                {
                    "entry_reference": raw.entry_reference,
                    "entity_kind": raw.entity_kind,
                    "primary_name": raw.primary_name,
                    "aliases": list(raw.aliases),
                    "dob": raw.dob.isoformat() if raw.dob else None,
                    "nationalities": list(raw.nationalities),
                    "identifiers": {
                        k: list(v) for k, v in (raw.identifiers or {}).items()
                    },
                }
            )
            entry_id = make_compliance_id(
                "sent",
                {
                    "list_version_id": list_version_id,
                    "entry_reference": raw.entry_reference,
                    "entry_content_hash": content_hash,
                },
            )
            new_entries.append(
                SanctionsListEntry(
                    entry_id=entry_id,
                    list_version_id=list_version_id,
                    list_code=list_code,
                    entry_reference=raw.entry_reference,
                    entry_content_hash=content_hash,
                    entity_kind=raw.entity_kind,
                    primary_name=raw.primary_name,
                    primary_name_norm=normalize_name(raw.primary_name),
                    aliases=tuple(raw.aliases),
                    aliases_norm=tuple(normalize_name(a) for a in raw.aliases),
                    dob=raw.dob,
                    nationalities=tuple(raw.nationalities),
                    identifiers={k: tuple(v) for k, v in (raw.identifiers or {}).items()},
                    dossier_pointer=f"{self._objstore_prefix}{entry_id}/dossier",
                )
            )
            seen_refs.add(raw.entry_reference)
            if content_hash not in previous_hashes:
                if raw.entry_reference in previous_refs:
                    changed += 1
                else:
                    added += 1
        removed = sum(1 for ref in previous_refs if ref not in seen_refs)

        version = SanctionsListVersion(
            list_version_id=list_version_id,
            list_code=list_code,
            source_artifact_sha256=artifact_sha256,
            source_artifact_pointer=artifact_pointer,
            entry_count=len(new_entries),
            added_count=added,
            changed_count=changed,
            removed_count=removed,
            status="ACTIVE",
            ingested_by=ingested_by,
            connector_result_id=connector_result_id,
            ingested_at=now,
        )
        if previous is not None:
            self._store.save_version(replace(previous, status="SUPERSEDED", superseded_at=now))
        self._store.insert_version(version)
        for entry in new_entries:
            self._store.insert_entry(entry)
        self._audit.append(
            AuditEventSpec(
                event_type="SANCTIONS_LIST_INGESTED",
                actor_id=ingested_by,
                subject_type="SanctionsListVersion",
                subject_id=list_version_id,
                payload={
                    "list_code": list_code,
                    "entry_count": len(new_entries),
                    "added": added,
                    "changed": changed,
                    "removed": removed,
                },
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="sanctions_list.ingested",
                subject_type="SanctionsListVersion",
                subject_id=list_version_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={"list_code": list_code, "entry_count": len(new_entries)},
                occurred_at=now,
            ),
            conn=conn,
        )
        return version

    # -- screening ------------------------------------------------------------------

    def _active_entries(self) -> tuple[tuple[str, ...], list[SanctionsListEntry]]:
        versions = self._store.active_versions()
        version_ids = tuple(v.list_version_id for v in versions)
        entries: list[SanctionsListEntry] = []
        for version in versions:
            entries.extend(self._store.entries_for_version(version.list_version_id))
        return version_ids, entries

    @staticmethod
    def _identifier_exact(
        identifiers: Mapping[str, str], entry: SanctionsListEntry
    ) -> bool:
        if not identifiers:
            return False
        supplied = {str(v) for v in identifiers.values() if v}
        if not supplied:
            return False
        if entry.entry_reference in supplied:
            return True
        for values in entry.identifiers.values():
            if any(v in supplied for v in values):
                return True
        return False

    def screen_subject(
        self,
        *,
        subject_type: str,
        subject_id: str | None,
        names: Sequence[str],
        identifiers: Mapping[str, str] | None = None,
        context: str = "MANUAL",
        payment_intent_id: str | None = None,
        rescreen_run_id: str | None = None,
        conn: Any | None = None,
    ) -> ScreeningRecord:
        """Run one synchronous screen; persists the per-screen audit record.

        ``decision=HIT`` side effects happen here: the SanctionsHit row is
        created born-frozen with the freeze applied in the same operation
        (fail-closed — a freeze failure aborts everything).
        """
        if subject_type not in _SUBJECT_TYPES:
            raise InvalidRequestError(
                f"subject_type must be one of {_SUBJECT_TYPES}", code="invalid_subject_type"
            )
        if context not in _CONTEXTS:
            raise InvalidRequestError(
                f"context must be one of {_CONTEXTS}", code="invalid_context"
            )
        if not names:
            raise InvalidRequestError(
                "at least one name is required to screen", code="names_required"
            )
        identifiers = dict(identifiers or {})
        version_ids, entries = self._active_entries()
        if not entries:
            # The dataroom anti-false-cleared guard, adopted verbatim:
            # an empty active list refuses to screen — decline, never allow.
            raise SanctionsBlockError(
                "sanctions screening requires a non-empty watchlist - cannot honestly "
                "claim screening against nothing (fail-closed refusal)",
                code="sanctions_screening_refused",
            )

        started = self._clock.now()
        names_norm = tuple(normalize_name(n) for n in names)

        # Score every entry: max across supplied name forms x entry name forms.
        matches: list[_Match] = []
        for entry in entries:
            identifier_exact = self._identifier_exact(identifiers, entry)
            best = Decimal(0)
            best_alias = entry.primary_name_norm
            for subject_norm in names_norm:
                for alias_norm in entry.all_norms():
                    score = match_score(subject_norm, alias_norm)
                    if score > best:
                        best = score
                        best_alias = alias_norm
            if identifier_exact:
                best = Decimal(1)
            score4 = best.quantize(_FOUR_DP)
            if score4 >= self._thresholds.t_review or identifier_exact:
                matches.append(
                    _Match(
                        entry=entry,
                        score=score4,
                        matched_alias=best_alias,
                        identifier_exact=identifier_exact,
                    )
                )

        # Deterministic ordering: score DESC, list_priority ASC, entry_id ASC.
        matches.sort(
            key=lambda m: (
                -m.score,
                LIST_PRIORITY.get(m.entry.list_code, 99),
                m.entry.entry_id,
            )
        )

        # Whitelist suppression at hit-creation time only.
        now = self._clock.now()
        suppressed: list[_Match] = []
        if subject_id is not None:
            active_wl = {
                w.entry_content_hash: w
                for w in self._store.whitelist_for_subject(subject_type, subject_id)
                if w.active(now)
            }
            rewritten: list[_Match] = []
            for m in matches:
                wl = active_wl.get(m.entry.entry_content_hash)
                if wl is not None and (
                    m.score >= self._thresholds.t_hit or m.identifier_exact
                ):
                    rewritten.append(replace(m, suppressed_by_wl_id=wl.wl_id))
                else:
                    rewritten.append(m)
            matches = rewritten
            suppressed = [m for m in matches if m.suppressed_by_wl_id is not None]

        effective = [m for m in matches if m.suppressed_by_wl_id is None]
        top = effective[0] if effective else None
        if top is not None and (top.score >= self._thresholds.t_hit or top.identifier_exact):
            decision = "HIT"
        elif top is not None and top.score >= self._thresholds.t_review:
            decision = "POTENTIAL_MATCH"
        else:
            decision = "CLEAR"

        result_payload = [
            {
                "entry_id": m.entry.entry_id,
                "list_code": m.entry.list_code,
                "list_version_id": m.entry.list_version_id,
                "entry_reference": m.entry.entry_reference,
                "matched_alias": m.matched_alias,
                "score": m.score,
                "identifier_exact": m.identifier_exact,
                "suppressed_by_wl_id": m.suppressed_by_wl_id,
            }
            for m in matches
        ]
        result_hash = sha256_canonical(result_payload)

        screening_id = make_compliance_id(
            "scrn",
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "names_norm": list(names_norm),
                "identifiers": identifiers,
                "context": context,
                "list_version_ids": list(version_ids),
                "screened_at": started,
            },
        )
        finished = self._clock.now()
        duration_ms = max(int((finished - started).total_seconds() * 1000), 0)
        record = ScreeningRecord(
            screening_id=screening_id,
            subject_type=subject_type,
            subject_id=subject_id,
            payment_intent_id=payment_intent_id,
            context=context,
            names_screened=tuple(names),
            names_norm=names_norm,
            identifiers_hash=sha256_canonical(identifiers),
            list_version_ids=version_ids,
            algorithm_version=ALGORITHM_VERSION,
            translit_version=translit_version(),
            thresholds_version=self._thresholds.thresholds_version,
            decision=decision,
            top_score=top.score if top is not None else None,
            match_count=len(matches),
            result_hash=result_hash,
            result_pointer=f"{self._objstore_prefix}{screening_id}/result",
            rescreen_run_id=rescreen_run_id,
            duration_ms=duration_ms,
            screened_at=started,
        )

        hit: SanctionsHit | None = None
        if decision == "HIT" and top is not None:
            sanc_id = make_id(
                "sanc", {"screening_id": screening_id, "entry_id": top.entry.entry_id}
            )
            existing_hit = self._store.get_hit(sanc_id)
            if existing_hit is not None:
                # Replay of an identical screen against the same list version
                # (content-addressed dedupe, spec/07 §Re-screen): harmless
                # no-op — the existing hit's FSM state is NEVER reset.
                record = replace(record, sanctions_hit_id=sanc_id)
                self._store.insert_screening(record)
                return record
            # Born-frozen: freeze FIRST; a raise aborts before anything persists.
            if subject_id is not None:
                self._control.freeze_subject(
                    subject_type,
                    subject_id,
                    reason="SANCTIONS_HIT",
                    reference_id=screening_id,
                )
            hit = SanctionsHit(
                sanc_id=sanc_id,
                screening_id=screening_id,
                entry_id=top.entry.entry_id,
                entry_content_hash=top.entry.entry_content_hash,
                list_code=top.entry.list_code,
                list_version_id=top.entry.list_version_id,
                subject_type=subject_type,
                subject_id=subject_id,
                payment_intent_id=payment_intent_id,
                status="DETECTED_FROZEN",
                score=top.score,
                matched_alias=top.matched_alias,
                freeze_applied=subject_id is not None,
                detected_at=now,
            )
            record = replace(record, sanctions_hit_id=sanc_id)

        self._store.insert_screening(record)
        if hit is not None:
            self._store.insert_hit(hit)
            self._audit.append(
                AuditEventSpec(
                    event_type="SANCTIONS_HIT_DETECTED",
                    actor_id=PRODUCER,
                    subject_type="SanctionsHit",
                    subject_id=hit.sanc_id,
                    from_state=None,
                    to_state="DETECTED_FROZEN",
                    payload={"list_code": hit.list_code, "screening_id": screening_id},
                ),
                clock=self._clock,
                conn=conn,
            )
            self._outbox.enqueue(
                build_event(
                    event_type="sanctions_hit.detected",
                    subject_type="SanctionsHit",
                    subject_id=hit.sanc_id,
                    producer=PRODUCER,
                    topic="aml.events",
                    payload={
                        "sanc_id": hit.sanc_id,
                        "subject_id": subject_id or "",
                        "list_code": hit.list_code,
                    },
                    occurred_at=now,
                ),
                conn=conn,
            )
        self._audit.append(
            AuditEventSpec(
                event_type="SANCTIONS_SCREENING_COMPLETED",
                actor_id=PRODUCER,
                subject_type="SanctionsScreening",
                subject_id=screening_id,
                payload={
                    "decision": decision,
                    "match_count": len(matches),
                    "suppressed_count": len(suppressed),
                },
            ),
            clock=self._clock,
            conn=conn,
        )
        return record

    def check_payment(
        self,
        *,
        payment_intent_id: str,
        parties: Sequence[tuple[str, str | None, str]],
        conn: Any | None = None,
    ) -> str:
        """Pre-flight payment screen — synchronous ALWAYS, fail-closed.

        ``parties`` is ``(subject_type, subject_id, name)`` per party. Returns
        the worst decision across parties; anything other than CLEAR is a
        decline signal in the payment context (POTENTIAL_MATCH does NOT pass
        silently — spec/07 step 6 payment-context rule).
        """
        worst = "CLEAR"
        for subject_type, subject_id, name in parties:
            record = self.screen_subject(
                subject_type=subject_type,
                subject_id=subject_id,
                names=[name],
                context="PAYMENT",
                payment_intent_id=payment_intent_id,
                conn=conn,
            )
            if record.decision == "HIT":
                worst = "HIT"
            elif record.decision == "POTENTIAL_MATCH" and worst != "HIT":
                worst = "POTENTIAL_MATCH"
        return worst

    # -- periodic rescreen (spec/06 scheduler task) --------------------------------

    def rescreen_open_hits(self, *, conn: Any | None = None) -> list[ScreeningRecord]:
        """Re-screen every subject with an open hit against the current list.

        Open == DETECTED_FROZEN or UNDER_REVIEW.  Each subject is re-screened
        with ``context="RESCREEN"`` so a list update (a new ACTIVE version) is
        re-applied to subjects already under watch.  Returns the per-subject
        screening records (empty when no open hits exist — a legitimate no-op,
        not a silent pass).  Subjects with a null id are skipped: there is no
        stable identity to re-screen.
        """
        seen: set[tuple[str, str]] = set()
        records: list[ScreeningRecord] = []
        for status in ("DETECTED_FROZEN", "UNDER_REVIEW"):
            for hit in self._store.hits_by_status(status):
                if hit.subject_id is None:
                    continue
                key = (hit.subject_type, hit.subject_id)
                if key in seen:
                    continue
                seen.add(key)
                # Re-screen the SUBJECT's own originally-screened names against
                # the current list — NOT hit.matched_alias. Screening the matched
                # alias (the list-side string) is self-confirming: it only proves
                # the list entry still exists, never that this subject still
                # matches. The originating screening record persists the subject's
                # names (names_screened); use those.
                origin = self._store.get_screening(hit.screening_id)
                subject_names = list(origin.names_screened) if origin is not None else []
                if not subject_names:
                    # No persisted subject identity to re-screen — skip rather than
                    # fall back to the self-confirming alias path.
                    continue
                records.append(
                    self.screen_subject(
                        subject_type=hit.subject_type,
                        subject_id=hit.subject_id,
                        names=subject_names,
                        context="RESCREEN",
                        conn=conn,
                    )
                )
        return records

    # -- risk delta (spec/06 open question 7 resolution) ---------------------------

    def get_risk_delta(self, subject_type: str, subject_id: str) -> int:
        """+60 risk delta while the subject has a CONFIRMED sanctions hit."""
        for hit in self._store.hits_by_subject(subject_type, subject_id):
            if hit.status == "CONFIRMED":
                return 60
        return 0

    # -- SanctionsHit FSM -------------------------------------------------------

    def _require_hit(self, sanc_id: str) -> SanctionsHit:
        hit = self._store.get_hit(sanc_id)
        if hit is None:
            raise InvalidRequestError(f"unknown sanctions hit {sanc_id}", code="hit_not_found")
        return hit

    @staticmethod
    def _deny_unless(hit: SanctionsHit, expected: tuple[str, ...], trigger: str) -> None:
        if hit.status not in expected:
            raise ConflictError(
                f"transition {trigger!r} from state {hit.status!r} is DENIED "
                "(refusal-first)",
                code="fsm_transition_denied",
            )

    def _hit_effects(
        self,
        hit: SanctionsHit,
        *,
        event_type: str,
        actor_id: str,
        from_state: str,
        payload: dict,
        conn: Any | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="SANCTIONS_HIT_STATE_TRANSITION",
                actor_id=actor_id,
                subject_type="SanctionsHit",
                subject_id=hit.sanc_id,
                from_state=from_state,
                to_state=hit.status,
                payload=payload,
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="SanctionsHit",
                subject_id=hit.sanc_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={"sanc_id": hit.sanc_id, **payload},
                occurred_at=self._clock.now(),
            ),
            conn=conn,
        )

    def start_review(
        self, sanc_id: str, *, actor_id: str, actor_role: str, conn: Any | None = None
    ) -> SanctionsHit:
        """DETECTED_FROZEN -> UNDER_REVIEW."""
        hit = self._require_hit(sanc_id)
        self._deny_unless(hit, ("DETECTED_FROZEN",), "review_started")
        if actor_role not in ("COMPLIANCE_ANALYST", "CAMLCO"):
            raise AuthorizationError("review requires COMPLIANCE_ANALYST or CAMLCO role")
        updated = replace(
            hit,
            status="UNDER_REVIEW",
            reviewed_by=actor_id,
            review_started_at=self._clock.now(),
        )
        self._store.save_hit(updated)
        self._hit_effects(
            updated,
            event_type="sanctions_hit.review_started",
            actor_id=actor_id,
            from_state="DETECTED_FROZEN",
            payload={},
            conn=conn,
        )
        return updated

    def confirm(
        self,
        sanc_id: str,
        *,
        actor_id: str,
        actor_role: str,
        camlco_declaration: bool,
        alerts: Any,  # AmlAlertService
        str_workflow: Any,  # StrWorkflow
        rule_pack_id: str,
        conn: Any | None = None,
    ) -> SanctionsHit:
        """UNDER_REVIEW -> CONFIRMED (terminal). Freeze persists indefinitely.

        Creates the spec/06 ``rule_sanctions_confirmed_hit`` AmlAlert (alert
        idempotency derives from the sanc_id, so duplicate confirmations
        cannot duplicate alerts) and pre-seeds an StrReport in
        PENDING_CAMLCO_REVIEW.
        """
        hit = self._require_hit(sanc_id)
        self._deny_unless(hit, ("UNDER_REVIEW",), "confirmed")
        if actor_role != "CAMLCO":
            raise AuthorizationError("only the CAMLCO may confirm a sanctions hit")
        if camlco_declaration is not True:
            raise InvalidRequestError(
                "camlco_declaration must be true", code="camlco_declaration_required"
            )
        if hit.subject_id is None:
            raise ConflictError(
                "an EXTERNAL_PARTY hit has no platform subject to confirm against; "
                "handle via STR on CAMLCO decision (spec/07 open question 3)",
                code="external_party_hit",
            )
        alert = alerts.raise_alert(
            subject_type=hit.subject_type if hit.subject_type in ("Customer", "Merchant")
            else "Customer",
            subject_id=hit.subject_id,
            rule_id="rule_sanctions_confirmed_hit",
            rule_pack_id=rule_pack_id,
            risk_tier="HIGH",
            window_start=hit.detected_at,
            contributing_payment_ids=(
                (hit.payment_intent_id,) if hit.payment_intent_id else ()
            ),
            notes=f"CAMLCO-confirmed sanctions hit {hit.sanc_id}",
            conn=conn,
        )
        if hit.payment_intent_id:
            # Auto-open the case (errata E-C8): possible only when the FSM's
            # ">=1 contributing payment" guard is satisfiable; otherwise the
            # alert stays RAISED in the CAMLCO queue with the STR seeded.
            alerts.triage(
                alert.alert_id,
                actor_id=actor_id,
                actor_role="CAMLCO",
                assigned_to=actor_id,
                conn=conn,
            )
            alert = alerts.open_case(
                alert.alert_id,
                actor_id=actor_id,
                actor_role="CAMLCO",
                case_notes=f"auto-opened on sanctions hit confirmation {hit.sanc_id}",
                conn=conn,
            )
        report = str_workflow.create_from_alert(
            alert_id=alert.alert_id,
            subject_type=alert.subject_type,
            subject_id=alert.subject_id,
            camlco_id=actor_id,
            suspicion_narrative=(
                f"CAMLCO-confirmed sanctions hit {hit.sanc_id} against list entry "
                f"{hit.entry_id} ({hit.list_code}); freeze persists pending BFIU "
                "written instruction."
            ),
            contributing_payment_ids=(
                (hit.payment_intent_id,) if hit.payment_intent_id else ()
            ),
            requires_bfiu_escalation=True,
            conn=conn,
        )
        now = self._clock.now()
        updated = replace(
            hit,
            status="CONFIRMED",
            confirmed_by=actor_id,
            resolved_at=now,
            aml_alert_id=alert.alert_id,
            str_report_id=report.str_id,
        )
        self._store.save_hit(updated)
        self._hit_effects(
            updated,
            event_type="sanctions_hit.confirmed",
            actor_id=actor_id,
            from_state="UNDER_REVIEW",
            payload={"aml_alert_id": alert.alert_id, "str_report_id": report.str_id},
            conn=conn,
        )
        return updated

    def clear(
        self,
        sanc_id: str,
        *,
        actor_id: str,
        approval_request_id: str,
        whitelist: bool = False,
        rationale: str = "",
        conn: Any | None = None,
    ) -> SanctionsHit:
        """UNDER_REVIEW -> CLEARED_FALSE_POSITIVE via four-eyes ApprovalRequest.

        Only the approval callback executes the transition: the request must
        be APPROVED, for this hit, under ``sanctions_clear_or_unfreeze``.
        Unfreezes the subject; optionally writes a content-hash-anchored
        whitelist row (24-month max expiry).
        """
        hit = self._require_hit(sanc_id)
        self._deny_unless(hit, ("UNDER_REVIEW",), "cleared")
        if self._approvals is None:
            raise ConflictError(
                "clearance requires the ApprovalPort to be wired (four-eyes)",
                code="approval_port_missing",
            )
        if not rationale:
            raise InvalidRequestError(
                "clearance requires a recorded rationale", code="rationale_required"
            )
        record = self._approvals.get(approval_request_id)
        if record is None or record.state != "APPROVED":
            raise ConflictError(
                "clearance requires an APPROVED sanctions_clear_or_unfreeze "
                "ApprovalRequest (four-eyes)",
                code="approval_not_granted",
            )
        if record.action_type != "sanctions_clear_or_unfreeze" or record.subject_id != sanc_id:
            raise ConflictError(
                "approval request does not match this hit", code="approval_mismatch"
            )
        now = self._clock.now()
        if hit.subject_id is not None and hit.freeze_applied:
            self._control.unfreeze_subject(
                hit.subject_type,
                hit.subject_id,
                reason="SANCTIONS_HIT_CLEARED",
                reference_id=hit.sanc_id,
            )
        updated = replace(
            hit,
            status="CLEARED_FALSE_POSITIVE",
            approval_request_id=approval_request_id,
            resolved_at=now,
            freeze_released_at=now if hit.freeze_applied else None,
            freeze_applied=False,
            rationale_pointer=f"{self._objstore_prefix}{hit.sanc_id}/clearance_rationale",
        )
        self._store.save_hit(updated)
        if whitelist and hit.subject_id is not None:
            wl_id = make_compliance_id(
                "wl",
                {
                    "subject_type": hit.subject_type,
                    "subject_id": hit.subject_id,
                    "entry_content_hash": hit.entry_content_hash,
                },
            )
            self._store.insert_whitelist(
                WhitelistRow(
                    wl_id=wl_id,
                    subject_type=hit.subject_type,
                    subject_id=hit.subject_id,
                    entry_content_hash=hit.entry_content_hash,
                    list_code=hit.list_code,
                    source_sanc_id=hit.sanc_id,
                    approval_request_id=approval_request_id,
                    approved_by=record.approver_id or "",
                    rationale_pointer=f"{self._objstore_prefix}{wl_id}/rationale",
                    created_at=now,
                    expires_at=now + timedelta(days=30 * _WHITELIST_MAX_MONTHS),
                )
            )
        self._hit_effects(
            updated,
            event_type="sanctions_hit.cleared",
            actor_id=actor_id,
            from_state="UNDER_REVIEW",
            payload={"whitelisted": bool(whitelist and hit.subject_id is not None)},
            conn=conn,
        )
        return updated
