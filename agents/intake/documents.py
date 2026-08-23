"""Getting text out of whatever the payer actually sent.

Fax is the reality of this domain. A denial arrives as a born-digital PDF, or as
a scan of a printout, or as a photograph of a scan. Handling only the first is
handling the easy third of the problem.

The decision this module makes is narrow and it matters: whether a document
carries a usable text layer, or whether the bytes have to go to a multimodal
model as an image. Getting that wrong in the optimistic direction is the bad
one — a scanned PDF often carries a *tiny* text layer from a header stamp, and
treating that as the document means silently extracting from a fragment.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

MIN_TEXT_LAYER_CHARS = 400
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/tiff"})


@dataclass(frozen=True)
class SourceDocument:
    """The raw inbound document, before anything has interpreted it."""

    uri: str
    data: bytes
    mime_type: str

    @property
    def is_image(self) -> bool:
        return self.mime_type in IMAGE_MIME_TYPES

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == "application/pdf"

    @property
    def is_plain_text(self) -> bool:
        return self.mime_type.startswith("text/")


def extract_text_layer(document: SourceDocument) -> str | None:
    """Pull the embedded text out of a PDF, or the text out of a text file.

    Returns ``None`` when there is no text layer at all. Returns whatever is
    there when there is one, however thin — deciding whether it is *enough* is
    ``needs_ocr``'s job, not this one's.
    """
    if document.is_plain_text:
        return document.data.decode("utf-8", errors="replace")

    if not document.is_pdf:
        return None

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(document.data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        # A PDF that will not parse is not an error here. It is a document that
        # has to go to the multimodal path, which is exactly what a scan does.
        return None

    text = "\n".join(pages).strip()
    return text or None


def needs_ocr(document: SourceDocument, text: str | None) -> bool:
    """Whether the model has to look at the pixels.

    A scanned fax frequently carries a short text layer — a header stamp, a page
    number, an OCR artefact from the sending machine. Accepting that as the
    document is the failure mode worth guarding against, because it produces a
    confident extraction from a fragment rather than an obvious error.
    """
    if document.is_image:
        return True
    if text is None:
        return True
    return len(text.strip()) < MIN_TEXT_LAYER_CHARS


def as_model_part(document: SourceDocument) -> dict[str, object]:
    """The document as an inline part for a multimodal request."""
    return {"data": document.data, "mime_type": document.mime_type}
