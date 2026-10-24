# PREDEADLINE publication delay repair, 2026-10-24

The 20:36 JST Suminoe 12R deadline exposed avoidable publication delay.
Run 40090030558 saved P3 and V53 by 20:31:10, persisted the critical
checkpoint by 20:31:30, and confirmed Site GET only at 20:34:23.
The browser then polled once per 60 seconds. Goripan run 40090250550
started at 20:32:37 after waiting for run 40090169085, whose input check
reported zero unexpired races. Goripan Site GET succeeded at 20:34:19.

Changes:

- The existing early encrypted checkpoint now uses exact save/readback/hash
  verification. After its WILD4 handoff, a new saved-only publication adapter
  reads the frozen P3/V53 public projections and uses the existing Site
  authenticated transport before the 45-second continuation wait and optional
  dependency/research work. No inference, acquisition, settlement, authority
  resealing or model update is introduced by this publication adapter.
- Failure in early projection/publication retains the regular publication
  and continuation paths. Normal optional input/research delivery still runs.
- A follow-up that confirmed no outstanding inputs no longer dispatches an
  empty observer from the delivery/failure fallback. Existing public Goripan
  LOCKs are excluded only after hash-verified public readback and exact
  race/day/deadline checks. Unavailable readback preserves the retry path.
- The Site uses completion-based 15-second refresh for the current day,
  immediate focus/visibility refresh, and pauses hidden-tab reads. A slow
  response is not aborted by the next polling interval; history keeps 60s.
- HOME's saved odds evidence for freshly published V53 races is sent in the
  early lane too, so condition cards need not wait for the full historical
  evidence pass. Both evidence senders prioritize unexpired races while
  retaining every historical record and its existing validation.

Protected: frozen private source cf2769886e857369630512d2301558f6c17188a6,
existing forecast rules/models, saved morning and predeadline LOCKs/BETs,
official capture window, host pacing, 45-second continuation placement,
settlement and daily research/promotion. Compression evaluation and the
separate Abeken reproduction study are outside this change.

Validation is recorded separately from live race evidence. Passing the
contract gate does not prove any guaranteed five-minute publication margin.
