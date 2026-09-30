"""Checks against the real pinned reSpeaker repository. Opt in with XVFDIAG_NETWORK_TESTS=1.

Never talks to a device: CLI-contract probes use a PID that cannot match real hardware.
"""
import os
import subprocess
import sys

import pytest

import diagnose
from conftest import FAKES

pytestmark = pytest.mark.skipif(not os.environ.get("XVFDIAG_NETWORK_TESTS"), reason="set XVFDIAG_NETWORK_TESTS=1")


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    return diagnose.ensure_repo(tmp_path_factory.mktemp("cache"))


def test_pinned_firmware_hashes(upstream):
    rows = diagnose.firmware_paths(upstream, deep=True)
    assert [r[0] for r in rows] == list(diagnose.FIRMWARES)


def load_parameters(host_py):
    src = host_py.read_text(encoding="utf-8")
    ns = {}
    exec(compile(src.split("class ReSpeaker")[0], str(host_py), "exec"), ns)
    return ns["PARAMETERS"]


def test_fake_host_mirrors_upstream_parameters(upstream):
    real = load_parameters(upstream / "python_control" / "xvf_host.py")
    ns = {"__name__": "fake"}
    exec(compile((FAKES / "fake_xvf_host.py").read_text(encoding="utf-8"), "fake", "exec"), ns)
    for name, (count, typ, access) in ns["PARAMETERS"].items():
        _resid, _cmdid, r_count, r_access, r_type, _desc = real[name]
        assert (count, typ, access) == (r_count, r_type, r_access), name


def probe(upstream, *args):
    host = upstream / "python_control" / "xvf_host.py"
    return subprocess.run([sys.executable, str(host), "--pid", "0xFFFF", *args], capture_output=True, text=True, timeout=60)


def test_upstream_cli_contract(upstream):
    positional = probe(upstream, "AUDIO_MGR_OP_L", "8", "0")
    assert positional.returncode == 2 and "unrecognized arguments" in positional.stderr
    flagged = probe(upstream, "AUDIO_MGR_OP_L", "--values", "8", "0")
    assert flagged.returncode != 2 and "unrecognized arguments" not in flagged.stderr
