# PREDEADLINE publication delay repair, 2026-10-24

The 20:36 JST Suminoe 12R deadline exposed avoidable publication delay.
Run 40090030558 saved P3 and V53 by 20:31:10, persisted the critical
checkpoint by 20:31:30, and confirmed Site GET only at 20:34:23.
The browser then polled once per 60 seconds. Goripan run 40090250550
started at 20:32:37 after waiting for run 40090169085, whose input check
reported zero unexpired races. Goripan Site GET succeeded at 20:34:19.

Changes:

- The existing early encrypted checkpoint now uses exact save/readback/hash
  verification. After its WILD4 handoff, a new saved-only publication adapter
  reads the frozen P3/V53 public projections and uses the existing Site
  authenticated transport before the 45-second continuation wait and optional
  dependency/research work. No inference, acquisition, settlement, authority
  resealing or model update is introduced by this publication adapter.
- Failure in early projection/publication retains the regular publication
  and continuation paths. Normal optional input/research delivery still runs.
- A follow-up that confirmed no outstanding inputs no longer dispatches an
  empty observer from the delivery/failure fallback. Existing public Goripan
  LOCKs are excluded only after hash-verified public readback and exact
  race/day/deadline checks. Unavailable readback preserves the retry path.
- The Site uses completion-based 15-second refresh for the current day,
  immediate focus/visibility refresh, and pauses hidden-tab reads. A slow
  response is not aborted by the next polling interval; history keeps 60s.
- HOME's saved odds evidence for freshly published V53 races is sent in the
  early lane too, so condition cards need not wait for the full historical
  evidence pass. Both evidence senders prioritize unexpired races while
  retaining every historical record and its existing validation.
- Repeated `HOT_DELTA_EMPTY` failures on unchanged-race cycles came from a
  research-only payload whose microsecond timestamp does not meet this
  route's seconds-only JST contract. Such metadata is left byte-for-byte
  intact and delivered through the existing dedicated research endpoint.
  Canonical no-change delivery no longer retries this incompatible sidecar.
- Public Goripan deduplication uses the existing curl transport with a bounded
  six-second request and exact final-response hash. The former plain urllib
  read returned HTTP 403 while the canonical client succeeded; failures still
  preserve the original retry instead of suppressing an unpublished race.
- When no eligible race is currently inside its capture window, a critical
  invocation may wait once, at most five minutes, for the nearest saved official
  T-7 boundary. This avoids starting optional work just before the first input
  becomes due. An active or partially saved race never waits. The frozen
  collector still has no between-race waits and its eight-source limit; no
  input is acquired before the recorded official boundary.

Protected: frozen private source cf2769886e857369630512d2301558f6c17188a6,
existing forecast rules/models, saved morning and predeadline LOCKs/BETs,
official capture window, host pacing, 45-second continuation placement,
settlement and daily research/promotion. Compression evaluation and the
separate Abeken reproduction study are outside this change.

Validation is recorded separately from live race evidence. Passing the
contract gate does not prove any guaranteed five-minute publication margin.

## Validation and staged rollout evidence

- PR 112 gate 40091813520 and PR 113 gate 40092399630 passed before merge.
  The merged PR 113 head also passed gate 40092532720.
- Site v304 (`g11-source-15f3f1b20c1af8cb4c5a5f9b`) deployed successfully.
  Refresh scheduling tests, typecheck and production build passed. A separately
  checked existing analysis-view test has an unrelated baseline label mismatch
  (3/4 passed); its source and test were unchanged by this repair.
- Exact comparison of 790 previously public saved forecast objects found no
  changes. Frozen source and Goripan model identities stayed unchanged.
- Wakamatsu 8R, deadline 21:17 JST: transition run 40092523275 captured odds at
  21:10:51 and canonical Site readback passed at 21:12:48.727 (4m11s remaining).
  The v304 HOME updated automatically from 158 to 159 current P3 races.
  This race entered the regular acquisition path, so it does not establish
  the latency of the new critical-checkpoint publication path.
- Goripan run 40092987837 started at 21:12:41 without an empty predecessor,
  locked Wakamatsu 8R at 21:13:55.721 and passed Site GET at 21:14:23.755
  (2m36s remaining). Its new odds evidence was acknowledged at 21:14:25.120
  before historical evidence; this saved abstention correctly had no formal
  probability-proof record. The separate optional player-context import hold
  remained isolated and was not changed by this task.
- PR 114 gate 40093387407 passed, including the real dependency-free projector
  import and exact public-response hash/timeout cases. PR 115 gate 40094624411
  passed the expanded 18 publication/alignment tests and the full runtime gate;
  merged head d051823f2282af0c9b50231fd2d0afa5d55c3972 passed 40094719713 too.
- New-code run 40092820048 passed canonical no-change publication at
  21:17:05.252 without `HOT_DELTA_EMPTY` retries. Its dedicated research
  readback passed at 21:17:32.538; several subsequent runs also completed.
- Wakamatsu 9R, deadline 21:41: run 40094152506 acquired odds at 21:35:02,
  verified public readback at 21:36:24.021 (4m35s remaining), and the bounded
  Goripan public-lock check succeeded. Goripan run 40094610921 verified Site
  GET at 21:37:58.224 (3m1s remaining); current odds and its one saved
  probability proof were delivered before all 144 historical odds records and
  141 historical proofs. This run predates the capture-alignment addition.

## Completed live verification: Wakamatsu 10R

The official saved deadline was 22:05 JST. Run 40095817559 used the merged
capture-alignment and early-publication code end to end:

- The verified-plan alignment waited once for 39.907 seconds. V53's immutable
  LIVE timestamp is 21:58:02; official odds/P3 input is 21:58:11. The existing
  encrypted checkpoint was saved and read back by 21:58:44.
- Early publication began at 21:58:46. The Site canonical receipt header is
  21:58:58.347; exact Site GET readback passed at 21:59:09.106, with 5m50s
  remaining. HOME visibly refreshed itself from 160 to 161 current P3 races
  during the 21:59 check. Saved odds evidence was acknowledged at
  21:59:10.436 and the complete early-publication receipt passed at
  21:59:18.969, with zero prediction recalculation.
- Goripan run 40096167635 was handed off at 21:58:46 and started at 21:58:50.
  Its immutable LOCK timestamp is 22:00:01.066937; exact Site GET readback
  passed at 22:00:26.199 (4m33s remaining). The unchanged active model SHA is
  2d17db06cf9d7f0e73501e84b14f23621a42abb8a4ea1da3ef9d3fd07ac6c412.
  New odds evidence was acknowledged at 22:00:28.026 and its probability
  proof was delivered before historical records. The WILD4 market record was
  saved at 22:00:43.658 and its notification event was created at
  22:00:48.604 (4m11s remaining). These event timestamps are saved server
  evidence, not a claim of device push arrival at that exact instant.
- The regular publication in the same run also passed, at 22:02:29.625.
  The separate sequence slots therefore worked with a real early/regular pair.
  Follow-up correctly found no remaining inputs, and the dedicated research
  delivery passed at 22:03:14.594. Both workflows completed successfully.
- A second exact comparison after early publication still found 790 prior
  saved forecast objects, zero missing and zero changed, with the frozen
  runtime source unchanged. Fourteen new saved forecast objects had accumulated
  since the baseline; the new Goripan 10R LOCK followed separately.

This is measured improvement for the observed race, not a universal deadline
guarantee. Official input availability, runner scheduling and transport still
affect each race's available margin. Existing fallback and daily research
delivery remain active.
