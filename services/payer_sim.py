"""A simulated payer endpoint.

Overturn does not transmit anything to a real insurer. This stands in for the
thing that would, and it exists so the submission path is a real network-shaped
boundary with a confirmation number rather than a comment saying "would send
here".

It can be told to behave badly, because the interesting question is not what
happens when a payer responds promptly.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import StrEnum


class PayerBehaviour(StrEnum):
    """How the simulated payer responds. Set via ``OVERTURN_PAYER_BEHAVIOUR``."""

    ACCEPT = "accept"  # acknowledges receipt, then goes quiet
    SILENT = "silent"  # acknowledges nothing; drives the escalation ladder
    UPHOLD = "uphold"  # responds, denying again
    OVERTURN = "overturn"  # responds, granting the appeal
    ERROR = "error"  # the endpoint is down


class PayerUnavailable(RuntimeError):
    """The payer endpoint refused the submission."""


@dataclass(frozen=True)
class Acknowledgement:
    reference: str
    behaviour: PayerBehaviour


def _behaviour() -> PayerBehaviour:
    raw = os.getenv("OVERTURN_PAYER_BEHAVIOUR", PayerBehaviour.ACCEPT.value)
    try:
        return PayerBehaviour(raw)
    except ValueError:
        return PayerBehaviour.ACCEPT


def _reference(prefix: str, *parts: str) -> str:
    """A deterministic confirmation number.

    Deterministic rather than random so that a replayed action returns the same
    reference a human already wrote down, and so tests can assert on it.
    """
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:8].upper()
    return f"{prefix}-{digest}"


def submit(case_id: str, subject: str, body: str, citations: list[str]) -> str:
    """Submit an appeal. Returns the payer's confirmation reference."""
    behaviour = _behaviour()
    if behaviour is PayerBehaviour.ERROR:
        raise PayerUnavailable("payer intake endpoint returned 503")
    return _reference("NBH-ACK", case_id, subject, str(len(citations)))


def escalate(case_id: str, level: str, rationale: str) -> str:
    """Escalate to the next rung. Returns the payer's reference."""
    behaviour = _behaviour()
    if behaviour is PayerBehaviour.ERROR:
        raise PayerUnavailable("payer escalation endpoint returned 503")
    return _reference("NBH-ESC", case_id, level)
