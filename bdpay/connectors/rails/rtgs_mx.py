"""ISO 20022 MX build/parse for BD-RTGS (spec/11 §C) — stdlib ElementTree.

pacs.008.001.08 build, pacs.002.001.10 / camt.054.001.08 parse, plus the
fail-closed structural validator that stands in front of every transmit and
every ingest. Per the lane build rules the codec uses ``xml.etree`` (no lxml);
the spec's pinned-XSD validation is realized as a deterministic structural
check over the binding element table of §C.1 — every required path present,
amount shape exact, currency BDT — and full lxml XSD validation against the
official ISO 20022 XSDs stored in tests/fixtures/official/iso20022/ activates
in CERT-R4 tests (SPEC_ERRATA-LANE-B LB12). Defense parity with the
spec's defusedxml requirement: any document carrying a DTD or entity
declaration is refused BEFORE parsing (no external entities, fail-closed).

The single sanctioned integer->decimal boundary for RTGS lives here:
``paisa_to_decimal_str`` / ``decimal_str_to_paisa``. No float is ever
constructed (CERT-09).

Namespace note (SPEC_ERRATA-LANE-B LB12): the official ISO 20022 catalogue
publishes ``urn:iso:std:iso:20022:tech:xsd:<message-id>`` as the canonical
target namespace for message version .08 / .10.  The earlier
``urn:iso:20022:tech:xsd:`` form (without ``std:iso:``) originates from
pre-2013 SWIFT publications and is not valid against the pinned XSDs.
All three namespace constants below use the official ``urn:iso:std:iso:``
prefix (schema wins; errata LB12).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

__all__ = [
    "MxValidationError",
    "NS_CAMT054",
    "NS_PACS002",
    "NS_PACS008",
    "SIM_USTRD_PREFIX",
    "build_camt054",
    "build_pacs002",
    "build_pacs008",
    "decimal_str_to_paisa",
    "extract_camt054",
    "extract_pacs002",
    "extract_pacs008",
    "paisa_to_decimal_str",
    "validate_pacs008",
]

# Official ISO 20022 catalogue namespaces (urn:iso:std:iso:20022:tech:xsd:*).
# Pinned to the XSD versions stored in tests/fixtures/official/iso20022/.
# SPEC_ERRATA-LANE-B LB12: the earlier urn:iso:20022:tech:xsd: prefix (without
# std:iso:) does not match the official XSDs; schema wins.
NS_PACS008 = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"
NS_PACS002 = "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10"
NS_CAMT054 = "urn:iso:std:iso:20022:tech:xsd:camt.054.001.08"

#: The SIMULATOR-mode scenario hint marker (second RmtInf/Ustrd element).
SIM_USTRD_PREFIX = "SIM:"


class MxValidationError(ValueError):
    """Structural refusal — the message is never transmitted/accepted."""


def paisa_to_decimal_str(amount_minor: int) -> str:
    """Integer paisa -> wire decimal (the ONLY sanctioned conversion, C.1)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise MxValidationError("amount_minor must be int paisa")
    if amount_minor < 0:
        raise MxValidationError("amount_minor must be non-negative")
    return f"{amount_minor // 100}.{amount_minor % 100:02d}"


def decimal_str_to_paisa(text: str) -> int:
    """Wire decimal -> integer paisa; any other shape is rejected."""
    if not isinstance(text, str):
        raise MxValidationError("amount text must be str")
    whole, dot, frac = text.partition(".")
    if dot != "." or len(frac) != 2 or not whole.isdigit() or not frac.isdigit():
        raise MxValidationError(f"amount {text!r} is not a 2-fraction-digit decimal")
    return int(whole) * 100 + int(frac)


def _refuse_dtd(xml_bytes: bytes) -> None:
    """No DTDs, no entity declarations — fail-closed (C.3 hardening)."""
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        raise MxValidationError("DTD/entity declarations are refused (fail-closed)")


def _parse(xml_bytes: bytes) -> ET.Element:
    _refuse_dtd(xml_bytes)
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise MxValidationError(f"XML is not well-formed: {exc}") from exc


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pacs008(
    *,
    msg_id: str,
    end_to_end_id: str,
    amount_minor: int,
    debtor_name: str,
    debtor_account: str,
    debtor_agent_bic: str,
    creditor_agent_bic: str,
    creditor_name: str,
    creditor_account: str,
    remittance_info: str,
    created_at: datetime,
    sim_scenario: str | None = None,
) -> bytes:
    """Build a pacs.008.001.08 per the binding element table (C.1)."""
    if len(msg_id) > 35 or not msg_id:
        raise MxValidationError("MsgId must be 1..35 chars")
    ET.register_namespace("", NS_PACS008)
    ns = f"{{{NS_PACS008}}}"
    document = ET.Element(f"{ns}Document")
    root = ET.SubElement(document, f"{ns}FIToFICstmrCdtTrf")
    grp = ET.SubElement(root, f"{ns}GrpHdr")
    ET.SubElement(grp, f"{ns}MsgId").text = msg_id
    ET.SubElement(grp, f"{ns}CreDtTm").text = _rfc3339(created_at)
    ET.SubElement(grp, f"{ns}NbOfTxs").text = "1"
    sttlm = ET.SubElement(grp, f"{ns}SttlmInf")
    ET.SubElement(sttlm, f"{ns}SttlmMtd").text = "CLRG"
    tx = ET.SubElement(root, f"{ns}CdtTrfTxInf")
    pmt = ET.SubElement(tx, f"{ns}PmtId")
    ET.SubElement(pmt, f"{ns}EndToEndId").text = end_to_end_id
    ET.SubElement(pmt, f"{ns}TxId").text = msg_id
    amt = ET.SubElement(tx, f"{ns}IntrBkSttlmAmt", Ccy="BDT")
    amt.text = paisa_to_decimal_str(amount_minor)
    ET.SubElement(tx, f"{ns}ChrgBr").text = "SLEV"
    dbtr = ET.SubElement(tx, f"{ns}Dbtr")
    ET.SubElement(dbtr, f"{ns}Nm").text = debtor_name
    dbtr_acct = ET.SubElement(ET.SubElement(ET.SubElement(
        tx, f"{ns}DbtrAcct"), f"{ns}Id"), f"{ns}Othr")
    ET.SubElement(dbtr_acct, f"{ns}Id").text = debtor_account
    dbtr_agt = ET.SubElement(ET.SubElement(ET.SubElement(
        tx, f"{ns}DbtrAgt"), f"{ns}FinInstnId"), f"{ns}Othr")
    ET.SubElement(dbtr_agt, f"{ns}Id").text = debtor_agent_bic
    cdtr_agt = ET.SubElement(ET.SubElement(ET.SubElement(
        tx, f"{ns}CdtrAgt"), f"{ns}FinInstnId"), f"{ns}Othr")
    ET.SubElement(cdtr_agt, f"{ns}Id").text = creditor_agent_bic
    cdtr = ET.SubElement(tx, f"{ns}Cdtr")
    ET.SubElement(cdtr, f"{ns}Nm").text = creditor_name
    cdtr_acct = ET.SubElement(ET.SubElement(ET.SubElement(
        tx, f"{ns}CdtrAcct"), f"{ns}Id"), f"{ns}Othr")
    ET.SubElement(cdtr_acct, f"{ns}Id").text = creditor_account
    rmt = ET.SubElement(tx, f"{ns}RmtInf")
    ET.SubElement(rmt, f"{ns}Ustrd").text = remittance_info
    if sim_scenario:
        ET.SubElement(rmt, f"{ns}Ustrd").text = f"{SIM_USTRD_PREFIX}{sim_scenario}"
    return ET.tostring(document, encoding="utf-8", xml_declaration=True)


#: Required pacs.008 paths (relative to FIToFICstmrCdtTrf) — the C.1 table.
_PACS008_REQUIRED = (
    "GrpHdr/MsgId",
    "GrpHdr/CreDtTm",
    "GrpHdr/NbOfTxs",
    "GrpHdr/SttlmInf/SttlmMtd",
    "CdtTrfTxInf/PmtId/EndToEndId",
    "CdtTrfTxInf/PmtId/TxId",
    "CdtTrfTxInf/IntrBkSttlmAmt",
    "CdtTrfTxInf/ChrgBr",
    "CdtTrfTxInf/Dbtr",
    "CdtTrfTxInf/DbtrAcct/Id/Othr/Id",
    "CdtTrfTxInf/DbtrAgt/FinInstnId/Othr/Id",
    "CdtTrfTxInf/CdtrAgt/FinInstnId/Othr/Id",
    "CdtTrfTxInf/Cdtr",
    "CdtTrfTxInf/CdtrAcct/Id/Othr/Id",
    "CdtTrfTxInf/RmtInf/Ustrd",
)


def validate_pacs008(xml_bytes: bytes) -> None:
    """Structural fail-closed validation; raises MxValidationError on refusal."""
    document = _parse(xml_bytes)
    ns = {"d": NS_PACS008}
    root = document.find("d:FIToFICstmrCdtTrf", ns)
    if root is None:
        raise MxValidationError("missing FIToFICstmrCdtTrf")
    for path in _PACS008_REQUIRED:
        rel = "/".join(f"d:{part}" for part in path.split("/"))
        if root.find(rel, ns) is None:
            raise MxValidationError(f"missing required element {path}")
    msg_id = root.findtext("d:GrpHdr/d:MsgId", default="", namespaces=ns)
    if not msg_id or len(msg_id) > 35:
        raise MxValidationError("MsgId must be 1..35 chars")
    if root.findtext("d:GrpHdr/d:NbOfTxs", default="", namespaces=ns) != "1":
        raise MxValidationError("NbOfTxs must be 1 (single-txn policy v1)")
    amount = root.find("d:CdtTrfTxInf/d:IntrBkSttlmAmt", ns)
    if amount is None or amount.get("Ccy") != "BDT":
        raise MxValidationError("IntrBkSttlmAmt must carry Ccy=BDT")
    decimal_str_to_paisa(amount.text or "")  # shape-validates the amount


def extract_pacs008(xml_bytes: bytes) -> dict:
    """Parse a (validated) pacs.008 into the fields the rail needs."""
    document = _parse(xml_bytes)
    ns = {"d": NS_PACS008}
    root = document.find("d:FIToFICstmrCdtTrf", ns)
    if root is None:
        raise MxValidationError("missing FIToFICstmrCdtTrf")
    sim_scenario = None
    for ustrd in root.findall("d:CdtTrfTxInf/d:RmtInf/d:Ustrd", ns):
        text = ustrd.text or ""
        if text.startswith(SIM_USTRD_PREFIX):
            sim_scenario = text[len(SIM_USTRD_PREFIX) :]
    return {
        "msg_id": root.findtext("d:GrpHdr/d:MsgId", default="", namespaces=ns),
        "end_to_end_id": root.findtext(
            "d:CdtTrfTxInf/d:PmtId/d:EndToEndId", default="", namespaces=ns
        ),
        "amount": root.findtext(
            "d:CdtTrfTxInf/d:IntrBkSttlmAmt", default="", namespaces=ns
        ),
        "sim_scenario": sim_scenario,
    }


def build_pacs002(
    *,
    original_msg_id: str,
    end_to_end_id: str,
    tx_status: str,
    reason_code: str | None,
    created_at: datetime,
) -> bytes:
    """Build a pacs.002.001.10 status report (rail -> participant)."""
    ET.register_namespace("", NS_PACS002)
    ns = f"{{{NS_PACS002}}}"
    document = ET.Element(f"{ns}Document")
    root = ET.SubElement(document, f"{ns}FIToFIPmtStsRpt")
    grp = ET.SubElement(root, f"{ns}GrpHdr")
    ET.SubElement(grp, f"{ns}MsgId").text = f"STS{original_msg_id[:32]}"
    ET.SubElement(grp, f"{ns}CreDtTm").text = _rfc3339(created_at)
    tx = ET.SubElement(root, f"{ns}TxInfAndSts")
    ET.SubElement(tx, f"{ns}OrgnlEndToEndId").text = end_to_end_id
    ET.SubElement(tx, f"{ns}TxSts").text = tx_status
    if reason_code:
        rsn = ET.SubElement(ET.SubElement(tx, f"{ns}StsRsnInf"), f"{ns}Rsn")
        ET.SubElement(rsn, f"{ns}Cd").text = reason_code
    return ET.tostring(document, encoding="utf-8", xml_declaration=True)


def extract_pacs002(xml_bytes: bytes) -> dict:
    document = _parse(xml_bytes)
    ns = {"d": NS_PACS002}
    root = document.find("d:FIToFIPmtStsRpt", ns)
    if root is None:
        raise MxValidationError("missing FIToFIPmtStsRpt")
    tx = root.find("d:TxInfAndSts", ns)
    if tx is None:
        raise MxValidationError("missing TxInfAndSts")
    status = tx.findtext("d:TxSts", default="", namespaces=ns)
    if not status:
        raise MxValidationError("missing TxSts")
    return {
        "end_to_end_id": tx.findtext("d:OrgnlEndToEndId", default="", namespaces=ns),
        "tx_status": status,
        "reason_code": tx.findtext("d:StsRsnInf/d:Rsn/d:Cd", default=None, namespaces=ns),
    }


def build_camt054(*, end_to_end_id: str, amount: str, created_at: datetime) -> bytes:
    """Build a camt.054.001.08 credit notification (rail -> participant)."""
    ET.register_namespace("", NS_CAMT054)
    ns = f"{{{NS_CAMT054}}}"
    document = ET.Element(f"{ns}Document")
    root = ET.SubElement(document, f"{ns}BkToCstmrDbtCdtNtfctn")
    grp = ET.SubElement(root, f"{ns}GrpHdr")
    ET.SubElement(grp, f"{ns}MsgId").text = f"CAMT{end_to_end_id[:31]}"
    ET.SubElement(grp, f"{ns}CreDtTm").text = _rfc3339(created_at)
    ntfctn = ET.SubElement(root, f"{ns}Ntfctn")
    ntry = ET.SubElement(ntfctn, f"{ns}Ntry")
    amt = ET.SubElement(ntry, f"{ns}Amt", Ccy="BDT")
    amt.text = amount
    ET.SubElement(ntry, f"{ns}CdtDbtInd").text = "CRDT"
    refs = ET.SubElement(ET.SubElement(ET.SubElement(
        ntry, f"{ns}NtryDtls"), f"{ns}TxDtls"), f"{ns}Refs")
    ET.SubElement(refs, f"{ns}EndToEndId").text = end_to_end_id
    return ET.tostring(document, encoding="utf-8", xml_declaration=True)


def extract_camt054(xml_bytes: bytes) -> dict:
    document = _parse(xml_bytes)
    ns = {"d": NS_CAMT054}
    root = document.find("d:BkToCstmrDbtCdtNtfctn", ns)
    if root is None:
        raise MxValidationError("missing BkToCstmrDbtCdtNtfctn")
    end_to_end = root.findtext(
        "d:Ntfctn/d:Ntry/d:NtryDtls/d:TxDtls/d:Refs/d:EndToEndId",
        default="",
        namespaces=ns,
    )
    if not end_to_end:
        raise MxValidationError("camt.054 entry missing EndToEndId")
    amount = root.find("d:Ntfctn/d:Ntry/d:Amt", ns)
    if amount is None or amount.get("Ccy") != "BDT":
        raise MxValidationError("camt.054 entry missing BDT amount")
    decimal_str_to_paisa(amount.text or "")
    return {"end_to_end_id": end_to_end, "amount": amount.text}
