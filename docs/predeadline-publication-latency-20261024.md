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
