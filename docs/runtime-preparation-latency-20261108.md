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
