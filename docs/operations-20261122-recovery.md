# November 22 operational repair

## Incident and scope

The production audit found three local faults in addition to official-host refusals:

1. LIVE warm collection continued across other races after the inputs for an urgent saved LOCK were available. On November 22, Amagasaki 5R was read back publicly at 12:32:04.961 JST, 64.961 seconds after its 12:31 deadline. Its two official inputs had returned HTTP 200 at 12:27:30 and 12:27:59. See [run 44980310556](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/44980310556/job/137079364892).
2. Optional `PLAYER_CONTEXT_ONLY` input transport imported a module absent from the frozen projection checkout. The current reader and the pinned projection had different dependency requirements.
3. Site request logs contained CPU-limit exceptions. Some invocations returned HTTP 200 before failing in background work, so an HTTP success or a green Actions job alone is not a healthy-operation claim.

The repair keeps saved MORNING/PREDEADLINE LOCKs, authority, frozen model/runtime selections, and NIGHT records intact. Past-race replay does not count as restored live operation.

## Deadline-first warm publication

Warm collection now considers the earliest still-future deadline of pairs completed in that pass. When later GETs would consume its 145-second publication reserve, it may return for the existing final checkpoint/publication. Remaining due sources are deferred only if the existing continuation was accepted and the later cutoff leaves another 145-second setup reserve plus the unchanged per-slot 30-second degraded spacing. These are conservative scheduling reserves, not a GitHub queue-time guarantee. Expired completed pairs do not hide valid pairs. Without an admitted continuation or sufficient later-race margin, the same worker continues.

The final verified publication precedes the existing WILD4 follow-up dispatch. WILD4 still receives independently verified saved input if publication fails. The number of Site publication opportunities remains two; no third publication or new official-host probe is introduced. The next worker reuses saved sources without repeat GETs, and the canonical collector still enforces STOP, actual request pacing and cutoff after restore.

Fifty-one collector tests passed with the reviewed retained collector, including continuation admission, budget conservation, saved-file retention, no duplicate acquisition, delayed cutoff, STOP, and expired/future mixtures. The saved timing fixture removes about 59.891 seconds of later GET waiting and 2.759 seconds of dispatch overhead. Against the original incident it still projects approximately 2.311 seconds late by itself. This is a demonstrated reduction in avoidable waiting, not proof of recovered before-deadline publication; the combined production path must supply that evidence.

## Optional context dependency

Restore the actually called reusable workflow from `bbb4f12f81597e120d66a79b0de65c1876097333`. Its only operational additions are a sparse checkout of one read-only context helper and a path variable confined to the optional evidence step after the existing public-delivery opportunity. The caller is pinned to the real commit containing this restored workflow.

- Formal runtime remains `7faaacfec8a8643a588af7e23d062fc787ba24b8`.
- Projection runtime remains `7260ad5833722c2b803612507ea4fc7cc98ad91d`.
- Helper comes from `76106bbd82e4fdaf6889db063c66ae02aef47561`.
- Helper SHA-256 is `9094b1a6097e7be933552072f5d300f288d82320b05732c98cd954c9eab2042c`.

The reader executes the exact checked bytes without replacing the frozen `official_pipeline` import. A missing, changed, oversized, or linked helper holds only optional context; verified odds and saved LOCK evidence remain eligible. Module diagnostics disclose only an explicit allowlist, never exception text or private contents.

Offline validation used the real old `OfficialArtifactStore`, `RAW38_FIELDS`, and `parse_beforeinfo_html` with the pinned new helper and synthetic saved inputs. Seven contract cases passed, including raw/parsed/roster/hash tamper rejection and unchanged saved files. Fifty-one transport and response-observation tests passed. Private reference source and fixtures are not included in this repository.

## Timeout observation

`URLError` whose `reason` is a `TimeoutError` is recorded as `TIMEOUT`. Message text is not a classifier. The original exception and the single opener call are preserved; HTTP errors retain their HTTP status. Acquisition, pacing, retry, and STOP behavior are unchanged.

## Refusal, pause, and STOP review

The observed sequence on November 22 was an initial beforeinfo 403 at 11:46:48 JST, a 30-minute pause, successful normal acquisitions after release, and another beforeinfo 403 at 13:53:17. The second refusal activated the existing daily STOP. The second response arrived in 142.344 ms, advertised `AkamaiGHost`, and contained no `Retry-After`. See [run 44986759898](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/44986759898/job/137099201814).

The current general-endpoint contract pauses after the first 403 and stops after the second; general 429 responses wait at least 15 minutes, then 60 minutes, respect a longer valid `Retry-After`, and stop on the third. Odds have a separate existing classification. Successful responses do not erase the refusal history. This review does not change those thresholds.

STOP exists in the verified daily request ledger, the Site stop table, and a shared-host hold lasting through the day boundary. Deleting only one of those would create disagreement and would not establish that the official service accepts requests again. This repair neither resets the ledger nor clears the stop/hold nor sends diagnostic probes. After STOP there were no new official requests in the inspected LIVE jobs; that period cannot be described as continuous upstream rejection.

Any later change to recovery policy must define one coherent state transition across the ledger and Site gate, respect Retry-After and the daily/actual-request budget, admit only a bounded normally due request after the wait, stop again on refusal, and test duplicate/concurrent execution. A success must not erase historical refusal evidence. A generic timer or a green job alone is insufficient evidence to remove these controls.

## Production acceptance

Record code merged, Site deployed, and natural-operation verification separately. Require a future race's official inputs, immutable LOCK, durable checkpoint verification, and public saved-LOCK readback before the real deadline. Five minutes of lead is a stronger target and must not be confused with merely meeting the deadline. Check optional context delivery independently. For CPU work, inspect the production script version and whole invocation outcome, including background exceptions, rather than only its HTTP status.

If today's STOP prevents new acquisitions, retain that limitation explicitly. Existing saved-input publication and normal downstream jobs may still be checked; they do not establish upstream recovery or complete morning-to-NIGHT operation.
