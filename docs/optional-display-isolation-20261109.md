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

Implementation is not live completion. Required next checks: exact-head CI,
normal MORNING recovery, Site day/stage/count/LOCK readback, and the next normal
invocation. Then append observed run IDs and remaining research hold here.
