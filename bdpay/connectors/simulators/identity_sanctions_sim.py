"""Deterministic sanctions feed source simulator (spec/12 §G, §K).

Two pieces for ``sanctions_feed_v1``:

- :func:`build_un_consolidated_xml` — renders entry dicts into the UN Security
  Council Consolidated List XML shape the connector's ``parse_un_consolidated``
  reads (INDIVIDUALS/INDIVIDUAL with FIRST..FOURTH_NAME, INDIVIDUAL_ALIAS,
  INDIVIDUAL_DATE_OF_BIRTH, NATIONALITY/VALUE; ENTITIES/ENTITY analogous), so
  feed scenarios (normal / ``empty_file_guard`` / ``delta_shrink_50pct`` /
  unchanged re-fetch) are scripted as content, not as mocks.
- :class:`SanctionsFeedSimulatorSource` — a fake UN endpoint behind the
  ``WireTransport`` port serving a scripted artifact sequence per GET, so the
  REAL ``fetch_un_list`` wire path runs unmodified.

Everything is deterministic: artifacts are explicit byte strings; the
fixture-entry builder derives names from a fixed table.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree

from bdpay.connectors.mfs.wire import WireResponse
from bdpay.platform.clock import Clock

__all__ = [
    "SanctionsFeedSimulatorSource",
    "build_un_consolidated_xml",
    "fixture_entries",
]

#: Fixed deterministic name table for fixture entries (generic test names).
_FIXTURE_NAMES = (
    ("Abdul", "Karim", "", ""),
    ("Rahim", "Uddin", "Ahmed", ""),
    ("Selina", "Akter", "", ""),
    ("Tanvir", "Hossain", "Khan", ""),
    ("Farida", "Yasmin", "", ""),
    ("Mokbul", "Islam", "Sarker", ""),
    ("Nusrat", "Jahan", "", ""),
    ("Jashim", "Uddin", "Molla", ""),
)


def fixture_entries(count: int, *, listed_on: str = "2026-01-15") -> list[dict]:
    """``count`` deterministic INDIVIDUAL entries (name table cycles, refs
    are sequential — same input, same artifact, always)."""
    entries: list[dict] = []
    for index in range(count):
        first, second, third, fourth = _FIXTURE_NAMES[index % len(_FIXTURE_NAMES)]
        entries.append(
            {
                "entry_type": "INDIVIDUAL",
                "first_name": first,
                "second_name": second,
                "third_name": f"{third}{'' if index < len(_FIXTURE_NAMES) else index}",
                "fourth_name": fourth,
                "aliases": [],
                "dob": "1975-03-01",
                "nationality": "BD",
                "un_list_type": "Al-Qaida",
                "reference_number": f"QDi.{index + 1:03d}",
                "listed_on": listed_on,
            }
        )
    return entries


def build_un_consolidated_xml(
    individuals: list[dict],
    entities: list[dict] | None = None,
    *,
    date_generated: str = "",
) -> bytes:
    """Render the UN consolidated-list XML the feed parser reads.

    ``individuals`` dicts may carry ``first_name``/``second_name``/
    ``third_name``/``fourth_name`` (or a pre-joined ``primary_name`` whose
    words map onto those slots), ``aliases``, ``dob``, ``nationality``,
    ``un_list_type``, ``reference_number``, ``listed_on``. ``entities`` carry
    ``primary_name`` (FIRST_NAME slot) and the same trailing fields.
    ``date_generated`` sets the root ``dateGenerated`` attribute (the list's
    version stamp on the real wire) when non-empty.
    """
    root = ElementTree.Element("CONSOLIDATED_LIST")
    if date_generated:
        root.set("dateGenerated", date_generated)
    individuals_node = ElementTree.SubElement(root, "INDIVIDUALS")
    for entry in individuals:
        node = ElementTree.SubElement(individuals_node, "INDIVIDUAL")
        names = _name_slots(entry)
        for slot, value in zip(
            ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME"), names, strict=True
        ):
            if value:
                ElementTree.SubElement(node, slot).text = value
        ElementTree.SubElement(node, "UN_LIST_TYPE").text = str(
            entry.get("un_list_type", "")
        )
        ElementTree.SubElement(node, "REFERENCE_NUMBER").text = str(
            entry.get("reference_number", "")
        )
        ElementTree.SubElement(node, "LISTED_ON").text = str(entry.get("listed_on", ""))
        for alias in entry.get("aliases", []):
            alias_node = ElementTree.SubElement(node, "INDIVIDUAL_ALIAS")
            ElementTree.SubElement(alias_node, "QUALITY").text = "Good"
            ElementTree.SubElement(alias_node, "ALIAS_NAME").text = str(alias)
        if entry.get("dob"):
            dob_node = ElementTree.SubElement(node, "INDIVIDUAL_DATE_OF_BIRTH")
            ElementTree.SubElement(dob_node, "TYPE_OF_DATE").text = "EXACT"
            ElementTree.SubElement(dob_node, "DATE").text = str(entry["dob"])
        if entry.get("nationality"):
            nationality_node = ElementTree.SubElement(node, "NATIONALITY")
            ElementTree.SubElement(nationality_node, "VALUE").text = str(
                entry["nationality"]
            )
    entities_node = ElementTree.SubElement(root, "ENTITIES")
    for entry in entities or []:
        node = ElementTree.SubElement(entities_node, "ENTITY")
        ElementTree.SubElement(node, "FIRST_NAME").text = str(
            entry.get("primary_name", "")
        )
        ElementTree.SubElement(node, "UN_LIST_TYPE").text = str(
            entry.get("un_list_type", "")
        )
        ElementTree.SubElement(node, "REFERENCE_NUMBER").text = str(
            entry.get("reference_number", "")
        )
        ElementTree.SubElement(node, "LISTED_ON").text = str(entry.get("listed_on", ""))
        for alias in entry.get("aliases", []):
            alias_node = ElementTree.SubElement(node, "ENTITY_ALIAS")
            ElementTree.SubElement(alias_node, "QUALITY").text = "Good"
            ElementTree.SubElement(alias_node, "ALIAS_NAME").text = str(alias)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _name_slots(entry: dict) -> tuple[str, str, str, str]:
    if any(
        entry.get(key) for key in ("first_name", "second_name", "third_name", "fourth_name")
    ):
        return (
            str(entry.get("first_name", "")),
            str(entry.get("second_name", "")),
            str(entry.get("third_name", "")),
            str(entry.get("fourth_name", "")),
        )
    words = str(entry.get("primary_name", "")).split()
    padded = words[:3] + [" ".join(words[3:])] if len(words) > 4 else words + [""] * 4
    return (padded[0], padded[1], padded[2], padded[3])


class SanctionsFeedSimulatorSource:
    """Deterministic UN-list endpoint: serves a scripted artifact per GET.

    Script items are either raw ``bytes`` (served as ``200``) or a fully
    scripted :class:`WireResponse` (redirects, ``304`` conditional answers,
    ``5xx`` outages…), so the connector's REAL wire path — redirect
    following, ETag conditional GET, retry budget — runs unmodified against
    scripted content. Every request is recorded in :attr:`requests`
    (method/url/headers) for wire-shape assertions.
    """

    def __init__(
        self, *, clock: Clock, artifacts: list[bytes | WireResponse] | None = None
    ) -> None:
        self._clock = clock
        self._artifacts: list[bytes | WireResponse] = list(artifacts or [])
        self.fetch_count = 0
        self.requests: list[dict] = []

    def queue_artifact(self, raw: bytes | WireResponse) -> None:
        self._artifacts.append(raw)

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        self.fetch_count += 1
        self.requests.append({"method": method, "url": url, "headers": dict(headers)})
        if not self._artifacts:
            raise ConnectionError("simulated feed endpoint outage (no artifact scripted)")
        item = self._artifacts.pop(0) if len(self._artifacts) > 1 else self._artifacts[0]
        if isinstance(item, WireResponse):
            return item
        return WireResponse(status_code=200, body=item)
