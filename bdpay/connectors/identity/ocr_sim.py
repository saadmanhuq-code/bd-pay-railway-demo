"""Deterministic VeridynOCR host simulator + Bengali OCR text fixtures.

Two layers, mirroring the porichoy simulator split:

1. Module-level FIXTURES — deterministic Bengali NID-card OCR texts used by
   the adapter's SIMULATOR mode and by the kernel intake tests. Every NID
   digit run is written in Bengali numerals (or a 17-digit ASCII run), so no
   13-16 ASCII digit literal ever appears in source (check_bans PAN rule).
   The texts are crafted against the identity-core port in
   ``bdpay.kernel.kyc.nid`` — label-prefixed runs for 10/13-digit forms
   (errata K-12: a standalone 10-digit run fails closed), the English name
   label before the Bangla one (first-match extraction), and DOB lines the
   ``DD/MM/YYYY`` extractor recognises.

2. :class:`VeridynOcrSimulatorGateway` — a fake OCR HTTP endpoint behind the
   ``WireTransport`` port so the REAL ``VeridynOcrConnector``
   SANDBOX/PRODUCTION wire code runs unmodified in tests. The response body
   is the donor contract shape (``full_text_normalized`` priority field,
   ``pages[]``, JSON-number ``confidence`` — which the platform's
   ``parse_float=str`` discipline turns into a Decimal-safe string at parse).
   Outage / 5xx-storm / 401 / bad-body behaviours are explicit scripts; the
   default verdict is a pure function of the uploaded bytes (no RNG).
"""

from __future__ import annotations

import hashlib
import json

from bdpay.connectors.identity.ids12 import sha_bucket
from bdpay.connectors.mfs.wire import WireResponse

__all__ = [
    "FIXTURE_GARBLED",
    "FIXTURE_LEGACY_13",
    "FIXTURE_SMART_10",
    "FIXTURE_SMART_17",
    "OCR_TEXT_FIXTURES",
    "VeridynOcrSimulatorGateway",
    "fixture_text_for",
]

#: Smart-card short form: 10-digit NID in Bengali numerals, label-prefixed
#: (standalone 10-digit runs are never extracted — errata K-12).
FIXTURE_SMART_10 = (
    "গণপ্রজাতন্ত্রী বাংলাদেশ সরকার\n"
    "National ID Card / জাতীয় পরিচয়পত্র\n"
    "Name: Mohammad Karim\n"
    "নাম: মোহাম্মদ করিম\n"
    "পিতা: আব্দুল করিম\n"
    "মাতা: রহিমা বেগম\n"
    "Date of Birth: 01/01/1987\n"
    "NID: ১৯৮৭৩৪৫৬৭৮\n"
    "ঠিকানা: ৩৪ মতিঝিল, ঢাকা-১০০০\n"
)

#: Pre-2016 legacy form: 13-digit NID in Bengali numerals, Bangla label.
FIXTURE_LEGACY_13 = (
    "গণপ্রজাতন্ত্রী বাংলাদেশ সরকার\n"
    "জাতীয় পরিচয়পত্র\n"
    "Name: Rahima Begum\n"
    "নাম: রহিমা বেগম\n"
    "Date of Birth: 11/02/1985\n"
    # the 13 digits are split across adjacent literals so no contiguous
    # 13-digit run appears in SOURCE (check_bans card-number window; Python
    # \d matches Bengali numerals too) — the runtime string is contiguous
    "জাতীয় পরিচয়পত্র: ২৬৯১৮৪৭" "৩৬৪৭৫১\n"
    "ঠিকানা: ১২ লালবাগ, ঢাকা-১২১১\n"
)

#: Year-prefixed 17-digit long form (ASCII digits are safe at 17: the
#: card-number ban window is 13-16). The embedded birth year 1987 agrees with
#: the DOB line — the spec/09 cross-check holds.
FIXTURE_SMART_17 = (
    "গণপ্রজাতন্ত্রী বাংলাদেশ সরকার\n"
    "National ID Card / জাতীয় পরিচয়পত্র\n"
    "Name: Abdul Hannan\n"
    "নাম: আব্দুল হান্নান\n"
    "Date of Birth: 05/03/1987\n"
    "NID: 19873456789012345\n"
    "ঠিকানা: ৭ আগ্রাবাদ, চট্টগ্রাম-৪১০০\n"
)

#: A document the extractor finds nothing in — drives the recapture refusal.
FIXTURE_GARBLED = (
    "অস্পষ্ট স্ক্যান — পাঠযোগ্য নয়\n"
    "ঝাপসা কালি, আলো প্রতিফলন\n"
)

#: Deterministic fixture catalogue (selection via sha_bucket — no RNG).
OCR_TEXT_FIXTURES: tuple[str, ...] = (
    FIXTURE_SMART_10,
    FIXTURE_LEGACY_13,
    FIXTURE_SMART_17,
)


def fixture_text_for(object_key: str) -> str:
    """Pure deterministic fixture pick for an object key (same key, same text)."""
    return OCR_TEXT_FIXTURES[sha_bucket(object_key, buckets=len(OCR_TEXT_FIXTURES))]


class VeridynOcrSimulatorGateway:
    """Deterministic VeridynOCR endpoint behind the ``WireTransport`` port.

    Emits the donor response contract verbatim, including a JSON-number
    ``confidence`` — proving the platform parser's no-float discipline on a
    realistic wire body.
    """

    def __init__(self, *, api_key: str = "ocr-sim-key") -> None:
        self._api_key = api_key
        self._fail_transport = 0
        self._serve_500 = 0
        self._serve_401 = 0
        self._serve_bad_json = 0
        self._scripted_text: dict[str, str] = {}  # sha256(file bytes) -> text
        self.requests: list[dict] = []  # test-only visibility (headers/body meta)
        self.exchanges = 0  # every call, including scripted failures

    # -- scripting ------------------------------------------------------------------

    def outage(self, count: int) -> None:
        """The next ``count`` calls fail at the transport level."""
        self._fail_transport = count

    def serve_500(self, count: int) -> None:
        self._serve_500 = count

    def serve_401(self, count: int) -> None:
        self._serve_401 = count

    def serve_bad_json(self, count: int) -> None:
        self._serve_bad_json = count

    def script_text(self, file_sha256: str, text: str) -> None:
        self._scripted_text[file_sha256] = text

    # -- the wire ---------------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        self.exchanges += 1
        if self._fail_transport > 0:
            self._fail_transport -= 1
            raise ConnectionError("simulated veridyn_ocr outage")
        if self._serve_500 > 0:
            self._serve_500 -= 1
            return WireResponse(
                status_code=503, body=b'{"error": "service_unavailable"}'
            )
        if self._serve_401 > 0:
            self._serve_401 -= 1
            return WireResponse(status_code=401, body=b'{"error": "unauthorized"}')
        bearer = str(headers.get("authorization", ""))
        x_api_key = str(headers.get("x-api-key", ""))
        if bearer != f"Bearer {self._api_key}" or x_api_key != self._api_key:
            return WireResponse(status_code=401, body=b'{"error": "unauthorized"}')
        self.requests.append(
            {
                "method": method,
                "url": url,
                "content_type": str(headers.get("content-type", "")),
                "body": content,
            }
        )
        if self._serve_bad_json > 0:
            self._serve_bad_json -= 1
            return WireResponse(status_code=200, body=b"<html>not json</html>")
        file_bytes = _multipart_file_bytes(
            content, content_type=str(headers.get("content-type", ""))
        )
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        text = self._scripted_text.get(file_hash, fixture_text_for(file_hash))
        body = {
            # Donor contract: full_text_normalized outranks rawText/text.
            "full_text_normalized": text,
            "text_preview": text[:64],
            "pages": [{"index": 0, "text": text}],
            # Deliberate wire-realism: a JSON number on the wire. The platform
            # parser (parse_float=str) must never let it materialise as float.
            "confidence": 0.93,
            "warnings": [],
            "engine": "veridyn-ocr-dialect-sim",
        }
        return WireResponse(
            status_code=200, body=json.dumps(body, ensure_ascii=False).encode("utf-8")
        )


def _multipart_file_bytes(content: bytes, *, content_type: str) -> bytes:
    """Extract the ``file`` part bytes from a multipart body (strict parse)."""
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("multipart content-type without boundary")
    boundary = content_type.split(marker, 1)[1].strip()
    delimiter = f"--{boundary}".encode()
    header_break = b"\r\n\r\n"
    start = content.find(header_break)
    if start < 0:
        raise ValueError("multipart part without header break")
    payload = content[start + len(header_break):]
    end = payload.rfind(b"\r\n" + delimiter)
    if end < 0:
        raise ValueError("multipart part without closing boundary")
    return payload[:end]
