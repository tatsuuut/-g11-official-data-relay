"""Run the production shell resolver against delayed cron and day boundaries."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/g11-free-runner.yml"
MORNING_CRONS = ("15-55/5 17 * * *", "*/5 18-21 * * *", "0-5/5 22 * * *")
DAY_CRONS = ("*/5 23 * * *", "*/5 0-11 * * *", "0-10/5 12 * * *")


def production_resolver():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.split("      - name: Resolve the bounded phase and race day\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class ScheduledPhaseRoutingTests(unittest.TestCase):
    def resolve(self, at, *, event="schedule", cron=MORNING_CRONS[-1],
                phase="", day="", commit="", succeeds=True):
        # Only the clock is replaced. The workflow's actual shell, input checks,
        # operational-date rollover and GITHUB_OUTPUT are executed unchanged.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            date_command = root / "date"
            date_command.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                from datetime import datetime, timedelta
                import os
                import sys
                current = datetime.fromisoformat(os.environ["G11_TEST_NOW"])
                arguments = sys.argv[1:]
                if arguments == ["--date=yesterday", "+%F"]:
                    current -= timedelta(days=1)
                elif len(arguments) != 1:
                    raise SystemExit("unexpected date arguments")
                requested = arguments[-1]
                formats = {"+%H": "%H", "+%M": "%M", "+%H%M": "%H%M", "+%F": "%Y-%m-%d"}
                if requested not in formats:
                    raise SystemExit("unexpected date format")
                print(current.strftime(formats[requested]))
                """), encoding="utf-8")
            date_command.chmod(0o700)
            output = root / "outputs"
            environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                TZ="Asia/Tokyo", G11_TEST_NOW=at, GITHUB_OUTPUT=str(output),
                EVENT_NAME=event, SCHEDULE_EXPRESSION=cron, REQUESTED_PHASE=phase,
                REQUESTED_DATE=day, COMMIT_MESSAGE=commit)
            result = subprocess.run(["bash", "-e", "-o", "pipefail"],
                input=production_resolver(), env=environment, cwd=root,
                text=True, capture_output=True, timeout=10)
            if not succeeds:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                return None
            self.assertEqual(result.returncode, 0, result.stderr)
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def assert_route(self, at, expected_phase, expected_day="2026-11-01", **kwargs):
        self.assertEqual(self.resolve(at, **kwargs), {
            "phase": expected_phase, "operational_date": expected_day})

    def test_delayed_morning_crons_use_the_live_lane_at_naruto_incident_time(self):
        for cron in MORNING_CRONS:
            with self.subTest(cron=cron):
                self.assert_route("2026-11-01T09:45:00+09:00", "predeadline", cron=cron)

    def test_current_morning_window_wins_over_every_recognized_cron(self):
        for clock in ("02:15:00", "07:59:59"):
            for cron in MORNING_CRONS + DAY_CRONS:
                with self.subTest(clock=clock, cron=cron):
                    self.assert_route(f"2026-11-01T{clock}+09:00", "morning", cron=cron)

    def test_eight_oclock_is_predeadline_including_delayed_morning_crons(self):
        for cron in MORNING_CRONS + DAY_CRONS:
            with self.subTest(cron=cron):
                self.assert_route("2026-11-01T08:00:00+09:00", "predeadline", cron=cron)

    def test_daytime_schedule_keeps_normal_and_late_race_continuation(self):
        for clock in ("12:00:00", "21:10:00", "21:59:59"):
            for cron in MORNING_CRONS + DAY_CRONS:
                with self.subTest(clock=clock, cron=cron):
                    self.assert_route(f"2026-11-01T{clock}+09:00", "predeadline", cron=cron)

    def test_delayed_cron_before_morning_does_not_restart_morning(self):
        for clock, day in (("00:00:00", "2026-10-31"), ("01:59:59", "2026-10-31"),
                           ("02:00:00", "2026-11-01"), ("02:14:59", "2026-11-01")):
            for cron in MORNING_CRONS + DAY_CRONS:
                with self.subTest(clock=clock, cron=cron):
                    self.assert_route(f"2026-11-01T{clock}+09:00", "auto", day, cron=cron)

    def test_late_cron_uses_existing_automatic_night_boundary(self):
        for clock in ("22:00:00", "23:59:59"):
            for cron in MORNING_CRONS + DAY_CRONS:
                with self.subTest(clock=clock, cron=cron):
                    self.assert_route(f"2026-11-01T{clock}+09:00", "auto", cron=cron)

    def test_yesterdays_daytime_cron_at_next_morning_targets_current_day(self):
        for cron in DAY_CRONS:
            with self.subTest(cron=cron):
                self.assert_route("2026-11-02T02:15:00+09:00", "morning", "2026-11-02", cron=cron)

    def test_unknown_cron_does_not_gain_daytime_authority(self):
        self.assert_route("2026-11-01T09:45:00+09:00", "auto", cron="unrecognized")

    def test_explicit_manual_phase_and_date_are_preserved(self):
        for phase in ("morning", "predeadline", "night", "dry-run", "auto", "same-day-rescue"):
            with self.subTest(phase=phase):
                self.assert_route("2026-11-01T09:45:00+09:00", phase, "2026-10-31",
                    event="workflow_dispatch", cron="", phase=phase, day="2026-10-31")
        self.assert_route("2026-11-01T09:45:00+09:00", "dry-run", event="workflow_dispatch", cron="")

    def test_night_transport_stays_scoped_to_the_existing_historical_date(self):
        self.assert_route("2026-11-01T09:45:00+09:00", "night", "2026-10-13",
            event="workflow_dispatch", cron="", phase="night-transport", day="2026-10-13")
        self.resolve("2026-11-01T09:45:00+09:00", event="workflow_dispatch", cron="",
            phase="night-transport", day="2026-10-31", succeeds=False)

    def test_manual_invalid_phase_and_date_remain_rejected(self):
        for phase, day in (("unsafe", "2026-11-01"), ("night", "2026-02-30"),
                           ("night", "20261101")):
            with self.subTest(phase=phase, day=day):
                self.resolve("2026-11-01T09:45:00+09:00", event="workflow_dispatch", cron="",
                    phase=phase, day=day, succeeds=False)

    def test_existing_explicit_push_recovery_routes_are_preserved(self):
        for commit, phase in (("[morning-current]", "morning"),
                              ("[night-current]", "night"),
                              ("[daily-runtime-freeze]", "dry-run")):
            with self.subTest(commit=commit):
                self.assert_route("2026-11-01T09:45:00+09:00", phase,
                    event="push", cron="", commit=commit)


if __name__ == "__main__":
    unittest.main()
