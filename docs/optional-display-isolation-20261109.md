# Optional display research must not stop normal operation

This implements the already approved operational priority: an unavailable race
or independent research component cannot stop healthy morning/predeadline/night
delivery. Official-source, model/source identity, LOCK, result and transport
checks remain mandatory. Missing research is not success, an exclusion of a
formal prediction, or permission to invent missing data.

## November 9 incident

Run 42507069817 saved encrypted state after failing in `DISPLAY_RACE_PARTITION`.
Before that, optional morning research reported `PROBABILITY_120`. The research
writer did not complete its current-day manifest, but the public feed reader
required its completed/excluded partition to match today's races. That optional
failure escaped through `build_feed`, so no morning feed was emitted. The Site
still served November 8 NIGHT. The independently quarantined Miyajima 6R input
(`B_ENTRY_CARDINALITY:17:6:5`) does not establish the cause of this research error.

## Change

The original-display reader has a narrow exception boundary. Its normal return
is unchanged. A failure produces an unavailable research summary with
`TECHNICAL_HOLD`, no claimed audit/capture/settlement, and unknown per-race research
fields. Research files are retained byte for byte. Nothing catches errors in
the rest of feed construction, authority checks, official settlement or delivery.
The normal MORNING/NIGHT entry, retained LIVE entry and saved critical publication
all use this boundary. This changes no prediction, model, notification rule or
generation. It does not retrain P3 or repair historical research evidence.

November 9 also needs saved-only replay because the failure happened before
`MORNING_COMPLETE` was recorded. The incident-specific route verifies the exact
frozen source, every saved morning snapshot, the current locked publication and
current-day window. It builds a feed from saved values, applies the original
post-build seals, and records completion. It never acquires data or generates a
prediction. An existing completed morning bypasses this one-time recovery.

## Validation and next checkpoint

The PR gate restores cache `g11-state-v4-published-2026-11-09-42507069817` to a
disposable directory. It must reproduce the partition error, prove successful
replay with acquisition and prediction entrypoints forbidden, and verify that
all store files and the original authority remain byte identical. No cache save
or production publication occurs in this test. Fault tests cover incomplete,
malformed and unreadable research, healthy pass-through, nested scopes, and
continued rejection of core failures.

## Observed completion

- PR 164 tested head `cba9b30628fb994a9267aa6f80432db499449cb6`; complete runtime
  gate `42518176615` passed. The actual-checkpoint audit reproduced the failure,
  projected 131 formal races with acquisition/inference forbidden, and retained
  all 36,763 store files plus the original daily authority byte for byte.
- Merge `190ca52aba143c03c0c1a090d9548c853a3ff853` has the exact tested tree
  `a6725861dd8b7a0e85b11406f3079987e1649790`. Its post-merge gate also passed
  (`42518393116`).
- Production `42518393134`, job `128846321013`, completed successfully. At
  05:07:49 JST it reported saved MORNING replay with prediction recalculation
  zero. Publication state readback passed at 05:09:54; Site readback passed at
  05:10:35. The V53 morning readback passed too.
- Independent authenticated GETs confirmed latest and `view=v53-final` both
  show 2026-11-09 / MORNING / PASS, 131 formal races, 131 P3 saved predictions
  and 131 saved Muki morning projections. App-view body SHA-256:
  `35bf1358665b3dcacde3517b4d0623572024b75d2b9eea40d2d5182a01328d6b`.
  Frozen private source is still `5e728bb8ab2cdd608fbd4aeddb62c288dd1efa71`.
- November 8 canonical NIGHT remains 132 results / 125 formal / zero pending;
  its independent GET bytes stayed exactly identical (SHA-256
  `e22db8a52218f8c8d450706ef8213b78f00b3b45a3da165495cd5ec5aaec10e6`).
- A separate optional Deme timeout (`THIRTY_OPTIONAL_TIMEOUT`) occurred during
  this production run and stayed research-only; it did not prevent publication.

Remaining: original-display research is correctly TECHNICAL_HOLD, not repaired
or counted as completed. The single Miyajima 6R input remains quarantined; no
sixth entry was invented. Goripan's separate app status was
WAITING_CANONICAL_LOCK, not claimed as delivered by this repair. The next normal
scheduled MORNING invocation and today's live/night phases remain future checks;
do not describe the regression gate as their live completion. The next minimal
check is retained 131 morning LOCKs and continued delivery on the next existing
normal invocation. Do not rerun research or re-create predictions for this check.
