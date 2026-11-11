# November 11 delivery and whole-day scope follow-up

Reviewed on November 12 JST. Prediction rules, model/day pins, immutable
MORNING/PREDEADLINE/NIGHT records and their finance remain unchanged.

## Miyajima 3R: verified cause and historical handling

The exact published checkpoint `g11-state-v4-published-2026-11-11-43015511902`
and frozen source `6d8471adff17d9df0014b6e529a5af6e755a99ab` establish:

- Whole official K: 144 parsed races. Miyajima 3R finished with six boats,
  result 1-2-4, payout 5,370 yen.
- Morning admitted scope, LOCK49 and NIGHT: 143 races. The excluded key
  `20261111-17-03` has `B_ENTRY_CARDINALITY:17:3:5`.
- B contains six rows. Boat 3, racer 4728, has boat number 21 and boat top-two
  percentage 100.00, joined in the fixed-width source as `21100.00`. The old
  last-field separator rejects that line. It is not a five-boat official race.
- No target prediction snapshot exists in the saved canonical snapshot tree;
  the target is absent from LOCK49. No historical prediction is reconstructed.

The corrected decoder accepts only the exact adjacent full `100.00` field.
An isolated reparse of the original, unchanged whole B yields 144 races, no
quarantines, boat 21 and 100.00 for that row. All other parsed fields match the
same official row with an explicit separator. This is input-decoder analysis,
not a generated or scored historical prediction.

The date-bound decoder starts with new November 13 MORNING inputs. November 11
and the already-started November 12 keep their original admission and replay
rule, including their saved exclusion evidence. NIGHT may not expand that
historical scope. Miyajima 3R's official result belongs in a separate result
record labelled no saved prediction; it must not become a miss, an invented
ticket, a zero-investment observation or a retrospective win in prediction
KPIs or virtual accounts.

Original morning input SHA256:
`5400a7ab9cb6e99774979fd30c422950756e68ca7b620024f6cf1aedb197fc62`.
Original whole B archive SHA256:
`eab671451f1de08514ee59e6f2cbbe7aab18052631b44116e9f4c6a4ee673256`.
Original whole K archive SHA256:
`939541fde81453dc8515aaa53599ce5d6a21960d5e3fc6cd552e3d3fc33d513e`.

Verified saved-state audits:
[original scope](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43068592280/job/130690094924)
and [whole B correction without writes](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/43070526335/job/130696840971).
Both retain the original saved state and invoke no acquisition or prediction.

New normal NIGHT deliveries also emit an additive
`G11_OFFICIAL_SCHEDULE_SCOPE_V1` record: admitted, excluded and whole scheduled
counts, original morning hash and exclusion reasons. An admitted-scope
143/143 success with one excluded race is explicitly `PARTIAL_OFFICIAL_SCOPE`.
Old immutable completion receipts and financial records are not rewritten.

## Delivery: bounded reduction, prospective timing still required

Run [42964315390](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/42964315390/job/130338605850)
started its runner at 14:15:23 JST. The source retry was ready at 14:16:30,
critical capture completed at 14:17:01, and server readback at 14:18:10.
Kojima 8R closed at 14:19:00: readback had only 49.18 seconds remaining.
This does not establish smartphone screen or push arrival.

The successor was dispatched at 14:18:20, but the serialized writer continued
its sidecars, complete-state persistence, saved discovery retry and another
complete persistence through approximately 14:20:24. Three complete encrypted
archives were made in the observed cycle. Dispatch alone does not release the
shared writer slot.

For the normal frozen fast path, perform the same saved-only discovery retry
after successful critical publication and sidecars, before the existing complete
state encryption. That checkpoint now contains both sidecars and saved discovery
evidence. Its dedicated research projection is sent only after exact checkpoint
readback. A failed early publication or saved retry retains the old post-publication
retry, encryption, receipt persistence and verified research delivery route.
No request pacing, capture window, forecast, optional research, encryption,
exact cache readback, model pin or serialization boundary is removed.

The code change removes an avoidable full persistence round in the successful
fast path. It does not establish universal T-minus-five delivery. New natural
race observations must measure server completion and actual device arrival;
keep this item pending until that evidence exists.
