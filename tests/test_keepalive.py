"""Gardien du VPS : relance après un plantage ou une coupure, jamais après un arrêt voulu ou un refus."""

import subprocess
from datetime import UTC, datetime

import pytest

from goldbot.live.keepalive import FIRST_WAIT_S, MAX_WAIT_S, keep_running


class FakeProcess:
    def __init__(self, code, *, interrupt=False, slow=0):
        self.code, self.interrupt, self.slow = code, interrupt, slow

    def wait(self, timeout=None):
        if self.slow:  # le bot tourne encore
            self.slow -= 1
            raise subprocess.TimeoutExpired("bot", timeout)
        if self.interrupt:  # Ctrl+C reçu par le gardien pendant que le bot tourne
            self.interrupt = False
            raise KeyboardInterrupt
        return self.code


class World:
    def __init__(self, processes, run_seconds=10.0):
        self.processes = list(processes)
        self.started, self.sleeps, self.lines = [], [], []
        self.time, self.run_seconds = 0.0, run_seconds

    def start(self, command, cwd):
        self.started.append((command, cwd))
        self.time += self.run_seconds
        return self.processes.pop(0)

    def run(self, tmp_path, **kwargs):
        return keep_running(["python", "scripts/run_scalp.py"], cwd=tmp_path, log_path=tmp_path / "logs" / "g.log",
                            start=self.start, sleep=self.sleeps.append, clock=lambda: self.time,
                            now=lambda: datetime(2026, 9, 30, 8, 0, tzinfo=UTC), echo=self.lines.append,
                            **kwargs)  # fmt: skip


@pytest.mark.parametrize("code", [0, 3, 4, 5])
def test_a_deliberate_stop_or_a_refusal_is_never_restarted(tmp_path, code):
    world = World([FakeProcess(code)])
    assert world.run(tmp_path) == code
    assert len(world.started) == 1 and world.sleeps == []
    assert "Pas de relance automatique" in world.lines[-1]


def test_a_crash_or_a_lost_terminal_is_restarted_with_a_growing_wait(tmp_path):
    world = World([FakeProcess(1), FakeProcess(2), FakeProcess(1), FakeProcess(3)])
    assert world.run(tmp_path) == 3
    assert len(world.started) == 4 and world.sleeps == [FIRST_WAIT_S, 2 * FIRST_WAIT_S, 4 * FIRST_WAIT_S]
    assert any("code 2 : connexion au terminal MT5 impossible" in line for line in world.lines)
    assert all(command == ["python", "scripts/run_scalp.py"] for command, _ in world.started)  # jamais d'option
    log = (tmp_path / "logs" / "g.log").read_text(encoding="utf-8")
    assert log.count("démarrage de run_scalp.py") == 4 and "10:00:00" in log  # heure de Paris


def test_the_wait_is_capped_and_resets_after_a_long_normal_run(tmp_path):
    world = World([FakeProcess(1)] * 8, run_seconds=10.0)
    world.run(tmp_path, max_runs=8)
    assert world.sleeps[-1] == MAX_WAIT_S and max(world.sleeps) == MAX_WAIT_S
    long_run = World([FakeProcess(1), FakeProcess(1), FakeProcess(0)], run_seconds=3600.0)
    long_run.run(tmp_path)
    assert long_run.sleeps == [FIRST_WAIT_S, FIRST_WAIT_S]  # une heure de marche : attente courte


def test_ctrl_c_waits_for_the_bot_to_close_its_positions_then_stops(tmp_path):
    bot = FakeProcess(0, interrupt=True, slow=2)
    world = World([bot])
    assert world.run(tmp_path) == 0
    assert len(world.started) == 1 and world.sleeps == []
    assert any("attente de la fin propre du bot" in line for line in world.lines)
    assert world.lines[-1].endswith("bot arrêté (code 0). Pas de relance.")


def test_a_real_child_process_is_restarted_after_a_crash_then_stops_on_a_refusal(tmp_path):
    import sys

    child = tmp_path / "bot.py"
    child.write_text(
        "import pathlib, sys\n"
        "runs = pathlib.Path('runs.txt')\n"
        "count = int(runs.read_text()) + 1 if runs.exists() else 1\n"
        "runs.write_text(str(count))\n"
        "sys.exit(1 if count == 1 else 3)\n",
        encoding="utf-8",
    )
    lines, sleeps = [], []
    code = keep_running([sys.executable, str(child)], cwd=tmp_path, log_path=tmp_path / "g.log",
                        sleep=sleeps.append, echo=lines.append)  # fmt: skip
    assert code == 3 and (tmp_path / "runs.txt").read_text() == "2" and sleeps == [FIRST_WAIT_S]


def test_windows_scripts_stay_plain_ascii():
    # Windows PowerShell 5.1 lit un script sans BOM dans l'encodage ANSI : un accent y deviendrait illisible.
    from pathlib import Path

    for script in (Path(__file__).resolve().parents[1] / "scripts" / "windows").glob("*.ps1"):
        assert script.read_bytes().isascii(), script.name


def test_a_new_experiment_option_applies_to_the_first_launch_only(tmp_path):
    world = World([FakeProcess(1), FakeProcess(0)])
    keep_running(["python", "scripts/run_scalp.py"], cwd=tmp_path, log_path=tmp_path / "g.log",
                 first_extra=["--nouvelle-experience"], start=world.start, sleep=world.sleeps.append,
                 clock=lambda: world.time, echo=world.lines.append)  # fmt: skip
    first, second = (command for command, _ in world.started)
    assert first[-1] == "--nouvelle-experience" and "--nouvelle-experience" not in second
