# Separate saved-input research, 2026-11-14

Normal LIVE capture, frozen inference, immutable LOCKs, verified encrypted
checkpoints, WILD4 handoff and canonical delivery remain in the operational
writer. The writer activates an immutable daily separation marker before
capture, then skips optional saved-input research and its dependencies. It
never waits for research in LIVE. After verified delivery and a complete
checkpoint it sends a bounded enqueue request, without waiting for research.

The independent workflow also observes owned main-branch runtime notifications.
An explicit enqueue restores its exact verified complete checkpoint; a
completion notification selects a ready canonical checkpoint. An in-progress
notification waits for the parent's exact verified critical checkpoint and
selects a ready same-day superset. All inputs enter its own disposable copy.
Frozen source and existing
MORNING/PREDEADLINE bindings are verified before research. It runs only WILD
collection/BET evidence, confidence/MASHIRO evidence, and the pinned Deme
observer. Research does not reacquire official inputs or run core forecasts.

Only named research outputs leave that copy. Bundles bind the source snapshot
hashes, daily frozen authority, canonical seed hashes, original member bytes
and timestamps. Their cache namespace is `g11-research-v1-`; canonical cache
selectors do not accept it. Research has a separate concurrency group. Its
OIDC identity can publish the existing research display endpoint only; the
canonical sync, delta and NIGHT endpoints reject that identity.

After the last official deadline the research worker only closes and saves
its existing observations. It does not generate late predictions. NIGHT
imports only a closed verified bundle before settlement. The import preserves
capture timestamps, rejects stale sources or seed conflicts, excludes sealed
MORNING sidecars, verifies unchanged core snapshots, and appends a verified
PREDEADLINE authority binding. Copy or sealing failure rolls back the import.
An absent/unclosed bundle requeues NIGHT after 30 seconds. LIVE never waits.

Migration preserves all existing canonical history. This change does not prune
models, saved LOCKs or historical research. Optional research failure cannot
write the canonical forecast, although research/NIGHT status may remain held
until a valid closed handover is available. The saved-state integration audit
uses disposable copies and injects a future clock only into the close/import
validator, with no production cache writes or POST publication.

Rollback must restore both the main optional-research routing and the NIGHT
handover together, after preserving any independent research bundle. Removing
only the independent worker would leave an activated day waiting for closure.
The existing canonical encrypted checkpoints and all prediction engines are
unchanged; no saved prediction is regenerated for rollback.
