"""Keep the existing official K settlement transport independent of a live-input outage."""
from datetime import date
from contextlib import contextmanager


def separate_result_host(ledger, source_id, url, result_url):
    day = date.fromisoformat(ledger._manifest()["OPERATIONAL_DATE_JST"])
    if source_id != f"result-k-{day:%Y%m%d}" or url != result_url(day):
        return False
    # Only explain a terminal stop whose rejection receipts ALL belong to the
    # live race host. A refusal on the archive origin still stops that origin.
    request = None
    counts = {"HTTP_403": 0, "HTTP_429": 0}
    for e in ledger.events():
        payload = e["PAYLOAD"]
        if e["TYPE"] == "REQUEST_STARTED":
            if request is not None:
                return False
            request = payload
            continue
        if e["TYPE"] != "OUTCOME_RECORDED" or not request or request["REQUEST_ID"] != payload["REQUEST_ID"]:
            return False
        reason = payload["OUTCOME"]
        if reason in counts:
            if request["HOST"] != "www.boatrace.jp" or not request["SOURCE_ID"].startswith(("beforeinfo-", "odds3t-")):
                return False
            if request["SOURCE_ID"].startswith("beforeinfo-"):
                counts[reason] += 1
        request = None
    return request is None and (counts["HTTP_403"] >= 2 or counts["HTTP_429"] >= 3)


@contextmanager
def isolated_night_archive(official):
    from morning_race_isolation_v1 import replace_pinned
    ledger = official.DailyRequestLedger
    begin = ledger.begin
    try:
        ledger.begin = replace_pinned(begin, 'ca6ad66b971315ae3d340a67f1a206fe049216567089463307b6f1b73294d875', [
            ('_require(not state["STOPPED"], "PIPELINE_STOPPED")',
             '_require(not state["STOPPED"] or _separate_result_host(self, source_id, url, result_url), "PIPELINE_STOPPED")')
        ], {'_separate_result_host': separate_result_host})
        yield
    finally:
        ledger.begin = begin
