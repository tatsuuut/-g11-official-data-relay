# Bounded PREDEADLINE content retry

An incomplete official HTTP 200 response schedules a content retry, but the
collector returns after scanning the plan. The next Actions job may start after
the race cutoff. On 2026-10-30, race 20261030-14-03 had content rejections at
09:22 JST, with retry times at 09:22:39 and 09:22:49; the next observed capture
at 09:28 was already after the 09:27 cutoff. Evidence is in relay jobs
123813160774 and 123815936729. The exact rejected response bytes were not
retrieved for this change; it does not assert that the official parser is wrong
or that the historical race would have recovered on a retry.

## Change

The reviewed relay adapter gives an otherwise empty, no-future-wait capture one
additional attempt in the same process. It selects at most two sources already
rejected for content or paused for content. The existing pause must be at most
30 seconds away, and each source must have at least three minutes remaining
both before waiting and when waking up. The restricted second pass may start
requests for 60 seconds; an in-flight request retains the original timeout.
It never repeats itself, and it never captures a newly due unrelated race.

Any new successful capture returns immediately to prediction/checkpoint/
publication. HTTP failures, global deferrals, exhausted sources, large rejected
batches, other dates and near-deadline cases retain the original policy.
The original collector still enforces official hosts, serial pacing, the daily
request cap, source retry limits, parsing, eligibility, snapshot verification
and cutoff checks. Existing snapshots are not overwritten. Rejected bytes and
receipts remain rejected evidence, even when a later response succeeds.

The adapter is scoped to the normal critical and PREDEADLINE entries and
restores the method binding on exit. An unreviewed collector SHA is left
unchanged. MORNING, NIGHT, delayed rescue, forecast models, BET/ABSTAIN,
notification and Site source are not modified. No previous race is replayed.

## Validation and limits

Tests execute the actual reviewed private collector with fake HTTP, storage and
time. They cover transient/permanent rejection, unchanged valid captures,
publication priority, preserved snapshots, unrelated future races, cutoffs,
late responses, delayed wakes, source binding, HTTP errors, budgets, existing
content pauses and the real no-future-wait adapter. An AST comparison verifies
that the restricted collector differs only in its source filter and the added
retry start-time bound. No private runtime source is copied into this repository.

Production activation and successful transient recovery are separate evidence.
The `G11_PREDEADLINE_CONTENT_RETRY` log reports whether the adapter was applied,
skipped, waited or attempted once. A successful deployment does not establish
that all historical missing LOCKs, future scheduling latency or malformed
official responses are resolved.
