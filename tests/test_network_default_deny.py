"""The network guard, tested rather than assumed.

A guard nothing exercises is indistinguishable from a guard that silently stopped
working. These tests are what make the default-deny claim checkable.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import NetworkAccessDenied, may_use_network

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_socket_construction_is_blocked() -> None:
    with pytest.raises(NetworkAccessDenied):
        socket.socket()


def test_create_connection_is_blocked() -> None:
    # A second door into the same capability. Covering only the constructor is
    # the shape of guard this one replaces.
    with pytest.raises(NetworkAccessDenied):
        socket.create_connection(("127.0.0.1", 9))


def test_the_guard_applies_without_being_requested() -> None:
    """Autouse is the property under test.

    If the fixture had to be requested, it would protect the tests whose authors
    remembered it -- which is exactly the coverage the previous per-test patches
    already had.
    """

    assert socket.socket is not socket.socket.__class__
    with pytest.raises(NetworkAccessDenied):
        socket.socket()


def test_integration_marked_tests_are_exempt(tmp_path: Path) -> None:
    """The escape hatch works, verified by running pytest rather than reasoning.

    Runs in a subprocess because the marker's effect is decided at collection,
    so it cannot be observed from inside an already-collected unmarked test.
    """

    # The real conftest is copied next to the probe: a conftest governs its own
    # directory tree, so without this the probe runs unguarded and the marked
    # half would pass for the wrong reason -- which is exactly what it did on
    # the first attempt.
    (tmp_path / "conftest.py").write_text(
        (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import socket

            import pytest


            @pytest.mark.integration
            def test_marked_test_may_construct_a_socket() -> None:
                sock = socket.socket()
                sock.close()


            def test_unmarked_test_may_not() -> None:
                with pytest.raises(AssertionError):
                    socket.socket()
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-p",
            "no:cacheprovider",
            "-m",
            "",
            "-q",
            "--rootdir",
            str(REPO_ROOT),
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_marker_is_declared_so_it_cannot_be_a_typo() -> None:
    """An unregistered marker silently does nothing under `--strict-markers`-less runs.

    Reading it out of the project configuration means the exemption and the
    deselection in `make verify` refer to the same string.
    """

    config = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "integration:" in config
    assert "-m 'not integration'" in config


def test_policy_is_one_marker_wide(request: pytest.FixtureRequest) -> None:
    """The predicate itself, so the policy is checkable without a subprocess."""

    assert may_use_network(request) is False
