# PREDEADLINE latency follow-up, 2026-11-06

The earlier saved-only publication path is active, but it does not remove
the predecessor's remaining work from the serialized acquisition queue.

Observed 2026-11-05 evidence (JST):

- Run 41958528839 dispatched its successor at 12:05:28, then retained the
  shared runtime slot through approximately 12:10:33. The successor
  41959374631 started its runner at 12:10:41.
- Miyajima 5R closed at 12:14. Its official odds were saved at 12:11:24,
  its P3 confidence record at 12:12:06, and the critical canonical readback
  passed at 12:13:18. This was late in the buying window despite successful
  acquisition and publication. Readback is not device push arrival.
- The two sampled jobs restored approximately 806 MB of encrypted state.
  Each job encrypted the full state three times; each observed encryption
  step occupied approximately 29 seconds. P3 research occupied another
  58-68 seconds before the regular publication. Dependency installation and
  the private runtime also consumed time; they are not removed by this patch.

Changes:

1. Credit time spent persisting and publishing the critical checkpoint toward
   the existing 45-second continuation spacing. If those operations already
   consumed 45 seconds, do not sleep another 45. Bind the calculation to the
   same day's successful critical run; reject future or mismatched timestamps.
   The original official-host pacing, request budget and capture windows stay.
2. Keep the complete gzip/AES256 archive and exact cache readback. Prefer
   two-thread pigz when installed, otherwise gzip; tell GPG not to recompress
   the gzip stream. Keep atomic replacement, secret file-descriptor input and
   prior checkpoint retention on failure. Do not omit state or model files.
3. For normal PREDEADLINE, run the existing P3 research update after canonical
   prediction publication. Persist and read back that research state before
   sending its existing dedicated projection. Preserve MORNING/NIGHT order,
   every research invocation, failure isolation, original LOCKs and all
   prediction/model pins. A failed canonical publication still retains a
   successful research update, without publishing the research projection.
4. Log per-race remaining time at verified early publication, including
   five-minute misses and post-deadline readbacks. This is a server-readback
   measurement, not a device-arrival guarantee.

Validation covers the actual workflow continuation shell, spacing boundary
and clock failures, deferred research authority/credential isolation, complete
gzip/AES256 roundtrips, archive failure retention, and unchanged source bytes.
Real GPG roundtrip is mandatory in contract CI; a local environment without a
working gpg-agent cannot supply that evidence. Full runtime contracts and the
existing regression suite remain required before merge.

These changes reduce avoidable work and prioritize publication. They do not
prove universal T-5 delivery: GitHub runner queues, official content, remaining
runtime work and Goripan's separate inference/publication must be measured on
new races. Do not replay historical races or rewrite LOCKs to create evidence.
