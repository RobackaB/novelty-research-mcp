"""Testy extrakcie textu z PDF dokumentov."""

from __future__ import annotations

from tools.pdf_fetch import _looks_like_pdf, extract_pdf_text

PDF_TEXT = (
    "Smart door lock unlocked by a mobile application with temporary "
    "access codes entry history and owner notification support"
)


def _build_pdf(text: str) -> bytes:
    """Postaví minimálny platný jednostránkový PDF dokument s daným textom."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


def test_extract_pdf_text_reads_page_text():
    data = _build_pdf(PDF_TEXT)
    text = extract_pdf_text(data)
    assert "Smart door lock" in text
    assert "temporary" in text


def test_extract_pdf_text_rejects_non_pdf():
    assert extract_pdf_text(b"this is not a pdf document at all") == ""
    assert extract_pdf_text(b"") == ""


def test_extract_pdf_text_rejects_too_short_text():
    # Menej ako 12 slov sa nepovažuje za použiteľný obsah dokumentu.
    assert extract_pdf_text(_build_pdf("just a few words here")) == ""


def test_looks_like_pdf():
    assert _looks_like_pdf("application/pdf", b"anything")
    assert _looks_like_pdf("", b"%PDF-1.4 rest")
    assert not _looks_like_pdf("text/html", b"<html>")
