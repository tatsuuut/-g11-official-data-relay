# Short LIVE cycle, effective 2026-11-08 JST

The November 7 audit found that early publication did not release the serialized
runner: duplicate official capture, repeated morning preparation, discovery
research and redundant full encrypted checkpoints still delayed its successor.
In one observed race P3/ABEKEN server readback finished only 14.5 seconds before
the deadline; a different WILD readback finished after its deadline. An early
dispatch by itself therefore did not fix delivery latency.

From November 8, current-day normal PREDEADLINE with verified frozen authority:

1. Run the existing critical official capture and immutable P3/ABEKEN predictors.
2. Verify the complete encrypted checkpoint, dispatch WILD using its exact key,
   and publish the saved canonical LOCKs with server readback.
3. Queue the successor. Retain saved-input WILD PRE_RACE/BET and P3 prospective
   confidence/MASHIRO sidecars under the existing deadline and integrity guards.
4. Persist those sidecars and early delivery receipts once, then release the
   serialized slot. Do not repeat official acquisition, morning model preparation,
   the regular full publication, or the third encrypted checkpoint.

The heavier discovery refresh remains on the existing MORNING/NIGHT path; it
does not hold up each LIVE cycle. Existing discovery records are preserved.
Missing prospective research is never reconstructed after a race. The WILD and
P3 prospective sidecars required for NIGHT evaluation remain in the short cycle.
NIGHT settlement, generation history, notification adoption thresholds, model
coefficients, host pacing, source validation and the T-minus-seven capture gate
are unchanged. Historical/rescue paths are unchanged.

Saved-only publication retries even with zero new LOCKs. An early transport
failure gets another attempt after verified persistence; a later cycle can also
retry. WILD follow-up still checks saved unexpired inputs and public LOCKs.
Sidecar failure cannot replace the last verified critical checkpoint. All writers
remain serialized: no stale parallel full-state writer is introduced.

The app reduces the visible live polling gap from 15 to 5 seconds on November 8,
without interrupting slow responses or overlapping reads. This gap is measured
after a completed request; it is not a claim of five-second end-to-end delivery.

Verification: date boundaries, non-LIVE/rescue routing, immutable LOCK guards,
saved-only retries, independent sidecar failures and workflow persistence routing
are covered by the required runtime contract gate. Production timing still needs
November 8 observations. Keep reporting per-race server readback remaining time
and five-minute-target misses; do not label a green job as timely device arrival.


## November 14 capture-window reservation

The observed 13:00 JST Toda 6R and Biwako 6R forecasts were LOCKed before
cutoff, but the audited server readback finished at 13:00:31.674. This is
server evidence, not a device-arrival timestamp or proof of first visibility.
The previous run dispatched its successor at 12:54:18 but kept the shared
LIVE slot until 12:56:40 while performing optional research and persistence.

After successful critical publication, the short LIVE path now reads the
verified morning plan and formal scope. If an unsaved, unexpired source is
already due or due within 300 seconds, it defers optional dependency setup,
WILD/P3 research sidecars and the research-source checkout. An unreadable or
invalid schedule also defers only optional work. Expired and already saved
sources do not prevent research during an otherwise clear window.

The complete encrypted post-delivery checkpoint still runs and is read back,
so delivery receipts remain durable. The early encrypted checkpoint, OIDC
publication, next-cycle dispatch, WILD4 handoff, frozen runtime, prediction
cutoff guards, MORNING/NIGHT routing and saved LOCK bytes retain their existing
authority. Deferred research is explicitly reported and is not counted as
completed. Prospective research may have fewer timely samples during busy
windows; no post-cutoff prediction is promoted to a timely one.

The reservation removes optional contention; it does not guarantee runner
startup latency, a five-minute publication margin, or device receipt. Validate
new production runs using G11_PREDEADLINE_PUBLICATION_TIMING and the explicit
G11_FAST_RESEARCH_BUDGET log, preserving observed late deliveries as evidence.
