# Goripan publication recurrence — November 8

## Verified incident

The last successful projection was observed at 15:53:54 JST, with 98 LOCKs.
All 56 inspected failures from run 42456769204 (15:56 JST) through
42477092825 failed at `GORIPAN_PRIOR_PUBLIC_FEED_BINDING`. The exact live
PREDEADLINE GET was 9,470,117 bytes, SHA-256
`339bc9f13ddcff7a60811298dbdc7168a4b4b78fcd2b4632d696a1720a764fab`, matching
its response header. Status and November 8 date were valid. The prior repair
allowed 16 MiB only for NIGHT, leaving PREDEADLINE at 8 MiB.

The latest user-supplied failure was run 42475588084 / job 128716109336.
It saved Kiryu 12R before deadline and uploaded immutable encrypted artifact
13615580494. Publication failed afterwards. The next job nevertheless chose
the last wholly successful run's 98-LOCK ledger. Across the incident, 41
failed runs preserved artifacts, and 34 distinct missing races have original
predeadline saved timing receipts. Repeated attempts recreated some LOCKs;
recovery selects the **earliest saved** original per race, without consulting
results. The 33 donor artifact IDs, ZIP digests, run identities and saved times
are frozen in `scripts/wild4_publication_recovery_20261108.py`.

NIGHT is separate. At 20:49 JST, primary run 42477272260 reported
`NIGHT_WAITING_OFFICIAL_RESULTS`, reason `K_DATE_MISSING`, with a retry scheduled
and no feed emitted. Its successful job exit did not mean settlement completed.

## Repair and boundaries

- Use the existing bounded 16 MiB full-display-read budget in every phase.
  Preserve response hash, date/status, immutable LOCK and append-only checks.
  Log actual bytes, limit and stage to distinguish capacity failures.
- A failed publication may supply the next ledger only when the new-contract
  job completed decryption, original LOCK verification, encryption and immutable
  upload successfully, with no preceding failures and only publication failures
  afterwards. Old failed jobs are not automatically admitted.
- On November 8 only, authenticate the exact 33 original artifacts against GitHub
  run/job metadata and ZIP SHA-256, decrypt normally, validate the original model,
  day binding, all LOCK hashes and byte-identical 98-race overlap, and copy the
  34 original files. Validate every donor before atomically replacing the local
  restored ledger. Preserve original manifests and a sealed provenance receipt.
- The recovery refuses an already-processed NIGHT or a conflicting original.
  It never calls prediction, official capture, learning, model promotion or
  settlement. Existing normal NIGHT processing remains responsible for results.
- Existing P3/Muki predictions, all original bets, frozen models, notification
  conditions and Site source/audience remain unchanged. Previously missed
  predeadline delivery cannot be retroactively claimed as timely delivery.

## Validation and next checkpoint

Local tests cover the actual 9.47 MB response, all phase budgets and their hard
boundary, bad dates/status/hashes, publication-only failure admission, exact
copy/idempotence, changed overlap/model, a later resave, missing donors, invalid
hashes and already-started NIGHT. CI must also use the actual pinned private
model/projection verifier and pass the existing full runtime contract suite.

Next: merge only the checked head, observe the automatically triggered normal
Goripan run, require 34 restored / 132 LOCKs / zero generations and exact Site
GET readback, then inspect tonight's NIGHT and Goripan settlements separately.
No successful deployment or completed settlement is claimed by this document's
initial implementation checkpoint.

## NIGHT advanced during the repair (21:15 JST)

Normal run 42479252623/job 128727382154 has now settled the existing 98 LOCKs
and published them. The remaining 34 originals are still absent. Learning is
TECHNICAL_HOLD/OFFICIAL_DAY_PREDICTION_COVERAGE; `last_completed_night` is null.
Recovery therefore admits exactly this incomplete-coverage hold after checking
its missing-key set and every saved settlement's sealed LOCK binding. It retains
all existing settlement, lifecycle and retry-proof bytes. It does not reset the
immutable learning cohort or claim learning completed. Other processed NIGHT
states remain rejected. Existing frozen settlement code is triggered once when
originals were restored, to append the 34 missing results from already verified
NIGHT; no official acquisition or prediction is rerun by recovery. The frozen
settlement body hash remains unchanged; only this bounded trigger is added.
