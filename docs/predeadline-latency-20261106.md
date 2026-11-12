# PREDEADLINE latency follow-up, 2026-11-06

The earlier saved-only publication path is active, but it does not remove
the predecessor's remaining work from the serialized acquisition queue.

Observed 2026-11-05 evidence (JST):

- Run 41958528839 dispatched its successor at 12:05:28, then retained the
  shared runtime slot through approximately 12:10:33. The successor
  41959374631 started its runner at 12:10:41.
- Miyajima 5R closed at 12:14. Its official odds were saved at 12:11:24,
  its P3 confidence record at 12:12:06, and the critical canonical readback
  passed at 12:13:18. This was late in the buying window despite successful
  acquisition and publication. Readback is not device push arrival.
- The two sampled jobs restored approximately 806 MB of encrypted state.
  Each job encrypted the full state three times; each observed encryption
  step occupied approximately 29 seconds. P3 research occupied another
  58-68 seconds before the regular publication. Dependency installation and
  the private runtime also consumed time; they are not removed by this patch.

Changes:

1. Credit time spent persisting and publishing the critical checkpoint toward
   the existing 45-second continuation spacing. If those operations already
   consumed 45 seconds, do not sleep another 45. Bind the calculation to the
   same day's successful critical run; reject future or mismatched timestamps.
   The original official-host pacing, request budget and capture windows stay.
2. Keep the complete gzip/AES256 archive and exact cache readback. Prefer
   two-thread pigz when installed, otherwise gzip; tell GPG not to recompress
   the gzip stream. Keep atomic replacement, secret file-descriptor input and
   prior checkpoint retention on failure. Do not omit state or model files.
3. For normal PREDEADLINE, run the existing P3 research update after canonical
   prediction publication. Persist and read back that research state before
   sending its existing dedicated projection. Preserve MORNING/NIGHT/rescue order,
   every research invocation, failure isolation, original LOCKs and all
   prediction/model pins. A failed canonical publication still retains a
   successful research update, without publishing the research projection.
   Keep the original receipt-cache behavior outside deferred LIVE research,
   including NIGHT retries; only the new research delivery needs another
   verified checkpoint.
4. Log per-race remaining time at verified early publication, including
   five-minute misses and post-deadline readbacks. This is a server-readback
   measurement, not a device-arrival guarantee.

Validation covers the actual workflow continuation shell, spacing boundary
and clock failures, deferred research authority/credential isolation, complete
gzip/AES256 roundtrips, archive failure retention, and unchanged source bytes.
Real GPG roundtrip is mandatory in contract CI; a local environment without a
working gpg-agent cannot supply that evidence. Full runtime contracts and the
existing regression suite remain required before merge.

These changes reduce avoidable work and prioritize publication. They do not
prove universal T-5 delivery: GitHub runner queues, official content, remaining
runtime work and Goripan's separate inference/publication must be measured on
new races. Do not replay historical races or rewrite LOCKs to create evidence.

## November 12 continuation outage and recovery

Run [43186192542](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43186192542)
saved and published its critical checkpoint, then at 17:54:40-44 JST received
three `in_progress` index responses reporting two runs but returning one.
`RUN_LIST_INCOMPLETE` correctly prevented an unverified dispatch. The final
failure was reported at 17:56:36. No successor appeared until run
43206088425 at 20:05:27. The independent cron supervisor did not close this gap;
failure-driven recovery must not depend solely on a delayed cron or on a
workflow-name completion event.

The status-filtered index still requires complete pages and bounded rereads.
For continuation only, a persistent count mismatch now permits an independent
proof using the main-branch index without a status filter: complete pages,
strict descending unique run numbers, stable page totals, and the exact
current run ID/number as the lower boundary. Every newer active same-day LIVE
run blocks dispatch. Missing identity, malformed or incomplete pages, moving
pagination, or an absent boundary still prevents POST. Bootstrap/rerun retain
their original all-active-run requirement. Ambiguous POST responses still
require fresh reconciliation before any retry. The workflow calls this shared
guard directly, replacing a redundant shell/API precheck with the same newer
active-run protection, including runs without allocated jobs.
The three-phase static contract follows that shared guard and additionally
requires completeness, ordering, stable pagination and exact boundary checks.
Mutation tests prove that removing those guards still fails the audit; no
public-surface allowlist, size limit or safety check is relaxed.

A separate public-only job now follows a failed first-attempt PREDEADLINE
runtime. It verifies the exact current-day main-branch run, completed failed
runtime, successful encrypted critical checkpoint and explicit continuation
failure. A still-running job, unrelated failure, user cancellation, another
active run, previous day, completed NIGHT or last-race cutoff cannot authorize
recovery. It rereads the accepted feed and active runs, then requests a fresh
normal invocation through the bounded dispatcher. It never reruns the old
identity, decrypts/restores state, invokes a model, or publishes predictions.
Existing runtime concurrency, cutoff, immutable LOCK, exact persistence and
normal publication/NIGHT checks remain in force. The recovery job has only
contents-read/actions-write permission and a three-minute timeout.

The accepted app feed, generated at 20:59:27 JST and read back after 21:40,
contains 154 official results and 142 READY PREDEADLINE observations. Twelve
races in the outage window have no retained odds/READY observation:

| Venue | Races | Deadlines JST |
| --- | --- | --- |
| Gamagori | 8-11 | 18:23, 18:54, 19:25, 19:58 |
| Shimonoseki | 7-10 | 18:04, 18:33, 19:04, 19:36 |
| Omura | 7-10 | 18:13, 18:43, 19:14, 19:47 |

These are retained gaps, not reconstructed predictions or formal LIVE
successes. Existing MORNING records and official NIGHT results remain usable
under their own eligibility rules. NIGHT publication is not proof that the
twelve missing PREDEADLINE observations existed.

Regression coverage reproduces the observed count mismatch, incomplete and
unordered independent indexes, missing boundaries, every active state,
multi-page reconciliation, an ambiguous accepted POST, failed-job identity,
parallel successor arrival, old dates, NIGHT/cutoff gates, original immutable
inputs and the actual dependency/permission wiring. Existing public-surface,
workflow lint, private regression and saved-state contracts remain required.
Production rollout and prospective recovery observations are recorded below;
fault-injection success alone is not a new live-operation observation.
