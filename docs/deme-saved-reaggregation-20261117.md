# Saved Deme reaggregation repair

The isolated recovery path published fresh timestamps after updating only the
30x observation checkpoint. Its `fast=True` entry deliberately skipped the base
history and settlement phases. This left continuity results at November 9 and
venue research on an older optional-consumer budget while the display appeared
to have succeeded.

Recovery now uses the pinned research-only `deme_discovery_saved_refresh` entry.
It first persists the existing 30x captures, then independently resumes only
open races under frozen rules and reconciles all saved observations to verified
NIGHT/archive results. Venue date/meeting KPIs are rebuilt from those records.
Unchanged verified result sources are skipped on retry. Component timeouts are
visible as `DEFERRED` / `PARTIAL`; publishing alone no longer implies that the
settlement phase completed.

The research checkout is pinned to
`b9e0818413a0075cd29f2166c90a1c2ef88ce614`, a descendant of the existing research
pin, with only a new observer/reaggregation entry and its tests. Runtime source,
prediction models, candidate definitions, capture timestamps, formal LOCK/BET,
and canonical NIGHT files are unchanged. Existing original-input and deadline
checks apply. Historical missing observations are never reconstructed. Special
result fallback reads only saved evidence, without official acquisition.

Validation: 42 research tests and 3 relay recovery tests pass. They cover verified
payout aggregation, refreshed venue KPIs, missing/corrupt evidence, repair and
retry, immutable captures, rejection of retroactive writes, future-only captures,
and independent component failure reporting. Runtime verification must read back
the persisted research publication and its actual result-through date.
