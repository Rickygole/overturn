# Social posts — ready to paste

All three variants below are built from claims that trace to `docs/EVALUATION.md`
and `docs/SCREENING_LAYERS.md`. Every one carries `#AllThingsAgenticHackathon`,
the live URL, and the repo. Post after the Devpost submission is in, so the
links resolve to the version a judge will actually see.

---

## LinkedIn (long, professional)

```
A denial letter lands on a clinic billing clerk's desk. Appealing it properly
means finding the insurer's own published medical policy, checking the
patient's chart against it criterion by criterion, and writing a letter that
cites the policy back to them, section by section. That's real research, not
a form letter — and most clinics don't have anyone free to do it. Insurers
denied roughly 85 million in-network claims in 2024. Consumers appealed at
least 262,982 of them — fewer than one in three hundred — and even then,
insurers upheld 66% of the appeals they received (KFF, ACA Marketplace
claims data, 2024).

I built Overturn for the Google + Devpost All Things Agentic Hackathon: seven
agents on Google's ADK, running Gemini and Gemma on Vertex AI, that do that
research task end to end. Sentinel screens the incoming letter for prompt
injection across three independent layers. Intake extracts the claim.
Retrieval finds the governing policy — and declines the case outright if none
applies, rather than arguing against the wrong document. Mapping checks the
chart against every criterion. Drafting writes the appeal, with no retrieval
access of its own, so it can't go looking for support Mapping didn't hand it.
Verification then tries to tear the draft apart — checking every citation
against the real policy text before a human ever sees it — and a fabricated
or unsupported claim sends the letter back for a rewrite, not out the door.
Lifecycle runs on a Cloud Scheduler tick, weeks later, and escalates the case
on its own if the payer goes silent. Two humans sign before anything
transmits: a billing clerk on the paper trail, the ordering clinician on the
medicine.

The part I'm proudest of isn't the win. It's that when I read one case's
verification rejections against the policy text by hand, I found the checker
had been wrong twice — a well-founded cardiac MRI appeal for an 83-year-old
patient got killed by paraphrase-pedantry, not by an actual fabrication. I
published that finding instead of quietly picking a cleaner example. A
system that argues for a patient and can point at the exact place its own
safety net misfired is worth more than one that just claims to work.

Live on Google Cloud: https://overturn-kruy6aauaq-uc.a.run.app
Code: https://github.com/rickygole/overturn

#AllThingsAgenticHackathon
```

---

## X / Twitter (thread, 4-6 posts)

```
1/
A denial letter costs a clinic ~40 minutes of skilled research to appeal
properly: find the insurer's own policy, match the chart to it, cite it back
to them. Insurers denied ~85M in-network claims in 2024. Consumers appealed
<1 in 300. I built an agent fleet that does the research task instead.

2/
Overturn: 7 agents on Google ADK, running Gemini + Gemma on Vertex AI.
Sentinel screens the letter for prompt injection (3 layers). Intake
extracts. Retrieval finds the real policy. Mapping checks the chart
criterion by criterion. Drafting writes the appeal — with NO retrieval
access, by design, so it can't invent support.

3/
The layer that matters: Verification. It attacks its own fleet's draft —
checks every citation exists, checks the source text actually backs the
claim, checks every clinical fact traces to the chart. Fail, and it goes
back for a rewrite. 3 attempts, then a human reviews and nothing is sent.

4/
Built for @Google's Model Armor + Gemma (gemma-4-26b-a4b-it-maas) as a
guard layer. On my deployed test: Model Armor missed a real injection
payload (NO_MATCH_FOUND). My deterministic rules layer caught it. Publishing
that, not hiding it — that's what "defense in depth" is supposed to mean.

5/
The finding I'm proudest of: I read one case's Verification rejections
against the policy by hand and found the checker was wrong on 2 of 3 —
a well-founded cardiac MRI appeal for an 83-yr-old got killed by
paraphrase-pedantry. Published in docs/EVALUATION.md, not smoothed over.

6/
2 humans sign before anything transmits — a billing clerk, the ordering
clinician. Live on Cloud Run, case queue open to read, no login needed:
https://overturn-kruy6aauaq-uc.a.run.app
Code: https://github.com/rickygole/overturn
#AllThingsAgenticHackathon
```

---

## One-liner

```
Overturn: 7 agents that read a denied health insurance claim, find the
insurer's own policy, check the chart against it, draft the appeal, and
verify every citation before a human signs it — built for the Google +
Devpost All Things Agentic Hackathon. Live: https://overturn-kruy6aauaq-uc.a.run.app
Code: https://github.com/rickygole/overturn #AllThingsAgenticHackathon
```
