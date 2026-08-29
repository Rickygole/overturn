"""Probe which Gemini and Gemma model ids actually answer on this project.

Run once at the start of the build. The hackathon mandates Gemini 3.5 or newer,
so the point is to find the exact published id rather than guess at it.
"""

from __future__ import annotations

import sys

from google import genai
from google.genai import types

PROJECT = "overturn-506402"
LOCATION = "global"  # Gemini 3.x is served only from the global endpoint

CANDIDATES = [
    # Confirmed present in the us-central1 Model Garden catalog on 2026-08-22.
    # Anything the hackathon accepts must be Gemini 3.5 or newer.
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemma-4-26b-a4b-it-maas",
    "gemini-embedding-001",
    "text-embedding-005",
]


def probe(client: genai.Client, model: str) -> tuple[bool, str]:
    try:
        if model.startswith("text-embedding"):
            client.models.embed_content(model=model, contents="ping")
            return True, "embedding ok"
        resp = client.models.generate_content(
            model=model,
            contents="Reply with the single word: ok",
            config=types.GenerateContentConfig(max_output_tokens=2048),
        )
        return True, (resp.text or "").strip()[:40]
    except Exception as exc:
        msg = str(exc).replace("\n", " ")
        return False, msg[:110]


def main() -> int:
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    available = []
    for model in CANDIDATES:
        ok, detail = probe(client, model)
        print(f"{'OK  ' if ok else 'FAIL'}  {model:24} {detail}")
        if ok:
            available.append(model)
    print()
    print("available:", ", ".join(available) or "(none)")
    return 0 if available else 1


if __name__ == "__main__":
    sys.exit(main())
