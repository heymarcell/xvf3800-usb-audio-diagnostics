from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import scipy.signal as sig
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(REPO_ROOT))

import diagnose  # noqa: E402

CANONICAL_VOICE = REPO_ROOT / "assets" / "voice" / "xvf3800-test-vo.wav"


@pytest.fixture(scope="session")
def rt():
    return SimpleNamespace(np=np, sig=sig, sf=sf)


@pytest.fixture
def short_voice(tmp_path):
    """1.5 s speech-like stand-in so stimulus-heavy tests stay fast."""
    fs = 48000
    t = np.arange(int(1.5 * fs)) / fs
    x = 0.3 * np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
    x += 0.02 * np.random.default_rng(1).standard_normal(len(t))
    p = tmp_path / "short_voice.wav"
    sf.write(str(p), x, fs, subtype="PCM_16")
    return p


def fw_rate(label: str) -> int:
    return 48000 if "48k" in label else 16000


def read_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def xvf_state(tmp_path, monkeypatch):
    state = tmp_path / "xvf-state.json"
    state.write_text(json.dumps({"firmware": "v2.1.1_48k2ch", "regs": {}, "present": True}), encoding="utf-8")
    monkeypatch.setenv("XVFDIAG_FAKE_STATE", str(state))
    return state


@pytest.fixture
def host_cmd(xvf_state):
    return [sys.executable, str(FAKES / "fake_xvf_host.py")]


@pytest.fixture
def fake_dfu(tmp_path, xvf_state):
    script = FAKES / "fake_dfu_util.py"
    if os.name == "nt":
        exe = tmp_path / "dfu-util.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "dfu-util"
        exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
        exe.chmod(0o755)
    return str(exe)


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Firmware tree whose images contain their own label, with FIRMWARES re-pinned to match."""
    repo = tmp_path / "respeaker-repo"
    table = {}
    for label, meta in diagnose.FIRMWARES.items():
        p = repo / meta["rel"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(label + "\n", encoding="utf-8")
        table[label] = {**meta, "sha256": diagnose.sha256_file(p)}
    monkeypatch.setattr(diagnose, "FIRMWARES", table)
    return repo


class FakeStream:
    LATENCY_FRAMES = 37

    def __init__(self, sd: "FakeSoundDevice", rate: int, channels: int, callback):
        self.sd, self.rate, self.channels, self.callback = sd, rate, channels, callback

    def start(self):
        self.t0 = self.sd.clock.t

    def stop(self):
        t1 = self.sd.clock.t
        n = int(round((t1 - self.t0) * self.rate))
        y = np.random.default_rng(len(self.sd.plays)).normal(0, 1e-3, (n, self.channels))
        for p in self.sd.plays:
            if not (self.t0 <= p["start"] <= t1):
                continue
            x = p["data"]
            if p["rate"] != self.rate:
                g = math.gcd(p["rate"], self.rate)
                x = sig.resample_poly(x, self.rate // g, p["rate"] // g)
            a = int(round((p["start"] - self.t0) * self.rate)) + self.LATENCY_FRAMES
            m = max(0, min(len(x), n - a))
            y[a:a + m, 0] += 0.5 * x[:m]
            y[a:a + m, 1] += 0.45 * x[:m]
        if self.sd.click_bug_active():
            steps = np.zeros(n)
            at = np.arange(511, n - 1, 512 * 40)
            steps[at] = np.where(np.arange(len(at)) % 2 == 0, 1.0, -1.0)
            y[:, 1] += 0.4 * np.cumsum(steps)
        y = np.clip(y, -1.0, 0.99997)
        for i in range(0, n, 480):
            block = y[i:i + 480].astype(np.float32)
            self.callback(block, len(block), None, None)

    def close(self):
        pass


class FakeSoundDevice:
    """PortAudio stand-in. Like the real library it snapshots devices at initialization, so
    a runner that does not re-initialize after a reflash sees a stale rate, and the XVF moves
    to a different index on every re-initialization."""

    XVF = "reSpeaker XVF3800 4-Mic Array"
    SPEAKER = "MacBook Pro Speakers"

    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path
        self.clock = SimpleNamespace(t=0.0)
        self.inits = 0
        self.terminates = 0
        self.plays: list[dict] = []
        self.default = SimpleNamespace(device=[-1, -1])
        self._snapshot()

    def state(self) -> dict:
        if self.state_path and self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"firmware": "v2.1.1_48k2ch", "regs": {}}

    def click_bug_active(self) -> bool:
        # Emulated defect: v2.1.1 48 kHz processed/ASR outputs with upsampling enabled.
        st = self.state()
        regs = st.get("regs", {})
        left = regs.get("AUDIO_MGR_OP_L", [8, 0])
        up = regs.get("AUDIO_MGR_OP_UPSAMPLE", [1, 1])
        return st["firmware"] == "v2.1.1_48k2ch" and left[0] in (6, 7, 8) and up == [1, 1]

    def _snapshot(self):
        rate = float(fw_rate(self.state()["firmware"]))
        others = [
            {"name": "Display Audio", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
            {"name": "MacBook Pro Microphone", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
            {"name": self.SPEAKER, "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
        ]
        xvf = {"name": self.XVF, "hostapi": 0, "max_input_channels": 2, "max_output_channels": 2, "default_samplerate": rate}
        full = others + [xvf]
        k = self.inits % len(full)
        self.devices = full[k:] + full[:k]
        names = [d["name"] for d in self.devices]
        self.default.device = [names.index(self.XVF), names.index(self.SPEAKER)]

    def _terminate(self):
        self.terminates += 1

    def _initialize(self):
        self.inits += 1
        self._snapshot()

    def query_devices(self, device=None):
        return self.devices if device is None else self.devices[device]

    def query_hostapis(self):
        return [{"name": "Fake Audio"}]

    def check_input_settings(self, device, channels, dtype, samplerate):
        d = self.devices[device]
        if d["name"] != self.XVF or d["max_input_channels"] < channels:
            raise RuntimeError(f"Invalid input device {device}")
        if int(d["default_samplerate"]) != samplerate or fw_rate(self.state()["firmware"]) != samplerate:
            raise RuntimeError(f"Invalid sample rate {samplerate} for {d['name']}")

    def check_output_settings(self, device, channels, dtype, samplerate):
        if self.devices[device]["max_output_channels"] < channels:
            raise RuntimeError(f"Invalid output device {device}")

    def InputStream(self, device, channels, samplerate, dtype, blocksize, callback):
        self.check_input_settings(device, channels, dtype, samplerate)
        return FakeStream(self, samplerate, channels, callback)

    def play(self, data, samplerate, device, blocking):
        assert blocking
        self.check_output_settings(device, 1, "float32", samplerate)
        self.plays.append({"device": self.devices[device]["name"], "rate": int(samplerate),
                           "start": self.clock.t, "data": np.asarray(data, dtype=np.float64)})
        self.clock.t += len(data) / samplerate


@pytest.fixture
def fake_sd(xvf_state, monkeypatch):
    sd = FakeSoundDevice(xvf_state)
    fake_time = SimpleNamespace(
        sleep=lambda s: setattr(sd.clock, "t", sd.clock.t + s),
        monotonic=__import__("time").monotonic,
        strftime=__import__("time").strftime,
    )
    monkeypatch.setattr(diagnose, "time", fake_time)
    return sd
