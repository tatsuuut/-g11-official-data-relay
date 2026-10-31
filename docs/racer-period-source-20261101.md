# Official racer application period: 2026-11-01 recovery

The 05:27 JST MORNING run 41238988012 froze daily authority successfully,
then failed with HTTP_STATUS_UNSUPPORTED:404. The 05:42 retry 41240166694
restored the same authority and stopped at SOURCE_TECHNICAL_RETRY_EXHAUSTED.
No November 1 public feed existed when the repair started.

The retained URL resolver switched to fan2610 on November 1. That is the
end of the grading review period, not the start of its application period.
Official grades apply January-June / July-December. The official download
page lists fan2604 as 2026 second half; the downloaded file contains 1,643
records, all labelled 2026/2 with calculation dates 2025-11-01–2026-04-30.
The archive SHA256 is 5c1e21cfe5a4dd53ea26eb07aca4905c4c20c3160b4a206ba96e92702eb4b46d.

References:
- https://www.boatrace.jp/owpc/pc/extra/data/download.html
- https://www.boatrace.jp/owpc/pc/site/news/2026/05/49344/
- https://www.boatrace.jp/owpc/pc/extra/enjoy/guide/jiten/07/y_048.html

From November 1 forward, the acquisition adapter selects the correct
application-period URL and validates every parsed record's year, half and
calculation dates. It accepts only the reviewed legacy URL or the correct
URL; any other requested source is rejected. Historical calls are unchanged.

This does not edit retained source files, daily authority, existing response
bytes, request events, retry counters, clocks, predictions, LOCKs or NIGHT.
The corrected URL has its own honest source ID and request evidence; the
failed fan2610 request remains in the ledger. The complete existing
authority/LOCK wrapper still executes and parser references are restored
on success and error. Missing or invalid new-period data still fails closed.

Validation covers January/July and May/November boundaries, leap day,
wrong/future/mixed period records, historical isolation, unreviewed URLs,
retained failure counts, error cleanup and the actual private authority
guard. Live recovery is complete only after MORNING LOCK persistence,
publication and date-specific public feed readback pass.
