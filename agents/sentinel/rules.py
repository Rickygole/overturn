"""Deterministic screening of untrusted inbound documents.

A denial letter arrives from an outside party. Everything in it is data. The
job of this module is to notice when something in it is shaped like an
instruction, and to do so without asking a model — because a detector that can
be talked out of its finding is not a detector.

This is one of three layers. Model Armor and a Gemma pass sit alongside it and
catch things patterns miss. This layer exists because it is the one that cannot
be persuaded, it costs nothing, and it runs identically offline and in
production.

Two deliberate choices:

**Findings quote the offending span verbatim, and the span is never
re-executed.** An excerpt is stored so a human can see what was caught. It is
truncated and it is never concatenated into another prompt.

**PII is categorised, not blocked.** A denial letter contains a member id, a
name and a date of birth by definition. Finding those is expected and is merely
recorded. Finding a Social Security number or a card number is not expected in
this document type, and that is what a finding means here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from core.schemas.enums import ThreatCategory
from core.schemas.sentinel import ThreatFinding

EXCERPT_CHARS = 160


@dataclass(frozen=True)
class Rule:
    """One deterministic detector."""

    name: str
    category: ThreatCategory
    confidence: float
    fatal: bool
    rationale: str
    detect: Callable[[str], Iterator[str]] = field(repr=False)


def _regex_detector(pattern: str) -> Callable[[str], Iterator[str]]:
    compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE)

    def detect(text: str) -> Iterator[str]:
        for match in compiled.finditer(text):
            start = max(0, match.start() - 40)
            yield text[start : match.end() + 60].replace("\n", " ").strip()

    return detect


# --- Instruction-shaped content --------------------------------------------- #
# These target the grammar of an instruction addressed to a reader, not any
# particular payload. A letter that says "ignore the previous determination"
# is a legitimate sentence, so the patterns require the object to be about
# instructions, rules, or the reader's behaviour.

_OVERRIDE = _regex_detector(
    r"\b(?:ignore|disregard|forget|override|discard)\b[^.\n]{0,40}"
    r"\b(?:previous|prior|above|earlier|all|any|system|initial)\b[^.\n]{0,30}"
    r"\b(?:instruction|instructions|prompt|prompts|rule|rules|direction|directive|context)\b"
)
_ROLE_CHANGE = _regex_detector(
    r"\byou\s+are\s+(?:now|no\s+longer|an?\s+(?:ai|assistant|language\s+model|agent|bot))\b"
    r"|\bact\s+as\s+(?:an?\s+)?(?:ai|assistant|agent|administrator|system)\b"
    r"|\bfrom\s+now\s+on,?\s+you\b"
    r"|\bpretend\s+(?:to\s+be|you\s+are)\b"
)
_NEW_INSTRUCTIONS = _regex_detector(
    r"\b(?:new|updated|revised|additional|special)\s+(?:instruction|instructions|directive|task)\b"
    r"|\bsystem\s*(?:prompt|message|instruction)\b"
    r"|\byour\s+(?:new\s+)?(?:task|instruction|objective|goal)\s+is\b"
)
_ADDRESSED_TO_AGENT = _regex_detector(
    r"\b(?:attention|note\s+to|message\s+for|instruction\s+to)\s*[:,]?\s*"
    r"(?:the\s+)?(?:ai|agent|assistant|automated|bot|llm|model|system|reviewer\s+bot)\b"
    r"|\bif\s+you\s+are\s+an?\s+(?:ai|agent|automated|language\s+model)\b"
)

# --- Tool poisoning ---------------------------------------------------------- #
# Attempts to make the reading system take an action rather than extract data.

_ACTION_DEMAND = _regex_detector(
    r"\b(?:do\s+not|don't|never)\s+(?:appeal|contest|dispute|challenge|escalate|submit)\b"
    r"|\b(?:mark|set|record|classify|treat)\s+(?:this|the)\s+"
    r"(?:claim|case|denial|appeal)\s+as\s+(?:approved|resolved|closed|valid|final|upheld)\b"
    r"|\b(?:close|drop|abandon|withdraw)\s+(?:this|the)\s+(?:case|claim|appeal)\b"
    r"|\bstop\s+processing\b"
)
_TOOL_INVOCATION = _regex_detector(
    r"\b(?:call|invoke|execute|run)\s+(?:the\s+)?(?:function|tool|command|api|endpoint)\b"
    r"|\b(?:tool_code|function_call|tool_call)\b"
)
_EXFILTRATION = _regex_detector(
    r"\b(?:send|email|forward|post|transmit|upload)\b[^.\n]{0,30}"
    r"\b(?:chart|record|records|patient\s+data|phi|credentials|api\s+key|token)\b"
    r"|\breveal\s+(?:your|the)\s+(?:prompt|instructions|system)\b"
)

# --- Prompt-structure injection ---------------------------------------------- #
# Chat-template control tokens have no business in a fax from an insurer.

_CONTROL_TOKENS = _regex_detector(
    r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>"
    r"|\[/?(?:INST|SYS|SYSTEM)\]"
    r"|^\s*###\s*(?:instruction|system|human|assistant)\b"
    r"|\bBEGIN\s+SYSTEM\s+PROMPT\b"
)

RULES: tuple[Rule, ...] = (
    Rule(
        "instruction_override",
        ThreatCategory.PROMPT_INJECTION,
        0.95,
        True,
        "Text attempts to countermand instructions given to the reading system.",
        _OVERRIDE,
    ),
    Rule(
        "role_reassignment",
        ThreatCategory.PROMPT_INJECTION,
        0.9,
        True,
        "Text attempts to reassign the role or persona of the reading system.",
        _ROLE_CHANGE,
    ),
    Rule(
        "injected_instructions",
        ThreatCategory.INSTRUCTION_CONTENT,
        0.85,
        True,
        "Text presents itself as a new instruction set rather than as correspondence.",
        _NEW_INSTRUCTIONS,
    ),
    Rule(
        "addressed_to_automation",
        ThreatCategory.PROMPT_INJECTION,
        0.9,
        True,
        "Text is addressed to an automated reader rather than to a person. "
        "A payer writing to a clinic has no legitimate reason to do this.",
        _ADDRESSED_TO_AGENT,
    ),
    Rule(
        "action_demand",
        ThreatCategory.TOOL_POISONING,
        0.9,
        True,
        "Text instructs the reading system to take or suppress an action on the case.",
        _ACTION_DEMAND,
    ),
    Rule(
        "tool_invocation",
        ThreatCategory.TOOL_POISONING,
        0.85,
        True,
        "Text references invoking a tool or function.",
        _TOOL_INVOCATION,
    ),
    Rule(
        "exfiltration_request",
        ThreatCategory.TOOL_POISONING,
        0.95,
        True,
        "Text asks for records or system context to be sent somewhere.",
        _EXFILTRATION,
    ),
    Rule(
        "chat_control_tokens",
        ThreatCategory.SUSPICIOUS_ENCODING,
        0.8,
        True,
        "Text contains chat-template control tokens, which cannot occur in a "
        "genuine payer document.",
        _CONTROL_TOKENS,
    ),
)


# --- Encoding tricks --------------------------------------------------------- #

_ZERO_WIDTH = {
    "​",  # zero width space
    "‌",  # zero width non-joiner
    "‍",  # zero width joiner
    "﻿",  # zero width no-break space
    "⁠",  # word joiner
}
_BIDI = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}
_BASE64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b")


def scan_encoding(text: str) -> list[ThreatFinding]:
    """Catch text hidden from a human reader but visible to a parser."""
    findings: list[ThreatFinding] = []

    hidden = {ch for ch in text if ch in _ZERO_WIDTH}
    if hidden:
        names = ", ".join(sorted(unicodedata.name(ch, repr(ch)) for ch in hidden))
        findings.append(
            ThreatFinding(
                category=ThreatCategory.SUSPICIOUS_ENCODING,
                excerpt=f"{len([c for c in text if c in _ZERO_WIDTH])} zero-width characters",
                detector="rule:zero_width",
                confidence=0.75,
                rationale=f"Document contains invisible characters ({names}). Text a "
                f"human reviewer cannot see is not text a payer meant them to read.",
            )
        )

    if any(ch in _BIDI for ch in text):
        findings.append(
            ThreatFinding(
                category=ThreatCategory.SUSPICIOUS_ENCODING,
                excerpt="bidirectional override characters present",
                detector="rule:bidi_override",
                confidence=0.8,
                rationale="Bidirectional control characters can make displayed text "
                "differ from parsed text.",
            )
        )

    for match in _BASE64_BLOB.finditer(text):
        findings.append(
            ThreatFinding(
                category=ThreatCategory.SUSPICIOUS_ENCODING,
                excerpt=match.group()[:EXCERPT_CHARS],
                detector="rule:base64_blob",
                confidence=0.6,
                rationale="Long base64-like run in a document that should be prose.",
            )
        )
        break

    return findings


# --- PII --------------------------------------------------------------------- #

EXPECTED_PII: frozenset[str] = frozenset(
    {"member_id", "claim_number", "person_name", "date_of_birth", "postal_address", "phone"}
)

_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    "payment_card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "bank_account": re.compile(r"\b(?:routing|account)\s*(?:number|no\.?|#)\s*:?\s*\d{6,}\b", re.I),
    "email_address": re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
}


def _luhn(digits: str) -> bool:
    """Card-number checksum, so a claim number is not reported as a card."""
    nums = [int(d) for d in digits if d.isdigit()][::-1]
    if len(nums) < 13:
        return False
    total = 0
    for index, value in enumerate(nums):
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def detect_pii(text: str) -> tuple[list[str], list[ThreatFinding]]:
    """Return ``(categories_found, findings_for_unexpected_categories)``."""
    found: list[str] = []
    findings: list[ThreatFinding] = []

    for label, pattern in _PII_PATTERNS.items():
        for match in pattern.finditer(text):
            if label == "payment_card" and not _luhn(match.group()):
                continue
            found.append(label)
            if label != "email_address":
                findings.append(
                    ThreatFinding(
                        category=ThreatCategory.UNEXPECTED_PII,
                        excerpt=f"{label} pattern at offset {match.start()}",
                        detector=f"rule:pii_{label}",
                        confidence=0.85,
                        rationale=f"A {label.replace('_', ' ')} has no reason to appear "
                        f"in a coverage determination notice.",
                    )
                )
            break

    return sorted(set(found)), findings


# --- Public surface ----------------------------------------------------------- #


def scan(text: str) -> list[ThreatFinding]:
    """Run every deterministic detector over the document text."""
    findings: list[ThreatFinding] = []

    for rule in RULES:
        for excerpt in rule.detect(text):
            findings.append(
                ThreatFinding(
                    category=rule.category,
                    excerpt=excerpt[:EXCERPT_CHARS],
                    detector=f"rule:{rule.name}",
                    confidence=rule.confidence,
                    rationale=rule.rationale,
                )
            )
            break  # one finding per rule; the excerpt shows the first instance

    findings.extend(scan_encoding(text))
    _, pii_findings = detect_pii(text)
    findings.extend(pii_findings)
    return findings


FATAL_RULES: frozenset[str] = frozenset(f"rule:{r.name}" for r in RULES if r.fatal)


def decide_quarantine(findings: list[ThreatFinding]) -> bool:
    """Whether the pipeline halts.

    Python decides this, not a model. The model layers contribute findings; the
    consequence of a finding is not something a model gets a vote on.
    """
    return any(f.detector in FATAL_RULES for f in findings)


def sanitize(text: str, findings: list[ThreatFinding]) -> str:
    """Neutralise instruction-shaped spans in a document that is not quarantined.

    Used for the suspicious-but-not-disqualifying path: strip invisible
    characters and control tokens so that whatever reaches Intake cannot carry
    a payload, while leaving the readable prose intact.
    """
    cleaned = "".join(ch for ch in text if ch not in _ZERO_WIDTH and ch not in _BIDI)
    cleaned = _regex_sub_control_tokens(cleaned)
    return cleaned


def _regex_sub_control_tokens(text: str) -> str:
    return re.sub(
        r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>|\[/?(?:INST|SYS|SYSTEM)\]",
        "[removed]",
        text,
        flags=re.IGNORECASE,
    )
