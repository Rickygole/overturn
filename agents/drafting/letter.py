"""The letter around the argument.

The Drafting agent writes an argument. It does not write a letter, and it must
not: a date line, an addressee block, a member identifier and an NPI are facts
about the case record, and a language model asked to render facts it was handed
will occasionally render one it was not. An NPI is nine digits with a check
digit — the single easiest field in this document to hallucinate plausibly, and
the single worst one to get wrong.

So the furniture of the letter is assembled here, in deterministic Python, from
values already on the case record. Everything in this module is either copied
from a field or is a bracketed gap. There is no third case.

Three consequences worth stating, because they are the reason this is a module
and not four f-strings in the agent:

* **Verification is unaffected.** It checks ``citations`` and
  ``clinical_assertions``; it never reads ``body``. Nothing here reaches either
  list, and nothing here is a claim about the patient's care — a letterhead is
  not a clinical assertion. The one judgement call is the member's date of
  birth, which is identity rather than clinical fact and is copied from the
  record; it is called out in ``_reference_block`` where it happens.

* **The offline and live paths cannot diverge here.** The scripted backend and
  Gemini both return an argument, and both get the same header from this
  function. A shape change is one edit, not two that drift.

* **It is plain text.** No markdown, no tabs, no column padding — the letter
  renders in a proportional font in the approval screen and in a monospace one
  in a terminal, and it has to read correctly in both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from agents.drafting.brief import DraftingBrief

# Every gap this module can emit, as the text that appears in the letter. The
# clerk fills these; the system will not guess at them. Keyed by the field name
# so a caller (the agent, the audit trail, a test) can name what is missing
# without pattern-matching on prose.
GAP_TEXT: dict[str, str] = {
    "sender_letterhead": (
        "[Practice name, address and telephone — clinic letterhead, not in the case record]"
    ),
    "payer_appeals_address": (
        "[Appeals mailing address — printed on the determination notice, not captured at intake]"
    ),
    "member_dob": "[date of birth — not in the case record]",
    "date_of_service": "[date of service — not stated in the determination notice]",
    "amount_in_dispute": "[amount in dispute — not stated in the determination notice]",
    "ordering_provider": (
        "[ordering provider — the case record does not identify one unambiguously]"
    ),
    "ordering_provider_credential": "[credential — MD, DO, NP, PA; not in the case record]",
    "ordering_provider_npi": "[NPI — not in the case record; add before sending]",
    "preparer": "[name and title of the person filing this appeal]",
    "enclosures": "[list the records enclosed with this appeal]",
}

LEVEL_TITLES = {
    "first_level_appeal": "First-level appeal of an adverse benefit determination",
    "peer_to_peer_review": "Request for peer-to-peer review of an adverse benefit determination",
    "second_level_appeal": "Second-level appeal of an adverse benefit determination",
    "independent_external_review": "Request for independent external review",
}

# Lines a model writes when it is trying to be helpful and has been told not to
# be. Matched only at the head of the body, and only when the line is plainly a
# label rather than a sentence — "To Appeals Coordinator," is furniture, "To
# establish that the record documents..." is the argument.
_HEAD_LABELS = re.compile(
    r"^\s*(re|ref|subject|attn|attention|to|dear|member|member name|member id|"
    r"patient|claim|claim number|claim no\.?|date of service|service denied|"
    r"service at issue|policy|policy number|group|group number|date|from|"
    r"ordering provider|npi|amount in dispute)\b",
    re.IGNORECASE,
)
_SIGN_OFF = re.compile(
    r"^\s*(sincerely|respectfully|regards|kind regards|best regards|"
    r"yours (truly|sincerely|faithfully)|thank you for your (time|consideration|review)|"
    r"enclosures?\b|encl\.?\b|cc:)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Letter:
    """A finished letter and an honest list of what a human must still fill in."""

    body: str
    gaps: tuple[str, ...]

    @property
    def gap_text(self) -> tuple[str, ...]:
        return tuple(GAP_TEXT[name] for name in self.gaps)


def long_date(value: date) -> str:
    """``June 2, 2026``. The convention the inbound notice itself uses."""
    return f"{value:%B} {value.day}, {value.year}"


def _is_furniture_line(line: str) -> bool:
    """Whether a line is a label rather than prose.

    A label either carries a colon early or ends in a comma — the two shapes
    every reference line and every salutation takes. Requiring one of those is
    what keeps ``To establish that ...`` out of this.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if not _HEAD_LABELS.match(stripped):
        return False
    return ":" in stripped[:36] or stripped.endswith((",", ":"))


def strip_furniture(argument: str) -> str:
    """Remove any letter furniture the model wrote anyway.

    The prompt asks for the argument alone. Models comply most of the time, and
    a duplicated salutation is the kind of defect that survives review because
    everyone assumes someone else's layer removed it. Cheaper to remove it here
    than to depend on the instruction.

    Deliberately conservative: only label lines at the very head, and only a
    sign-off in the last three paragraphs. If stripping would empty the body,
    nothing is stripped — an empty letter is a worse failure than a doubled
    ``Sincerely``.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", argument.strip()) if p.strip()]
    if not paragraphs:
        return argument.strip()

    while paragraphs:
        lines = [line for line in paragraphs[0].splitlines() if line.strip()]
        if lines and all(_is_furniture_line(line) for line in lines):
            paragraphs.pop(0)
            continue
        # A salutation with no blank line under it — "Dear Appeals Coordinator,"
        # and the opening sentence in one block — is the same defect wearing a
        # different shape, so the head of the surviving paragraph is trimmed the
        # same way rather than only whole paragraphs.
        kept = list(lines)
        while kept and _is_furniture_line(kept[0]):
            kept.pop(0)
        if kept and len(kept) != len(lines):
            paragraphs[0] = "\n".join(kept)
        break

    for index in range(max(0, len(paragraphs) - 3), len(paragraphs)):
        first_line = paragraphs[index].splitlines()[0]
        if _SIGN_OFF.match(first_line):
            paragraphs = paragraphs[:index]
            break

    if not paragraphs:
        return argument.strip()
    return "\n\n".join(p.strip() for p in paragraphs)


def _reference_block(brief: DraftingBrief, gaps: list[str]) -> list[str]:
    """The block a payer's mailroom routes on.

    Every line is a copy of a record field or a bracketed gap. The member's date
    of birth is the only line that is a fact about the person rather than about
    the claim; it is identity, not clinical assertion, and it is copied from the
    chart or the denial letter rather than composed.
    """
    lines = [f"Re: {LEVEL_TITLES.get(brief.appeal_level, LEVEL_TITLES['first_level_appeal'])}"]

    if brief.patient_name:
        lines.append(f"Member: {brief.patient_name}")
    if brief.patient_dob:
        lines.append(f"Date of birth: {long_date(brief.patient_dob)}")
    else:
        gaps.append("member_dob")
        lines.append(f"Date of birth: {GAP_TEXT['member_dob']}")

    lines.append(f"Member ID: {brief.member_id or '[member ID — not stated in the notice]'}")
    lines.append(
        f"Claim number: {brief.claim_number or '[claim number — not stated in the notice]'}"
    )

    if brief.date_of_service:
        lines.append(f"Date of service: {long_date(brief.date_of_service)}")
    else:
        gaps.append("date_of_service")
        lines.append(f"Date of service: {GAP_TEXT['date_of_service']}")

    service = brief.service_line
    if not service:
        service = brief.service_description
        if brief.procedure_code:
            service += f" (CPT/HCPCS {brief.procedure_code})"
    lines.append(f"Service at issue: {service}")
    if brief.diagnosis_code:
        # The notice's own wording. This is a statement about what was submitted
        # on the claim, not a diagnosis this letter is asserting.
        lines.append(f"Diagnosis submitted: {brief.diagnosis_code}")

    if brief.amount_in_dispute:
        basis = f" ({brief.amount_basis})" if brief.amount_basis else ""
        lines.append(f"Amount in dispute: {brief.amount_in_dispute}{basis}")
    else:
        gaps.append("amount_in_dispute")
        lines.append(f"Amount in dispute: {GAP_TEXT['amount_in_dispute']}")

    if brief.date_of_denial:
        code = f" (reason code {brief.denial_reason_code})" if brief.denial_reason_code else ""
        lines.append(f"Determination dated: {long_date(brief.date_of_denial)}{code}")
    elif brief.denial_reason_code:
        lines.append(f"Denial reason code: {brief.denial_reason_code}")

    if brief.ordering_provider:
        specialty = (
            f", {brief.ordering_provider_specialty}" if brief.ordering_provider_specialty else ""
        )
        lines.append(f"Ordering provider: {brief.ordering_provider}{specialty}")
    else:
        gaps.append("ordering_provider")
        lines.append(f"Ordering provider: {GAP_TEXT['ordering_provider']}")

    gaps.append("ordering_provider_npi")
    lines.append(f"Ordering provider NPI: {GAP_TEXT['ordering_provider_npi']}")

    return lines


def _signature_block(brief: DraftingBrief, gaps: list[str]) -> list[str]:
    """Who signs.

    A medical-necessity appeal is signed by the clinician who ordered the care —
    that is the same person :class:`core.schemas.case.ClinicianCosign` requires
    before anything is transmitted, so the block is addressed to them by name
    where the record identifies them and left open where it does not. The
    credential and the NPI are always gaps: the chart names clinicians, it does
    not carry their registry identifiers, and a letter with an invented NPI is
    worse in every way than a letter with a blank one.
    """
    lines = ["Sincerely,", "", "", "____________________________________"]

    if brief.ordering_provider:
        specialty = (
            f"\n{brief.ordering_provider_specialty}" if brief.ordering_provider_specialty else ""
        )
        lines.append(f"{brief.ordering_provider}{specialty}")
    else:
        lines.append(GAP_TEXT["ordering_provider"])
    lines.append("Ordering provider")

    gaps.append("ordering_provider_credential")
    lines.append(f"Credential: {GAP_TEXT['ordering_provider_credential']}")
    lines.append(f"NPI: {GAP_TEXT['ordering_provider_npi']}")

    gaps.append("preparer")
    gaps.append("enclosures")
    lines += [
        "",
        f"Prepared by: {GAP_TEXT['preparer']}",
        f"Enclosures: {GAP_TEXT['enclosures']}",
    ]
    return lines


def compose_letter(brief: DraftingBrief, argument: str) -> Letter:
    """Wrap the model's argument in the letter a clinic would actually post.

    The argument is dropped in whole and unedited apart from furniture removal.
    Nothing in the surround makes a claim the case record does not carry.
    """
    gaps: list[str] = ["sender_letterhead", "payer_appeals_address"]

    blocks: list[str] = [
        GAP_TEXT["sender_letterhead"],
        long_date(brief.letter_date),
        "\n".join(
            [
                brief.payer_name,
                "Attn: Appeals Department",
                GAP_TEXT["payer_appeals_address"],
            ]
        ),
        "\n".join(_reference_block(brief, gaps)),
        "Dear Appeals Coordinator:",
        strip_furniture(argument),
    ]

    if brief.appeal_deadline:
        blocks.append(
            "This appeal is filed within the period the plan's notice allows, "
            f"which runs to {long_date(brief.appeal_deadline)}."
        )

    blocks.append("\n".join(_signature_block(brief, gaps)))

    # Order-preserving de-duplication: the NPI gap is emitted in two places in
    # the letter and is one thing for a human to fill in.
    seen: list[str] = []
    for name in gaps:
        if name not in seen:
            seen.append(name)

    return Letter(body="\n\n".join(blocks), gaps=tuple(seen))
