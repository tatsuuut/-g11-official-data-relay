# Normal operation latency repair — 2026-11-08

## Observed gap

The November 8 fast cycle reduced median LIVE job duration, but did not remove
the delay between a saved final prediction and server readback. In the compared
10:15–11:30 JST deadline window, P3 capture-to-readback was about 85 seconds on
both days. Goripan was about 161 seconds on November 8. These are server
readbacks, not phone display or push arrival measurements.

One sampled Goripan job spent 28 seconds installing the same public dependency
before restoring state. The primary LIVE job spent about 25 seconds installing
public dependencies after publication, holding its serialized slot and delaying
the next capture. Both workflows also scanned the compressed complete archive
before extracting it, forcing another gzip pass.

## Change

- Reuse an isolated public Python environment, keyed by the pinned package
  requirements, exact Python binary/version, runner image, architecture and
  absolute environment path. Check exact direct versions, importability and the
  recorded resolved distribution list on every hit. Rebuild on a miss or failed
  validation. Cache outages fall back to the existing pinned pip installation.
- Cache only venv-created files and public package dependencies. Save before any
  private code runs. Never cache private source, models, credentials or race data
  in this cache. `lxml==6.1.1` and `catboost==1.2.8` stay pinned.
- Restore the entire authenticated state in one gzip pass into a disposable
  sibling directory. Reject unsafe/duplicate paths, links, device entries, empty
  archives, truncated content and corrupt gzip CRC before exposing the result.
  A nonempty existing state is never overwritten.
- Keep the original full AES256 checkpoint, exact cache-key readback, hashes,
  daily source/model binding and immutable LOCK checks. No small/incomplete
  checkpoint replaces the complete saved state.
- Keep primary dependency preparation after critical publication and successor
  dispatch. Goripan uses the same reviewed public helper through an immutable
  action ref; its inference, learning, settlement and notification blocks remain
  unchanged.

## Validation and resume point

Required: restoration byte equality and negative cases, cache hit/miss/import
failure tests, real-runner repeated preparation, all existing runtime contracts,
and protected Goripan logic hashes. The reusable workflow must be pinned in both
the main caller and the contract gate to the reviewed new commit before merge.

After merge, inspect the next naturally scheduled LIVE and Goripan jobs for
`G11_COMPLETE_STATE_EXTRACT=PASS` and `G11_PUBLIC_PYTHON_ACTION=REUSED_VERIFIED`,
then compare the next unexpired races' saved timestamps and server readbacks.
Cold caches still install once. No historical prediction or NIGHT rerun is
authorized or required for this validation. Do not label every final prediction
as delivered five minutes before deadline until actual readbacks establish it.

## Deployed result and next gap (2026-11-08, 12:07 JST)

- PR #160 merged as `41c2fe56e69b387211ccb9de902a29fd463de7d2` at
  11:59 JST. Its tree exactly equals the tested head
  `a21b864bae7f466980439269944854f494dba73d`.
- Reusable Goripan workflow: `94e0de64725df5014addd6724cac7a3e5935480e`;
  immutable public helper: `ef232393e67b6de01407ae1a988f36fcc7ed9b05`.
  Only preparation and complete-state restoration changed from the deployed
  reusable baseline. The seven protected source/model/inference/settlement/
  learning blocks retain their original hashes.
- Final-head contract run `42440606726`: both `contract` and
  `public-runtime-preparation` passed. Post-merge contract run `42440776887`
  also passed. The first real-runner test caught launcher-path instability;
  binding to the base interpreter fixed it before production activation.
- Normal primary run `42440916106`, job `128610106052`: SUCCESS. Complete
  restoration of 52,009 archive members; verified early and final full-state
  persistence; one new formal P3 and one Muki LOCK; application GET readback
  PASS; zero prediction recalculation by publication. The private daily source
  remained `18ceb523ea2136a4b8173d9d20a78945cb4c2cda`.
- Goripan runs `42440873120` / `42441175567`: SUCCESS, one new LOCK each,
  application GET readback PASS. The first warm production preparation took
  4.1 seconds including restore (verification itself 0.489 seconds), compared
  with the earlier sampled 28-second preparation. The next warm preparation
  took 3.5 seconds. Primary preparation also reused the verified environment,
  taking about 4.1 seconds including restore.

For the same new race, Tokoname 5R (`20261108-08-05`, deadline 12:10 JST):

| Saved/verified event | JST | Time before deadline |
| --- | --- | --- |
| Official odds captured | 12:03:39 | 6m21s |
| P3 and Muki application readback | 12:05:13.950 | 4m46.050s |
| Goripan LOCK saved/read back locally | 12:05:31.831 | 4m28.169s |
| Goripan application readback | 12:05:55.786 | 4m04.214s |

This proves the preparation repair and successful normal delivery, **not**
universal T-5-minute delivery or phone/push arrival. The five-minute target is
still missed in this sample. Do not call overall delivery latency resolved.

NEXT_FIRST_GAP: the remaining serialized queue and complete-checkpoint handoff
cost. In this primary run, critical work ended at 12:04:16.978, complete state
encryption ended at 12:04:35.649, verified cache persistence at 12:04:48.778,
and application readback at 12:05:13.950. A future compact handoff must first
prove complete reconstruction from an immutable verified parent plus a verified
delta. It must not omit history, skip persistence/readback, weaken hashes,
change frozen models or regenerate old predictions. Resume from these timings
and the new main source; do not repeat the November 7/8 comparison or replay
completed operations. No NIGHT job was manually run by this repair.
