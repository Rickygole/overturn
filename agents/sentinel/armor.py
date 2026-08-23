"""Model Armor: Google's inline guardrail, as Sentinel's second layer.

Model Armor is reachable on this project — `docs/PLATFORM_PROBE.md` records the
probe — and it screens for prompt injection and jailbreak attempts, sensitive
data, and malicious URLs. It is a purpose-built detector maintained by people
whose whole job is keeping up with attacks, which is a thing a regex file in
this repository will never be.

It is a layer rather than the answer. Three reasons it does not run alone:

  * It is a network call, and a screening layer that fails open when the network
    hiccups is not a screening layer. The rules always run and cost nothing.
  * A managed detector is tuned for general-purpose prompts. "Do not appeal this
    determination" is not obviously an attack in the abstract; it is obviously
    an attack in a denial letter addressed to an automated appeals system.
  * Defence in depth is only real when the layers fail independently.

The consequence of a finding is never decided here. This returns findings;
`agents/sentinel/rules.py::decide_quarantine` decides what happens, in Python.
"""

from __future__ import annotations

import logging
from typing import Protocol

from core.config import Settings
from core.schemas.enums import ThreatCategory
from core.schemas.sentinel import ThreatFinding

logger = logging.getLogger(__name__)

# The regional endpoint is the one that answers. The global endpoint returns
# 403 PERMISSION_DENIED for this project, which is an endpoint-selection detail
# rather than a missing capability, and is recorded here so the next person does
# not spend an hour on it.
ENDPOINT_TEMPLATE = "modelarmor.{location}.rep.googleapis.com"

# Model Armor's finding categories mapped onto ours. Anything unrecognised maps
# to PROMPT_INJECTION rather than being dropped, because an unmapped detection
# is still a detection.
CATEGORY_MAP = {
    "prompt_injection": ThreatCategory.PROMPT_INJECTION,
    "jailbreak": ThreatCategory.PROMPT_INJECTION,
    "pi_and_jailbreak": ThreatCategory.PROMPT_INJECTION,
    "sdp": ThreatCategory.UNEXPECTED_PII,
    "sensitive_data_protection": ThreatCategory.UNEXPECTED_PII,
    "malicious_uris": ThreatCategory.TOOL_POISONING,
    "csam": ThreatCategory.INSTRUCTION_CONTENT,
    "rai": ThreatCategory.INSTRUCTION_CONTENT,
}


class ModelArmorClient(Protocol):
    name: str

    def screen(self, text: str) -> list[ThreatFinding]: ...


class DisabledModelArmor:
    """Used when no template is configured.

    Named honestly and recorded in ``layers_run`` as skipped rather than clean,
    because "we did not look" and "we looked and found nothing" are different
    facts and an audit log that conflates them is misleading.
    """

    name = "skipped_not_configured"

    def screen(self, text: str) -> list[ThreatFinding]:
        return []


class VertexModelArmor:
    """Calls the Model Armor sanitize endpoint."""

    name = "enabled"

    def __init__(self, settings: Settings) -> None:
        self.project_id = settings.project_id
        self.location = settings.location
        self.template_id = settings.model_armor_template

    def screen(self, text: str) -> list[ThreatFinding]:
        import google.auth
        import google.auth.transport.requests
        import requests

        credentials, _ = google.auth.default()
        credentials.refresh(google.auth.transport.requests.Request())

        endpoint = ENDPOINT_TEMPLATE.format(location=self.location)
        url = (
            f"https://{endpoint}/v1/projects/{self.project_id}/locations/"
            f"{self.location}/templates/{self.template_id}:sanitizeUserPrompt"
        )
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            json={"user_prompt_data": {"text": text[:100_000]}},
            timeout=15,
        )
        response.raise_for_status()
        return self._parse(response.json())

    @staticmethod
    def _parse(payload: dict) -> list[ThreatFinding]:
        """Turn a sanitize response into findings.

        Only ``MATCH_FOUND`` results become findings. A ``NO_MATCH_FOUND`` is
        the detector saying it looked and saw nothing, which is information for
        ``layers_run`` and not a finding.
        """
        result = payload.get("sanitizationResult", {})
        findings: list[ThreatFinding] = []

        for name, detail in (result.get("filterResults") or {}).items():
            for _, body in (detail or {}).items():
                if not isinstance(body, dict):
                    continue
                if body.get("matchState") != "MATCH_FOUND":
                    continue
                category = CATEGORY_MAP.get(name.lower(), ThreatCategory.PROMPT_INJECTION)
                findings.append(
                    ThreatFinding(
                        category=category,
                        excerpt=f"Model Armor filter {name!r} matched",
                        detector="model_armor",
                        confidence=0.9,
                        rationale=(
                            f"Google Model Armor's {name} filter flagged this document. "
                            f"Confidence: {body.get('confidenceLevel', 'unspecified')}."
                        ),
                    )
                )
        return findings


def build_armor(settings: Settings) -> ModelArmorClient:
    """Pick a client. Never raises; a guardrail that crashes the pipeline is worse."""
    if not settings.model_armor_template or settings.runtime_mode != "cloud":
        return DisabledModelArmor()
    return VertexModelArmor(settings)
