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

### Rollout and handoff, November 12

- [PR 179](https://github.com/tatsuuut/-g11-official-data-relay/pull/179) merged
  as `abd2348b3aceee428c5f85b11d3cd0e651dcb547`. Final pre-merge head
  `0556b2cbb56745ba61990799f93389edde6b2e63` passed all applicable jobs in
  [contract run 43222017469](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43222017469).
- The deployed source passed
  [contract run 43222479679](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43222479679)
  and [runtime validation 43222479604](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43222479604).
  The latter reports `DRY_RUN_PASS`; its next-day authority check confirms
  `prediction_executed=false`, `production_state_mutated=false` and
  `site_write=false`. This is prospective configuration validation, not a
  claim that the November 13 MORNING/LIVE/NIGHT cycle has already operated.
- [Deployed watchdog 43222479521](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43222479521)
  passed and correctly returned `NIGHT_ALREADY_PUBLISHED`. The new dependent
  recovery job is present and correctly skipped after a successful runtime;
  no production failure was injected to force it to run.
- After rollout, the accepted NIGHT app feed still has all 154 results and
  142 READY observations. Protected fields across all 154 races (morning/live
  hash fields, saved P3 bets, imitation records, WILD projections and
  result/payout fields) match the pre-rollout readback, including unchanged
  absences. Their combined SHA-256 is
  `2db91bcf0042879d5337ed5004c3dd5ac1a5f9926ee6554c399c90eae719fdf8`.
  Today's runtime source and daily authority are unchanged.
- Implementation, reproduction tests and production source deployment are
  complete. Historical PREDEADLINE coverage remains **PARTIAL: 142/154**;
  the twelve missing observations above cannot be recreated after cutoff.
  The next natural LIVE chain and a natural failure-driven recovery remain
  prospective operation checks. Do not mark the historical gap or an
  unobserved future full cycle as a completed normal-operation success.


## November 14: authenticated full-checkpoint acceleration

The post-reservation pipeline still encrypts and reads back every complete
checkpoint. Observed same-day archives were approximately 1.45 GB, with 4.27 GB
of source files and 58,108 members. A read-only comparison on an authenticated
saved November 14 state found gzip encryption at 47.346 seconds, two-worker
pigz at 22.021 seconds, and four-worker pigz at 21.144 seconds. Every restored
file/directory matched, and source state remained identical. The CPU-only
change is not adopted: less than one second per checkpoint is insufficient.

The revised candidate combines supported authenticated OCB with available CPU
affinity bounded to four pigz workers (standard gzip fallback). A legacy writer
keeps two workers. It retains complete tar contents, AES256, passphrase handling,
exact encrypted readbacks and
GPG-authenticated restoration before extraction. It selects the writer's
supported authenticated OCB option, preferring --use-ocb-sym, then --force-ocb,
then the older --force-aead alias. A writer without these capabilities retains
its existing authenticated CFB+MDC mode. An encryption failure never triggers
a downgrade/retry under a different mode.

GnuPG documents OCB symmetric decryption support since version 2.2.21:
https://www.gnupg.org/documentation/manuals/gnupg/OpenPGP-Options.html
The main relay and pinned WILD4 reader run on Ubuntu 24.04. Separate CI jobs
verify a public synthetic OCB fixture with ordinary GPG readers on both Ubuntu
22.04 and 24.04, including rejection of truncation, tampering and wrong passwords.
Only the synthetic fixture is passed between these jobs; private state is never
exported as an artifact or job output.

A read-only branch-scoped benchmark compares full AES256 CFB+MDC and AES256 OCB
archives using a disposable saved checkpoint. Both are decrypted and fully
restored, and every file/directory is compared using size and SHA256. Only
aggregate timings and success/failure codes are logged. No prediction is
published or regenerated, and no saved source, LOCK, or accounting is changed.
Measured results and production timing must be checked before rollout.


The full-state cipher-only comparison also passed both reader generations and
all byte comparisons, but reduced 25.983 seconds to 22.759 seconds; that alone
is not a sufficient improvement. The next comparison includes ISA-L igzip's
standard gzip-compatible level 0 stream and native decompression. The native
reader retains all existing tar member checks and data filters, drains past
the tar end, requires successful gzip CRC/footer completion, and exposes the
staged state atomically only after the decompressor exits successfully. Python
gzip remains the fallback. The system package is optional with a bounded setup
attempt; no private runtime package, model or source is changed.

The three paired variants are legacy pigz/Python gzip, ISA-L with the legacy
cipher, and ISA-L with OCB. Tests cover a late native decompressor failure,
concatenated gzip members, late CRC corruption, traversal, special files,
nonempty destinations and byte-identical roundtrips. The benchmark compares
all saved-state bytes after each candidate and reports archive size as well as
encryption and extraction time. Production timing must include setup and cache
transfer, rather than treating CPU-only improvements as end-to-end gains.

ISA-L igzip's standard gzip interface is documented by its upstream:
https://github.com/intel/isa-l/blob/master/programs/igzip.1


Level 0 ISA-L/OCB reduced full encryption from 23.257 to 13.219 seconds and
extraction from 18.918 to 16.746 seconds, with every file unchanged. Its archive
increased from 1,447,935,823 to 1,771,584,034 bytes (about 22%), so it is not
adopted on CPU timing alone. The final comparison evaluates standard-compatible
ISA-L levels 1 and 2 to account for cache transfer cost. Native corruption and
full real encryption roundtrip tests are required by the public-runtime gate
on future PRs, independent from the one-off saved-state benchmark.
