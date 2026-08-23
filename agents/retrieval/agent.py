"""The Retrieval agent.

Finds the policy sections that govern a denial and returns them with stable
identifiers and verbatim text. That returned set is the closed world for
everything downstream: Drafting may cite nothing outside it, and Verification
enforces that by set membership.

Two properties worth stating.

**Whole policies, not top-k sections.** Once the governing policy is identified,
every criteria-bearing section of it is returned regardless of its own rank.
Mapping has to render a verdict on each criterion, and a criteria list
truncated by similarity score is a criteria list with silent holes in it —
holes that would read downstream as "the policy did not ask for that".

**The model gets one narrow job.** The only generative call reformulates the
query, and it happens only when the first search scores below the floor. It is
given the denial reason and the service description and is shown neither the
corpus nor the previous results, so it rewrites a question rather than picking
an answer. Exactly one field of its response is read; everything else about the
result is computed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.retrieval.corpus import load_corpus
from agents.retrieval.lexical import TfidfIndex, build_index
from core.audit import Recording
from core.schemas.denial import DenialExtraction
from core.schemas.enums import AgentName
from core.schemas.policy import PolicySection, RetrievalResult, RetrievedSection

from .prompts import RETRIEVAL_REFORMULATE_SYSTEM


@dataclass(frozen=True)
class RetrievalRequest:
    """What Retrieval is given. Notably not the patient chart."""

    denial: DenialExtraction

    def to_firestore(self) -> dict:
        return {"denial": self.denial.to_firestore()}


def build_query(denial: DenialExtraction) -> str:
    """Compose the search query from the extracted denial.

    The service description and the denial reason both carry signal and they
    carry different signal: the service names the policy, the reason names the
    criterion that is disputed.
    """
    parts: list[str] = []
    for service in denial.services:
        parts.append(service.description)
        if service.procedure_code:
            parts.append(service.procedure_code)
    parts.append(denial.denial_reason_text)
    if denial.referenced_policy_hint:
        parts.append(denial.referenced_policy_hint)
    return " ".join(p for p in parts if p).strip()


def expand_to_policy(
    policy_id: str,
    index: TfidfIndex,
    query: str,
    top_score: float,
) -> list[RetrievedSection]:
    """Every section of the winning policy, scored, criteria intact."""
    per_section = {section.section_id: score for section, score in index.search(query, k=64)}
    sections = [s for s in index.sections if s.policy_id == policy_id]
    return [
        RetrievedSection(
            **section.model_dump(),
            similarity=min(1.0, per_section.get(section.section_id, 0.0) or top_score * 0.1),
            matched_query=query,
        )
        for section in sections
    ]


class RetrievalAgent(OverturnAgent[RetrievalRequest, RetrievalResult]):
    """Locates the governing policy for a denial."""

    name = AgentName.RETRIEVAL
    operation = "reformulate"

    def __init__(self, deps, index: TfidfIndex | None = None) -> None:
        super().__init__(deps)
        self._index = index or build_index()

    def _summarise(self, request: RetrievalRequest) -> str:
        codes = ",".join(s.procedure_code for s in request.denial.services if s.procedure_code)
        return f"denial reason code {request.denial.denial_reason_code or 'unstated'}; codes {codes or 'none'}"

    def _execute(
        self,
        case_id: str,
        request: RetrievalRequest,
        rec: Recording,
        attempt: int,
    ) -> RetrievalResult:
        query = build_query(request.denial)
        best = self._index.best_policy(query)
        reformulated: str | None = None

        # Reformulate only when the first search was weak. A good first
        # retrieval costs zero model calls, which is most of them.
        if best is None or best[1] < self.settings.retrieval_score_floor:
            reformulated = self._reformulate(case_id, request, query, rec)
            if reformulated:
                candidate = self._index.best_policy(reformulated)
                if candidate and (best is None or candidate[1] > best[1]):
                    best, query = candidate, reformulated

        if best is None or best[1] < self.settings.retrieval_no_policy_floor:
            rec.decision = (
                f"no policy in the corpus governs this denial "
                f"(best score {best[1]:.3f} below floor "
                f"{self.settings.retrieval_no_policy_floor})"
                if best
                else "no policy matched this denial at all"
            )
            return RetrievalResult(
                query=query,
                reformulated_query=reformulated,
                sections=[],
                top_similarity=best[1] if best else 0.0,
                no_applicable_policy=True,
            )

        policy_id, top_score = best
        sections = expand_to_policy(policy_id, self._index, query, top_score)
        rec.decision = (
            f"retrieved {policy_id} ({len(sections)} sections, "
            f"{sum(len(s.criteria) for s in sections)} criteria) at similarity "
            f"{top_score:.3f}" + (" after reformulation" if reformulated else "")
        )
        return RetrievalResult(
            query=query,
            reformulated_query=reformulated,
            sections=sections,
            top_similarity=top_score,
            no_applicable_policy=False,
        )

    def _reformulate(
        self,
        case_id: str,
        request: RetrievalRequest,
        query: str,
        rec: Recording,
    ) -> str | None:
        """Ask the model for a better query, and read exactly one field back."""
        prompt = (
            f"Original search query:\n{query}\n\n"
            f"Denial reason as stated by the payer:\n{request.denial.denial_reason_text}\n\n"
            f"Services denied:\n"
            + "\n".join(f"- {s.description}" for s in request.denial.services)
            + "\n\nRewrite the search query."
        )
        try:
            result, response = self.llm.structured(
                agent=self.name.value,
                operation=self.operation,
                system=RETRIEVAL_REFORMULATE_SYSTEM,
                prompt=prompt,
                schema=RetrievalResult,
                model=self.settings.model_flash,
            )
        except Exception as exc:  # a failed reformulation is not a failed retrieval
            rec.decision = f"reformulation unavailable ({type(exc).__name__}); used original query"
            return None

        rec.model = response.model
        rec.input_tokens = response.input_tokens
        rec.output_tokens = response.output_tokens
        # Everything except the rewritten query is discarded. A model that
        # returns a fabricated section list has no effect on the outcome.
        return (result.reformulated_query or "").strip() or None


def load_sections() -> list[PolicySection]:
    """Convenience for callers that want the corpus without an index."""
    return load_corpus()
