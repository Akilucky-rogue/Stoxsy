"""Operator console: the parser, process helpers and the safe-by-default behaviours (no network, no processes spawned)."""
import os

import pytest

from qsde.cli import main as C


def test_every_command_parses_and_destructive_restore_needs_yes(capsys):
    p = C.build_parser()
    for argv in (["status"], ["doctor"], ["heartbeat"], ["coverage"], ["ingest", "daily", "--dry-run"], ["alerts"], ["alerts", "run"], ["backup"], ["kite", "login"],
                 ["stream", "start"], ["stream", "stop"], ["stream", "status"], ["restore", "--dump", "x.dump"]):
        assert p.parse_args(argv).fn
    assert C.main(["restore", "--dump", "x.dump"]) == 2                     # refuses without --yes
    assert "--yes" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        p.parse_args(["ingest", "bogus"])
    with pytest.raises(SystemExit):
        p.parse_args(["kite", "logout"])


def test_pid_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "RUN", tmp_path)
    assert C._read_pid("none") is None
    (tmp_path / "me.pid").write_text(str(os.getpid()))
    assert C._read_pid("me") == os.getpid()
    (tmp_path / "dead.pid").write_text("999999")
    assert C._read_pid("dead") is None
    (tmp_path / "junk.pid").write_text("not a pid")
    assert C._read_pid("junk") is None


def test_stream_start_refuses_without_a_valid_kite_session(monkeypatch, capsys):
    monkeypatch.setattr(C, "_read_pid", lambda name: None)
    monkeypatch.setattr(C, "_kite_token_hours", lambda: None)
    spawned = []
    monkeypatch.setattr(C, "_spawn", lambda *a, **k: spawned.append(a) or 1)
    assert C.main(["stream", "start"]) == 2 and not spawned
    assert "stoxsy kite login" in capsys.readouterr().out
    monkeypatch.setattr(C, "_kite_token_hours", lambda: 0.1)                # about to lapse: also refused
    assert C.main(["stream", "start"]) == 2 and not spawned


def test_stream_start_does_not_start_a_second_stream(monkeypatch, capsys):
    monkeypatch.setattr(C, "_read_pid", lambda name: 4242)
    spawned = []
    monkeypatch.setattr(C, "_spawn", lambda *a, **k: spawned.append(a) or 1)
    assert C.main(["stream", "start"]) == 0 and not spawned and "already running" in capsys.readouterr().out
