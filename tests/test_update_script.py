"""Runs deploy/update.sh against fake installs. systemctl is a stub that records calls and keeps
unit state in files, so nothing touches the real system."""
import fcntl, os, shutil, stat, subprocess, sys, tempfile, textwrap, time, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "update.sh"
TOP_LEVEL_EXCLUDE = {".venv", "data", "logs", "reports", ".env", ".release"}   # only at the project root


def skip(rel: Path) -> bool:
    return rel.parts[0] in TOP_LEVEL_EXCLUDE or "__pycache__" in rel.parts or rel.suffix == ".pyc"


def ignore_for_copy(directory, names):
    out = {n for n in names if n == "__pycache__" or n.endswith(".pyc")}
    if Path(directory) == ROOT:
        out |= TOP_LEVEL_EXCLUDE & set(names)
    return out

STUB = """#!/usr/bin/env bash
S="$STUB_STATE"; echo "$*" >> "$S/calls.log"
cmd="$1"; shift
case "$cmd" in
  is-enabled) [[ -e "$S/enabled-$1" ]] ;;
  is-active) [[ -e "$S/active-$1" ]] ;;
  stop) for u in "$@"; do rm -f "$S/active-$u"; done; [[ -n "${STUB_ON_STOP:-}" ]] && eval "$STUB_ON_STOP" ;;
  start|restart) for u in "$@"; do [[ -e "$S/failstart-$u" ]] && exit 1; touch "$S/active-$u"; done ;;
  enable) for u in "$@"; do [[ "$u" == --now ]] || touch "$S/enabled-$u"; done ;;
esac
"""


def project_zip(dest: Path, replace: dict | None = None, extra: dict | None = None, truncate=False) -> Path:
    replace, extra = replace or {}, extra or {}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob("*")):
            rel = p.relative_to(ROOT)
            if p.is_dir() or skip(rel):
                continue
            name = f"groww-swing-agent/{rel.as_posix()}"
            data = replace.get(rel.as_posix(), p.read_bytes())
            info = zipfile.ZipInfo(name, (2026, 10, 25, 12, 0, 0))
            info.external_attr = (p.stat().st_mode & 0o777) << 16
            z.writestr(info, data)
        for name, data in extra.items():
            z.writestr(name, data)
    if truncate:
        dest.write_bytes(dest.read_bytes()[: dest.stat().st_size // 2])
    return dest


class Env:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.home, self.units, self.tmp, self.state = (self.root / d for d in ("home", "units", "tmp", "state"))
        for d in (self.home, self.units, self.tmp, self.state, self.root / "bin"):
            d.mkdir()
        stub = self.root / "bin" / "systemctl"
        stub.write_text(STUB); stub.chmod(0o755)
        self.app = self.home / "groww-swing-agent"
        self.procs = []

    def env(self, **kw):
        e = {**os.environ, "AGENT_USER": subprocess.check_output(["id", "-un"], text=True).strip(),
             "HOME_DIR": str(self.home), "UNIT_DIR": str(self.units), "ZIP_DIR": str(self.tmp),
             "LOCK_FILE": str(self.root / "lock"), "ALLOW_NON_ROOT": "1", "STUB_STATE": str(self.state),
             "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}", "SKIP_PIP": "1", "PYTHON_BIN": "python3",
             "VENV_ARGS": "--without-pip --system-site-packages", "SMOKE_TESTS": "tests.test_core",
             "SETTLE_SECONDS": "0", "WAIT_SECONDS": "0", "UPDATE_NOW": "2026-10-25 17:00"}
        e.update(kw)
        return e

    def run(self, *args, **kw):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=self.env(**kw), timeout=240)

    def calls(self) -> list[str]:
        f = self.state / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def changes(self) -> list[str]:
        """systemctl calls that change anything (is-enabled / is-active queries are read-only)."""
        return [c for c in self.calls() if c.split()[0] in {"stop", "start", "restart", "enable", "disable", "daemon-reload"}]

    def enable(self, *units):
        for u in units:
            (self.state / f"enabled-{u}").touch(); (self.state / f"active-{u}").touch()

    def active(self, unit) -> bool:
        return (self.state / f"active-{unit}").exists()

    def install_old(self, marker="OLD_VERSION", env_text=None, data=True):
        """A previous install: the project tree plus .env, data and a marker file."""
        shutil.copytree(ROOT, self.app, ignore=ignore_for_copy)
        (self.app / "OLD_MARKER").write_text(marker)
        (self.app / ".env").write_text(env_text or "GROWW_MODE=PAPER\nTELEGRAM_BOT_TOKEN=keepme123\nPAPER_CAPITAL=99999\n")
        (self.app / ".env").chmod(0o600)
        if data:
            (self.app / "data").mkdir(exist_ok=True)
            (self.app / "data" / "paper.db").write_text("PAPER-LEDGER-CONTENT")
            (self.app / "data" / "market.db").write_text("MARKET-CONTENT")
        (self.app / "requirements.lock").write_text("pandas\n")

    def job(self, body: str, name="python") -> subprocess.Popen:
        """A fake agent process whose command line matches the one the script looks for."""
        d = self.app / ".venv" / "bin"; d.mkdir(parents=True, exist_ok=True)
        f = d / name; f.write_text("#!/usr/bin/env bash\n" + body); f.chmod(0o755)
        p = subprocess.Popen([str(f), "-m", "app.run", "daily"], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # must not inherit the test's pipes
        self.procs.append(p); time.sleep(0.3)
        return p

    def cleanup(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, 9)          # the whole group: the job's own children (e.g. sleep) too
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(self.root, ignore_errors=True)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zdir = Path(tempfile.mkdtemp())
        cls.good = project_zip(cls.zdir / "good.zip")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.zdir, ignore_errors=True)

    def setUp(self):
        self.e = Env(); self.addCleanup(self.e.cleanup)

    def put(self, zip_path: Path, name="groww-swing-agent.zip") -> Path:
        dst = self.e.tmp / name
        shutil.copy(zip_path, dst)
        return dst

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestHappyPaths(Base):
    def test_first_install(self):
        z = self.put(self.good)
        r = self.e.run(); self.ok(r)
        self.assertTrue((self.e.app / "app" / "run.py").exists())
        self.assertTrue((self.e.app / ".venv" / "bin" / "python").exists())
        self.assertTrue((self.e.app / ".release").read_text().startswith("released"))
        self.assertTrue((self.e.app / "deploy" / "install_systemd.sh").stat().st_mode & stat.S_IXUSR)
        self.assertFalse(z.exists(), "zip should be removed from /tmp after success")
        self.assertEqual(self.e.calls().count("restart groww-agent-daily.timer"), 0)   # nothing was enabled

    def test_update_preserves_settings_and_data_and_restarts_enabled_services(self):
        self.e.install_old()
        self.e.enable("groww-agent-daily.timer", "groww-agent-dashboard.service")
        self.put(self.good)
        r = self.e.run(); self.ok(r)
        env = (self.e.app / ".env").read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=keepme123", env); self.assertIn("PAPER_CAPITAL=99999", env)
        self.assertIn("DASHBOARD_PASSWORD=", env); self.assertIn("HEARTBEAT_URL=", env)       # new keys added
        self.assertEqual(env.count("TELEGRAM_BOT_TOKEN="), 1)                                # not duplicated
        self.assertEqual(stat.S_IMODE((self.e.app / ".env").stat().st_mode), 0o600)
        self.assertEqual((self.e.app / "data" / "paper.db").read_text(), "PAPER-LEDGER-CONTENT")
        self.assertEqual((self.e.app / "data" / "market.db").read_text(), "MARKET-CONTENT")
        self.assertFalse((self.e.app / "OLD_MARKER").exists())
        backups = list(self.e.home.glob("groww-swing-agent.bak-*"))
        self.assertEqual(len(backups), 1); self.assertEqual((backups[0] / "OLD_MARKER").read_text(), "OLD_VERSION")
        calls = self.e.calls()
        self.assertLess(calls.index("stop groww-agent-daily.timer groww-agent-monitor.timer"),
                        calls.index("restart groww-agent-daily.timer"))
        self.assertTrue(self.e.active("groww-agent-daily.timer") and self.e.active("groww-agent-dashboard.service"))
        self.assertNotIn("restart groww-agent-monitor.timer", calls)                          # was not enabled
        unit = (self.e.units / "groww-agent-daily.service").read_text()
        self.assertIn(f"WorkingDirectory={self.e.app}", unit); self.assertNotIn("__", unit)

    def test_install_units_flag_enables_daily_timer_and_dashboard(self):
        self.put(self.good)
        self.ok(self.e.run("--install-units"))
        self.assertTrue((self.e.state / "enabled-groww-agent-daily.timer").exists())
        self.assertTrue(self.e.active("groww-agent-dashboard.service"))

    def test_monitor_units_kept_only_if_they_existed(self):
        self.e.install_old(); self.e.enable("groww-agent-daily.timer", "groww-agent-monitor.timer")
        self.put(self.good); self.ok(self.e.run())
        self.assertTrue((self.e.units / "groww-agent-monitor.timer").exists())
        self.assertIn("restart groww-agent-monitor.timer", self.e.calls())

    def test_newest_zip_wins(self):
        old = self.put(self.good, "groww-swing-agent-old.zip")
        os.utime(old, (time.time() - 1000, time.time() - 1000))
        bad = project_zip(self.e.tmp / "groww-swing-agent-new.zip", truncate=True)
        r = self.e.run()
        self.assertNotEqual(r.returncode, 0)                       # newest (corrupt) one was chosen, not the old good one
        self.assertIn("groww-swing-agent-new.zip", r.stdout)

    def test_old_backups_are_pruned(self):
        self.e.install_old()
        for i in range(5):
            d = self.e.home / f"groww-swing-agent.bak-2026010{i}-000000"; d.mkdir()
            os.utime(d, (time.time() - 5000 + i, time.time() - 5000 + i))
        self.put(self.good); self.ok(self.e.run("--keep", "2"))
        self.assertEqual(len(list(self.e.home.glob("groww-swing-agent.bak-*"))), 2)


class TestZipSwappedAfterValidation(Base):
    def test_update_uses_the_validated_private_copy_not_the_tmp_file(self):
        self.e.install_old()
        z = self.put(self.good)
        evil = project_zip(self.e.root / "evil.zip", extra={"groww-swing-agent/EVIL_MARKER": "pwned"})
        r = self.e.run(STUB_ON_STOP=f"cp '{evil}' '{z}'")          # another user swaps /tmp's zip mid-update
        self.ok(r)
        self.assertFalse((self.e.app / "EVIL_MARKER").exists(), "deployed a file that was swapped in after validation")

    def test_update_survives_the_tmp_zip_vanishing(self):
        self.e.install_old()
        z = self.put(self.good)
        self.ok(self.e.run(STUB_ON_STOP=f"rm -f '{z}'"))
        self.assertTrue((self.e.app / "app" / "run.py").exists())

    def test_validated_copy_is_kept_and_matches_release_file(self):
        self.put(self.good); self.ok(self.e.run())
        copies = list((self.e.home / "releases").glob("groww-swing-agent-*.zip"))
        self.assertEqual(len(copies), 1)
        import hashlib
        sha = hashlib.sha256(copies[0].read_bytes()).hexdigest()[:16]
        self.assertIn(sha, (self.e.app / ".release").read_text())


class TestStopping(Base):
    def test_running_job_is_killed_when_it_will_not_finish(self):
        self.e.install_old(); self.put(self.good)
        p = self.e.job("sleep 300\n")
        self.ok(self.e.run())
        time.sleep(0.5); self.assertIsNotNone(p.poll(), "job should have been terminated")

    def test_job_gets_a_chance_to_shut_down_cleanly_before_any_kill(self):
        self.e.install_old(); self.put(self.good)
        marker = self.e.root / "got-term"
        self.e.job(f"trap 'touch {marker}; exit 0' TERM\nsleep 300 &\nwait $!\n")
        self.ok(self.e.run())
        time.sleep(0.5)
        self.assertTrue(marker.exists(), "job was killed without a SIGTERM first")

    def test_stubborn_job_gets_sigkill(self):
        self.e.install_old(); self.put(self.good)
        p = self.e.job("trap '' TERM\nwhile true; do sleep 1; done\n")
        self.ok(self.e.run())
        time.sleep(0.5); self.assertIsNotNone(p.poll())

    def test_script_waits_for_a_job_that_finishes_on_its_own(self):
        self.e.install_old(); self.put(self.good)
        marker = self.e.root / "finished"
        p = self.e.job(f"sleep 3\ntouch {marker}\n")
        self.ok(self.e.run(WAIT_SECONDS="30"))
        self.assertTrue(marker.exists(), "job was killed instead of being allowed to finish")


class TestRefusals(Base):
    def snapshot(self):
        return sorted(str(p.relative_to(self.e.app)) for p in self.e.app.rglob("*")) if self.e.app.exists() else []

    def assert_untouched(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.e.changes(), [], "services must not be touched when the update is refused")
        self.assertEqual((self.e.app / "OLD_MARKER").read_text(), "OLD_VERSION")

    def test_corrupt_zip(self):
        self.e.install_old(); before = self.snapshot()
        project_zip(self.e.tmp / "groww-swing-agent.zip", truncate=True)
        r = self.e.run(); self.assertNotEqual(r.returncode, 0); self.assert_untouched(before)

    def test_wrong_zip_contents(self):
        self.e.install_old(); before = self.snapshot()
        with zipfile.ZipFile(self.e.tmp / "groww-swing-agent.zip", "w") as z:
            z.writestr("groww-swing-agent/readme.txt", "not a release")
        r = self.e.run(); self.assertNotEqual(r.returncode, 0); self.assert_untouched(before)

    def test_path_traversal_zip(self):
        self.e.install_old(); before = self.snapshot()
        project_zip(self.e.tmp / "groww-swing-agent.zip", extra={"groww-swing-agent/../evil.txt": "x"})
        r = self.e.run(); self.assertNotEqual(r.returncode, 0); self.assert_untouched(before)
        self.assertFalse((self.e.home / "evil.txt").exists())

    def test_symlink_zip(self):
        self.e.install_old(); before = self.snapshot()
        with zipfile.ZipFile(self.good) as src, zipfile.ZipFile(self.e.tmp / "groww-swing-agent.zip", "w") as z:
            for i in src.infolist():
                z.writestr(i, src.read(i))
            link = zipfile.ZipInfo("groww-swing-agent/evil_link"); link.external_attr = (0o120777) << 16
            z.writestr(link, "/etc/passwd")
        r = self.e.run(); self.assertNotEqual(r.returncode, 0); self.assert_untouched(before)

    def test_no_zip_present(self):
        self.e.install_old(); before = self.snapshot()
        r = self.e.run(); self.assertNotEqual(r.returncode, 0); self.assertIn("no ", r.stdout + r.stderr); self.assert_untouched(before)

    def test_dry_run_changes_nothing(self):
        self.e.install_old(); before = self.snapshot(); z = self.put(self.good)
        r = self.e.run("--dry-run"); self.ok(r)
        self.assertIn("dry run", r.stdout); self.assert_untouched(before); self.assertTrue(z.exists())

    def test_second_update_cannot_run_at_the_same_time(self):
        self.e.install_old(); before = self.snapshot(); self.put(self.good)
        with open(self.e.root / "lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.e.run()
        self.assertNotEqual(r.returncode, 0); self.assertIn("already running", r.stdout + r.stderr); self.assert_untouched(before)

    def test_live_mode_refused_while_market_open_but_allowed_after_close_or_forced(self):
        live = "GROWW_MODE=LIVE\nTELEGRAM_BOT_TOKEN=keepme123\n"
        self.e.install_old(env_text=live); before = self.snapshot(); self.put(self.good)
        r = self.e.run(UPDATE_NOW="2026-10-26 11:00")                               # Monday, market open
        self.assertNotEqual(r.returncode, 0); self.assertIn("market is open", r.stdout + r.stderr); self.assert_untouched(before)
        self.ok(self.e.run("--force", UPDATE_NOW="2026-10-26 11:00"))

    def test_live_mode_allowed_after_close_and_on_weekends(self):
        live = "GROWW_MODE=LIVE\n"
        self.e.install_old(env_text=live); self.put(self.good)
        self.ok(self.e.run(UPDATE_NOW="2026-10-26 16:30"))
        self.e.cleanup(); self.e = Env(); self.addCleanup(self.e.cleanup)
        self.e.install_old(env_text=live); self.put(self.good)
        self.ok(self.e.run(UPDATE_NOW="2026-10-24 11:00"))                          # Saturday

    def test_paper_mode_not_blocked_during_market_hours(self):
        self.e.install_old(); self.put(self.good)
        self.ok(self.e.run(UPDATE_NOW="2026-10-26 11:00"))


class TestRollback(Base):
    def prepare(self):
        self.e.install_old()
        self.e.enable("groww-agent-daily.timer", "groww-agent-dashboard.service")

    def assert_restored(self, r):
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.e.app / "OLD_MARKER").read_text(), "OLD_VERSION")
        self.assertIn("TELEGRAM_BOT_TOKEN=keepme123", (self.e.app / ".env").read_text())
        self.assertEqual((self.e.app / "data" / "paper.db").read_text(), "PAPER-LEDGER-CONTENT")
        self.assertTrue(self.e.active("groww-agent-daily.timer"), "previous services must be running again")
        self.assertTrue(self.e.active("groww-agent-dashboard.service"))
        self.assertEqual(len(list(self.e.home.glob("groww-swing-agent.failed-*"))), 1)
        self.assertEqual(list(self.e.home.glob("groww-swing-agent.bak-*")), [])             # backup became the live copy

    def test_failing_tests_roll_back(self):
        self.prepare()
        bad = project_zip(self.e.tmp / "groww-swing-agent.zip", replace={
            "tests/test_core.py": b"import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.fail('broken release')\n"})
        r = self.e.run()
        self.assert_restored(r); self.assertIn("smoke tests failed", r.stdout + r.stderr)

    def test_unimportable_release_rolls_back(self):
        self.prepare()
        project_zip(self.e.tmp / "groww-swing-agent.zip", replace={"app/run.py": b"def broken(:\n"})
        r = self.e.run(); self.assert_restored(r); self.assertIn("cannot be imported", r.stdout + r.stderr)

    def test_failed_package_install_rolls_back(self):
        self.prepare(); self.put(self.good)
        r = self.e.run(SKIP_PIP="")                          # the test venv has no pip, so installing fails like a dead network
        self.assert_restored(r)

    def test_timer_that_will_not_start_rolls_back(self):
        self.prepare(); self.put(self.good)
        (self.e.state / "failstart-groww-agent-daily.timer").touch()
        r = self.e.run()
        self.assertNotEqual(r.returncode, 0); self.assertEqual((self.e.app / "OLD_MARKER").read_text(), "OLD_VERSION")
        self.assertIn("did not start", r.stdout + r.stderr)

    def test_dashboard_that_will_not_start_only_warns(self):
        self.prepare(); self.put(self.good)
        (self.e.state / "failstart-groww-agent-dashboard.service").touch()
        r = self.e.run(); self.ok(r)
        self.assertIn("is not running", r.stdout + r.stderr); self.assertFalse((self.e.app / "OLD_MARKER").exists())

    def test_rollback_restores_old_unit_files(self):
        self.prepare()
        (self.e.units / "groww-agent-daily.service").write_text("OLD UNIT FILE\n")
        project_zip(self.e.tmp / "groww-swing-agent.zip")
        (self.e.state / "failstart-groww-agent-daily.timer").touch()
        r = self.e.run(); self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.e.units / "groww-agent-daily.service").read_text(), "OLD UNIT FILE\n")

    def test_failure_before_swap_restarts_services_and_keeps_old_version(self):
        self.prepare()
        with zipfile.ZipFile(self.e.tmp / "groww-swing-agent.zip", "w") as z:
            with zipfile.ZipFile(self.good) as src:
                for i in src.infolist():
                    if i.filename != "groww-swing-agent/app/run.py":                 # looks valid until unpacked? no: validation rejects
                        z.writestr(i, src.read(i))
        r = self.e.run()
        self.assertNotEqual(r.returncode, 0); self.assertEqual((self.e.app / "OLD_MARKER").read_text(), "OLD_VERSION")
        self.assertTrue(self.e.active("groww-agent-daily.timer"))


if __name__ == "__main__":
    unittest.main()
