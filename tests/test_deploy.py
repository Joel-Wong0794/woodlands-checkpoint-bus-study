"""deploy/update.sh against throwaway git repos: a failed step after the merge must be retried on the next run."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "update.sh"
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, msg):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/deploy")


def test_failed_pip_install_is_retried(tmp_path):
    origin, dev, home = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "home"
    vm = home / "woodlands-checkpoint-bus-study"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    commit(dev, "requirements.txt", "requests\n", "first")
    git(tmp_path, "clone", "-q", "-b", "deploy", str(origin), str(vm))

    # fake pip: fails while the file `pip_fails` exists, else records that it ran
    pip = vm / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text('#!/bin/sh\n[ -e "$HOME/pip_fails" ] && exit 1\necho ran >> "$HOME/pip_ran"\n')
    pip.chmod(0o755)
    env = {**os.environ, "HOME": home.as_posix()}
    run = lambda: subprocess.run([SH, SCRIPT.as_posix()], env=env, capture_output=True, text=True)

    commit(dev, "requirements.txt", "requests\npandas\n", "new package")
    (home / "pip_fails").touch()
    assert run().returncode != 0  # pip failed after the merge
    assert git(vm, "rev-parse", "HEAD") == git(dev, "rev-parse", "HEAD")  # code is already new
    assert not (home / "pip_ran").exists()

    (home / "pip_fails").unlink()
    assert run().returncode == 0  # next run: retries the install although HEAD didn't move
    assert (home / "pip_ran").read_text() == "ran\n"
    assert run().returncode == 0  # and then nothing more to do
    assert (home / "pip_ran").read_text() == "ran\n"
