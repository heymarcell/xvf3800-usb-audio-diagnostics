"""Read-only checks against a connected XVF3800. Opt in with XVFDIAG_HARDWARE_TESTS=1.

Only issues read commands and opens no audio stream: no flashing, no configuration writes,
no recording. Requires network access on first use to fetch the pinned host script.
"""
import os

import pytest

import diagnose
from conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(not os.environ.get("XVFDIAG_HARDWARE_TESTS"), reason="set XVFDIAG_HARDWARE_TESTS=1")


@pytest.fixture(scope="module")
def host_cmd():
    cache = REPO_ROOT / diagnose.CACHE_DIR_NAME
    cache.mkdir(exist_ok=True)
    return diagnose.locate_xvf_host(diagnose.ensure_repo(cache))


def test_version_is_readable(host_cmd):
    assert diagnose.VERSION_RE.search(diagnose.wait_for_xvf(host_cmd, timeout_s=10))


def test_snapshot_parses_every_value(host_cmd):
    snap = diagnose.snapshot_xvf(host_cmd)
    for cmd, line in snap.items():
        assert line.startswith(cmd + ": ["), (cmd, line)


def test_audio_input_matches_firmware_rate():
    import sounddevice as sd
    idx = diagnose.find_input_device(sd)
    rate = int(sd.query_devices(idx)["default_samplerate"])
    assert rate in (16000, 48000)
    sd.check_input_settings(device=idx, channels=2, dtype="float32", samplerate=rate)
