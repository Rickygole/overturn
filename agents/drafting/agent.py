"""The Drafting agent.

The one call in the pipeline where output quality is the product. Everything
else extracts, matches, or checks; this writes the thing a person reads.

It is also the most confined agent in the fleet, and deliberately so. It
receives a :class:`DraftingBrief` and nothing else — no retrieval result, no
chart, no store handle. Its signature will not accept them.
"""

from __future__ import annotations

from agents.base import OverturnAgent
from agents.drafting.brief import DraftingBrief, render
from agents.drafting.letter import compose_letter
from core.audit import Recording
from core.schemas.draft import AppealDraft
from core.schemas.enums import AgentName

from .prompts import DRAFTING_SYSTEM, SABOTAGE_SUFFIX


class DraftingAgent(OverturnAgent[DraftingBrief, AppealDraft]):
    """Writes the appeal letter."""

    name = AgentName.DRAFTING
    operation = "compose"

    def _summarise(self, request: DraftingBrief) -> str:
        return (
            f"attempt {request.attempt}, {len(request.verdicts)} satisfied criteria, "
            f"{len(request.revision_instructions)} revision instruction(s)"
        )

    def _execute(
        self,
        case_id: str,
        request: DraftingBrief,
        rec: Recording,
        attempt: int,
    ) -> AppealDraft:
        system = DRAFTING_SYSTEM
        if self.settings.sabotage_drafting_on(request.attempt):
            # Loud on purpose. A fault injection that is not obvious in the
            # audit trail is indistinguishable from a real defect later.
            system += SABOTAGE_SUFFIX
            rec.extra["fault_injection"] = f"sabotage enabled for attempt {request.attempt}"

        draft, response = self.llm.structured(
            agent=self.name.value,
            operation=self.operation,
            system=system,
            prompt=render(request),
            schema=AppealDraft,
            model=self.settings.model_heavy,
            temperature=0.2,
        )

        # Identity of the draft is a fact about the request, not the model's to
        # decide. A draft that mislabels its own attempt number breaks the
        # approval gate, which pins the approved draft by attempt.
        draft.case_id = case_id
        draft.attempt = request.attempt
        draft.model_used = response.model
        draft.backend_used = response.backend
        draft.revision_feedback_applied = list(request.revision_instructions)

        # The model wrote an argument. A letter is an argument inside a date
        # line, an addressee, a reference block and a signature — none of which
        # a model should be composing, because every one of them is a field on
        # the case record and one of them is an NPI. Assembled in code, from the
        # record, with an explicit gap wherever the record is silent.
        #
        # Nothing added here enters `citations` or `clinical_assertions`, so
        # Verification's world is unchanged: a letterhead is not a claim about
        # the patient and must not be presented to the verifier as one.
        letter = compose_letter(request, draft.body)
        draft.body = letter.body

        rec.model = response.model
        rec.input_tokens = response.input_tokens
        rec.output_tokens = response.output_tokens

        uncitable = draft.cited_ids() - request.citable_ids
        rec.decision = (
            f"attempt {request.attempt}: {len(draft.citations)} citation(s), "
            f"{len(draft.clinical_assertions)} clinical assertion(s), "
            f"{len(draft.body)} characters"
        )
        # Named in the persisted line, not counted and not left in
        # ``rec.extra`` — which no ``AuditEvent`` field carries, so nothing put
        # there survives the write. "What did this system decline to know" is
        # the question the whole project turns on, and it should be answerable
        # from the audit trail without reading the prose.
        if letter.gaps:
            rec.decision += f"; letter gaps for a human to fill: {', '.join(letter.gaps)}"
        rec.extra["letter_gaps"] = list(letter.gaps)
        if uncitable:
            # Recorded, not corrected. Verification is what rejects it, and
            # quietly stripping it here would hide the failure the retry loop
            # exists to surface.
            rec.decision += f"; cites {len(uncitable)} identifier(s) not in the brief"
            rec.extra["uncitable_ids"] = sorted(uncitable)
        return draft
