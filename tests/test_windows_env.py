import subprocess

from goldbot.broker.checks import Level
from goldbot.broker.windows_env import TerminalProcess, check_windows_environment, running_terminals

AXI = r"C:\Program Files\Axi MetaTrader 5\terminal64.exe"


def _check(terminal_path=AXI, *, processes=(), bits=64, admin=False, exists=True):
    return check_windows_environment(
        terminal_path,
        python_version="3.13.7",
        python_bits=bits,
        admin=admin,
        processes=None if processes is None else list(processes),
        file_exists=lambda path: exists,
    )


def _errors(findings):
    return " | ".join(f.message for f in findings if f.level is Level.ERROR)


def test_parses_running_terminals():
    output = f"4242|{AXI}|\"{AXI}\" /portable\n\n5151||\n"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    processes = running_terminals(run=fake_run)
    assert processes == [TerminalProcess(4242, AXI, f'"{AXI}" /portable'), TerminalProcess(5151, "", "")]
    assert processes[0].portable and not processes[1].portable


def test_query_failure_returns_none():
    def failing_run(*args, **kwargs):
        raise OSError("powershell introuvable")

    assert running_terminals(run=failing_run) is None


def test_matching_terminal_is_ok():
    findings = _check(processes=[TerminalProcess(1, AXI.upper(), f'"{AXI}"')])
    assert _errors(findings) == ""
    assert any("correspond au terminal ouvert" in f.message for f in findings)


def test_path_mismatch_is_reported():
    other = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    assert "ne correspond à aucun terminal" in _errors(_check(processes=[TerminalProcess(1, other, "")]))


def test_hidden_path_means_different_rights():
    assert "administrateur" in _errors(_check(processes=[TerminalProcess(7, "", "")]))


def test_portable_terminal_is_reported():
    assert "portable: true" in _errors(_check(processes=[TerminalProcess(1, AXI, f'"{AXI}" /portable')]))


def test_no_terminal_running():
    assert "Aucun terminal MT5" in _errors(_check(processes=[]))


def test_missing_file_and_store_version():
    store = r"C:\Program Files\WindowsApps\MetaQuotes\terminal64.exe"
    errors = _errors(_check(store, processes=None, exists=False))
    assert "Microsoft Store" in errors and "aucun fichier" in errors


def test_32_bit_python_is_an_error():
    assert "64 bits" in _errors(_check(bits=32, processes=None))
