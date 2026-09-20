"""Deterministic render surface for Bangla QR assets (spec/13 render contract).

All three renders are **pure functions of their inputs** — byte-identical
across calls and across hosts:

- PNG: 1024x1024, error-correction M, quiet zone 4 modules.
- SVG: vector render of the same matrix, quiet zone 4 modules.
- kit.pdf: A5 printable stand — wordmark, QR, merchant name/name_bn,
  payload-hash footer.  PDF creation date is derived from the payload hash
  (no wall-clock timestamps — hash-stable).

Fonts are vendored under ``assets/`` (OFL-1.1, see FONTS-LICENSE.txt) so the
PDF bytes do not depend on host-installed fonts.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import segno
from fpdf import FPDF
from PIL import Image

from bdpay.qr.codec import payload_hash_of

__all__ = [
    "ERROR_CORRECTION",
    "PNG_SIZE",
    "QUIET_ZONE_MODULES",
    "content_sha256",
    "kit_pdf_subject",
    "render_kit_pdf",
    "render_png",
    "render_svg",
]

PNG_SIZE = 1024  # spec/13: PNG render is 1024x1024
QUIET_ZONE_MODULES = 4  # spec/13: quiet zone 4 modules
ERROR_CORRECTION = "m"  # spec/13: error-correction M

_ASSETS = Path(__file__).resolve().parent / "assets"
_FONT_LATIN = _ASSETS / "NotoSans-Regular.ttf"
_FONT_BENGALI = _ASSETS / "NotoSansBengali-Regular.ttf"

WORDMARK = "বাংলা কিউআর / Bangla QR"

# Creation-date epoch for kit.pdf: the hash picks a deterministic instant in
# the year after this base, so the date field is fixed per payload.
_PDF_DATE_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def content_sha256(data: bytes) -> str:
    """Hex digest for the X-BDPay-Content-Sha256 response header."""
    return sha256(data).hexdigest()


def _matrix(payload: str) -> segno.QRCode:
    return segno.make(payload, error=ERROR_CORRECTION, micro=False)


def render_png(payload: str) -> bytes:
    """1024x1024 PNG, EC-M, quiet zone 4 modules, black on white."""
    qr = _matrix(payload)
    rows = list(qr.matrix_iter(scale=1, border=QUIET_ZONE_MODULES))
    total = len(rows)  # modules incl. quiet zone; matrix is square
    scale = PNG_SIZE // total
    if scale < 1:
        raise ValueError(f"payload yields a {total}-module symbol > {PNG_SIZE}px")
    rendered = total * scale
    offset = (PNG_SIZE - rendered) // 2

    image = Image.new("1", (PNG_SIZE, PNG_SIZE), 1)  # white
    pixels = image.load()
    for y, row in enumerate(rows):
        py0 = offset + y * scale
        for x, bit in enumerate(row):
            if bit:
                px0 = offset + x * scale
                for dy in range(scale):
                    for dx in range(scale):
                        pixels[px0 + dx, py0 + dy] = 0
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_svg(payload: str) -> bytes:
    """Vector render of the same matrix — pure function of the payload."""
    qr = _matrix(payload)
    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", border=QUIET_ZONE_MODULES, scale=10, dark="#000000")
    return buffer.getvalue()


def _pdf_creation_date(payload_hash_hex: str) -> datetime:
    """Fixed creation date derived from the payload hash (spec/13)."""
    return _PDF_DATE_BASE + timedelta(seconds=int(payload_hash_hex[:8], 16) % 31_536_000)


def render_kit_pdf(payload: str, *, merchant_name: str, merchant_name_bn: str) -> bytes:
    """A5 printable merchant stand (spec/13): wordmark, QR, names, hash footer.

    The payload hash also lands in the PDF Subject metadata so the footer is
    machine-checkable without decompressing content streams.
    """
    payload_hash = payload_hash_of(payload)
    # Explicit A5 portrait mm (the "a5"-string form warns on fpdf2 >= 2.8.5)
    pdf = FPDF(format=(148, 210))
    pdf.set_creation_date(_pdf_creation_date(payload_hash))
    pdf.set_title("Bangla QR merchant kit")
    pdf.set_subject(f"sha256:{payload_hash}")
    pdf.add_font("noto", style="", fname=str(_FONT_LATIN))
    pdf.add_font("noto-bn", style="", fname=str(_FONT_BENGALI))
    pdf.set_fallback_fonts(["noto-bn"], exact_match=False)
    pdf.set_text_shaping(True)
    pdf.set_auto_page_break(False)
    pdf.add_page()

    pdf.set_font("noto", size=22)
    pdf.set_y(16)
    pdf.cell(w=0, text=WORDMARK, align="C")

    png = render_png(payload)
    qr_side = 100  # mm
    pdf.image(io.BytesIO(png), x=(148 - qr_side) / 2, y=34, w=qr_side, h=qr_side)

    pdf.set_font("noto", size=18)
    pdf.set_y(142)
    pdf.cell(w=0, text=merchant_name, align="C")
    pdf.set_y(152)
    pdf.cell(w=0, text=merchant_name_bn, align="C")

    pdf.set_font("noto", size=8)
    pdf.set_y(196)
    pdf.cell(w=0, text=f"sha256:{payload_hash}", align="C")

    return bytes(pdf.output())


def kit_pdf_subject(pdf_bytes: bytes) -> str | None:
    """Extract the Subject metadata string from kit.pdf bytes (audit helper).

    spec/13 counterfeit-sticker mitigation: the kit carries a payload-hash
    footer for spot audit; this reads it back without a PDF parser dependency.
    """
    marker = b"/Subject ("
    start = pdf_bytes.find(marker)
    if start == -1:
        return None
    end = pdf_bytes.find(b")", start)
    raw = pdf_bytes[start + len(marker):end]
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be")
    return raw.decode("latin-1")
