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
- The recovery refuses a completed NIGHT or a conflicting original. The exact
  incomplete 98-LOCK coverage hold observed during this repair is admitted only
  with existing settlements and learning retry proof preserved (see below).
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

The initial checkpoint required the checked head to be merged, 34 restored /
132 LOCKs / zero generations, exact Site GET readback and separate NIGHT and
Goripan settlement verification. These have now been observed as recorded below.

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

## Deployed and verified (21:29 JST)

- PR #162 merged as `e77ad4adfa43f9ef812d5aaf4e56fdd73c59b469` at
  21:26 JST. Its tree `fe53b8be807290a8d706ca75263bf52980834941` exactly
  matches tested head `245f0e0217d8c167e27b1c55d3aec89a7cca529a`.
- Required final-head CI `42480043920` and post-merge CI `42480270468`
  passed, including 13 publication-continuity tests using the actual private
  model/LOCK verifier. Reusable workflow:
  `1bdabbdcce8b3a3b1b43f88b42ecbdaeabe45d8d`; immutable public helper:
  `57883a3ba0a77d674f749acfaba0bbc2054a05ee`.
- The natural push-triggered production run `42480270537`, job `128730475327`,
  completed SUCCESS. At 21:27:38 it reported 34 restored original LOCKs,
  132 total and **zero prediction generations by recovery**.
- At 21:27:56 existing official settlement added exactly 34 records; total
  **132**, exclusions 0, settlement COMPLETE. No official result acquisition
  was run by the recovery helper.
- Encrypted artifact `13616909263` was finalized before publication at 21:28:15,
  1,419,795 bytes, ZIP SHA256
  `1b78d7aab31e689c7d47e608eaa950b783ae41d10facbad64aabd85cc2b17507`.
- At 21:28:31 the normal publisher reported `GORIPAN_SITE_GET_READBACK=PASS`
  and `GORIPAN_SAVED_LOCKS=132`.
- An independent authenticated Site read returned 10,415,484 bytes with matching
  response/header SHA256
  `6ed2821dc5937186e5efdd6decc74529990332118ee0eff7d5015dec6740a3d3`.
  It contained all 132 Goripan LOCKs and all 132 settlements. Every prior 98
  Goripan record, including its settlement, equaled the pre-repair read.
  The 34 appended records matched their earliest original saved timestamps,
  deadlines and bet counts; each settlement referenced its delivered LOCK hash.
  All P3 and Muki saved prediction fields remained equal. Canonical NIGHT had
  132 official results, 0 pending and 125 formal P3 races (7 pre-existing morning
  exclusions remain separate). This is server-data verification, not proof of
  earlier phone/push arrival.

## Remaining separate checkpoint: learning proof

Learning is still `TECHNICAL_HOLD`, now `NIGHT_RETRY_INPUT_CHANGED`, with
`last_completed_night = null`. The immutable retry cohort was bound while only
98 originals were available. Restoring the missing originals correctly changes
that proof set; the existing learning guard refuses to silently rebase it.
Do not call learning complete, delete the original retry binding, reset a
model/generation, or regenerate/retrain P3. Delivery and financial settlement
above are complete independently of this hold.

NEXT_FIRST_GAP: inspect the preserved 98-record retry proof and sealed recovery
receipt in artifact `13616909263`. Establish an explicit append-only provenance
path for the 34 recovered originals before proposing a learning-cohort transition.
Any future learning action must preserve the original proof/history, use the
existing model-specific authorization, and remain separate from completed
prediction/settlement/publication. Do not repeat donor downloads or original
recovery: the receipt makes subsequent normal Nov 8 runs a verified no-op.
