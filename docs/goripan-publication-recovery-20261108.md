# Goripan publication recurrence — November 8, 2026

## Verified incident

Run 42475588084/job 128716109336 saved the Kiryu 12R original before its
20:30 JST deadline and uploaded encrypted artifact 13615580494. Projection
then stopped at `GORIPAN_PRIOR_PUBLIC_FEED_BINDING`, before sending to the Site.
The deployed reusable workflow is 94e0de64725df5014addd6724cac7a3e5935480e.
Do not roll back to the older research branch.

An exact same-day Site GET was PASS/PREDEADLINE, 9,470,117 bytes, with verified
response/header SHA256 339bc9f13ddcff7a60811298dbdc7168a4b4b78fcd2b4632d696a1720a764fab.
The old guard allows 16 MiB only for NIGHT but 8 MiB for PREDEADLINE.
The last accepted Goripan observation was 15:53:54 JST, 98 LOCKs. All 56
subsequent inspected observer runs through 20:44 failed at this guard.

A second defect amplified the outage: the prior-artifact selector accepts
only successful whole runs, despite an immutable encrypted upload before
publication. The failures therefore kept restoring the same 98-LOCK baseline.
The 34 omitted races all have saved predeadline originals, across 33 earliest
donor artifacts. Some later retries resaved the same race. Recovery selects
the earliest stored LOCK deterministically, without looking at results.

## Repair boundaries

- Apply the existing bounded 16 MiB display-response allowance to every phase,
  including the same display response used by the specialty prerequisite.
  Keep exact GET/header hash, day, PASS, model and LOCK validation. Log byte
  count and phase. This does not change the Site upload/storage budget.
- Future failed runs may supply a prior ledger only when the new persistence
  marker and required restoration/encryption steps succeeded, no earlier step
  failed, the trusted main workflow completed, and failures are confined to
  the later publication steps. Failed prediction/settlement, cancelled,
  foreign and old-contract runs remain ineligible.
- A November 8-only repair authenticates each allowlisted run, job, artifact
  identity, ZIP digest and GPG payload. It verifies the original frozen model,
  day binding, per-LOCK hash and timestamp with the existing private verifier.
  All 98 overlapping LOCKs must remain byte-identical. Only 34 missing original
  files and provenance are appended, with a derived manifest listing 132.
- Validate all donors in a disposable directory before an atomic replacement.
  Preserve the original baseline and donor manifests. Require no prior
  same-day settlement/learning attempt in the baseline. Idempotent retries
  verify the sealed recovery receipt and recovered file hashes.
- No prediction generation by recovery, no result-based bet selection,
  retraining, model/notification changes, odds replacement or NIGHT rerun.
  Existing normal NIGHT and publication remain responsible for settlement.

## Verification and resume

Local tests: exact incident bytes; 16 MiB boundary for MORNING/PREDEADLINE/NIGHT;
wrong date/status/hash and malformed JSON rejection; saved-work eligibility;
byte preservation, atomic validation, duplicates, changed models, later
resaves, invalid LOCKs, missing donors, prior NIGHT and idempotency.
The required contract CI additionally uses the pinned private model/LOCK
verifier, and verifies existing frozen inference/settlement block hashes.

Deployment and observed recovery are not yet verified in this record.
After the tested caller pin is merged, inspect the naturally triggered observer
for `WILD4_PUBLICATION_RECOVERY` (34 restored, 132 total, zero generation),
encrypted persistence and exact application readback. Compare all previously
published 98 LOCKs with the pre-repair Site snapshot. Verify NIGHT separately.

At 20:49 JST primary NIGHT reported `NIGHT_WAITING_OFFICIAL_RESULTS`, reason
`K_DATE_MISSING`, no feed emitted, bounded retry scheduled. A successful job
at this stage does not mean completed settlement; yesterday's success is not
evidence for tonight.
