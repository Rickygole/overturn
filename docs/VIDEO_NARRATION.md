# Demo video — narration as recorded

Voice: ElevenLabs "Sarah", model eleven_multilingual_v2. Runtime 3:21, 1920x1080.
Built by capturing the live deployment with headless Chrome and animating vertical
pans over each page, so every frame is the real product at the real URL.

## 01 — door

A clinic gets a denial letter. Fighting it means finding the insurer's published policy, checking the chart against every criterion, and writing a letter that cites the policy by section. Two hours of skilled work, for one claim. So most denials are never appealed at all.

## 02 — door

The numbers are brutal. Marketplace insurers denied about eighty five million in-network claims in twenty twenty four. Consumers appealed two hundred sixty two thousand of them. Fewer than one in three hundred. The barrier is not merit. It is labour. Overturn exists to write the appeal that would not have been written.

## 03 — system

Seven agents on Google's Agent Development Kit, running Gemini on Vertex AI. A denial lands in Cloud Storage, Pub Sub notifies Cloud Run, and the fleet works the case end to end. Firestore holds the state. Every agent has its own service account and its own permissions.

## 04 — cloud

This is running on Google Cloud right now. Three services: ingest, the approval interface, and a scheduler. Ingest and scheduler are private, invoked by Pub Sub and Cloud Scheduler, never by a browser. And a scheduler job firing every five minutes, which is what makes the next part possible.

## 05 — queue

This is the human review queue. Eight cases across six states. Three of them are refusals. A case the system declined to argue, or a document it quarantined unread. A refusal here is the system working, not a gap in it.

## 06 — case001

This table is the whole thesis. What the letter asserts. The insurer's own policy text behind it. The chart evidence, with the encounter it came from. And a verdict for each. Nothing in the appeal is allowed to exist without all three.

## 07 — case003

Verification is a second model whose only job is to attack the first one's work. It checks every citation, and it rejects drafts that overreach. Here it rejected three in a row and the case stopped. We read those rejections by hand and two of them were wrong. That correction is printed on the page, because a system that hides its own false positives cannot be trusted about anything else.

## 08 — case006

And this is the part that makes it a fleet rather than a script. Nobody touched this case. The payer's response window lapsed, a scheduled job read a timestamp in Firestore, and the case moved itself to peer to peer review. Rung two of four. It will escalate again on its own, without being asked.

## 09 — system

One more result, because it is the one we would rather not publish. On a denial letter carrying an injected instruction, Google's Model Armor returned no match found. Our own deterministic rules layer caught it and quarantined the document. That is a negative result about a sponsor's product, and it is why the screening layer has three layers instead of one.

## 10 — door

Nothing transmits without two human signatures. A billing clerk for the paperwork, the ordering clinician for the medicine. Overturn never decides whether care is appropriate. It decides whether the chart already matches criteria the payer already published. Everything here is synthetic, and everything here is measured.
