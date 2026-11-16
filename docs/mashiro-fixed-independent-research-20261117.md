# Fixed-rule Mashiro observation, 2026-11-17

The independent saved-input worker reads official schedule and preview snapshots
to record the fixed first-place-only Mashiro cohort. Its implementation and
28-day protocol are pinned to boat-brain-lab commit
`724b660ae36e59eb989892b1a4050a54f65e8ab6`. The primary boat-number cohort and the
secondary exhibition-course cohort are separate. A1/A2 is the inner upper
class, B1/B2 the outer lower class. P3 outputs, odds and model scores are not
inputs. No new official requests, core inference, model training, scheduler or
automatic adoption are added.

Only 2026-11-17 through 2026-12-14 admits new observations, with a real
predeadline first write and readback. Existing records retain their original
clock; a source captured in time cannot justify a late research first write.
Missing observations are never reconstructed at NIGHT. The study's new capture,
result and coverage files are allowlisted in the existing independent bundle,
but are explicitly excluded from canonical NIGHT import. Normal forecasts,
daily authority, official LOCKs, NIGHT accounting, legacy Mashiro and other
research are unchanged.

After closure the worker may join only existing study observations to saved
official first-place results. It never reopens forecast capture. Complete NIGHT
input enables a small daily evidence export, including original official raw
responses, observations and the pinned implementation. The existing AES256
authenticated encryption is used, decrypted bytes and manifest are read back,
and the encrypted evidence is retained for 90 days as a GitHub Actions artifact.
The artifact name binds the day and protocol hash. Retries skip an already saved
same-study main-branch artifact. A failed archive can retry from the verified
closed cache without regenerating any observation.

The dedicated contract runs the 11 observer tests, 7 new isolation/archive tests and
15 existing isolation/routing tests. A read-only audit uses a disposable
authenticated November 16 checkpoint and its frozen source to verify actual
input formats. That audit writes no observation, reads no result, and is not
prospective performance evidence. The frozen saved-store reader is also tested
with `python3 -S`, matching the dependency-free closing lane.

Initial prospective observation count is zero until a real November 17 capture
succeeds. Implementation, contract success, deployment, capture persistence and
first result joins must be reported separately. The fixed 28-day report remains
pending; it is not a production model improvement claim.
