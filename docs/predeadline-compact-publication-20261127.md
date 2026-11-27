# PREDEADLINE compact publication and automatic recovery

On November 27, a normal relay execution prepared an approximately 7.8 MB
authenticated delta, then encrypted, saved and downloaded approximately 3.4 GB
before allowing the first saved prediction publication. Wakamatsu 4R had P3 and
Abeken LOCKs at 16:33:12, but its first public readback was at 16:35:08 against
a 16:35 deadline. Its WILD worker reached inference after the deadline and did
not produce a formal LOCK for that race.

This transport change allows the first publication after authenticating the
exact remotely saved delta and its complete reconstruction inventory. The
ordinary full encrypted backup and exact full readback still run afterwards.
Cold jobs still restore a full base. This change shortens the path to first
publication; it does not eliminate all full transfers or certify any particular
production headroom without a new per-race readback.

## Publication contract

Compact publication is opt-in for current-day PREDEADLINE from November 27,
with both frozen authority and isolated research enabled. The original full
checkpoint path remains the fallback. The proof must bind the actual remote
delta bytes, its exact saved full-base cache, authenticated cipher digest,
complete base and target file/directory inventories, every replacement digest,
day, run, immutable daily authority and runtime source. Missing or invalid
proof must not open publication.

`ready` means a verified full reconstruction is durably available, either as
base plus delta or as the complete checkpoint. `full-ready` means the complete
checkpoint itself was saved and read back. Compact readiness must not suppress
the later full save. Optional research dependencies and sidecars are skipped
in the compact-enabled frozen lane, so they do not delay that full backup.

## Recovery contract

The next same-day PREDEADLINE or NIGHT execution selects the latest logical
saved target across full and delta caches. Delayed storage of an older execution must not supersede a
newer target. A selected delta is restored exactly and authenticated before its
manifest can identify the exact full base to restore. The existing complete
delta application verifies the base ciphertext and full inventory, applies
only authenticated replacements, and verifies the complete target inventory.

After replay, the retained base key, ciphertext and inventory still identify
the physical full backup. A later delta therefore contains the cumulative
changes from that full base, rather than claiming a reconstructed state is an
existing full cache. Missing, corrupt or mismatched required evidence stops
recovery; it must not silently resume from an older state that loses published
LOCKs.

MORNING retains its existing cross-day reset. A missing previous NIGHT is
handled by the existing independent previous-NIGHT reconciliation, whose
day-specific NIGHT execution also uses this compact recovery path.

## Protected behavior and verification

The daily source, model bindings, forecasts, saved LOCKs, BET, NIGHT,
acquisition eligibility, host pauses, shared request ledger and capture budgets
remain governed by their existing contracts. The immutable HTTP readback helper
and workflow size limit remain unchanged. No official requests are made by the
offline tests.

Validation includes real authenticated GPG roundtrips, exact cache failure and
full fallback, cancellation after compact publication before full backup,
normal next-run replay, continued cumulative deltas, newer/older target order,
wrong or missing bases, digest/inventory/authority mismatches, and the existing
three-phase and saved-LOCK contracts. Production capture, formal LOCK and
before-deadline public readback are recorded separately for each engine.

## Continued capture and WILD preparation

The first rollout proved that the initial compact checkpoint could precede
publication, but the continued capture still waited for the later full backup.
On its second production cycle, all three engines reached Gamagori 7R and
Kiryu 7R before their deadlines, with only about 32 and 41 seconds remaining
at the WILD public readbacks. That did not establish the five-minute target.

The warm transport therefore has a separate immutable `-warm` delta and cache
path, while retaining the actual parent run's canonical critical checkpoint
identity. It authenticates and retains the first delta, protects the LOCKs and
bindings created by the first capture, and proves the second target before
publishing the warm forecasts. Same-run recovery orders the first delta,
critical full, warm delta and later published full in that order. A warm proof
failure returns to the existing later complete backup and saved publication.

WILD preparation starts before the continued capture and waits for the exact
warm delta. It uses the existing serialized worker, model bindings and LOCK
rules. A parent step that definitively skipped saving its warm checkpoint can
stop that exact waiting child; incomplete or unavailable metadata cannot
authorize inference. Public helper and worker transport changes are delivered
through reviewed complete commit SHAs. The seven prediction, model, input,
LOCK, settlement and learning blocks remain byte-identical to 0634adb.
