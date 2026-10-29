"""The data root must never silently fall back to the failed USB disk: environment first, then the repo .env, then the folder that holds this checkout, then C:\\Stoxsy (/srv/stoxsy on Linux)."""
from pathlib import Path

from qsde.config import paths


def test_environment_variable_wins(tmp_path):
    (tmp_path / ".env").write_text("STOXSY_ROOT=/from/dotenv\n")
    assert paths.resolve_root({"STOXSY_ROOT": "/from/env"}, tmp_path) == Path("/from/env")


def test_repo_dotenv_is_used_when_the_variable_is_unset(tmp_path):
    (tmp_path / ".env").write_text('KITE_API_KEY=x\n# STOXSY_ROOT=/commented\nSTOXSY_ROOT="/from/dotenv"\n')
    assert paths.resolve_root({}, tmp_path) == Path("/from/dotenv")


def test_a_blank_or_missing_dotenv_value_is_ignored_and_the_checkout_layout_is_used(tmp_path):
    repo = tmp_path / "stoxsy"
    repo.mkdir()
    (tmp_path / "data").mkdir()
    (repo / ".env").write_text("STOXSY_ROOT=\n")
    assert paths.resolve_root({}, repo) == tmp_path                                       # <root>/stoxsy next to <root>/data


def test_last_resort_is_the_platform_default_never_d_drive(tmp_path):
    repo = tmp_path / "stoxsy"
    repo.mkdir()
    r = paths.resolve_root({}, repo)
    assert r == paths.DEFAULT_ROOT and not str(r).upper().startswith("D:")


def test_the_module_level_root_is_not_the_old_usb_default():
    assert not str(paths.ROOT).upper().startswith("D:\\STOXSY") or paths.os.environ.get("STOXSY_ROOT", "").upper().startswith("D:")
