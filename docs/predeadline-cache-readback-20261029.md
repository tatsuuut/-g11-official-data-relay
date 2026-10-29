# PREDEADLINE checkpoint visibility and continuation repair

Observed failure: run 40813928711 captured timely P3/V53 inputs at 12:49 JST,
saved the encrypted cache successfully, then the immediate exact-key restore
reported a cache miss. The verification failure correctly withheld publication,
but also suppressed WILD4 handoff and the next capture dispatch.

Changes:
- Retry the same exact-key restore up to three times, with 3 and 7 second waits.
- Require an exact hit and unchanged encrypted SHA-256 before opening the existing
  publication gate. A local archive, prefix hit or corrupt restore is insufficient.
- Continue the existing bounded PREDEADLINE capture chain after this readback
  failure. This does not publish the unverified state or pass it to WILD4.
- Keep cancellation, successful capture, encryption, current phase, last-race
  boundary and active-run deduplication guards. Preserve MORNING/NIGHT routes.

No changes to prediction rules, current-day source pin, model, past LOCKs,
settlement, morning schedule or night handoff. Missing past predictions must not
be generated after their deadline. WILD4 remains gated on verified saved inputs.

Validation: actual composite shell gates exercised with mocked transient misses,
persistent misses and corrupt bytes; existing heartbeat and WILD4 handoff tests;
three-phase static contract and NIGHT boundary self-tests. Production recovery
is assessed separately using subsequent workflow and delivery evidence.

## Production verification (2026-10-29, JST)

- Corrected source: `159147ec95a01d9c834df4f4824527d18d2c6324`.
  The first repair commit was held by the public-file allowlist; the follow-up
  added this reviewed document and aligned the duplicate capture-order guard.
  Both complete public audits now pass; no publication guard was removed.
- 27 checkpoint/heartbeat/WILD4-handoff tests pass; the existing NIGHT boundary
  self-test passes all 34 checks. MORNING/PREDEADLINE/NIGHT contract audit passes.
- Relay run `40818348506` completed successfully at 13:38:57. Critical capture
  saved P3 and V53 for `20261029-03-07` and `20261029-14-11`; verified early
  publication passed at 13:34:43. Regular delivery/readback also passed.
- WILD4 run `40818505864` completed successfully. For Naruto 11R (13:39 cutoff),
  official odds were captured at 13:33:46, its existing model saved an 8-ticket
  LOCK at 13:35:50.238, and exact Site GET readback passed at 13:36:05.872.
  Source LOCK mutation count was zero. This is an observed timing, not a claim
  that every race will have the same remaining margin.
- Continuation run `40818611078` reached successful early checkpoint, WILD4
  handoff and critical publication, confirming that the next acquisition ran.
- The recovery cycle reported Amagasaki 6R's BEFOREINFO/ODDS3T as missed. No
  after-deadline prediction was generated to fill this gap. The screenshot's
  separate P3 morning-unconfirmed state is not relabelled by this repair.
- Current-day runtime source remains `1c95a497d5af5efceae33836d712860c41f077df`;
  authority remains `83390ec9974c63479d11c42c1e2205359ca22f4a281daff21d7fe206cdc2b787`.
