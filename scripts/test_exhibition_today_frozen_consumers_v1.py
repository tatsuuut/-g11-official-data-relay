"""Offline TODAY integration against separately checked-out frozen production.

The MORNING card, authority, clocks, model artifact and HTTP responses here are
explicitly synthetic test inputs. No live service is contacted. Real frozen
model/selection functions and real weather feature conversion remain loaded
from the November 27 source. Native inference artifacts are observed at their
boundary; this test does not claim operational acquisition or betting success.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

JST = timezone(timedelta(hours=9))
DAY = "2026-11-27"
KEY = "20261127-05-02"
PRIOR_KEY = "20261127-05-01"
CUTOFF = datetime.fromisoformat(DAY + "T23:59:00+09:00")
ACTIVATED = CUTOFF - timedelta(minutes=3)
FROZEN_SHA = "6797fa3205f62e0f969b0d76b5612aeb556802fe"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def file_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


class HTTPResponse(io.BytesIO):
    status = 200
    headers = {}

    def __init__(self, url, raw):
        super().__init__(raw)
        self.url = url

    def geturl(self):
        return self.url


def synthetic_authority(state, frozen, store, daily, current, official):
    """Seal genuine source bytes with an explicitly synthetic model artifact."""
    model = store.root / "g11/wild-pack-models/exhibition-fixture/model.json"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b'{"purpose":"OFFLINE_INFERENCE_BOUNDARY_ONLY","weights":[1]}\n')
    source_files = daily.source_inventory(frozen)
    model_files = daily.model_inventory(store.root)
    archive = daily._archive(frozen, source_files)
    folder = daily.day_root(state, DAY)
    archive_name = "source-" + daily.digest(archive) + ".tgz"
    daily.immutable(folder / archive_name, archive)
    authority = {
        "schema": daily.AUTHORITY_SCHEMA, "operational_date_jst": DAY,
        "p3": {"version": current.RUNTIME_VERSION, "source_sha": FROZEN_SHA},
        "abeken_v53": {"version": official.ABEKEN_V53.RUNTIME_DISPLAY_VERSION,
                       "logic_version": official.ABEKEN_V53.RUNTIME_LOGIC_VERSION,
                       "source_sha": FROZEN_SHA},
        "wild": {"runtime_version": "G11_WILD_FROZEN_CURRENT_AND_SHADOW_V1", "source_sha": FROZEN_SHA},
        "true_ai": {"runtime_version": "G11_TRUE_AI_RESEARCH_RUNTIME_V1", "source_sha": FROZEN_SHA},
        "runtime_source_sha": FROZEN_SHA, "source_archive": archive_name,
        "source_archive_sha256": daily.digest(archive), "source_files": source_files,
        "model_files": model_files, "model_set_sha256": daily.digest(daily.canonical(model_files)),
        "created_at_jst": DAY + "T07:00:00+09:00", "immutable": True,
        "adoption": "EXISTING_ACCEPTED_RUNTIME_AND_HASH_VERIFIED_CURRENT_MODELS",
    }
    authority["authority_sha256"] = daily.digest(daily.canonical(authority))
    daily.immutable(folder / "authority.json", daily.canonical(authority) + b"\n")
    daily.verify_runtime(state, DAY, frozen)
    return authority


def prepare_state(state, frozen):
    from g11.canonical import official_pipeline as official, published_preview_v1 as preview
    from g11.canonical.test_official_pipeline import make_verified_snapshot, beforeinfo_html, odds_html
    from g11.growth_p3.v1_1 import current_runtime_v1 as current
    from g11.relay import daily_runtime_authority_v1 as daily

    clock = {"now": ACTIVATED}
    store = official.OfficialArtifactStore.initialize(state / "store", DAY, 2, DAY + "T07:00:00+09:00")
    synthetic_authority(state, frozen, store, daily, current, official)
    text = json.dumps(make_verified_snapshot()["SNAPSHOT"]["PAYLOAD"], ensure_ascii=False)
    text = (text.replace("20260826", "20261127").replace("2026-08-26", DAY)
            .replace("T08:30:00", "T23:30:00").replace("T09:00:00", "T23:59:00")
            .replace("T08:23:00", "T23:23:00").replace("T08:53:00", "T23:52:00")
            .replace('"08:30"', '"23:30"').replace('"09:00"', '"23:59"'))
    morning = json.loads(text)
    for row in morning["RAW38_ROWS"]:
        row[morning["RAW38_FIELDS"].index("公式URL")] = official.schedule_url(CUTOFF.date())
    receipts = []
    for sid, url in (("schedule-b-20261127", official.schedule_url(CUTOFF.date())),
                     ("racer-period-fan2605", "https://www.boatrace.jp/owpc/pc/extra/data/fan2605.lzh")):
        receipts.append(store.commit_response(sid, url, CUTOFF - timedelta(hours=2), sid.encode(),
                        content_type="application/test", final_url=url))
    store.commit_snapshot("morning", "official-day-input", morning, receipts)
    client = official.OfficialHttpClient(store, clock=lambda: clock["now"],
        opener=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unplanned national HTTP")))
    runner = official.CanonicalRunner(store, client=client)
    morning_result = runner.run_morning_predictions()
    require(KEY in morning_result["COMPLETED"], "frozen native MORNING did not complete")
    wild_admission = store.root / "g11/wild-pack-oos/morning" / DAY / "position-oos-admission.manifest.json"
    wild_admission.parent.mkdir(parents=True, exist_ok=True)
    wild_admission.write_bytes(b'{"purpose":"OFFLINE_SEALED_MORNING_ADMISSION"}\n')
    daily.seal(state, DAY, "MORNING")
    daily.seal(state, DAY, "PREDEADLINE")

    # A legitimate earlier native forecast is retained before activation.
    prior_capture = CUTOFF.replace(hour=23, minute=25)
    clock["now"] = prior_capture
    raw = beforeinfo_html().replace(b"</body>", b'<p class="is-direction1"></p></body>')
    sid = "beforeinfo-" + PRIOR_KEY
    url = official.beforeinfo_url(CUTOFF.date(), "05", 1)
    receipt = store.commit_response(sid, url, prior_capture, raw, content_type="text/html", final_url=url)
    store.commit_snapshot("predeadline", sid,
        {"STATUS": "PREDEADLINE_SOURCE_COMPLETE", "KIND": "BEFOREINFO", "RACE_KEY": PRIOR_KEY,
         "CAPTURED_AT_JST": receipt["CAPTURED_AT_JST"], "PARSED": official.parse_beforeinfo_html(raw)}, [receipt])
    put_odds(store, official, PRIOR_KEY, prior_capture, odds_html())
    prior_result = runner.run_due_predeadline_predictions()
    require(PRIOR_KEY in prior_result["COMPLETED"], "frozen earlier formal forecast missing")
    daily.seal(state, DAY, "PREDEADLINE")
    clock["now"] = ACTIVATED
    return official, preview, current, daily, store, runner, clock, morning


def put_odds(store, official, key, captured, raw):
    sid = "odds3t-" + key
    url = official.odds3t_url(captured.date(), key[9:11], int(key[-2:]))
    rc = store.commit_response(sid, url, captured, raw, content_type="text/html", final_url=url)
    store.commit_snapshot("predeadline", sid,
        {"STATUS": "PREDEADLINE_SOURCE_COMPLETE", "KIND": "ODDS3T", "RACE_KEY": key,
         "CAPTURED_AT_JST": rc["CAPTURED_AT_JST"], "PARSED": {"ODDS_ROWS": official.parse_odds3t_html(raw)}}, [rc])


def boatcast_packet(candidate, source, roster, store, clock):
    fixture = candidate / "g11/canonical/fixtures"
    raw = {kind: (fixture / ("boatcast_preview_20261127_18_03_" + kind + ".txt")).read_bytes()
           for kind in source.KINDS}
    tables = {kind: [line.split("\t") for line in raw[kind].decode().splitlines()]
              for kind in ("str3", "tkz", "stt")}
    for boat, (racer, motor, name) in roster.items():
        tables["str3"][boat + 1][0] = str(racer)
        tables["str3"][boat + 1][1] = name
        tables["str3"][boat + 1][17] = str(motor)
        tables["tkz"][boat + 1][0] = name
    for row in tables["stt"][2:]:
        row[2] = roster[int(row[1])][2]
    for kind, rows in tables.items():
        raw[kind] = ("\n".join("\t".join(row) for row in rows) + "\n").encode()
    weather = raw["sui"].decode().strip().split("\t")
    weather[0] = "2355"
    raw["sui"] = ("\t".join(weather) + "\n").encode()
    calls = []
    def read(url, _cutoff, _clock, _seconds):
        calls.append(url)
        clock["now"] += timedelta(milliseconds=1)
        kind = next(k for k, expected in source.urls_for(KEY).items() if url == expected)
        return raw[kind], clock["now"], clock["now"] - timedelta(minutes=1)
    proof = source.due_observation(store, KEY, CUTOFF, roster, lambda: clock["now"], read=read)
    require(proof is not None and len(proof["SNAPSHOT"]["SOURCE_RECEIPTS"]) == 5,
            "five original exhibition receipts were not retained")
    require(calls == list(source.urls_for(KEY).values()), "exhibition resources fetched more than once")
    return proof, calls


def frozen_feature_parity(store, source, true, old_predict, proof, roster):
    before = proof["SNAPSHOT"]["PAYLOAD"]["PARSED"]
    captured = proof["SNAPSHOT"]["PAYLOAD"]["CAPTURED_AT_JST"]
    predicted = (datetime.fromisoformat(captured) + timedelta(seconds=1)).isoformat()
    morning_record = {"RACE_ID": KEY, "FEATURE_ASOF_TIME": DAY + "T07:00:00+09:00", "BOATS": []}
    for boat in range(1, 7):
        features = {name: 1 for name in true.ENGINE._MORNING_NUMERIC_SOURCE}
        features.update({name: "__MISSING__" for name in true.ENGINE._MORNING_CATEGORICAL_SOURCE})
        features.update(boat=boat, course=boat, average_st=.17, national_win_rate=5.,
                        grade="B1", venue_code="05", racer_id=str(roster[boat][0]))
        morning_record["BOATS"].append({"BOAT": boat, "FEATURES": features})
    adoption = {true.PIN: {"CONTENT_SHA256": "1" * 64}, "RACE_ID": KEY,
        "SOURCE_HASHES": {"FEATURE_RECORD_SHA256": digest(true.canonical(morning_record) + b"\n"),
                          "MORNING_ARTIFACT_SHA256": "3" * 64},
        "OFFICIAL_DEADLINE_JST": CUTOFF.isoformat(), "CONTENT_SHA256": "2" * 64,
        "OPERATIONAL_DATE_JST": DAY, "SELECTION_POLICY": true.POLICY}
    bundle = source.model_input_source(store, KEY, CUTOFF, proof["SNAPSHOT_SHA256"])[1]
    with patch.object(true, "verify_pin"), patch.object(true.ARTICLES, "saved_context", return_value={}), \
            patch.object(true, "infer", return_value={"fixture": "frozen inference boundary"}) as infer:
        result = true.predict(store.root, adoption, morning_record, before, captured,
                              proof["SNAPSHOT_SHA256"], predicted, bundle, beforeinfo_source=source.HOST)
        alternate = deepcopy(infer.call_args.args[1])
        official_html = (f'<p class="is-direction1"></p><p class="is-wind{before["WIND_DIRECTION_CODE"]}"></p>').encode()
        old_result = old_predict(store.root, adoption, morning_record, before, captured,
                                proof["SNAPSHOT_SHA256"], predicted, official_html)
        require(alternate == infer.call_args.args[1], "new transport changed frozen feature record")
        require(result["INPUT_RECORD_SHA256"] == old_result["INPUT_RECORD_SHA256"],
                "frozen official/alternate feature hashes differ")
        require(result["OUTPUTS"] == old_result["OUTPUTS"], "inference boundary arguments differ")
        require(result["BEFOREINFO_SOURCE"] == source.HOST, "source identity was relabelled")
        require(infer.call_count == 2, "expected one alternate and one old-source inference observation")
    return True


def collect_wild(store, proof, wild, clock, *, expected_calls=1):
    features = {"SCHEMA": "G11_TRUE_AI_MORNING_FEATURE_BATCH_V1", "OPERATIONAL_DATE_JST": DAY,
        "RACE_COUNT": 1, "RECORDS": [{"RACE_ID": KEY, "MORNING_FEATURE_SNAPSHOT_SHA256": "d" * 64,
                                      "FEATURE_ASOF_TIME": DAY + "T07:00:00+09:00"}]}
    prediction = {name: {"MODEL_VERSION": name + "-fixture", "CURRENT_ID": name + "-current"}
                  for name in wild.PRE_RACE_WILD_IDS}
    prediction.update(SELECTED={"HEAD": 1, "SECOND": 2, "THIRD": 3},
                      FULL_PATH_COUNTS={"HEAD": 6, "SECOND": 30, "THIRD": 120})
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]
    with patch.object(wild, "datetime", Clock), \
            patch.object(wild, "_load_manifest_artifact", return_value=({"ARTIFACT_SHA256": "e" * 64}, features)), \
            patch.object(wild.pre_position, "predict_live", return_value=prediction) as predict, \
            patch.object(wild, "_pre_race_status", return_value={"fixture": True}):
        wild._collect_pre_race_packets(store.root, CUTOFF.date())
        require(predict.call_count == expected_calls, "frozen WILD inference call count changed")
        if expected_calls:
            require(predict.call_args.args[:4] == (proof["SNAPSHOT"]["PAYLOAD"]["PARSED"], KEY,
                    proof["SNAPSHOT"]["PAYLOAD"]["CAPTURED_AT_JST"], CUTOFF.isoformat()),
                    "WILD input facts were changed at handoff")
    packet = store.root / "g11/wild-pack-oos/pre_race" / DAY / ("collection-packet-" + KEY + ".json")
    saved = json.loads(packet.read_bytes())
    require(saved["SOURCES"]["BEFOREINFO"]["SOURCE_RECEIPTS"] == proof["SNAPSHOT"]["SOURCE_RECEIPTS"],
            "WILD lock lost original source receipts")
    return packet


def fun_packet(candidate, fun, shared, display, store, roster, clock):
    import re
    from concurrent.futures import Future

    raw = (candidate / "g11/canonical/fixtures/fun_original_20261126_20_12.html").read_bytes()
    names = fun._summary_names(fun.fromstring(raw))
    text = raw.decode().replace("2026-11-26", DAY).replace("/20/12/", "/05/2/")
    text = text.replace("若松", "多摩川").replace("12R", "2R")
    for boat, name in names.items():
        pattern = r"\s+".join(map(re.escape, name.split()))
        text = re.sub(pattern, roster[boat][2], text)
    synthetic = text.encode()
    calls = []
    def read(request, **_kwargs):
        calls.append(request.full_url)
        require(request.full_url == fun.FUN.url_for(KEY), "unexpected HTTP outside independent FUN source")
        return HTTPResponse(request.full_url, synthetic)
    proof = shared.acquire(store.root, KEY, CUTOFF, clock=lambda: clock["now"], opener=read, total_seconds=2)
    require(proof.get("STATUS") == "CAPTURED" and proof.get("PROVIDER") == fun.FUN.NAME,
            "FUN original proof was not captured: " + repr(proof) + " calls=" + repr(calls))
    require(calls == [fun.FUN.url_for(KEY)], "FUN proof repeated an acquisition")
    task = Future(); task.set_result(proof)
    with patch.object(display.study, "clock", return_value=clock["now"]):
        accepted = display.finish(task, KEY, CUTOFF,
            {boat: {"RACER_ID": racer, "RACER_NAME": name} for boat, (racer, _motor, name) in roster.items()},
            root=store.root)
    require(accepted == proof, "frozen native display finish did not accept verified original metrics")
    require(bool(display.columns(proof, KEY, CUTOFF, clock["now"])), "FUN raw measurements disappeared")
    return proof


def saved_fun_lock_replay_fixture(store, proof, true, clock, cleanup):
    """Use frozen model-pin fixtures and native selection/NIGHT proof builders."""
    from g11.growth_p3.v1_1 import test_true_only_models_v2 as fixture
    case = fixture.TrueOnlyTests()
    try:
        case.setUp()
        return _saved_fun_lock_replay_fixture(store, proof, true, clock, case)
    finally:
        case.doCleanups()


def _saved_fun_lock_replay_fixture(store, proof, true, clock, case):
    from g11.growth_p3.v1_1 import head_selection_diagnostics_v1 as night
    from g11.growth_p3.v1_1 import true_ai_head_adoption_v1 as adoption_module
    pin = deepcopy(case.pin); pin.pop("CONTENT_SHA256")
    pin.update(OPERATIONAL_DATE_JST=DAY, PREPARED_AT=DAY + "T07:00:00+09:00")
    pin[true.DISPLAY.FIELD] = true.DISPLAY.VERSION
    pin = true.seal(pin)
    adoption = deepcopy(case.adoption); adoption.pop("CONTENT_SHA256")
    adoption.update(RACE_ID=KEY, OPERATIONAL_DATE_JST=DAY, OFFICIAL_DEADLINE_JST=CUTOFF.isoformat(),
                    ADMITTED_AT_JST=DAY + "T07:01:00+09:00")
    adoption[true.PIN] = pin
    morning = deepcopy(adoption[true.MORNING]); morning.pop("CONTENT_SHA256")
    morning.update(RACE_ID=KEY, PREDICTED_AT=DAY + "T07:00:00+09:00",
                   VENUE_PROFILE=true.ENGINE.VENUE.evidence(KEY))
    adoption[true.MORNING] = true.seal(morning)
    adoption = adoption_module._seal(adoption)
    case.pin, case.adoption = pin, adoption
    value = case.ev(); value.pop("CONTENT_SHA256")
    value.update(RACE_ID=KEY, PIN_SHA256=pin["CONTENT_SHA256"],
        MORNING_ADOPTION_SHA256=adoption["CONTENT_SHA256"],
        CAPTURED_AT=proof["CAPTURED_AT"], PREDICTED_AT=clock["now"].isoformat(),
        DEADLINE=CUTOFF.isoformat(), VENUE_PROFILE=true.ENGINE.VENUE.evidence(KEY))
    value[true.DISPLAY.FIELD] = proof
    value = true.seal(value)
    true.verify_prediction(value, adoption)
    bets, ranking, selection = true.select(value, adoption, "LIVE")
    require(bool(bets), "FUN-based native saved selection unexpectedly abstained")
    selection.update(PRODUCTION_BETS=list(bets), PRODUCTION_POINTS=len(bets),
                     MORNING_ADOPTION_SHA256=adoption["CONTENT_SHA256"])
    selection["CONTENT_SHA256"] = night.digest(selection)
    bet_sha = night.digest({"RACE_ID": KEY, "SNAPSHOT": "LIVE", "PRODUCTION_BET_SET": sorted(bets),
                           "PRODUCTION_BET_ORDER": bets, "PRODUCTION_BET_COUNT": len(bets)})
    stamp = clock["now"].isoformat()
    payload = {"BET_SELECTION_MODE": true.POLICY, "RACE_ID": KEY, "SNAPSHOT": "LIVE",
        "TRUE_AI_SELECTION": selection, "TRUE_AI_ADOPTION": adoption,
        "PRODUCTION_BETS": list(bets), "PRODUCTION_POINTS": len(bets), "PREDICTION_AT": stamp,
        "BET_ARTIFACT_SHA": bet_sha, "TRIFECTA_CONFIDENCE": {
            "RACE_ID": KEY, "MORNING_OR_LIVE": "LIVE", "PREDICTION_LOCKED_AT": stamp,
            "CONFIDENCE_CAPTURED_AT": stamp, "RACE_DEADLINE_AT": CUTOFF.isoformat(),
            "PRODUCTION_BET_ORDER": list(bets), "PRODUCTION_BET_COUNT": len(bets),
            "BET_ARTIFACT_SHA": bet_sha}}
    payload["CONTENT_SHA256"] = night.digest(payload)
    diagnostic_proof = night.capture_saved_proof(payload, "a" * 64)
    diagnostic, outputs = night.read_proof(diagnostic_proof)
    outcome = night.join_outcome(diagnostic, outputs, "1-2-3")
    copied = true.DISPLAY.study.copy_formal(payload, "a" * 64, CUTOFF, true)
    require(copied["profiles"]["combined"] == bets and diagnostic["SAVED_SELECTION_EXACT"],
            "native NIGHT proof did not retain the original FUN buyset")
    folder = store.root / "g11/exhibition-fixture-saved-p3" / KEY
    folder.mkdir(parents=True)
    saved = {"payload": payload, "prediction": value, "adoption": adoption,
             "night_proof": diagnostic_proof, "bets": bets, "ranking": ranking,
             "outcome": outcome, "formal_copy": copied, "release": deepcopy(true.release())}
    for name, item in saved.items():
        (folder / (name + ".json")).write_text(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return folder, saved


def exercise():
    frozen = Path(os.environ["G11_EXHIBITION_FROZEN_DIR"]).resolve()
    candidate = Path(os.environ["G11_EXHIBITION_CANDIDATE_DIR"]).resolve()
    scripts = Path(__file__).resolve().parent
    sys.path[:] = [str(frozen), str(scripts), *[p for p in sys.path if p not in (str(candidate), str(frozen))]]
    os.chdir(frozen)
    import exhibition_input_compatibility_v1 as compatibility
    from g11.growth_p3.v1_1 import true_only_models_v2 as true, official_display_tickets_v1 as display
    from g11.wild_pack.v1 import runtime_orchestrator_v1 as wild
    from g11.canonical.test_official_pipeline import odds_html

    with TemporaryDirectory(prefix="exhibition-today-consumer-") as temporary, ExitStack() as test_scope:
        state = Path(temporary)
        official, preview, current, daily, store, runner, clock, morning = prepare_state(state, frozen)
        if os.environ.get("G11_EXHIBITION_ALLOW_API_MATERIALIZATION_TEST") == "1":
            # Local-only fixture: all candidate bytes are independently checked
            # by the helper; only the absent authenticated Git checkout is mocked.
            test_scope.enter_context(patch.object(compatibility, "_candidate_head",
                return_value=compatibility.CANDIDATE_SHA))
        old_predict = true.predict
        native = (true.infer, true.ENGINE._runtime_record, true.RACER.enrich,
                  true.TACTICAL_HEAD.enrich, official.ABEKEN_V53.derive_v53_pre_race,
                  wild.pre_position.predict_live, display.finish)
        original_wrapped = official.CanonicalRunner.run_due_predeadline_predictions
        old_sources = daily.source_inventory(frozen)
        old_models = daily.model_inventory(store.root)
        old_morning = file_bytes(store.root / "snapshots/morning")
        old_authority = file_bytes(state / "daily-runtime")
        old_locks = file_bytes(store.root / "snapshots/predeadline")
        checks = []
        with compatibility.context(state, DAY, candidate, nowfunc=lambda: clock["now"]):
            from g11.canonical import boatcast_preview_v1 as source, fun_original_display_v1 as fun
            from g11.canonical import shared_original_display_v1 as shared
            require(native == (true.infer, true.ENGINE._runtime_record, true.RACER.enrich,
                    true.TACTICAL_HEAD.enrich, official.ABEKEN_V53.derive_v53_pre_race,
                    wild.pre_position.predict_live, display.finish), "native model function identity changed")
            checks.append("FROZEN_NATIVE_INFERENCE_IDENTITIES")
            require(hasattr(official.CanonicalRunner.run_due_predeadline_predictions, "__wrapped__"),
                    "daily authority wrapper was bypassed")
            roster = preview.race_roster(morning, KEY)
            put_odds(store, official, KEY, clock["now"], odds_html())
            proof, calls = boatcast_packet(candidate, source, roster, store, clock)
            require(len(calls) == 4, "incorrect standard exhibition request count")
            result = runner.run_due_predeadline_predictions()
            require(KEY in result["COMPLETED"], "future frozen native formal P3 did not lock")
            v53 = store.load_verified_snapshot("predeadline", runner._abeken_v53_live_name(KEY))
            require(v53["SNAPSHOT"]["PAYLOAD"]["BEFOREINFO_SNAPSHOT_SHA256"] == proof["SNAPSHOT_SHA256"],
                    "future frozen V53 lock lost shared source")
            checks.extend(["FUTURE_NATIVE_FORMAL_P3_LOCK", "FUTURE_NATIVE_V53_LOCK"])
            frozen_feature_parity(store, source, true, old_predict, proof, roster)
            checks.append("REAL_FROZEN_WEATHER_AND_FEATURE_PARITY")
            wild_packet = collect_wild(store, proof, wild, clock)
            require(wild_packet.is_file(), "future WILD collection lock missing")
            checks.append("FUTURE_WILD_SHARED_INPUT_LOCK")
            original_proof = fun_packet(candidate, fun, shared, display, store, roster, clock)
            checks.append("FUN_ORIGINAL_NATIVE_DISPLAY_HANDOFF")
            saved_p3_folder, saved_p3 = saved_fun_lock_replay_fixture(
                store, original_proof, true, clock, test_scope)
            checks.append("FUN_NATIVE_SAVED_P3_SELECTION_AND_NIGHT_PROOF")
            stopped = deepcopy(store.ledger.state())
            with patch.object(type(store.ledger), "state", return_value={**stopped, "STOPPED": True}), \
                    patch.object(source, "_read", side_effect=AssertionError("STOP made an HTTP request")):
                reused = source.due_observation(store, KEY, CUTOFF, roster, lambda: clock["now"])
                require(reused is None or reused == proof, "STOP created or replaced an input")
                require(store.ledger.state()["STOPPED"] is True, "STOP flag was cleared")
            require(store.ledger.state() == stopped, "STOP policy ledger was changed")
            changed = deepcopy(proof["SNAPSHOT"])
            changed["SOURCE_RECEIPTS"][0]["HOST"] = "unreviewed.invalid"
            try:
                source.validate_snapshot(changed, KEY)
            except ValueError:
                pass
            else:
                raise AssertionError("unknown exhibition source was accepted")
            checks.append("GLOBAL_STOP_AND_UNKNOWN_SOURCE_REJECTION")
            with patch.object(source, "_read", side_effect=AssertionError("expired race made an HTTP request")):
                try:
                    expired = source.due_observation(store, PRIOR_KEY, CUTOFF.replace(minute=30),
                        preview.race_roster(morning, PRIOR_KEY), lambda: clock["now"])
                except ValueError:
                    expired = None
                require(expired is None, "expired race produced a new exhibition packet")
            checks.append("EXPIRED_RACE_NO_NEW_REQUEST_OR_LOCK")
        require(original_wrapped is official.CanonicalRunner.run_due_predeadline_predictions,
                "context did not restore frozen daily boundary")
        require(true.predict is old_predict, "context did not restore frozen source predictor")
        from g11.growth_p3.v1_1 import head_selection_diagnostics_v1 as night
        try:
            night.read_proof(saved_p3["night_proof"])
        except ValueError:
            pass
        else:
            raise AssertionError("unadapted frozen NIGHT reader unexpectedly accepted FUN source")
        controls = store.root / "g11/exhibition-input-compat"
        saved_controls = file_bytes(controls)
        saved_packet = wild_packet.read_bytes()
        with compatibility.context(state, DAY, candidate, nowfunc=lambda: clock["now"], read_only=True):
            from g11.canonical import boatcast_preview_v1 as source
            from g11.canonical import shared_original_display_v1 as shared
            with patch.object(source, "_read", side_effect=AssertionError("saved consumer acquired exhibition")), \
                    patch.object(shared, "urlopen", side_effect=AssertionError("saved consumer acquired original display")):
                retained = source.read_observation(store, KEY, CUTOFF, roster)
                require(retained == proof, "read-only worker changed retained exhibition packet")
                collect_wild(store, retained, wild, clock, expected_calls=0)
        require(wild_packet.read_bytes() == saved_packet, "read-only WILD worker rewrote an existing lock")
        require(file_bytes(controls) == saved_controls, "read-only worker changed activation or audit controls")
        checks.append("READ_ONLY_SAVED_WILD_WORKER_ZERO_HTTP_AND_IMMUTABLE_ACTIVATION")
        next_day = datetime.fromisoformat("2026-11-28T01:00:00+09:00")
        stored_p3_bytes = file_bytes(saved_p3_folder)
        old_forecast_files = file_bytes(store.root / "snapshots")
        with compatibility.context(state, DAY, candidate, nowfunc=lambda: next_day, read_only=True):
            from g11.canonical import shared_original_display_v1 as replay_shared
            with patch.object(true, "infer", side_effect=AssertionError("NIGHT reran model inference")), \
                    patch.object(true, "release", return_value=saved_p3["release"]), \
                    patch.object(display, "urlopen", side_effect=AssertionError("NIGHT acquired display")), \
                    patch.object(replay_shared, "urlopen", side_effect=AssertionError("NIGHT acquired source")):
                retained = {p.stem: json.loads(p.read_bytes()) for p in saved_p3_folder.glob("*.json")}
                true.verify_prediction(retained["prediction"], retained["adoption"])
                replay_bets, replay_rank, _ = true.select(retained["prediction"], retained["adoption"], "LIVE")
                diagnostic, outputs = night.read_proof(retained["night_proof"])
                outcome = night.join_outcome(diagnostic, outputs, "1-2-3")
                copied = display.study.copy_formal(retained["payload"], "a" * 64, CUTOFF, true)
                require(replay_bets == retained["bets"] and replay_rank == retained["ranking"],
                        "next-day read-only decoder changed saved production bets")
                require(outcome == retained["outcome"] and copied == retained["formal_copy"],
                        "next-day native NIGHT replay changed saved result attribution")
        require(file_bytes(saved_p3_folder) == stored_p3_bytes, "NIGHT rewrote saved P3 proof")
        require(file_bytes(store.root / "snapshots") == old_forecast_files, "NIGHT wrote a new forecast")
        require(file_bytes(controls) == saved_controls, "next-day read-only replay changed activation/audit controls")
        checks.append("NEXT_DAY_FUN_SAVED_P3_AND_NATIVE_NIGHT_REPLAY_WITHOUT_NEW_FORECAST")
        try:
            with compatibility.context(state, DAY, candidate, nowfunc=lambda: clock["now"]):
                raise RuntimeError("injected consumer exception")
        except RuntimeError as error:
            require(str(error) == "injected consumer exception", "wrong compatibility failure")
        require(true.predict is old_predict and original_wrapped is official.CanonicalRunner.run_due_predeadline_predictions,
                "exception context leaked a transport patch")
        require(daily.source_inventory(frozen) == old_sources, "frozen source files changed")
        require(daily.model_inventory(store.root) == old_models, "frozen model artifact changed")
        require(file_bytes(store.root / "snapshots/morning") == old_morning, "MORNING lock bytes changed")
        require(all((store.root / "snapshots/predeadline" / name).read_bytes() == raw for name, raw in old_locks.items()),
                "existing predeadline LOCK bytes changed")
        require(all((state / "daily-runtime" / name).read_bytes() == raw for name, raw in old_authority.items()),
                "existing daily authority/binding bytes changed")
        daily.verify_runtime(state, DAY, frozen)
        daily.verify_locks(state, DAY, "MORNING", exact=True)
        checks.extend(["MORNING_MODELS_EXISTING_LOCKS_UNCHANGED", "EXCEPTION_RESTORES_ALL_BOUNDARIES"])
        return {"STATUS": "PASS", "CHECKS": checks, "FROZEN_SOURCE_SHA": FROZEN_SHA,
                "SOURCE_DIRECTORY": str(frozen), "SYNTHETIC_INPUTS_AND_CLOCKS": True,
                "NATIVE_INFERENCE_ARTIFACTS": "OBSERVED_AT_BOUNDARY", "LIVE_HTTP_REQUESTS": 0}


class FrozenConsumerIntegrationTests(unittest.TestCase):
    def test_today_overlay_runs_frozen_consumers_and_preserves_existing_authority(self):
        if not os.environ.get("G11_EXHIBITION_FROZEN_DIR") or not os.environ.get("G11_EXHIBITION_CANDIDATE_DIR"):
            self.skipTest("requires explicit frozen and candidate source checkouts")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--frozen-exercise"],
                                env=dict(os.environ), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + "\n" + result.stderr[-10000:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["STATUS"], "PASS")
        self.assertEqual(report["LIVE_HTTP_REQUESTS"], 0)
        self.assertGreaterEqual(len(report["CHECKS"]), 13)
        self.assertIn("FUN_NATIVE_SAVED_P3_SELECTION_AND_NIGHT_PROOF", report["CHECKS"])
        self.assertIn("NEXT_DAY_FUN_SAVED_P3_AND_NATIVE_NIGHT_REPLAY_WITHOUT_NEW_FORECAST", report["CHECKS"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--frozen-exercise"]:
        print(json.dumps(exercise(), sort_keys=True))
    else:
        unittest.main()
