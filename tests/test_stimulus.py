"""Deterministic technical stimulus and the fixed-gain voice insertion."""
import json

import numpy as np
import pytest

import diagnose
from conftest import CANONICAL_VOICE


@pytest.fixture(scope="module")
def canonical_stimulus(tmp_path_factory, rt):
    out = tmp_path_factory.mktemp("stim")
    before = diagnose.sha256_file(CANONICAL_VOICE)
    wav, manifest = diagnose.generate_stimulus(CANONICAL_VOICE, out, rt.np, rt.sig, rt.sf)
    assert diagnose.sha256_file(CANONICAL_VOICE) == before, "runner must never modify the fixture"
    return wav, manifest


def test_stimulus_is_byte_for_byte_deterministic(canonical_stimulus, tmp_path, rt):
    wav, _ = canonical_stimulus
    again, _ = diagnose.generate_stimulus(CANONICAL_VOICE, tmp_path, rt.np, rt.sig, rt.sf)
    assert diagnose.sha256_file(again) == diagnose.sha256_file(wav)
    assert (tmp_path / "stimulus_manifest.json").read_bytes() == (wav.parent / "stimulus_manifest.json").read_bytes()


def test_manifest_is_contiguous_and_matches_audio(canonical_stimulus, rt):
    wav, manifest = canonical_stimulus
    data, fs = rt.sf.read(str(wav))
    assert fs == 48000 and data.ndim == 1
    assert manifest[0]["start_s"] == 0
    for a, b in zip(manifest, manifest[1:]):
        assert a["end_s"] == pytest.approx(b["start_s"])
    assert manifest[-1]["end_s"] == pytest.approx(len(data) / fs)
    names = [s["name"] for s in manifest]
    assert len(names) == len(set(names))
    assert names[:2] == ["silence_start", "sync_chirp"]
    voice = next(s for s in manifest if s["name"] == "english_voice")
    assert voice["duration_s"] == pytest.approx(63.04)
    assert json.loads((wav.parent / "stimulus_manifest.json").read_text())["segments"] == manifest
    assert np.max(np.abs(data)) <= 0.95 + 1 / 32768


def test_voice_inserted_at_fixed_minus_6_db(canonical_stimulus, rt):
    wav, manifest = canonical_stimulus
    stim, fs = rt.sf.read(str(wav), dtype="float64")
    src, _ = rt.sf.read(str(CANONICAL_VOICE), dtype="float64")
    seg = next(s for s in manifest if s["name"] == "english_voice")
    a = int(round(seg["start_s"] * fs))
    out = stim[a:a + len(src)]
    fade = int(0.010 * fs)
    gain = 10 ** (diagnose.VOICE_PLAYBACK_GAIN_DB / 20)
    body = slice(fade, len(src) - fade)
    np.testing.assert_allclose(out[body], src[body] * gain, atol=1.5 / 32768)
    # The only other change is the documented 10 ms edge fade.
    assert abs(out[0]) <= 1 / 32768 and abs(out[-1]) <= 1 / 32768


def test_placeholder_when_voice_missing(tmp_path, rt):
    _, manifest = diagnose.generate_stimulus(None, tmp_path, rt.np, rt.sig, rt.sf)
    ph = next(s for s in manifest if s["name"] == "english_voice_placeholder")
    assert ph["duration_s"] == pytest.approx(3.0)


def test_load_voice_resamples_and_downmixes(tmp_path, rt):
    fs_in = 44100
    t = np.arange(fs_in) / fs_in
    tone = 0.5 * np.sin(2 * np.pi * 1000 * t)
    p = tmp_path / "v.wav"
    rt.sf.write(str(p), np.column_stack([tone, tone]), fs_in, subtype="FLOAT")
    y = diagnose.load_voice(p, 48000, rt.np, rt.sig, rt.sf)
    assert abs(len(y) - 48000) <= 1
    mid = y[10000:38000]
    assert np.max(np.abs(mid)) == pytest.approx(0.5 * 10 ** (-6 / 20), rel=0.01)


def test_load_voice_limits_hot_input(tmp_path, rt):
    p = tmp_path / "hot.wav"
    rt.sf.write(str(p), np.full(4800, 3.0), 48000, subtype="FLOAT")
    assert np.max(np.abs(diagnose.load_voice(p, 48000, rt.np, rt.sig, rt.sf))) == pytest.approx(0.95)


def test_signal_helpers(rt):
    assert diagnose.db_to_amp(-6.0) == pytest.approx(0.501187, rel=1e-5)
    assert diagnose.dbfs(1.0) == 0.0 and diagnose.dbfs(0.0) == -240.0
    assert diagnose.rms(np.array([]), np) == 0.0
    x = diagnose.normalize_rms(np.random.default_rng(0).standard_normal(48000), -24, np, peak_limit_db=-10)
    assert np.max(np.abs(x)) <= diagnose.db_to_amp(-10) + 1e-12
    f = diagnose.fade(np.ones(1000), 1000, 100, np)
    assert f[0] == 0 and f[-1] == 0 and f[500] == 1
    pn = diagnose.pink_noise(4096, np)
    assert pn.mean() == pytest.approx(0, abs=1e-9) and pn.std() == pytest.approx(1)
    np.testing.assert_array_equal(pn, diagnose.pink_noise(4096, np))
