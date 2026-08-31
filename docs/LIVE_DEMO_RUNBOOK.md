# The live, unedited demo — verified shot list

The judging criteria for **Demo & Production Readiness (30%)** asks, in these
words, for *"a live, unedited demo"*, and the tips add *"show your agent doing
something, not just talking"* and *"show it working live."*

`overturn-demo.mp4` is a produced film. It wins the value-proposition half of
the video requirement and it is the wrong shape for this half. This runbook is
the other half: one continuous screen recording, no cuts, of the fleet actually
working. Roughly four minutes if you do not rush.

Every command below was executed against the live project on 2026-08-30 and its
output confirmed, so nothing here should surprise you on camera.

**Before you hit record**

- One browser window, one terminal, side by side. Terminal font 20pt minimum.
- `gcloud config set project overturn-506402` and `REGION=us-central1`.
- Close anything with a notification badge.
- Do not clear the terminal mid-take. An unedited demo is allowed to be slow;
  it is not allowed to look assembled.

---

## 0:00 — the services are real, and they are running

```
gcloud run services list --region=us-central1
```

Four services, all `True`. Say out loud that `overturn` is the human interface,
`overturn-ingest` and `overturn-scheduler` are private — Pub/Sub and Cloud
Scheduler invoke them, never a browser — and that a 403 from those two in a
browser is correct rather than broken.

```
gcloud scheduler jobs describe overturn-tick --location=us-central1
```

`schedule: */5 * * * *`, `state: ENABLED`. This is the clock behind the
multi-week claim. Point at it before you make the claim, not after.

## 0:40 — drop a real denial letter into the bucket

```
gcloud storage cp data/denials/CASE-005.txt gs://overturn-506402-intake/
```

Nothing is watching this by hand. Cloud Storage notifies Pub/Sub, Pub/Sub
pushes to Cloud Run.

```
gcloud beta run services logs tail overturn-ingest --region=us-central1
```

Wait on camera for the push to arrive. **Do not cut here.** The waiting is the
point: the thing ran without you.

## 1:40 — the queue, in a browser

`https://overturn-kruy6aauaq-uc.a.run.app/queue`

Reading is open — no password, no sign-in. The caseload bar is a control: click
a segment and the table narrows. Open the case named under *Start here*.

## 2:10 — the assertion table

On the case page, scroll to *what the letter claims, the policy and the chart
behind it*. This is the whole product in one table: what the appeal asserts,
the insurer's own policy text behind it, the chart quote with the encounter it
came from, and a verdict. Say plainly that nothing enters the letter that
cannot be traced to both sides.

## 2:40 — the system arguing with itself

Open `CASE-003`. Three drafts, all rejected, and the correction printed on the
row: two of those three rejections were wrong. Read it out. A judge has already
said in writing that publishing this is worth more than a feature.

## 3:05 — the case that escalated itself

`.../case/CASE-006`. *Was on* first-level appeal, *now on* peer-to-peer review,
rung 2 of 4, moved by the orchestrator on the schedule with no person involved.
The panel discloses that the response window is compressed for the demo; say
that out loud too, and say the mechanism is not compressed.

## 3:25 — the two signatures

Back on `CASE-001`, show the clerk's checklist, then `/case/CASE-001/clinical`
and the clinician's attestation. Two screens, two people, two different
questions. Nothing transmits until both are present.

## 3:45 — the audit trail

Expand the trail on any case: agent, operation, decision, model, timestamp,
trace and span id, append-only. Note there is deliberately no Cloud Trace link
because it 403s for anyone without project access — the id is pasteable, a
broken link is not.

---

## What to say if a case fails on camera

Leave it in. The attempt cap firing, or verification rejecting a draft, is the
system doing its job, and an unedited demo that survives a stumble is worth
more than a clean one that looks rehearsed. The one thing not to do is stop
recording and start again.
