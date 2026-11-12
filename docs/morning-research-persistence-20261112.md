# MORNING research persistence — November 12

The 24-race capture in run 43144255621 completed locally, then the research
history push failed with HTTP 403. That run uploaded no artifact and its
normal path was absent from the remote runtime tree. Its log records counts
and identifiers, not the original 120-way odds payloads. The lost capture is
not reconstructed from later odds or results and is not counted as retained
research. A delayed schedule also ran outside the intended MORNING window.

## Persistent research transport

The existing private credential remains read-only. Detector source and the
legacy research history are checked out without persisted Git credentials.
No branch-write permission, production source, prediction, model, saved LOCK,
official ledger, notification or purchase configuration is changed.

The research job restores the newest verified main-branch encrypted checkpoint
from its own workflow. Each checkpoint carries the entire retained research
history, including prior calibration days, plus a SHA-256 index and a coverage
report. Immutable files must match the legacy history byte for byte. A bad or
expired newest checkpoint fails closed instead of silently rolling backward.

After collection or settlement, including partial failures, the job encrypts
the history with the existing AES-256/GPG mechanism, uploads ciphertext only,
downloads it again, and verifies exact ciphertext, plaintext and per-file hashes.
The cumulative checkpoint is retained for 30 days and copied forward on each
run; it is the saved research state for subsequent runs. Failed research jobs
can still contain a valid retained checkpoint. Repeated Actions attempts must
use a fresh dispatch instead of overwriting an existing artifact.

## Completeness and time

- The collector accepts only actual 07:05–08:55 JST observations, before the
  individual race deadline. Delayed daytime starts do not make network requests.
- Coverage compares the verified feed's eligible race set with the original
  scope, retained valid captures, analysis source hashes and required settlements.
- A partial MORNING chunk may be saved and retried. NIGHT/audit stays incomplete
  while any original capture, analysis or required settlement is missing.
- An absent scope is `MISSING_MORNING_DATA`, never a successful settlement no-op.
- A missing feed is unknown, not zero eligible races. An explicitly excluded
  rescue day is separately `NOT_APPLICABLE_EXCLUDED`.
- A no-result, saved-state `audit` mode can check the real transport and report
  existing gaps. A `[footprint-storage-check]` deployment invokes that audit;
  it does not launch WILD4 or manufacture MORNING captures.

The job summary separates local source counts from verified remote persistence.
The final completion gate requires both remote readback and complete research.
Unit tests cover loss, partial success, time boundaries, unknown feed, integrity,
encryption, recovery, exclusion, missing settlements and workflow ordering.
