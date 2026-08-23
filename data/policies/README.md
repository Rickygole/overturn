# Northbeck Health Plan medical policies

**These documents are synthetic.** Northbeck Health Plan is a fictional payer
invented for this project. No real insurer's name, logo, or copyrighted policy
text appears anywhere in this corpus.

They are modelled on the *structure* of medical policy documents that real
payers publish openly: a scope statement, a coverage position, numbered coverage
criteria, documentation requirements, and exclusions. That structure is what
makes the appeal problem tractable, and it is the part worth reproducing. The
clinical content is written for this project.

## Why the identifiers matter

Every section carries a stable identifier, `NBH-<SERVICE>-<NUMBER>-<SECTION>`:

```
NBH-CARD-014-3.2
    │    │   │
    │    │   └── section and sub-criterion
    │    └────── policy number within the service line
    └─────────── service line
```

Appeals cite these identifiers verbatim, and the Verification agent rejects any
draft citing an identifier that does not appear in the retrieved section set.
The identifier is therefore not decoration; it is the join key that makes a
citation checkable by a machine instead of by a hopeful reader.

## Format

`## <section-id> — <heading>` opens a section. `### <criterion-id>` opens a
numbered criterion inside it. `agents/retrieval/corpus.py` parses this into
`PolicySection` objects; the file you read here is the same text the system
retrieves and quotes, so what a judge sees on screen is the source, not a
rendering of it.
