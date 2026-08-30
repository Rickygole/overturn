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

import logging
from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.retrieval.calibration import PolicyMargin, describe_decline, describe_match
from agents.retrieval.corpus import load_corpus
from agents.retrieval.lexical import TfidfIndex, build_index
from core.audit import Recording
from core.schemas.denial import DenialExtraction
from core.schemas.enums import AgentName
from core.schemas.policy import PolicySection, RetrievalResult, RetrievedSection

from .prompts import RETRIEVAL_REFORMULATE_SYSTEM

logger = logging.getLogger(__name__)


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


def rank_with_margin(index: TfidfIndex, query: str) -> PolicyMargin | None:
    """Rank the corpus and keep the runner-up attached to the winner.

    The agent needs the winner to act. The audit trail needs the runner-up to
    be readable, and the two must come from the same ranking call or they can
    disagree. They did, once, in `scripts/calibrate_retrieval.py`.
    """
    ranked = index.rank_policies(query)
    if not ranked:
        return None
    policy_id, score = ranked[0]
    runner_up_id, runner_up_score = ranked[1] if len(ranked) > 1 else (None, 0.0)
    return PolicyMargin(
        policy_id=policy_id,
        score=score,
        runner_up_id=runner_up_id,
        runner_up_score=runner_up_score,
    )


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
        first_pass = rank_with_margin(self._index, query)
        best = first_pass
        reformulated: str | None = None
        floor = self.settings.retrieval_score_floor

        # Reformulate only when the first search was weak. A good first
        # retrieval costs zero model calls, which is most of them.
        #
        # "Weak" here means "below `retrieval_score_floor`", and that floor is
        # set deliberately just above the weakest known-correct match so this
        # path is exercised rather than being dead code. Tripping it therefore
        # does NOT mean the first search failed -- on the measured cases it
        # trips on matches that were already correct and already ranked first.
        # The audit line below has to say which of those two happened, because
        # a log that prints only the final score makes them indistinguishable,
        # and a reader who assumed the worse one was reading a defect that was
        # not there.
        if first_pass is None or first_pass.score < floor:
            reformulated, failure = self._reformulate(case_id, request, query, rec)
            candidate = (
                rank_with_margin(self._index, reformulated) if reformulated else None
            )
            adopted = candidate is not None and (
                first_pass is None or candidate.score > first_pass.score
            )
            if adopted and candidate is not None:
                best, query = candidate, reformulated
            note = self._reformulation_note(first_pass, candidate, failure, adopted, floor)
        else:
            note = (
                f"first-pass query scored above the {floor:.2f} reformulate floor; "
                f"no model call was made"
            )

        if best is None or best.score < self.settings.retrieval_no_policy_floor:
            rec.decision = (
                f"{describe_decline(best, self.settings.retrieval_no_policy_floor)}; {note}"
            )
            return RetrievalResult(
                query=query,
                reformulated_query=reformulated,
                sections=[],
                top_similarity=best.score if best else 0.0,
                no_applicable_policy=True,
            )

        sections = expand_to_policy(best.policy_id, self._index, query, best.score)
        rec.decision = (
            f"retrieved {best.policy_id} ({len(sections)} sections, "
            f"{sum(len(s.criteria) for s in sections)} criteria); "
            f"{describe_match(best, self.settings.retrieval_no_policy_floor)}; {note}"
        )
        return RetrievalResult(
            query=query,
            reformulated_query=reformulated,
            sections=sections,
            top_similarity=best.score,
            no_applicable_policy=False,
        )

    @staticmethod
    def _reformulation_note(
        first_pass: PolicyMargin | None,
        candidate: PolicyMargin | None,
        failure: str | None,
        adopted: bool,
        floor: float,
    ) -> str:
        """Say what the second query did, in terms that distinguish the cases.

        "after reformulation" was the old wording and it was fired whenever the
        model returned a non-empty string -- including when the rewrite scored
        worse and was thrown away. So the trail read "similarity 0.092 after
        reformulation" on a run where 0.092 *was the first query's score* and the
        rewrite had been discarded. That is the exact opposite of what it says,
        and it was read, correctly given the words, as a failed first attempt
        rescued by a lucky retry.
        """
        trip = f"first-pass query scored below the {floor:.2f} reformulate floor"
        if failure:
            return f"{trip}; reformulation unavailable ({failure}), so this is the first-pass result"
        if candidate is None:
            return f"{trip}; reformulation returned no usable query, so this is the first-pass result"
        if not adopted:
            return (
                f"{trip}; the rewrite scored no better ({candidate.score:.3f} vs "
                f"{first_pass.score:.3f}) and was discarded, so this is the first-pass result"
                if first_pass
                else f"{trip}; the rewrite was discarded"
            )
        if first_pass is None:
            return f"{trip} (nothing matched at all); the rewrite found {candidate.policy_id}"
        if first_pass.policy_id == candidate.policy_id:
            return (
                f"{trip}; the first-pass query had already ranked {candidate.policy_id} "
                f"first, and the rewrite confirmed it at a higher score "
                f"({first_pass.score:.3f} -> {candidate.score:.3f})"
            )
        return (
            f"{trip}; the rewrite changed the winning policy from "
            f"{first_pass.policy_id} ({first_pass.score:.3f}) to "
            f"{candidate.policy_id} ({candidate.score:.3f})"
        )

    def _reformulate(
        self,
        case_id: str,
        request: RetrievalRequest,
        query: str,
        rec: Recording,
    ) -> tuple[str | None, str | None]:
        """Ask the model for a better query, and read exactly one field back.

        Returns ``(rewritten_query, failure_reason)``. It used to write the
        failure straight onto ``rec.decision`` and return ``None``, and
        ``_execute`` then overwrote that line unconditionally on its way out --
        so a reformulation that raised was invisible in the audit trail. The
        caller now folds the reason into the line it actually persists. ``rec``
        is still passed in, but only for token telemetry.
        """
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
            logger.warning(
                "reformulation failed for %s, continuing on the first-pass query: %s",
                case_id,
                exc,
                exc_info=True,
            )
            return None, type(exc).__name__

        rec.model = response.model
        rec.input_tokens = response.input_tokens
        rec.output_tokens = response.output_tokens
        # Everything except the rewritten query is discarded. A model that
        # returns a fabricated section list has no effect on the outcome.
        return (result.reformulated_query or "").strip() or None, None


def load_sections() -> list[PolicySection]:
    """Convenience for callers that want the corpus without an index."""
    return load_corpus()
