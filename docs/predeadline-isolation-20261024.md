# PREDEADLINE input-failure isolation, 2026-10-24

The 13:55 JST relay acquired the official Miyajima 9R inputs, but the P3
numeric exhibition-start gate rejected the input (`P12_EXHIBITION_START`).
ABEKEN V53 independently saved its LIVE snapshot at 13:55:12 before the 14:01
deadline. The WILD PRE_RACE collector raised `RUNTIME_START_TIMING` and
stopped its entire race loop, including the following Fukuoka 7R. An optional
original-display audit likewise raised `BEFOREINFO_TIMING_FIELDS` outside
its per-race error boundary. Missing P3/WILD output was not a universal
official-fetch failure.

Evidence: relay runs 40063898571 and 40064194591; the public feed generated
at 2026-10-24T15:02:52+09:00 has 112 formal races whose deadlines precede that
timestamp: P3 LIVE 111, V53 LIVE 112, WILD PRE_RACE 110. These are distinct
stages; WILD PRE_RACE counts do not by themselves establish formal WILD4 BET
coverage. The official page retrieved after the incident displays `L` for
boat 1's exhibition start; this later page is diagnostic corroboration, not
a replacement for the saved predeadline source.

The formal WILD4 path has an additional handoff fault. Run 40064143006 read
93 official odds snapshots from the early checkpoint at 13:56:08 JST; its
latest was Fukuoka 7R captured at 13:53:23, and it saved that formal WILD4 lock
at 13:56:08. The regular relay acquired additional Miyajima input after that
early checkpoint. Its accepted early-dispatch flag suppressed the follow-up
dispatch. The next WILD4 run 40064502380 read 95 snapshots at 14:01:45, already
past Miyajima's 14:01 deadline. Numeric ST rejection in the optional position
model is not the cause of this independent formal WILD4 handoff miss.

## Change

The public operational adapter isolates identified input/contract failures
per race and records exact race/reason pairs. It keeps missing numeric ST
missing, retains all source-hash/model/input gates, and continues other races.
Independent morning BET odds are saved before optional PRE_RACE inference.
WILD checks the current time again after inference and immediately before
writing, and records the actual completion time. The original-display audit
records its failed race using its existing failure-control mechanism.

After the complete encrypted state is saved and read back, the relay now
checks verified unexpired ODDS3T snapshots against the early checkpoint time.
Additional inputs trigger the existing WILD4 published-state receiver before
Site publication. Accepted dispatch is recorded only after the command
succeeds. Empty/expired input does not queue a duplicate job, while a failed
dispatch retains the existing retry path. No prediction input is fetched or
invented by this handoff helper.

Only three reviewed orchestration function hashes are adapted in memory.
The frozen daily source/model files, MORNING/previous LIVE LOCKs, P3/V53
predictors, WILD feature/model/selection rules, NIGHT settlement and research
promotion thresholds are unchanged. Unreviewed future sources produce an
explicit adapter warning and keep their original behavior.

The historical missed races are not regenerated or backdated. This repair
protects subsequent operations; an input which the existing engine cannot
accept remains unavailable for that engine.

## Validation

23 regression tests exercise the handoff and actual reviewed orchestration functions with
controlled source/clock/model doubles: one failed race followed by a healthy
venue; byte-identical healthy predictions; immutable retry; deadline crossing;
later-deadline continuation; source-hash and saved-LOCK corruption; global
manifest rejection; uncaught programming errors; independent odds persistence;
and original-display failure continuation. The pre-existing 48 recovery tests
also pass, along with the 10 checkpoint/protected-logic tests. The isolation
suite is connected to the existing PREDEADLINE and three-phase contract gates.

## Deployment and live readback

PR #111 merged as `d789eb011796864073ec4570c23fb8d6a406e6c5`.
The full three-phase contract gate passed on the reviewed PR head (run
40071061732) and merged commit (40071173198). The normal relay run
40071173212 completed successfully, with the adapter applied and no failures
in its two WILD PRE_RACE attempts. The next automatic LIVE run 40071308262
started and saved its critical checkpoint without manual dispatch.

| Race | Official odds captured JST | WILD4 locked JST | Deadline JST | Evidence |
| --- | --- | --- | --- | --- |
| Amagasaki 11R, 20261024-13-11 | 15:41:56 | 15:43:53 | 15:46 | Early receiver 40071257791 |
| Biwako 12R, 20261024-11-12 | 15:43:43 | 15:46:22 | 15:50 | Follow-up receiver 40071425089 |

The parent explicitly selected Biwako's additional snapshot at 15:44:57,
dispatched it at 15:44:58, and the receiver saved it before the deadline.
Both receiver runs completed successfully and reported
`GORIPAN_SITE_GET_READBACK=PASS`. A direct final application GET confirmed
both saved locks and their exact timestamps. A comparison of all 630
previously populated forecast objects found zero changes. The frozen daily
runtime remains `cf2769886e857369630512d2301558f6c17188a6`; both new WILD4
locks retain model `2d17db06cf9d7f0e73501e84b14f23621a42abb8a4ea1da3ef9d3fd07ac6c412`.

## Rollout incident and unrecoverable gaps

The initial public-surface audit blocked the relay because the new adapter,
test and note had not yet been registered. Registration was corrected, then
the full gate was completed before merging the follow-up repair. The live
relay restarted at 15:41. During that interruption four races passed their
deadlines without BEFOREINFO/ODDS3T capture: Fukuoka 10R (15:29), Miyajima
12R (15:33), Tsu 11R (15:34), and Heiwajima 11R (15:40).

Run 40071173212 records these eight missing source tasks explicitly. Their
LIVE predictions remain absent; the valid morning locks are retained. They
are not retrospectively generated or counted as completed LIVE acquisition.
The earlier Miyajima 9R numeric-ST gate hold is a separate pre-existing gap.
This report establishes the repaired future path, not complete coverage of
every race earlier in this day.
