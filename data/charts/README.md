# Patient charts

**These charts are synthetic.** No real patient data appears anywhere in this
repository, and none was consulted in building it.

## What was deliberately not used

MIMIC-III and MIMIC-IV were considered and rejected. The PhysioNet credentialed
data use agreement prohibits redistribution, and this project's demonstration
video is posted publicly. A repository or a video containing MIMIC-derived
records would violate that agreement regardless of intent, so the corpus here
is generated and authored instead.

## How these were produced

Patient demographics, encounter scaffolding, and background conditions come
from [Synthea](https://github.com/synthetichealth/synthea), an open-source
synthetic patient generator that produces statistically plausible records with
no connection to any real person.

The clinically relevant encounters — the notes, results and dates that a payer
policy criterion actually turns on — are authored for this project. Synthea
generates realistic populations, but it does not generate a chart that lines up
against a specific fictional payer's specific numbered criteria, which is what a
documentation-matching demonstration needs. Where a chart was authored rather
than generated, `chart_provenance` in the record says so per encounter.

## Chart format

Each chart is JSON: demographics, a problem list, a medication list, and a
chronological list of encounters. Encounters carry a `locator` string, and that
locator is what the Mapping agent cites as chart evidence, so a human reviewing
a verdict can find the exact note it came from.
