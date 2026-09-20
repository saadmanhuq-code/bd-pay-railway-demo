"""``sanctions_feed_v1`` — UN Consolidated + BFIU domestic list feeds (spec/12 §G).

Two duties:

1. **Feed ingestion (primary).** Fetch artifacts (UN Security Council
   Consolidated List XML; BFIU domestic uploads as structured CSV/JSON), parse
   with defusedxml (hardened, fail-closed), and drive the SanctionsFeedVersion
   FSM: ``FETCHED -> VALIDATED -> HANDED_OFF -> INGESTED`` with the
   anti-false-cleared guard — an empty or >50%-shrunken list is REJECTED with
   a CRITICAL page and the PREVIOUS list stays active (**an empty list must
   never silently clear the book** — the dataroom-bd matcher guard).
   Staleness (>24h without a successful INGESTED per source) raises the
   side-channel STALE_ALARM (cut-last #5: never silent staleness).

2. **``screen()``** — the frozen ``sdk.SanctionsConnector`` surface. The
   matching algorithm is the ported dataroom-bd Jaro-Winkler matcher
   (faithful 0.90/0.80 thresholds); spec/07's in-process screener is injected
   at integration in its place (SPEC_ERRATA-LANE-B LB7). Screening against an
   empty active list raises — never a false "cleared".

Live wire path (SANDBOX/PRODUCTION — and the SIMULATOR source runs the SAME
code, so the wire path is certification-exercised):

- Canonical source URL = :data:`UN_CONSOLIDATED_URL` (registry config key
  ``un_list_url``). Observed wire behavior (recorded 2026-06-12): the official
  URL answers **302** to a time-limited Azure-blob URL carrying the artifact,
  so the fetch follows redirects itself (``max_redirect_hops``, default 3) —
  it works with any injected ``WireTransport`` and needs no client-level
  redirect support.
- Hard per-attempt timeout ``fetch_timeout_s`` (default 300s, the spec/10
  budget row) via ``asyncio.wait_for``; retry budget ``fetch_retries``
  (default 3, spec/10) with injected-sleep backoff; breaker-wrapped
  (admit kind ``QUERY_STATUS`` — an idempotent read; OPEN ⇒ the fetch is
  refused and the previously ingested list stays active, fail-closed).
- **ETag conditional fetch:** the blob response carries an ``ETag``; it is
  remembered per source at INGESTED and sent as ``If-None-Match`` on the next
  poll. A ``304`` resolves to the same terminal no-op ``INGESTED
  unchanged=true`` row as the content-hash dedupe.
- **Monotonic version stamp:** the UN root element carries
  ``dateGenerated`` (e.g. ``2026-06-11T23:00:02.349Z``). An artifact whose
  stamp is strictly OLDER than the active ingested one is REJECTED
  (``version_regressed``) — a feed can only move forward, never resurrect a
  stale book. Empty / >50%-shrunk artifacts are REJECTED by the existing
  CERT-S1 guards, which apply to live data identically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.identity.ids12 import make_spec12_id
from bdpay.connectors.identity.matcher import (
    EmptyWatchlistError,
    WatchlistEntry,
    normalize_name,
    screen_names,
)
from bdpay.connectors.mfs.wire import WireResponse, WireTransport
from bdpay.connectors.ports import (
    AuditSink,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
)
from bdpay.connectors.registry import ConnectorMode
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, ConnectorError, InvalidRequestError
from bdpay.security.outbound_allowlist import validate_outbound_url_async

__all__ = [
    "FeedTransitionDeniedError",
    "FeedUnavailableError",
    "SanctionsFeedConnector",
    "SanctionsFeedVersionRow",
    "UN_CONSOLIDATED_URL",
    "parse_domestic_list",
    "parse_un_consolidated",
    "parse_un_list_version",
]

CONNECTOR_ID = "sanctions_feed_v1"

#: Canonical UN Security Council consolidated-list XML endpoint (spec/12 §G.1).
#: Registry-config override key: ``un_list_url``. The endpoint 302-redirects to
#: a time-limited blob URL (observed 2026-06-12); the connector follows hops.
UN_CONSOLIDATED_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

STALENESS_ALARM_H = 24

#: spec/10 budget row ``sanctions_feed_v1``: 300s hard timeout (batch file),
#: 3 retries. Config override keys: ``fetch_timeout_s`` / ``fetch_retries``.
FETCH_TIMEOUT_S = 300.0
FETCH_RETRIES = 3

#: Backoff between fetch attempts (through the injected sleep — instant in tests).
FETCH_BACKOFF_S = (5, 15, 45)

#: Redirect-following cap (config key ``max_redirect_hops``).
MAX_REDIRECT_HOPS = 3

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_SOURCES = frozenset({"UN_CONSOLIDATED", "BFIU_DOMESTIC", "VENDOR"})
_STATES = frozenset(
    {"FETCHED", "VALIDATED", "HANDED_OFF", "INGESTED", "REJECTED", "STALE_ALARMED"}
)
_ALLOWED = frozenset(
    {
        ("FETCHED", "VALIDATED"),
        ("FETCHED", "REJECTED"),
        ("VALIDATED", "HANDED_OFF"),
        ("HANDED_OFF", "INGESTED"),
    }
)

_SYSTEM_ACTOR = "system:sanctions-feed"


class FeedTransitionDeniedError(ConflictError):
    default_code = "sanctions_feed_transition_denied"


class FeedUnavailableError(ConnectorError):
    """Typed fetch unavailability — the previously ingested list stays active
    (screening never depends on feed availability; staleness alarms at 24h)."""

    default_code = "sanctions_feed_unavailable"


@dataclass
class SanctionsFeedVersionRow:
    """One ``sanctions_feed_versions`` row.

    ``version_stamp`` (the UN root ``dateGenerated``) and ``etag`` are
    connector-held fetch metadata for the monotonic-version and conditional-GET
    semantics; they live on the in-memory row (the durable table keeps the
    spec/12 DDL shape — the artifact itself is content-addressed in the
    object store via ``raw_pointer``).
    """

    feed_version_id: str
    source: str
    content_hash: str
    raw_pointer: str
    state: str = "FETCHED"
    entry_count: int | None = None
    previous_entry_count: int | None = None
    unchanged: bool = False
    spec07_list_version_id: str | None = None
    fetched_at: datetime | None = None
    ingested_at: datetime | None = None
    version_stamp: str = ""
    etag: str = ""
    schema_version: int = 1


# -- Parsers ---------------------------------------------------------------------------


def _individual_name(node) -> str:
    parts = []
    for tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME"):
        text = (node.findtext(tag) or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def parse_un_consolidated(xml_bytes: bytes) -> list[dict]:
    """Parse the UN consolidated XML into normalized entry dicts (spec/12 §G)."""
    from defusedxml.ElementTree import fromstring

    root = fromstring(xml_bytes.decode("utf-8"))
    entries: list[dict] = []
    individuals = root.find("INDIVIDUALS")
    if individuals is not None:
        for node in individuals.findall("INDIVIDUAL"):
            aliases = [
                (alias.findtext("ALIAS_NAME") or "").strip()
                for alias in node.findall("INDIVIDUAL_ALIAS")
                if (alias.findtext("ALIAS_NAME") or "").strip()
            ]
            dob_node = node.find("INDIVIDUAL_DATE_OF_BIRTH")
            dob = ""
            if dob_node is not None:
                dob = (dob_node.findtext("DATE") or dob_node.findtext("YEAR") or "").strip()
            entries.append(
                {
                    "entry_type": "INDIVIDUAL",
                    "primary_name": _individual_name(node),
                    "aliases": aliases,
                    "dob": dob,
                    "nationality": (
                        node.find("NATIONALITY").findtext("VALUE", default="")
                        if node.find("NATIONALITY") is not None
                        else ""
                    ).strip(),
                    "un_list_type": (node.findtext("UN_LIST_TYPE") or "").strip(),
                    "reference_number": (node.findtext("REFERENCE_NUMBER") or "").strip(),
                    "listed_on": (node.findtext("LISTED_ON") or "").strip(),
                }
            )
    entity_block = root.find("ENTITIES")
    if entity_block is not None:
        for node in entity_block.findall("ENTITY"):
            aliases = [
                (alias.findtext("ALIAS_NAME") or "").strip()
                for alias in node.findall("ENTITY_ALIAS")
                if (alias.findtext("ALIAS_NAME") or "").strip()
            ]
            entries.append(
                {
                    "entry_type": "ENTITY",
                    "primary_name": (node.findtext("FIRST_NAME") or "").strip(),
                    "aliases": aliases,
                    "dob": "",
                    "nationality": "",
                    "un_list_type": (node.findtext("UN_LIST_TYPE") or "").strip(),
                    "reference_number": (node.findtext("REFERENCE_NUMBER") or "").strip(),
                    "listed_on": (node.findtext("LISTED_ON") or "").strip(),
                }
            )
    return entries


def parse_un_list_version(xml_bytes: bytes) -> str:
    """The UN list's own version stamp: the root ``dateGenerated`` attribute
    (RFC3339 ``Z`` form, so string order == chronological order); '' when the
    artifact carries none (e.g. locally built test content)."""
    from defusedxml.ElementTree import fromstring

    root = fromstring(xml_bytes.decode("utf-8"))
    return str(root.attrib.get("dateGenerated", "")).strip()


def parse_domestic_list(raw: bytes) -> list[dict]:
    """BFIU domestic designations: operator upload as JSON list or CSV rows
    (``name,entity_type,reference``) until a machine-readable channel is
    confirmed (spec/07 open Q1)."""
    import csv
    import io
    import json

    text = raw.decode("utf-8")
    stripped = text.strip()
    entries: list[dict] = []
    if stripped.startswith("["):
        for item in json.loads(stripped, parse_float=str):
            entries.append(
                {
                    "entry_type": str(item.get("entity_type", "INDIVIDUAL")).upper(),
                    "primary_name": str(item["name"]),
                    "aliases": [str(a) for a in item.get("aliases", [])],
                    "dob": str(item.get("dob", "")),
                    "nationality": "BD",
                    "un_list_type": "BFIU_DOMESTIC",
                    "reference_number": str(item.get("reference", "")),
                    "listed_on": str(item.get("listed_on", "")),
                }
            )
        return entries
    reader = csv.reader(io.StringIO(stripped))
    for line_no, fields in enumerate(reader, 1):
        if not fields or not "".join(fields).strip():
            continue
        if line_no == 1 and fields[0].strip().lower() == "name":
            continue  # header row
        if len(fields) < 3:
            raise InvalidRequestError(
                f"domestic list row {line_no} needs name,entity_type,reference",
                code="domestic_list_row_invalid",
            )
        entries.append(
            {
                "entry_type": fields[1].strip().upper() or "INDIVIDUAL",
                "primary_name": fields[0].strip(),
                "aliases": [],
                "dob": "",
                "nationality": "BD",
                "un_list_type": "BFIU_DOMESTIC",
                "reference_number": fields[2].strip(),
                "listed_on": "",
            }
        )
    return entries


# -- The connector --------------------------------------------------------------------------


class SanctionsFeedConnector:
    """Frozen ``sdk.SanctionsConnector`` implementation for ``sanctions_feed_v1``."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        transport: WireTransport | None = None,
        config: dict | None = None,
        handoff=None,
        screener=None,
        breaker: CircuitBreaker | None = None,
        object_store: ObjectStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        sleep=None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        self._transport = transport
        self._config = dict(config or {})
        self._handoff = handoff
        self._screener = screener
        self._breaker = breaker
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._sleep = sleep if sleep is not None else _no_sleep_default
        self._rows: dict[str, SanctionsFeedVersionRow] = {}
        self._order: list[str] = []
        self._seen: dict[tuple[str, str], str] = {}
        self._active_entries: dict[str, list[WatchlistEntry]] = {}
        #: Parsed-entry count of the ACTIVE book per source. The delta-shrink
        #: guard compares parsed entry counts to parsed entry counts — never to
        #: the alias-expanded watchlist length (live UN data carries many
        #: aliases per entry, so the watchlist is several times larger than the
        #: entry count and would falsely trip the guard on every real update).
        self._active_entry_counts: dict[str, int] = {}
        #: (content_hash, raw_pointer) of the ACTIVE artifact per source —
        #: what a 304 conditional re-fetch resolves to.
        self._active_artifacts: dict[str, tuple[str, str]] = {}
        self._etags: dict[str, str] = {}
        self._version_stamps: dict[str, str] = {}
        self._pending_entries: dict[str, list[WatchlistEntry]] = {}
        self._last_ingested_at: dict[str, datetime] = {}

    # -- access -----------------------------------------------------------------------------

    def rows(self) -> list[SanctionsFeedVersionRow]:
        return [self._rows[k] for k in self._order]

    def active_watchlist(self) -> list[WatchlistEntry]:
        merged: list[WatchlistEntry] = []
        for source in sorted(self._active_entries):
            merged.extend(self._active_entries[source])
        return merged

    def _advance(self, row: SanctionsFeedVersionRow, to_state: str) -> None:
        if to_state not in _STATES:
            raise ValueError(f"unknown feed state {to_state!r}")
        if (row.state, to_state) not in _ALLOWED:
            raise FeedTransitionDeniedError(
                f"feed FSM transition {row.state} -> {to_state} is DENIED (refusal-first)"
            )
        row.state = to_state

    async def _admit(self) -> None:
        """Breaker gate for the fetch (idempotent read ⇒ probe kind). OPEN ⇒
        the fetch is refused fail-closed; the active book keeps screening."""
        if self._breaker is None:
            return
        if not await self._breaker.admit(CONNECTOR_ID, kind="QUERY_STATUS"):
            raise FeedUnavailableError(
                "sanctions feed circuit is open; previous list stays active",
                code="circuit_open",
            )

    async def _observe(self, *, ok: bool, transport_error: bool = False) -> None:
        if self._breaker is None:
            return
        from bdpay.connectors.sdk import ConnectorStatus

        await self._breaker.observe(
            CONNECTOR_ID,
            ConnectorStatus.SUCCESS if ok else ConnectorStatus.FAILED,
            transport_error=transport_error,
        )

    def _emit(self, event_type: str, row: SanctionsFeedVersionRow, **extra) -> None:
        payload = {
            "feed_version_id": row.feed_version_id,
            "source": row.source,
            "entry_count": row.entry_count,
            "content_hash": row.content_hash,
        }
        payload.update(extra)
        self._events.emit(event_type, payload)

    def _to_watchlist(
        self, source: str, entries: list[dict], version: str
    ) -> list[WatchlistEntry]:
        watchlist: list[WatchlistEntry] = []
        for index, entry in enumerate(entries):
            names = [entry["primary_name"], *entry.get("aliases", [])]
            for alias_no, name in enumerate(n for n in names if n):
                watchlist.append(
                    WatchlistEntry(
                        entry_id=(
                            f"{source.lower()}-{entry.get('reference_number') or index}"
                            f"-{alias_no}"
                        ),
                        list_source=source,
                        raw_name=name,
                        normalized_name=normalize_name(name),
                        entity_type=entry.get("entry_type", "INDIVIDUAL"),
                        snapshot_date=entry.get("listed_on", ""),
                        version=version,
                    )
                )
        return watchlist

    # -- ingestion FSM -----------------------------------------------------------------------

    def _unchanged_refetch_row(
        self, source: str, content_hash: str, raw_pointer: str, *, etag: str = ""
    ) -> SanctionsFeedVersionRow:
        """Terminal no-op ``INGESTED unchanged=true`` row (hash dedupe / 304)."""
        now = self._clock.now()
        row = SanctionsFeedVersionRow(
            feed_version_id=make_spec12_id(
                "sfeed", {"source": source, "content_hash": content_hash, "refetch": now}
            ),
            source=source,
            content_hash=content_hash,
            raw_pointer=raw_pointer,
            state="INGESTED",
            unchanged=True,
            entry_count=self._active_entry_counts.get(source),
            fetched_at=now,
            ingested_at=now,
            version_stamp=self._version_stamps.get(source, ""),
            etag=etag or self._etags.get(source, ""),
        )
        self._rows[row.feed_version_id] = row
        self._order.append(row.feed_version_id)
        active = self._active_artifacts.get(source)
        if etag and active is not None and active[0] == content_hash:
            self._etags[source] = etag  # provider re-stamped the same content
        return row

    def ingest_feed(self, source: str, raw: bytes, *, etag: str = "") -> SanctionsFeedVersionRow:
        """FETCHED -> VALIDATED | REJECTED (+ unchanged short-circuit)."""
        if source not in _SOURCES:
            raise InvalidRequestError(f"unknown feed source {source!r}", code="feed_source_invalid")
        content_hash = sha256_canonical({"raw": raw.decode("utf-8", errors="replace")})
        now = self._clock.now()
        raw_pointer = f"sanctions-feeds/{source}/{content_hash}"
        self._objects.put(raw_pointer, raw)
        if (source, content_hash) in self._seen:
            # Re-fetch of an unchanged list: terminal no-op INGESTED alias.
            return self._unchanged_refetch_row(source, content_hash, raw_pointer, etag=etag)
        row = SanctionsFeedVersionRow(
            feed_version_id=make_spec12_id(
                "sfeed", {"source": source, "content_hash": content_hash}
            ),
            source=source,
            content_hash=content_hash,
            raw_pointer=raw_pointer,
            previous_entry_count=self._active_entry_counts.get(source),
            fetched_at=now,
            etag=etag,
        )
        self._rows[row.feed_version_id] = row
        self._order.append(row.feed_version_id)
        self._emit("sanctions_feed.fetched", row)
        try:
            if source == "UN_CONSOLIDATED":
                entries = parse_un_consolidated(raw)
                row.version_stamp = parse_un_list_version(raw)
            else:
                entries = parse_domestic_list(raw)
        except Exception:
            return self._reject(row, reason="parse_failed")
        row.entry_count = len(entries)
        previous = row.previous_entry_count
        if len(entries) == 0:
            return self._reject(row, reason="empty_list")
        if previous is not None and len(entries) < (previous // 2):
            return self._reject(row, reason="shrunk_over_50pct")
        active_stamp = self._version_stamps.get(source, "")
        if row.version_stamp and active_stamp and row.version_stamp < active_stamp:
            # Monotonic-version guard: a feed only moves forward; an artifact
            # older than the active book must never replace it.
            return self._reject(row, reason="version_regressed")
        self._advance(row, "VALIDATED")
        self._audit.record(
            "SANCTIONS_FEED_VALIDATED",
            actor_id=_SYSTEM_ACTOR,
            occurred_at=self._clock.now(),
            detail={"feed_version_id": row.feed_version_id, "entry_count": row.entry_count},
        )
        version = f"{source.lower()}-{content_hash[:12]}"
        self._pending_entries[row.feed_version_id] = self._to_watchlist(
            source, entries, version
        )
        self._seen[(source, content_hash)] = row.feed_version_id
        return row

    def _reject(self, row: SanctionsFeedVersionRow, *, reason: str) -> SanctionsFeedVersionRow:
        self._advance(row, "REJECTED")
        self._audit.record(
            "SANCTIONS_FEED_REJECTED",
            actor_id=_SYSTEM_ACTOR,
            occurred_at=self._clock.now(),
            detail={
                "feed_version_id": row.feed_version_id,
                "source": row.source,
                "reason": reason,
                "severity": "CRITICAL",  # page — never silently clear the book
            },
        )
        self._emit("sanctions_feed.rejected", row, reason=reason)
        return row

    async def hand_off(self, feed_version_id: str) -> SanctionsFeedVersionRow:
        """VALIDATED -> HANDED_OFF via the spec/07 ingest port (202 accepted)."""
        row = self._rows[feed_version_id]
        if row.state != "VALIDATED":
            raise FeedTransitionDeniedError("handoff requires a VALIDATED feed version")
        if self._handoff is not None:
            accepted = await self._handoff(row, self._pending_entries.get(feed_version_id, []))
            if not accepted:
                return row  # stays VALIDATED; next sweep retries
        self._advance(row, "HANDED_OFF")
        return row

    def mark_ingested(
        self, feed_version_id: str, *, spec07_list_version_id: str
    ) -> SanctionsFeedVersionRow:
        """``sanctions_list.ingested`` observed (spec/07): HANDED_OFF -> INGESTED;
        the new entries become the connector's active screening snapshot."""
        row = self._rows[feed_version_id]
        self._advance(row, "INGESTED")
        row.spec07_list_version_id = spec07_list_version_id
        row.ingested_at = self._clock.now()
        self._active_entries[row.source] = self._pending_entries.pop(feed_version_id, [])
        self._active_entry_counts[row.source] = row.entry_count or 0
        self._active_artifacts[row.source] = (row.content_hash, row.raw_pointer)
        if row.etag:
            self._etags[row.source] = row.etag
        if row.version_stamp:
            self._version_stamps[row.source] = row.version_stamp
        self._last_ingested_at[row.source] = row.ingested_at
        self._emit("sanctions_feed.ingested", row)
        return row

    # -- staleness (side-channel alarm state) ------------------------------------------------

    def staleness_check(self) -> list[str]:
        """15-min scheduler sweep: any source whose newest INGESTED is >24h old
        raises the STALE alarm (side-channel; CRITICAL — cut-last #5)."""
        alarmed: list[str] = []
        now = self._clock.now()
        for source in sorted(self._last_ingested_at):
            hours_stale = (now - self._last_ingested_at[source]).total_seconds() / 3600
            if hours_stale > STALENESS_ALARM_H:
                alarmed.append(source)
                self._events.emit(
                    "sanctions_feed.stale",
                    {"source": source, "hours_stale": int(hours_stale)},
                )
                self._audit.record(
                    "SANCTIONS_FEED_STALE",
                    actor_id=_SYSTEM_ACTOR,
                    occurred_at=now,
                    detail={"source": source, "hours_stale": int(hours_stale)},
                )
        return alarmed

    # -- fetch cycle ---------------------------------------------------------------------------

    async def _wire_get(self, url: str, headers: dict) -> WireResponse:
        """One GET, following redirects up to ``max_redirect_hops`` (the
        official UN URL 302s to a time-limited blob URL — observed 2026-06-12).
        Conditional headers travel with every hop.

        Every redirect ``Location`` is resolved against the current URL and
        passed through
        :func:`~bdpay.security.outbound_allowlist.validate_outbound_url_async`
        (DNS off the event loop, bounded, fail-closed on a resolver stall)
        before the next GET is issued, blocking SSRF via a malicious redirect.
        The production transport
        (:class:`~bdpay.connectors.mfs.wire.PinnedHttpxTransport`) additionally
        pins each connection to the address it validated, so a DNS answer that
        changes between validation and connect cannot re-target the GET.
        """
        hops = int(self._config.get("max_redirect_hops", MAX_REDIRECT_HOPS))
        current = url
        await validate_outbound_url_async(current)
        for _ in range(hops + 1):
            response = await self._transport.request(
                "GET", current, headers=dict(headers), content=b""
            )
            if response.status_code in _REDIRECT_STATUSES:
                location = response.header("location")
                if not location:
                    return response  # malformed redirect: surfaced as wire failure
                current = urljoin(current, location)
                await validate_outbound_url_async(current)
                continue
            return response
        raise FeedUnavailableError(
            f"un list fetch exceeded {hops} redirect hops", code="redirect_hops_exceeded"
        )

    async def fetch_un_list(self) -> SanctionsFeedVersionRow:
        """Scheduler ``sanctions-feed-poll`` (6h): fetch + ingest the UN list.

        Breaker-wrapped, hard per-attempt timeout, spec/10 retry budget,
        ETag conditional GET. Every failure path is fail-closed: the
        previously ingested list keeps screening and staleness alarms at 24h.
        """
        if self._mode is ConnectorMode.DISABLED:
            raise FeedTransitionDeniedError("sanctions_feed_v1 is DISABLED")
        if self._transport is None:
            raise FeedUnavailableError(
                "sanctions_feed_v1 has no wire transport configured",
                code="transport_missing",
            )
        await self._admit()
        source = "UN_CONSOLIDATED"
        url = str(self._config.get("un_list_url", UN_CONSOLIDATED_URL))
        timeout_s = float(self._config.get("fetch_timeout_s", FETCH_TIMEOUT_S))
        retries = max(int(self._config.get("fetch_retries", FETCH_RETRIES)), 0)
        headers: dict[str, str] = {"accept": "application/xml"}
        sent_etag = self._etags.get(source, "")
        if sent_etag:
            headers["if-none-match"] = sent_etag
        waits = tuple(
            FETCH_BACKOFF_S[min(attempt, len(FETCH_BACKOFF_S) - 1)] for attempt in range(retries)
        )
        last_error: Exception | None = None
        last_status: int | None = None
        for backoff_s in (*waits, None):
            try:
                response = await asyncio.wait_for(
                    self._wire_get(url, headers), timeout=timeout_s
                )
            except Exception as exc:  # transport failure / hard timeout: retry
                last_error, last_status = exc, None
                await self._observe(ok=False, transport_error=True)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
                continue
            if response.status_code == 304 and sent_etag:
                # Conditional-GET dedupe: the active artifact is unchanged.
                await self._observe(ok=True)
                content_hash, raw_pointer = self._active_artifacts[source]
                return self._unchanged_refetch_row(
                    source, content_hash, raw_pointer, etag=sent_etag
                )
            if response.status_code == 200 and response.body.strip():
                await self._observe(ok=True)
                return self.ingest_feed(source, response.body, etag=response.header("etag"))
            # Non-200 (or a bodyless 200): wire-level failure, retry then refuse.
            last_error, last_status = None, response.status_code
            await self._observe(ok=False)
            if backoff_s is not None:
                await self._sleep(backoff_s)
        raise FeedUnavailableError(
            "un list fetch failed after retry budget "
            f"(last_status={last_status}); previous list stays active",
            code="feed_fetch_failed",
        ) from last_error

    # -- SanctionsConnector ----------------------------------------------------------------------

    async def screen(self, entity_name: str, entity_type: str, identifiers: dict) -> dict:
        """Screen one subject against the active ingested snapshot.

        Delegates to the injected spec/07 screener when present; otherwise the
        ported Jaro-Winkler matcher runs over the active watchlist. An empty
        active list raises (anti-false-cleared) — callers fail closed.
        """
        if self._mode is ConnectorMode.DISABLED:
            raise FeedTransitionDeniedError("sanctions_feed_v1 is DISABLED")
        if self._screener is not None:
            return await self._screener(entity_name, entity_type, identifiers)
        watchlist = self.active_watchlist()
        if not watchlist:
            raise EmptyWatchlistError(
                "sanctions screening requires a non-empty watchlist — "
                "cannot honestly claim screening against nothing"
            )
        subject_ref = str(identifiers.get("ref", "")) if identifiers else ""
        outcome = screen_names(
            [{"ref": subject_ref, "name": entity_name}],
            watchlist,
            screened_at=self._clock.now(),
        )
        entry = outcome["results"][0]
        return {
            "screening_id": outcome["screening_id"],
            "entity_name": entity_name,
            "entity_type": entity_type,
            "hit": entry["hit"],
            "match_score": entry["match_score"],
            "disposition": entry["disposition"],
            "matched_list": entry["matched_list"],
            "matched_entry_id": entry["matched_entry_id"],
            "list_version": outcome["list_version"],
            "sources": outcome["sources"],
        }


async def _no_sleep_default(_seconds: float) -> None:
    return None
