"""Sanctions watchlist feed loader for the composition root (COMP-01 fix).

The :class:`~bdpay.compliance.sanctions.screening.SanctionsScreener` is
fail-closed: an empty active watchlist refuses to screen.  The boot path must
therefore load at least one ACTIVE list version before the gateway serves
traffic, otherwise every payment/onboarding screen declines.

Feed sources (``SANCTIONS_FEED_SOURCE`` setting):

- ``""`` (default / unset)  -> NO ingestion at boot.  The screener stays empty
  and fail-closed.  This is the correct posture for a deployment whose real
  UN + BFIU feed connector has not yet been wired: the platform declines to
  screen rather than silently clear, and an operator must point the setting at
  the real source before go-live.
- ``"bootstrap"``           -> ingest the small, content-addressed bootstrap
  list defined below.  It is a deterministic, non-empty UN-1267 + BFIU list
  sufficient to (a) make the screener provably non-empty at boot and (b) drive
  the periodic-rescreen scheduler task against a real ACTIVE version.  The real
  operator-provided feed REPLACES this on first live ingestion (the screener's
  artifact-keyed idempotency + supersede semantics handle the swap cleanly).

The function returns the list of ingested ``SanctionsListVersion`` objects so
the caller can assert a non-empty active list at boot.
"""

from __future__ import annotations

from datetime import date

from bdpay.compliance.sanctions.screening import (
    ListEntryInput,
    SanctionsListVersion,
    SanctionsScreener,
)

__all__ = [
    "BOOTSTRAP_FEED_SOURCE",
    "bootstrap_un_1267_entries",
    "bootstrap_bfiu_domestic_entries",
    "load_sanctions_feed",
]

#: ``SANCTIONS_FEED_SOURCE`` value that selects the bootstrap list below.
BOOTSTRAP_FEED_SOURCE = "bootstrap"

# Content-addressed artifact digests.  These are STABLE strings (64 hex chars)
# so re-ingestion at every boot is idempotent (UNIQUE list_code+artifact).
# They are deliberately NOT real feed hashes — the real feed connector supplies
# the gazetted artifact + its true sha256 and supersedes these on first run.
_UN_1267_ARTIFACT_SHA256 = "b0" + "0" * 62
_BFIU_DOMESTIC_ARTIFACT_SHA256 = "b1" + "0" * 62
_INGESTED_BY = "sanctions_feed_bootstrap_v1"


def bootstrap_un_1267_entries() -> list[ListEntryInput]:
    """A minimal, non-empty UN-1267 consolidated-list excerpt for boot.

    These are illustrative designations used only to seed a non-empty ACTIVE
    list so the fail-closed screener can run; the live UN feed supersedes them.
    """
    return [
        ListEntryInput(
            entry_reference="UN-1267-QDi.001",
            entity_kind="INDIVIDUAL",
            primary_name="Usama Muhammed Awad Bin Laden",
            aliases=("Osama Bin Laden", "Abu Abdallah"),
            dob=date(1957, 3, 10),
            nationalities=("Saudi Arabia",),
        ),
        ListEntryInput(
            entry_reference="UN-1267-QDe.004",
            entity_kind="ENTITY",
            primary_name="Al-Qaida",
            aliases=("Al Qaeda", "International Front for Fighting Jews and Crusaders"),
        ),
        ListEntryInput(
            entry_reference="UN-1267-QDe.012",
            entity_kind="ENTITY",
            primary_name="Islamic State in Iraq and the Levant",
            aliases=("ISIL", "ISIS", "Daesh"),
        ),
    ]


def bootstrap_bfiu_domestic_entries() -> list[ListEntryInput]:
    """A minimal, non-empty BFIU domestic-list excerpt for boot.

    Domestic proscribed-organisation entries; the live BFIU feed supersedes
    them on first ingestion.
    """
    return [
        ListEntryInput(
            entry_reference="BFIU-DOM-0001",
            entity_kind="ENTITY",
            primary_name="Jamaatul Mujahideen Bangladesh",
            aliases=("JMB",),
        ),
        ListEntryInput(
            entry_reference="BFIU-DOM-0002",
            entity_kind="ENTITY",
            primary_name="Ansarullah Bangla Team",
            aliases=("ABT", "Ansar al-Islam"),
        ),
    ]


def load_sanctions_feed(
    screener: SanctionsScreener, *, source: str
) -> list[SanctionsListVersion]:
    """Ingest the configured feed into ``screener``; return ingested versions.

    ``source == ""`` (or unset) ingests nothing and leaves the screener
    fail-closed.  ``source == "bootstrap"`` ingests the bootstrap UN + BFIU
    lists.  Any other value is rejected so a typo in deployment config cannot
    silently leave the screener empty.
    """
    if not source:
        return []
    if source != BOOTSTRAP_FEED_SOURCE:
        raise ValueError(
            f"unknown SANCTIONS_FEED_SOURCE {source!r}; "
            f"allowed: '' (fail-closed, no ingest) or {BOOTSTRAP_FEED_SOURCE!r}"
        )
    versions = [
        screener.ingest_list(
            list_code="UN_1267",
            entries=bootstrap_un_1267_entries(),
            artifact_sha256=_UN_1267_ARTIFACT_SHA256,
            artifact_pointer="objstore://sanctions/bootstrap/un_1267_v1.xml",
            ingested_by=_INGESTED_BY,
        ),
        screener.ingest_list(
            list_code="BFIU_DOMESTIC",
            entries=bootstrap_bfiu_domestic_entries(),
            artifact_sha256=_BFIU_DOMESTIC_ARTIFACT_SHA256,
            artifact_pointer="objstore://sanctions/bootstrap/bfiu_domestic_v1.xml",
            ingested_by=_INGESTED_BY,
        ),
    ]
    return versions
