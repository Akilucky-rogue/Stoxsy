"""Process checks used by `stoxsy stream status|stop`: a live process is seen as alive, a dead pid is not, and nothing is signalled to find out."""
import os
import subprocess
import sys

from qsde.cli import main as cli


def test_alive_sees_a_running_process_and_not_a_dead_one():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert cli._alive(p.pid) and cli._alive(os.getpid())
    finally:
        p.kill()
        p.wait()
    assert not cli._alive(p.pid)


def test_no_unsafe_os_kill_probe_is_left_in_the_cli():
    import inspect

    assert "os.kill" not in inspect.getsource(cli)
