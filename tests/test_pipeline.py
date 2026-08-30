"""End-to-end tests over the whole fleet.

Every other test file checks a mechanism. This one checks the product: given a
denial letter and a chart, does the system reach the right conclusion, and does
it refuse to reach a wrong one.

These run offline against the scripted backend, so they are deterministic and
free, and they exercise the real orchestrator, the real retry loop, the real
idempotency guard, the real gateway and the real validation. Only the generative
calls are answered locally.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator import effects
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.audit import read_case_trail
from core.gateway import Access, GatewayHandle, PolicyViolation
from core.schemas.enums import ActionType, AgentName, CaseStatus, CriterionVerdictValue
from core.schemas.sentinel import ScreeningResult
from core.store import MemoryStore

DENIALS = Path(__file__).resolve().parents[1] / "data" / "denials"


def _document(case_id: str) -> SourceDocument:
    return SourceDocument(
        uri=f"gs://overturn-intake/{case_id}.txt",
        data=(DENIALS / f"{case_id}.txt").read_bytes(),
        mime_type="text/plain",
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def pipeline(store: MemoryStore) -> Pipeline:
    return Pipeline(build_fleet(store=store, llm=build_offline_llm()))


def run(pipeline: Pipeline, case_id: str):
    return pipeline.ingest(_document(case_id), case_id=case_id)


class TestCleanCase:
    """CASE-001: the chart plainly documents everything the policy asks for."""

    def test_reaches_the_human_gate(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert case.status is CaseStatus.AWAITING_APPROVAL

    def test_retrieves_the_governing_policy(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert {s.policy_id for s in case.retrieval.sections} == {"NBH-ENDO-031"}

    def test_every_satisfied_criterion_cites_a_real_chart_location(self, pipeline):
        case = run(pipeline, "CASE-001")
        from agents.mapping.charts import load_chart

        locators = load_chart("CASE-001").locators()
        for verdict in case.criteria.verdicts:
            for evidence in verdict.evidence:
                assert evidence.locator in locators, (
                    f"{verdict.criterion_id} cites {evidence.locator}, which is not in the chart"
                )

    def test_the_draft_cites_only_real_policy_sections(self, pipeline):
        case = run(pipeline, "CASE-001")
        known = case.retrieval.section_ids()
        assert case.latest_draft.cited_ids() <= known

    def test_verification_passed_on_the_first_attempt(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert len(case.drafts) == 1
        assert case.latest_verification.passed is True

    def test_nothing_is_submitted_without_signatures(self, pipeline, store):
        """The whole point of the gate."""
        run(pipeline, "CASE-001")
        submissions = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert submissions == []


class TestInjectionCase:
    """CASE-002: the letter carries an instruction payload."""

    def test_the_document_is_quarantined(self, pipeline):
        case = run(pipeline, "CASE-002")
        assert case.status is CaseStatus.QUARANTINED

    def test_intake_never_runs(self, pipeline, store):
        """The pipeline must halt before anything extracts from the document."""
        run(pipeline, "CASE-002")
        agents_that_ran = {e.agent for e in read_case_trail(store, "CASE-002")}
        assert AgentName.SENTINEL in agents_that_ran
        assert AgentName.INTAKE not in agents_that_ran
        assert AgentName.DRAFTING not in agents_that_ran

    def test_no_denial_is_extracted(self, pipeline):
        case = run(pipeline, "CASE-002")
        assert case.denial is None
        assert case.drafts == []

    def test_the_quarantine_is_recorded_for_review(self, pipeline, store):
        case = run(pipeline, "CASE-002")
        record = store.get("quarantine", case.source_sha256)
        assert record is not None
        assert len(record["findings"]) >= 3

    def test_a_human_is_told(self, pipeline, store):
        run(pipeline, "CASE-002")
        notifications = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.NOTIFY_HUMAN.value
        ]
        assert len(notifications) == 1


class TestNoSecondDoor:
    """`agents/orchestrator/effects.py` used to call `fleet.store.set` directly,
    which bypassed `core/gateway.py` entirely -- the one file in this codebase
    whose "no second door" claim is load-bearing for everything else it says
    about the boundary being real rather than described. Worse, the bypassed
    check would have refused the write it was skipping: `POLICY` grants the
    orchestrator only `Access.READ` on `quarantine`, so a raw write succeeding
    under the orchestrator's own name was never a coincidence -- it was the
    second door.

    These two tests pin the fix from both directions: the real pipeline run
    actually calls through the gateway with the right identity, and a handle
    that lacks the grant is refused rather than silently allowed through.
    """

    def test_the_quarantine_write_is_authorized_through_the_gateway(self, pipeline, monkeypatch):
        """A spy on `GatewayHandle.authorize`, not a mock of the write itself.

        If `quarantine_document` ever goes back to writing straight to
        `fleet.store`, this call never happens and the assertion below fails
        -- that is the regression this test exists to catch.
        """
        calls: list[tuple[AgentName, str, Access]] = []
        original = GatewayHandle.authorize

        def spy(self, collection, access):
            calls.append((self.agent, collection, access))
            return original(self, collection, access)

        monkeypatch.setattr(GatewayHandle, "authorize", spy)
        run(pipeline, "CASE-002")

        assert (AgentName.SENTINEL, "quarantine", Access.WRITE) in calls, (
            "the quarantine write did not go through GatewayHandle.authorize "
            "as the Sentinel identity that actually holds the grant"
        )

    def test_the_quarantine_write_is_refused_under_an_identity_without_the_grant(self, store):
        """Verification holds no grant on `quarantine` (`core/gateway.py`).

        Routed through Verification's handle instead of Sentinel's, the same
        write must raise `PolicyViolation` rather than land -- proof the check
        is actually in the path, not merely available to it.
        """
        fake_fleet = SimpleNamespace(
            store=store,
            sentinel=SimpleNamespace(
                deps=SimpleNamespace(gateway=GatewayHandle(AgentName.VERIFICATION))
            ),
        )
        screening = ScreeningResult(
            document_uri="gs://overturn-intake/CASE-XYZ.txt",
            content_sha256="deadbeef",
            quarantine=True,
        )
        with pytest.raises(PolicyViolation):
            effects.quarantine_document(fake_fleet, "CASE-XYZ", screening)
        assert store.get("quarantine", "deadbeef") is None, (
            "a refused authorize() call must not be followed by the write anyway"
        )


class TestUndocumentedCriterion:
    """CASE-003: most criteria met, one genuinely not documented.

    The chart notes a pacemaker and never says whether it is MRI-conditional.
    The correct behaviour is to mark that criterion insufficient and write the
    appeal without it — not to argue around it.
    """

    def test_the_unresolved_criterion_is_not_claimed_as_satisfied(self, pipeline):
        case = run(pipeline, "CASE-003")
        contraindication = next(
            v for v in case.criteria.verdicts if v.criterion_id == "NBH-CARD-014-3.5"
        )
        assert contraindication.verdict is CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION

    def test_drafting_never_sees_the_unmet_criterion(self, pipeline):
        case = run(pipeline, "CASE-003")
        appealable = {v.criterion_id for v in case.criteria.appealable_verdicts()}
        assert "NBH-CARD-014-3.5" not in appealable

    def test_the_letter_does_not_cite_it(self, pipeline):
        case = run(pipeline, "CASE-003")
        assert "NBH-CARD-014-3.5" not in case.latest_draft.cited_ids()

    def test_the_appeal_still_goes_forward_on_what_is_documented(self, pipeline):
        case = run(pipeline, "CASE-003")
        assert case.status is CaseStatus.AWAITING_APPROVAL
        assert len(case.latest_draft.citations) >= 4


class TestFaultInjection:
    """Verification catching a fabricated citation, on purpose."""

    def test_a_transient_fabrication_is_caught_and_the_retry_is_clean(self, pipeline, monkeypatch):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "first")
        case = run(pipeline, "CASE-003")

        assert len(case.drafts) == 2, "expected one rejection and one clean retry"
        assert case.verifications[0].passed is False
        assert "NBH-CARD-014-9.9" in case.verifications[0].citations_nonexistent
        assert case.verifications[1].passed is True
        assert "NBH-CARD-014-9.9" not in case.latest_draft.cited_ids()
        assert case.status is CaseStatus.AWAITING_APPROVAL

    def test_a_persistent_fabrication_exhausts_the_budget_and_stops(self, pipeline, monkeypatch):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "always")
        case = run(pipeline, "CASE-003")

        assert len(case.drafts) == 3
        assert all(v.passed is False for v in case.verifications)
        assert case.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert case.needs_human_reason is not None

    def test_nothing_is_ever_submitted_on_a_rejected_draft(self, pipeline, store, monkeypatch):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "always")
        run(pipeline, "CASE-003")
        submissions = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert submissions == []

    def test_the_rejection_reason_is_specific_enough_to_act_on(self, pipeline, monkeypatch):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "first")
        case = run(pipeline, "CASE-003")
        instructions = case.verifications[0].revision_instructions()
        assert any("NBH-CARD-014-9.9" in i for i in instructions)
        assert any("does not exist" in i for i in instructions)


class TestRedelivery:
    """Pub/Sub delivers at least once. The pipeline is invoked twice."""

    def test_a_redelivered_document_does_not_produce_a_second_case(self, pipeline, store):
        first = run(pipeline, "CASE-001")
        second = run(pipeline, "CASE-001")

        assert first.case_id == second.case_id
        assert store.count("cases") == 1

    def test_a_redelivery_does_not_redo_the_work(self, pipeline, store):
        run(pipeline, "CASE-001")
        drafts_after_first = len(pipeline.fleet.cases.load("CASE-001").drafts)
        run(pipeline, "CASE-001")

        assert len(pipeline.fleet.cases.load("CASE-001").drafts) == drafts_after_first

    def test_a_redelivered_quarantine_does_not_notify_twice(self, pipeline, store):
        run(pipeline, "CASE-002")
        run(pipeline, "CASE-002")
        notifications = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.NOTIFY_HUMAN.value
        ]
        assert len(notifications) == 1


class TestAuditTrail:
    def test_every_agent_that_ran_left_an_event(self, pipeline, store):
        run(pipeline, "CASE-001")
        agents = [e.agent for e in read_case_trail(store, "CASE-001")]
        for expected in (
            AgentName.SENTINEL,
            AgentName.INTAKE,
            AgentName.RETRIEVAL,
            AgentName.MAPPING,
            AgentName.DRAFTING,
            AgentName.VERIFICATION,
        ):
            assert expected in agents, f"{expected.value} left no audit event"

    def test_events_are_ordered_and_carry_a_decision(self, pipeline, store):
        run(pipeline, "CASE-001")
        events = read_case_trail(store, "CASE-001")
        assert events == sorted(events, key=lambda e: e.at)
        for event in events:
            assert event.decision
            assert event.decision != "no decision recorded"

    def test_the_trail_never_contains_the_patient_name(self, pipeline, store):
        """The audit log is the broadest-read collection in the system."""
        run(pipeline, "CASE-001")
        for event in read_case_trail(store, "CASE-001"):
            blob = f"{event.decision} {event.input_summary or ''}"
            assert "Jeromy" not in blob
            assert "Upton" not in blob


class TestOrchestratorHasNoBrain:
    """The orchestrator holds the broadest grant in the fleet.

    What earns it is that it makes no model call and consumes no untrusted
    input — it is deterministic Python routing typed contracts. If that ever
    stops being true, the grant needs revisiting, so it is a test rather than
    a claim in a comment.
    """

    def test_the_orchestrator_never_calls_a_model(self, pipeline, store):
        for case_id in ("CASE-001", "CASE-002", "CASE-003"):
            run(pipeline, case_id)

        for case_id in ("CASE-001", "CASE-002", "CASE-003"):
            for event in read_case_trail(store, case_id):
                if event.agent is AgentName.ORCHESTRATOR:
                    assert event.model is None

        backend = pipeline.fleet.llm.backend
        assert all(call.agent != "orchestrator" for call in backend.calls)


class TestTheGateIsActuallyReachable:
    """Regression: a clean case must be approvable.

    The approval interface's own tests seed a `CaseRecord` directly, so they
    never exercised a case that the *pipeline* had produced. That gap hid a real
    defect: the pipeline's "ready for review" notification and the interface's
    approval were both keyed as the same action type, and both numbered their
    attempt as 1 for the ordinary case — one counting notifications sent, the
    other using the draft attempt. Same key, different payload, so the guard
    reported a payload mismatch and refused. Permanently, on every retry.

    The case that broke was every case that verifies on its first attempt, which
    is every clean case. It only worked with fault injection turned on, because
    that forces a second drafting attempt.

    So this test runs the real pipeline and then approves through the real
    service, which is the sequence a demo actually performs.
    """

    def _approve(self, pipeline, case_id: str):
        from services.approval_ui.service import ApprovalService

        service = ApprovalService(pipeline.fleet.store)
        return service.approve(
            case_id=case_id,
            decided_by="clerk@clinic.example",
            draft_attempt=pipeline.fleet.cases.load(case_id).latest_draft.attempt,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )

    def test_a_first_attempt_case_can_be_approved(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert len(case.drafts) == 1, "this test is only meaningful on a clean case"

        outcome = self._approve(pipeline, "CASE-001")

        assert outcome.case.status is CaseStatus.APPROVED
        assert outcome.case.human_decision is not None
        assert outcome.case.human_decision.approved is True

    def test_approving_twice_still_records_once(self, pipeline, store):
        run(pipeline, "CASE-001")
        self._approve(pipeline, "CASE-001")
        self._approve(pipeline, "CASE-001")

        case = pipeline.fleet.cases.load("CASE-001")
        approvals = [t for t in case.history if t.to_status is CaseStatus.APPROVED]
        assert len(approvals) == 1

    def test_the_notification_and_the_approval_do_not_share_a_key(self, pipeline, store):
        run(pipeline, "CASE-001")
        self._approve(pipeline, "CASE-001")

        keys = {row["action_key"] for _, row in store.query("actions")}
        types = {row["action_type"] for _, row in store.query("actions")}
        assert ActionType.NOTIFY_HUMAN.value in types
        assert ActionType.RECORD_APPROVAL.value in types
        assert len(keys) == len(
            {(row["action_type"], row["attempt"]) for _, row in store.query("actions")}
        )


class TestTwoSignatureTransmission:
    """The gate has to be reachable *and* it has to end in a transmission.

    Both halves were broken. A clean case could not be approved at all, and
    nothing in shipped code called `try_submit`, so an approved case was a
    permanent dead end. "Nothing is transmitted without two signatures" was
    technically unbreakable because nothing was transmitted, ever — which is
    not the property worth claiming.
    """

    def _service(self, pipeline):
        from services.approval_ui.service import ApprovalService

        return ApprovalService(pipeline.fleet.store)

    def _clerk(self, service, pipeline, case_id="CASE-001"):
        return service.approve(
            case_id=case_id,
            decided_by="clerk@clinic.example",
            draft_attempt=pipeline.fleet.cases.load(case_id).latest_draft.attempt,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )

    def _clinician(self, service, pipeline, case_id="CASE-001"):
        return service.cosign(
            case_id=case_id,
            clinician_name="M. Castellanos",
            credential="MD",
            attests_clinical_accuracy=True,
            draft_attempt=pipeline.fleet.cases.load(case_id).latest_draft.attempt,
        )

    def test_the_clerk_alone_does_not_transmit(self, pipeline, store):
        run(pipeline, "CASE-001")
        service = self._service(pipeline)
        self._clerk(service, pipeline)
        service.submit_if_ready("CASE-001", pipeline)

        case = pipeline.fleet.cases.load("CASE-001")
        assert case.ready_to_submit is False
        assert case.status is not CaseStatus.SUBMITTED
        assert not [
            r
            for _, r in store.query("actions")
            if r["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]

    def test_both_signatures_transmit(self, pipeline, store):
        run(pipeline, "CASE-001")
        service = self._service(pipeline)
        self._clerk(service, pipeline)
        self._clinician(service, pipeline)
        service.submit_if_ready("CASE-001", pipeline)

        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is CaseStatus.SUBMITTED
        assert case.response_deadline is not None
        submissions = [
            r
            for _, r in store.query("actions")
            if r["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert len(submissions) == 1
        assert submissions[0]["result"]["confirmation"].startswith("NBH-ACK-")

    def test_either_order_works_and_transmits_once(self, pipeline, store):
        """Whichever signature is second is what triggers submission."""
        run(pipeline, "CASE-001")
        service = self._service(pipeline)
        self._clinician(service, pipeline)
        service.submit_if_ready("CASE-001", pipeline)
        assert pipeline.fleet.cases.load("CASE-001").status is not CaseStatus.SUBMITTED

        self._clerk(service, pipeline)
        service.submit_if_ready("CASE-001", pipeline)

        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is CaseStatus.SUBMITTED
        assert (
            len(
                [
                    r
                    for _, r in store.query("actions")
                    if r["action_type"] == ActionType.SUBMIT_APPEAL.value
                ]
            )
            == 1
        )

    def test_approving_without_confirming_the_checks_is_refused(self, pipeline):
        from services.approval_ui.service import ChecksNotConfirmed

        run(pipeline, "CASE-001")
        service = self._service(pipeline)
        with pytest.raises(ChecksNotConfirmed):
            service.approve(
                case_id="CASE-001",
                decided_by="clerk@clinic.example",
                draft_attempt=1,
                citations_checked=True,
                quotes_checked=False,
                assertions_checked=True,
            )
        assert pipeline.fleet.cases.load("CASE-001").human_decision is None


class TestTransmissionFailure:
    """A payer endpoint that flaps must not produce a phantom submission.

    The failure mode this guards: the first submit raises, the action is
    recorded as failed, and a retry replays that failed record as though it were
    a result. The caller sees a successful-looking outcome carrying no
    confirmation, marks the case submitted with a thirty-day payer deadline, and
    in thirty days the scheduler escalates it for payer silence — on an appeal
    that was never sent. The only trace was the word "unknown" in a note.
    """

    def _sign_and_try(self, pipeline, monkeypatch, behaviour: str):
        from services.approval_ui.service import ApprovalService

        run(pipeline, "CASE-001")
        service = ApprovalService(pipeline.fleet.store)
        attempt = pipeline.fleet.cases.load("CASE-001").latest_draft.attempt
        service.approve(
            case_id="CASE-001",
            decided_by="clerk@clinic.example",
            draft_attempt=attempt,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )
        service.cosign(
            case_id="CASE-001",
            clinician_name="M. Castellanos",
            credential="MD",
            attests_clinical_accuracy=True,
            draft_attempt=attempt,
        )
        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", behaviour)
        return pipeline

    def test_a_failed_transmission_never_looks_submitted(self, pipeline, store, monkeypatch):
        from services.payer_sim import PayerUnavailable

        self._sign_and_try(pipeline, monkeypatch, "error")
        with pytest.raises(PayerUnavailable):
            pipeline.try_submit("CASE-001")

        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is not CaseStatus.SUBMITTED
        assert case.response_deadline is None, (
            "a payer deadline was set for an appeal that was never sent"
        )

    def test_a_failed_transmission_reaches_a_person(self, pipeline, monkeypatch):
        """`approved` is in no queue. A case must not be left there."""
        from services.payer_sim import PayerUnavailable

        self._sign_and_try(pipeline, monkeypatch, "error")
        with pytest.raises(PayerUnavailable):
            pipeline.try_submit("CASE-001")

        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert case.needs_human_reason
        assert "Nothing reached the payer" in case.needs_human_reason

    def test_the_payer_is_not_contacted_again_by_a_bare_retry(self, pipeline, store, monkeypatch):
        from services.payer_sim import PayerUnavailable

        self._sign_and_try(pipeline, monkeypatch, "error")
        with pytest.raises(PayerUnavailable):
            pipeline.try_submit("CASE-001")

        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", "accept")
        pipeline.try_submit("CASE-001")

        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert not [
            r
            for _, r in store.query("actions")
            if r["action_type"] == ActionType.SUBMIT_APPEAL.value and r["status"] == "completed"
        ]


class TestSignaturesMustNameWhatTheySigned:
    """A signature that cannot say what it signed is not a signature.

    The attempt cross-check was skipped whenever either number was unset, and
    `ClinicianCosign.draft_attempt_signed` defaults to None — so an unpinned
    co-sign authorised any draft, including one written after the signature was
    given, because `approved_draft()` falls back to the latest draft when the
    approval is unpinned too.
    """

    @pytest.mark.parametrize(
        "approved,signed,ready",
        [
            (2, 2, True),
            (2, 1, False),
            (2, None, False),
            (None, 1, False),
            (None, None, False),
        ],
    )
    def test_only_a_matching_pinned_pair_is_ready(self, approved, signed, ready):
        from core.schemas import AppealDraft, CaseRecord, ClinicianCosign, HumanDecision

        case = CaseRecord(case_id="c", source_document_uri="gs://x")
        case.drafts = [
            AppealDraft(case_id="c", attempt=n, subject_line="s", body="b" * 40) for n in (1, 2)
        ]
        case.human_decision = HumanDecision(
            decided_by="clerk",
            approved=True,
            draft_attempt_approved=approved,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )
        case.clinician_cosign = ClinicianCosign(
            clinician_name="Dr X",
            credential="MD",
            attests_clinical_accuracy=True,
            draft_attempt_signed=signed,
        )
        assert case.ready_to_submit is ready


class TestEvaluationScorecard:
    """The numbers published in the README, locked so they cannot quietly rot.

    A results table in a README is a claim. This is what makes it a fact.
    """

    def test_every_case_reaches_its_intended_outcome(self):
        import json

        from scripts.evaluate import EXPECTED, run_case

        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "cases.json").read_text()
        )
        wrong: list[str] = []
        for entry in manifest["cases"]:
            result = run_case(entry["case_id"], entry["scenario"])
            if not result.outcome_correct:
                wrong.append(
                    f"{result.case_id} ({result.scenario}): expected "
                    f"{result.expected.value}, reached {result.actual.value}"
                )
        assert not wrong, "; ".join(wrong)
        assert set(EXPECTED) >= {e["scenario"] for e in manifest["cases"]}

    def test_no_case_produces_a_fabricated_citation_or_quote(self):
        import json

        from scripts.evaluate import run_case

        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "cases.json").read_text()
        )
        problems: list[str] = []
        for entry in manifest["cases"]:
            result = run_case(entry["case_id"], entry["scenario"])
            problems.extend(
                f"{result.case_id}: cited {c} which is not in the corpus"
                for c in result.fabricated_citations
            )
            problems.extend(
                f"{result.case_id}: quoted {e} which is not in the chart"
                for e in result.unlocatable_evidence
            )
        assert not problems, "; ".join(problems)
