"""Tests for ``bootstrap.py`` — the one-command dev environment setup.

The interesting half of that script is platform-specific, and CI only ever runs
Linux. So rather than guard the Windows branches behind ``skipif`` (which would
mean nobody ever executes them), every platform-dependent function reads the
``IS_WINDOWS`` module global at call time and these tests flip it. The Windows
error-code paths below therefore genuinely run — on any machine.

Permission failures are simulated by patching the syscall to raise, not by
chmod'ing a real file: chmod does not stop root on POSIX and does not mean the
same thing on Windows, so a chmod-based test would silently pass by not testing
anything.
"""

from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path

import pytest

import bootstrap

# ── repo facts ───────────────────────────────────────────────────────────────


def test_required_python_matches_the_lockfile() -> None:
    version = bootstrap.required_python()
    assert version.count(".") == 1
    major, minor = version.split(".")
    assert major.isdigit() and minor.isdigit()
    assert (int(major), int(minor)) >= (3, 13)


def test_required_python_falls_back_when_nothing_is_readable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PROJECTS", tmp_path / "projects")
    assert bootstrap.required_python() == bootstrap.FALLBACK_WORKSPACE_PYTHON


def test_python_projects_are_discovered() -> None:
    names = [p.name for p in bootstrap.python_projects()]
    assert "23-dynamodb-core" in names
    assert all((p / "pyproject.toml").is_file() for p in bootstrap.python_projects())


def test_projects_needing_docker_is_a_subset() -> None:
    docker = set(bootstrap.projects_needing_docker())
    everything = {p.name for p in bootstrap.python_projects()}
    assert docker <= everything


# ── platform layout ──────────────────────────────────────────────────────────


def test_venv_python_uses_scripts_on_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(bootstrap, "VENV", tmp_path / ".venv")
    assert bootstrap.venv_python().parts[-2:] == ("Scripts", "python.exe")


def test_venv_python_uses_bin_on_posix(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", False)
    monkeypatch.setattr(bootstrap, "VENV", tmp_path / ".venv")
    assert bootstrap.venv_python().parts[-2:] == ("bin", "python")


def test_activate_hint_is_platform_correct(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    assert "Scripts" in bootstrap.activate_hint()
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", False)
    assert bootstrap.activate_hint().startswith("source ")


def test_uv_candidates_are_exe_suffixed_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\dev\AppData\Local")
    candidates = bootstrap.uv_candidate_paths()
    assert candidates, "expected at least one candidate location"
    assert all(str(p).endswith(".exe") for p in candidates)
    assert any("AppData" in str(p) for p in candidates)


def test_uv_candidates_have_no_exe_on_posix(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", False)
    candidates = bootstrap.uv_candidate_paths()
    assert candidates
    assert not any(str(p).endswith(".exe") for p in candidates)


def test_uv_candidates_survive_missing_localappdata(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert bootstrap.uv_candidate_paths()  # must not raise or come back empty


# ── permission remedies: the Windows error codes ─────────────────────────────


def _oserror(*, winerror: int | None = None, err: int | None = None) -> OSError:
    exc = OSError(err or 0, "simulated")
    if winerror is not None:
        # OSError.winerror is read-only on Windows builds and absent elsewhere,
        # so set it as an attribute the same way the tested code reads it.
        try:
            object.__setattr__(exc, "winerror", winerror)
        except AttributeError:  # pragma: no cover - CPython allows this
            exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    return monkeypatch


def test_sharing_violation_blames_a_running_process(on_windows) -> None:
    remedies = bootstrap.permission_remedies(
        _oserror(winerror=bootstrap.WIN_SHARING_VIOLATION), Path(r"C:\src\gauntlet\.venv")
    )
    joined = " ".join(remedies).lower()
    assert "holding" in joined
    assert "--recreate" in joined


def test_lock_violation_gets_the_same_advice(on_windows) -> None:
    remedies = bootstrap.permission_remedies(
        _oserror(winerror=bootstrap.WIN_LOCK_VIOLATION), Path(r"C:\src\gauntlet\.venv")
    )
    assert any("recreate" in r for r in remedies)


def test_access_denied_warns_against_running_elevated(on_windows) -> None:
    remedies = bootstrap.permission_remedies(
        _oserror(winerror=bootstrap.WIN_ACCESS_DENIED), Path(r"C:\src\gauntlet")
    )
    joined = " ".join(remedies).lower()
    assert "administrator" in joined
    assert "antivirus" in joined


def test_privilege_not_held_suggests_copy_link_mode(on_windows) -> None:
    remedies = bootstrap.permission_remedies(
        _oserror(winerror=bootstrap.WIN_PRIVILEGE_NOT_HELD), Path(r"C:\src\gauntlet\.venv")
    )
    joined = " ".join(remedies)
    assert "UV_LINK_MODE=copy" in joined
    assert "Developer Mode" in joined


def test_long_path_suggests_the_registry_fix(on_windows) -> None:
    deep = Path("C:/" + "nested/" * 40 + "file.txt")
    remedies = bootstrap.permission_remedies(_oserror(winerror=bootstrap.WIN_PATH_TOO_LONG), deep)
    joined = " ".join(remedies)
    assert "LongPathsEnabled" in joined


def test_unknown_windows_error_still_says_something_useful(on_windows) -> None:
    remedies = bootstrap.permission_remedies(_oserror(winerror=1), Path(r"C:\x"))
    assert remedies and all(isinstance(r, str) for r in remedies)


def test_windows_remedies_never_suggest_chmod(on_windows) -> None:
    """chmod/chown advice on Windows is noise — it must not leak across."""
    for code in (
        bootstrap.WIN_ACCESS_DENIED,
        bootstrap.WIN_SHARING_VIOLATION,
        bootstrap.WIN_PRIVILEGE_NOT_HELD,
        bootstrap.WIN_PATH_TOO_LONG,
        1,
    ):
        joined = " ".join(bootstrap.permission_remedies(_oserror(winerror=code), Path(r"C:\x")))
        assert "chmod" not in joined
        assert "sudo" not in joined


# ── permission remedies: POSIX ───────────────────────────────────────────────


@pytest.fixture
def on_posix(monkeypatch):
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", False)
    return monkeypatch


def test_readonly_filesystem_is_called_out(on_posix) -> None:
    remedies = bootstrap.permission_remedies(_oserror(err=errno.EROFS), Path("/mnt/ro"))
    assert "read-only filesystem" in " ".join(remedies)


def test_disk_full_is_called_out(on_posix) -> None:
    remedies = bootstrap.permission_remedies(_oserror(err=errno.ENOSPC), Path("/home/dev"))
    assert "disk is full" in " ".join(remedies).lower()


def test_root_owned_venv_suggests_sudo_removal(on_posix, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "owner_description", lambda _p: "root:root")
    monkeypatch.setattr(bootstrap.os, "getuid", lambda: 1000, raising=False)
    remedies = bootstrap.permission_remedies(_oserror(err=errno.EACCES), Path("/home/dev/.venv"))
    joined = " ".join(remedies)
    assert "sudo rm -rf" in joined
    assert "chmod -R u+rwX" in joined


def test_posix_remedies_never_mention_windows_things(on_posix, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "owner_description", lambda _p: "")
    joined = " ".join(bootstrap.permission_remedies(_oserror(err=errno.EACCES), Path("/x")))
    assert "PowerShell" not in joined
    assert "Administrator" not in joined


# ── uv sync failure classification ───────────────────────────────────────────


@pytest.mark.parametrize(
    "output",
    [
        "error: Failed to hardlink files; falling back to full copy",
        "Caused by: Access is denied. (os error 5)",
        "failed to create file (os error 1314)",
        "A required privilege is not held: sharing violation",
    ],
)
def test_link_failures_are_recognised(output: str) -> None:
    assert bootstrap.Bootstrap.is_link_failure(output)


@pytest.mark.parametrize(
    "output",
    [
        "error: The lockfile at `uv.lock` needs to be updated",
        "Resolution failed: no matching distribution",
        "",
    ],
)
def test_non_link_failures_are_not_misread(output: str) -> None:
    assert not bootstrap.Bootstrap.is_link_failure(output)


def test_stale_lock_is_reported_as_a_repo_problem() -> None:
    remedies = bootstrap.Bootstrap.sync_remedies("error: the lockfile needs to be updated")
    assert any("uv lock" in r for r in remedies)


def test_missing_interpreter_points_at_uv_python_install() -> None:
    remedies = bootstrap.Bootstrap.sync_remedies("error: No interpreter found for Python 3.13")
    assert any("uv python install" in r for r in remedies)


def test_tls_failure_suggests_a_ca_bundle() -> None:
    remedies = bootstrap.Bootstrap.sync_remedies("error: invalid peer certificate: UnknownIssuer")
    joined = " ".join(remedies)
    assert "SSL_CERT_FILE" in joined or "--native-tls" in joined


def test_disk_full_during_sync_is_recognised() -> None:
    remedies = bootstrap.Bootstrap.sync_remedies("No space left on device (os error 28)")
    assert any("disk is full" in r.lower() for r in remedies)


def test_unclassified_sync_failure_still_offers_a_next_step() -> None:
    assert bootstrap.Bootstrap.sync_remedies("something nobody predicted")


# ── the command runner never raises ──────────────────────────────────────────


def test_missing_binary_becomes_exit_127() -> None:
    result = bootstrap.run(["definitely-not-a-real-binary-xyz"])
    assert result.code == 127
    assert not result.ok
    assert "not found" in result.output


def test_timeout_becomes_exit_124(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=1)

    monkeypatch.setattr(bootstrap.subprocess, "run", _boom)
    result = bootstrap.run(["slow"], timeout=1)
    assert result.code == 124
    assert "timed out" in result.output


def test_permission_error_becomes_exit_126(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(bootstrap.subprocess, "run", _boom)
    assert bootstrap.run(["blocked"]).code == 126


def test_successful_command_captures_output() -> None:
    result = bootstrap.run([sys.executable, "-c", "print('hello')"])
    assert result.ok
    assert "hello" in result.output


def test_tool_version_returns_none_for_missing_tools() -> None:
    assert bootstrap.tool_version("definitely-not-a-real-binary-xyz") is None


# ── file operations under hostile permissions ────────────────────────────────


def test_copy_preserving_retries_after_clearing_readonly(monkeypatch, tmp_path) -> None:
    src = tmp_path / ".env.example"
    src.write_text("KEY=value\n", encoding="utf-8")
    dst = tmp_path / ".env"
    dst.write_text("stale\n", encoding="utf-8")

    calls = {"copy": 0, "cleared": 0}
    real_copyfile = bootstrap.shutil.copyfile

    def flaky(a, b):
        calls["copy"] += 1
        if calls["copy"] == 1:
            raise PermissionError(13, "read-only")
        return real_copyfile(a, b)

    monkeypatch.setattr(bootstrap.shutil, "copyfile", flaky)
    monkeypatch.setattr(
        bootstrap, "clear_readonly", lambda _p: calls.__setitem__("cleared", 1) or True
    )

    bootstrap.copy_preserving(src, dst)
    assert calls["copy"] == 2
    assert calls["cleared"] == 1
    assert dst.read_text(encoding="utf-8") == "KEY=value\n"


def test_copy_preserving_raises_setup_error_with_remedies(monkeypatch, tmp_path) -> None:
    src = tmp_path / "a"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "b"

    def always_denied(_a, _b):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(bootstrap.shutil, "copyfile", always_denied)
    monkeypatch.setattr(bootstrap, "clear_readonly", lambda _p: False)

    with pytest.raises(bootstrap.SetupError) as caught:
        bootstrap.copy_preserving(src, dst)
    assert caught.value.remedies


def test_probe_writable_raises_setup_error(monkeypatch, tmp_path) -> None:
    def denied(*_args, **_kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(bootstrap.Path, "write_text", denied)
    with pytest.raises(bootstrap.SetupError) as caught:
        bootstrap.probe_writable(tmp_path)
    assert "No write permission" in caught.value.message


def test_probe_writable_cleans_up_after_itself(tmp_path) -> None:
    bootstrap.probe_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_rmtree_robust_removes_readonly_trees(tmp_path) -> None:
    victim = tmp_path / "venv"
    (victim / "nested").mkdir(parents=True)
    locked = victim / "nested" / "pyvenv.cfg"
    locked.write_text("home = /usr\n", encoding="utf-8")
    locked.chmod(0o444)
    bootstrap.rmtree_robust(victim)
    assert not victim.exists()


def test_rmtree_robust_is_a_noop_for_missing_paths(tmp_path) -> None:
    bootstrap.rmtree_robust(tmp_path / "never-existed")


def test_rmtree_robust_reports_undeletable_trees(monkeypatch, tmp_path) -> None:
    victim = tmp_path / "venv"
    victim.mkdir()

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(bootstrap.shutil, "rmtree", denied)
    with pytest.raises(bootstrap.SetupError):
        bootstrap.rmtree_robust(victim)


# ── console ──────────────────────────────────────────────────────────────────


def test_console_falls_back_to_ascii_glyphs(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "_console_handles_unicode", lambda: False)
    console = bootstrap.Console()
    assert console.g("ok") == "[ok]"
    assert all(ch in "[]!x><>-* " or ch.isalnum() for ch in "".join(console.glyphs.values()))


def test_console_quiet_still_emits_the_summary(capsys) -> None:
    console = bootstrap.Console(quiet=True)
    console.say("suppressed")
    console.emit("shown")
    captured = capsys.readouterr().out
    assert "suppressed" not in captured
    assert "shown" in captured


def test_console_records_warnings(capsys) -> None:
    console = bootstrap.Console(quiet=True)
    console.warn("careful")
    capsys.readouterr()
    assert console.warnings == ["careful"]


def test_shlex_join_quotes_paths_with_spaces() -> None:
    joined = bootstrap.shlex_join(["uv", "sync", r"C:\Program Files\x"])
    assert '"C:\\Program Files\\x"' in joined


def test_prepare_windows_console_is_a_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", False)
    bootstrap._prepare_windows_console()  # must not raise


# ── check mode writes nothing ────────────────────────────────────────────────


def test_check_mode_does_not_create_env_files(monkeypatch, tmp_path, capsys) -> None:
    project = tmp_path / "99-fake"
    project.mkdir()
    (project / ".env.example").write_text("PORT=1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "python_projects", lambda: [project])

    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True), check_only=True)
    setup.seed_env_files()
    capsys.readouterr()

    assert not (project / ".env").exists()
    assert setup.results[-1].status == "ok"


def test_apply_mode_creates_env_files(monkeypatch, tmp_path, capsys) -> None:
    project = tmp_path / "99-fake"
    project.mkdir()
    (project / ".env.example").write_text("PORT=1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "python_projects", lambda: [project])

    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True), check_only=False)
    setup.seed_env_files()
    capsys.readouterr()

    assert (project / ".env").read_text(encoding="utf-8") == "PORT=1\n"


def test_existing_env_is_never_overwritten(monkeypatch, tmp_path, capsys) -> None:
    project = tmp_path / "99-fake"
    project.mkdir()
    (project / ".env.example").write_text("PORT=1\n", encoding="utf-8")
    (project / ".env").write_text("PORT=9999  # mine\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "python_projects", lambda: [project])

    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True), check_only=False)
    setup.seed_env_files()
    capsys.readouterr()

    assert "9999" in (project / ".env").read_text(encoding="utf-8")


# ── preflight guards ─────────────────────────────────────────────────────────


def test_preflight_rejects_a_non_repo_directory(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True))
    with pytest.raises(bootstrap.SetupError) as caught:
        setup.preflight()
    capsys.readouterr()
    assert "repo root" in caught.value.message


def test_synced_folder_is_warned_about(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "ROOT", Path("/Users/dev/OneDrive/backend-gauntlet"))
    monkeypatch.setattr(bootstrap, "VENV", Path("/nonexistent"))
    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True))
    warnings = " ".join(setup.environment_warnings())
    capsys.readouterr()
    assert "cloud-synced" in warnings


def test_deep_windows_path_is_warned_about(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(bootstrap, "ROOT", Path("C:/" + "deeply-nested-folder/" * 12 + "gauntlet"))
    monkeypatch.setattr(bootstrap, "VENV", Path("C:/nonexistent"))
    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True))
    warnings = " ".join(setup.environment_warnings())
    capsys.readouterr()
    assert "260-char" in warnings


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_help_exits_zero() -> None:
    result = bootstrap.run([sys.executable, str(bootstrap.ROOT / "bootstrap.py"), "--help"])
    assert result.ok
    assert "--recreate" in result.output


def test_check_and_recreate_are_mutually_exclusive(capsys) -> None:
    assert bootstrap.main(["--check", "--recreate"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_unknown_flag_exits_two() -> None:
    result = bootstrap.run([sys.executable, str(bootstrap.ROOT / "bootstrap.py"), "--nope"])
    assert result.code == 2


def test_rmtree_robust_clears_readonly_then_retries(monkeypatch, tmp_path) -> None:
    """Drive the retry handler directly — chmod does not stop root on POSIX,
    so a plain read-only file would delete on the first try and prove nothing."""
    victim = tmp_path / "venv"
    victim.mkdir()
    stubborn = victim / "locked"
    stubborn.write_text("x", encoding="utf-8")

    cleared: list[Path] = []
    monkeypatch.setattr(bootstrap, "clear_readonly", lambda p: cleared.append(Path(p)) or True)

    def rmtree_that_needs_help(_path, **kwargs):
        handler = kwargs.get("onexc") or kwargs.get("onerror")
        assert handler is not None, "rmtree_robust must always pass a retry handler"
        removed: list[Path] = []
        if kwargs.get("onexc"):
            handler(lambda t: removed.append(Path(t)), str(stubborn), PermissionError(13, "ro"))
        else:
            handler(
                lambda t: removed.append(Path(t)),
                str(stubborn),
                (PermissionError, PermissionError(13, "ro"), None),
            )
        assert removed == [stubborn]

    monkeypatch.setattr(bootstrap.shutil, "rmtree", rmtree_that_needs_help)
    bootstrap.rmtree_robust(victim)
    assert cleared == [stubborn]


def test_clear_readonly_makes_a_file_writable(tmp_path) -> None:
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)
    assert bootstrap.clear_readonly(target) is True
    assert target.stat().st_mode & 0o200
    assert bootstrap.clear_readonly(target) is False  # already writable


def test_clear_readonly_is_safe_on_missing_files(tmp_path) -> None:
    assert bootstrap.clear_readonly(tmp_path / "nope") is False


def test_unc_path_is_warned_about(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(bootstrap, "ROOT", Path(r"\\fileserver\home\dev\backend-gauntlet"))
    monkeypatch.setattr(bootstrap, "VENV", Path(r"\\fileserver\home\dev\backend-gauntlet\.venv"))
    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True))
    warnings = " ".join(setup.environment_warnings())
    capsys.readouterr()
    assert "UNC/network path" in warnings


def test_install_uv_needs_a_powershell_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    with pytest.raises(bootstrap.SetupError) as caught:
        bootstrap.install_uv(bootstrap.Console(quiet=True))
    assert "winget" in " ".join(caught.value.remedies)


def test_install_uv_prefers_pwsh_when_powershell_is_absent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: r"C:\pwsh\pwsh.exe" if name == "pwsh" else None
    )
    seen: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        seen["argv"] = argv
        return bootstrap.CommandResult(argv, 0, "")

    monkeypatch.setattr(bootstrap, "run", fake_run)
    monkeypatch.setattr(bootstrap, "find_uv", lambda: Path(r"C:\uv\uv.exe"))

    bootstrap.install_uv(bootstrap.Console(quiet=True))
    capsys.readouterr()
    assert seen["argv"][0].endswith("pwsh.exe")
    assert "-ExecutionPolicy" in seen["argv"] and "Bypass" in seen["argv"]


def test_install_uv_needs_curl_or_wget_on_posix(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "IS_WINDOWS", False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    with pytest.raises(bootstrap.SetupError) as caught:
        bootstrap.install_uv(bootstrap.Console(quiet=True))
    assert "curl nor wget" in caught.value.message


def test_failed_run_headlines_incomplete_and_hides_next(capsys) -> None:
    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True))
    setup.record("preflight", "ok", "fine")
    setup.mark_failed("uv is not installed")
    setup.summary(0.1)
    out = capsys.readouterr().out
    assert "setup incomplete" in out
    assert "Next" not in out
    assert "re-run" in out


def test_successful_run_prints_next_steps(capsys) -> None:
    setup = bootstrap.Bootstrap(console=bootstrap.Console(quiet=True))
    setup.record("venv", "ok", "Python 3.13.7")
    setup.summary(0.1)
    out = capsys.readouterr().out
    assert "setup complete" in out
    assert "Next" in out


def test_warnings_downgrade_a_clean_headline(capsys) -> None:
    console = bootstrap.Console(quiet=True)
    setup = bootstrap.Bootstrap(console=console)
    setup.record("venv", "ok", "Python 3.13.7")
    console.warn("docker daemon is not running")
    capsys.readouterr()
    setup.summary(0.1)
    out = capsys.readouterr().out
    assert "finished with warnings" in out
