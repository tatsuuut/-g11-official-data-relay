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
