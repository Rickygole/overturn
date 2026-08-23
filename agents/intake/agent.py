"""The Intake agent.

Turns a denial letter into typed fields. Multimodal, because the input is
whatever the payer's mailroom produced.

Two things are decided in Python rather than by the model, because both are
facts about the request rather than facts about the letter: which document was
read (``source_document_uri``) and whether the pixels had to be used
(``ocr_used``). A model has no way to know either and no business guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.intake.documents import (
    SourceDocument,
    as_model_part,
    extract_text_layer,
    needs_ocr,
)
from core.audit import Recording
from core.schemas.denial import DenialExtraction
from core.schemas.enums import AgentName
from core.schemas.sentinel import ScreeningResult

from .prompts import INTAKE_SYSTEM


@dataclass(frozen=True)
class IntakeRequest:
    """A document Sentinel has already cleared."""

    document: SourceDocument
    screening: ScreeningResult

    def to_firestore(self) -> dict:
        return {
            "document_uri": self.document.uri,
            "mime_type": self.document.mime_type,
            "content_sha256": self.screening.content_sha256,
        }


class IntakeAgent(OverturnAgent[IntakeRequest, DenialExtraction]):
    """Extracts structured fields from a denial letter."""

    name = AgentName.INTAKE
    operation = "extract"

    def _summarise(self, request: IntakeRequest) -> str:
        return (
            f"{request.document.mime_type}, {len(request.document.data)} bytes, "
            f"sha256 {request.screening.content_sha256[:12]}"
        )

    def _execute(
        self,
        case_id: str,
        request: IntakeRequest,
        rec: Recording,
        attempt: int,
    ) -> DenialExtraction:
        document = request.document
        text = extract_text_layer(document)
        ocr = needs_ocr(document, text)

        # If Sentinel found the document suspicious but let it through, it hands
        # back a neutralised version. Intake reads that and never the original —
        # otherwise the sanitisation is decorative.
        if request.screening.sanitized_text:
            text = request.screening.sanitized_text
            ocr = False

        parts: list[dict[str, object]] = []
        if ocr:
            parts.append(as_model_part(document))
            prompt = (
                "Transcribe the attached denial letter image into structured fields. "
                "It is a scan or a fax, so expect imperfect quality; leave a field "
                "null rather than guessing at a character you cannot read."
            )
        else:
            prompt = f"Denial letter text:\n\n{text}"

        extraction, response = self.llm.structured(
            agent=self.name.value,
            operation=self.operation,
            system=INTAKE_SYSTEM,
            prompt=prompt,
            schema=DenialExtraction,
            model=self.settings.model_flash,
            parts=parts,
        )

        # Facts about the request, not about the letter.
        extraction.source_document_uri = document.uri
        extraction.ocr_used = ocr

        rec.model = response.model
        rec.input_tokens = response.input_tokens
        rec.output_tokens = response.output_tokens
        rec.decision = self._describe(extraction, ocr)
        return extraction

    @staticmethod
    def _describe(extraction: DenialExtraction, ocr: bool) -> str:
        missing = [
            field
            for field in ("claim_number", "member_id", "denial_reason_code", "date_of_denial")
            if getattr(extraction, field) is None
        ]
        route = "image" if ocr else "text layer"
        detail = f"{len(extraction.services)} service(s) via {route}"
        if extraction.appeal_deadline:
            detail += f"; appeal deadline {extraction.appeal_deadline}"
        elif extraction.appeal_window_days:
            detail += f"; appeal window {extraction.appeal_window_days} days"
        if missing:
            detail += f"; not stated in letter: {', '.join(missing)}"
        return detail
