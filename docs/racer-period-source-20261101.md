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
authority/LOCK wrapper still executes. The instance's acquisition hook is
restored on success and error, and parser globals are never changed.
Missing or invalid new-period data still fails closed.

Validation covers January/July and May/November boundaries, leap day,
wrong/future/mixed period records, historical isolation, unreviewed URLs,
retained failure counts, error cleanup and the actual private authority
guard. Live recovery is complete only after MORNING LOCK persistence,
publication and date-specific public feed readback pass.

The first live retry with the calendar correction passed the old 404 and
stopped at RACELIST_FL_ST for a newly listed racer. The current B file has
only two racers absent from fan2604: 5482 (Fukuoka 1R) and 5492 (Fukuoka 2R).
Their official race pages both explicitly show F0, L0 and average ST '-'.
The existing formal gate already allows average ST to be OFFICIAL_NOT_AVAILABLE;
F/L remain strictly required. The acquisition decoder now preserves the
official dash as None from November 1, without substituting zero or an estimate.
The original raw HTML, URL, response receipt, scope and request evidence stay
unchanged. Malformed values, missing F/L, wrong identities and arbitrary blanks
still fail. The original parser and historical-date behavior are retained.
The full intake regression preserves all 24 race rows and produces 23 formal
rows; the original P3 structure check excludes the one unavailable-ST race
without generating practical bets. No prediction requirement is weakened.
