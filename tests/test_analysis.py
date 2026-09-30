"""Discontinuity/periodicity detection, alignment, classification and report writers."""
import csv
import json

import numpy as np
import pytest

import diagnose
from diagnose import CLEAN, PERIODIC


def stepped(n: int, every: int = 512 * 40, amp: float = 0.4, phase: int = 511, noise: float = 1e-3, seed: int = 0):
    x = np.random.default_rng(seed).normal(0, noise, n)
    steps = np.zeros(n)
    at = np.arange(phase, n - 1, every)
    steps[at] = np.where(np.arange(len(at)) % 2 == 0, 1.0, -1.0)
    return x + amp * np.cumsum(steps), at


def as_test(channels, name="t"):
    return {"firmware": name, "route": "r", "analysis": {"channels": channels}}


def test_step_discontinuities_report_phase_511():
    x, at = stepped(48000 * 10)
    ch = diagnose.analyze_channel(x, 48000, np)
    assert ch["jumps"]["gt_0.20"] == len(at)
    assert all(t["mod_512"] == 511 for t in ch["top_jumps"][: len(at)])
    b = ch["fold_best"]
    assert b["phase"] % 512 == 511 and b["top12_at_phase"] == 12
    assert diagnose.classify_test(as_test([ch])) == PERIODIC


def test_broadband_noise_is_not_a_click_signature():
    x = np.clip(np.random.default_rng(5).normal(0, 0.15, 48000 * 10), -1, 1)
    ch = diagnose.analyze_channel(x, 48000, np)
    assert ch["jumps"]["gt_0.20"] > 1000, "noise must produce many raw threshold crossings"
    assert diagnose.classify_test(as_test([ch])) == "no strong periodic-click signature"


def test_digital_silence_is_handled():
    ch = diagnose.analyze_channel(np.zeros(16000 * 5), 16000, np)
    assert ch["jumps"]["gt_0.05"] == 0 and ch["clipped_samples"] == 0 and ch["fold_best"]["ratio"] == 0.0
    assert diagnose.classify_test(as_test([ch])) == CLEAN


@pytest.mark.parametrize("z,ratio,sharpness,expected", [
    (10.0, 1.25, 1.2, PERIODIC),
    (9.9, 3.0, 3.0, CLEAN),
    (40.0, 1.24, 3.0, CLEAN),
    (40.0, 2.0, 1.19, CLEAN),
])
def test_classification_thresholds(z, ratio, sharpness, expected):
    b = {"period": 512, "phase": 0, "z": z, "ratio": ratio, "sharpness": sharpness, "top12_at_phase": 12}
    assert diagnose.classify_test(as_test([{"fold_best": None}, {"fold_best": b}])) == expected


def test_classification_without_channels():
    assert diagnose.classify_test(as_test([])) == "unknown"


@pytest.fixture(scope="module")
def stim(tmp_path_factory, rt):
    out = tmp_path_factory.mktemp("stim")
    path, manifest = diagnose.generate_stimulus(None, out, rt.np, rt.sig, rt.sf)
    data, fs = rt.sf.read(str(path), dtype="float64")
    return path, manifest, data, fs


@pytest.mark.parametrize("rate", [48000, 16000])
def test_alignment_recovers_playback_offset(stim, rt, rate):
    _, manifest, data, fs = stim
    x = data if rate == fs else rt.sig.resample_poly(data, 1, 3)
    offset = 0.8123
    rec = np.random.default_rng(2).normal(0, 1e-3, len(x) + 2 * rate)
    a = int(round(offset * rate))
    rec[a:a + len(x)] += 0.5 * x
    start, score = diagnose.align_reference(rec, rate, data, fs, manifest, np, rt.sig)
    assert start == pytest.approx(offset, abs=1.5 / rate)
    assert score > 0.9


def test_alignment_fallbacks(stim, rt):
    _, manifest, data, fs = stim
    assert diagnose.align_reference(np.zeros(100), fs, data, fs, manifest, np, rt.sig) == (0.75, 0.0)
    assert diagnose.align_reference(np.zeros(fs * 9), fs, data, fs, [], np, rt.sig) == (0.75, 0.0)


def write_recording(tmp_path, rt, stim_data, fs, delay_s=0.75, clicks=False):
    pad = np.zeros(int(delay_s * fs))
    y = np.concatenate([pad, stim_data, pad])
    y = np.column_stack([0.6 * y, 0.5 * y])
    if clicks:
        s, _ = stepped(len(y), noise=0)
        y[:, 1] += s
    p = tmp_path / "rec.wav"
    rt.sf.write(str(p), np.clip(y, -1, 0.99), fs, subtype="PCM_16")
    return p


def test_analyze_recording_segments(stim, tmp_path, rt):
    path, manifest, data, fs = stim
    rec = write_recording(tmp_path, rt, data, fs)
    a = diagnose.analyze_recording(rec, path, manifest, rt.sf, np, rt.sig)
    assert a["sample_rate"] == fs and len(a["channels"]) == 2
    assert a["playback_start_s"] == pytest.approx(0.75, abs=2 / fs)
    assert a["alignment_score"] > 0.99
    assert [s["name"] for s in a["segments"]] == [s["name"] for s in manifest]
    silent = next(s for s in a["segments"] if s["name"] == "silence_start")
    loud = next(s for s in a["segments"] if s["name"] == "white_noise")
    assert loud["channels"][0]["rms_dbfs"] > silent["channels"][0]["rms_dbfs"] + 40


def test_ambient_recording_has_no_alignment(stim, tmp_path, rt):
    path, _, data, fs = stim
    rec = write_recording(tmp_path, rt, data[: fs * 2], fs)
    a = diagnose.analyze_recording(rec, path, [], rt.sf, np, rt.sig)
    assert (a["playback_start_s"], a["alignment_score"], a["segments"]) == (0.0, 0.0, [])


def test_segment_past_end_of_recording(stim, tmp_path, rt):
    path, manifest, data, fs = stim
    rec = write_recording(tmp_path, rt, data[: fs * 4], fs)
    a = diagnose.analyze_recording(rec, path, manifest, rt.sf, np, rt.sig)
    assert a["segments"][-1]["channels"] == [{}, {}]


@pytest.fixture(scope="module")
def session(stim, tmp_path_factory, rt):
    path, manifest, data, fs = stim
    tmp = tmp_path_factory.mktemp("sess")
    rec = write_recording(tmp, rt, data, fs, clicks=True)
    a = diagnose.analyze_recording(rec, path, manifest, rt.sf, np, rt.sig)
    plots = diagnose.create_plots(rec, a, rt.sf, np)
    test = {"firmware": "<script>alert(1)</script>", "firmware_sha256": "x", "route": "normal", "route_description": "d & <b>",
            "xvf_before": {"VERSION": "VERSION: [2, 1, 1]"}, "capture_meta": {}, "recording_path": str(rec), "analysis": a, "plots": plots}
    return {"generated_at": "now", "environment": {"simulation": True}, "voice_fixture": None, "stimulus_sha256": "abc",
            "stimulus_manifest": manifest, "tests": [test]}, tmp


def test_plots_are_png_data_uris(session):
    plots = session[0]["tests"][0]["plots"]
    assert set(plots) == {"waveform", "jumps", "fold512", "spectrum", "spectrogram", "click_zoom"}
    assert all(v.startswith("data:image/png;base64,iVBOR") for v in plots.values())


def test_html_report_escapes_untrusted_strings(session):
    sess, tmp = session
    out = tmp / "report.html"
    diagnose.generate_html_report(sess, out)
    doc = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in doc and "&lt;script&gt;alert(1)&lt;/script&gt;" in doc
    assert "d &amp; &lt;b&gt;" in doc
    assert "periodic discontinuity signature" in doc
    assert doc.count("<img src='data:image/png") == 6


def test_summary_csv_and_comment(session):
    sess, tmp = session
    out = tmp / "summary.csv"
    diagnose.write_summary_csv(sess, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["classification"] == "periodic discontinuity signature"
    assert rows[0]["ch1_periodicity"].startswith("P=") and ", sharp;" in rows[0]["ch1_periodicity"]
    comment = diagnose.generate_github_comment(sess)
    assert "| `<script>alert(1)</script>` | `normal` | periodic discontinuity signature |" in comment
    assert "modulo-512" in comment
    json.dumps({k: v for k, v in sess.items() if k != "tests"})


# --- folded periodicity, upsampling signature, alignment robustness, buffer continuity ------

def spliced(n_blocks=900, drop=0.12, seed=4, block=512):
    """Band-limited noise with random whole blocks removed, like FFmpeg/AVFoundation at 48 kHz."""
    rng = np.random.default_rng(seed)
    b, a = __import__("scipy.signal", fromlist=["butter"]).butter(4, 0.05)
    ref = __import__("scipy.signal", fromlist=["lfilter"]).lfilter(b, a, rng.normal(0, 0.5, n_blocks * block))
    keep = rng.random(n_blocks) > drop
    keep[:3] = True
    test = np.concatenate([ref[k * block:(k + 1) * block] for k in range(n_blocks) if keep[k]])
    return ref, test, keep


def test_fold_finds_block_splices_at_the_right_period():
    _, test, _ = spliced()
    ch = diagnose.analyze_channel(test, 48000, np)
    b = ch["fold_best"]
    assert (b["period"], b["phase"]) == (512, 0) and b["sharpness"] > 1.5 and b["z"] > 10
    assert diagnose.classify_test(as_test([ch])) == PERIODIC


def test_broad_frame_modulation_is_not_a_click():
    rng = np.random.default_rng(1)
    n = 48000 * 30
    env = 1 + 0.4 * np.exp(-0.5 * ((np.arange(n) % 960 - 480) / 60.0) ** 2)  # smooth 20 ms bump
    ch = diagnose.analyze_channel(rng.normal(0, 0.05, n) * env, 48000, np)
    b = ch["fold_best"]
    assert b["period"] in (960, 1920) and b["z"] > 10 and b["sharpness"] < diagnose.FOLD_MIN_SHARPNESS
    assert diagnose.classify_test(as_test([ch])) == CLEAN


def test_fold_mask_excludes_samples():
    x, at = stepped(48000 * 10)
    mask = np.zeros(len(x), bool)
    assert diagnose.folded_discontinuity(x, [512], np, mask) == []
    mask[:] = True
    assert diagnose.folded_discontinuity(x, [512], np, mask)[0]["phase"] == 511


def test_periodic_stimulus_mask(stim):
    _, manifest, data, fs = stim
    mask = diagnose.periodic_stimulus_mask(len(data) + fs, fs, 0.5, manifest, np)
    tone = next(s for s in manifest if s["name"] == "tone_1000Hz")
    voice_like = next(s for s in manifest if s["name"] == "white_noise")
    assert not mask[int((0.5 + (tone["start_s"] + tone["end_s"]) / 2) * fs)]
    assert mask[int((0.5 + (voice_like["start_s"] + voice_like["end_s"]) / 2) * fs)]


def test_upsampling_signature(rt):
    rng = np.random.default_rng(2)
    base = rt.sig.lfilter(*rt.sig.butter(6, 0.8), rng.normal(0, 0.1, 16000 * 10))  # < 6.4 kHz at 16 kHz
    stuffed = np.zeros(len(base) * 3); stuffed[1::3] = base
    filtered = rt.sig.resample_poly(base, 3, 1)
    q = lambda x: np.round(x * 32768) / 32768
    kinds = {name: diagnose.upsampling_signature(q(x), 48000, np, rt.sig)["kind"] for name, x in
             (("stuffed", stuffed), ("filtered", filtered), ("wide", rng.normal(0, 0.1, 48000 * 10)))}
    assert kinds["stuffed"].startswith("zero-stuffed") and kinds["filtered"].startswith("band-limited") and kinds["wide"] == "full-band"
    assert diagnose.upsampling_signature(base, 16000, np, rt.sig) is None


def test_alignment_survives_a_suppressed_chirp(stim, rt):
    """Processed ASR output can nearly erase the chirp; the envelope stage still finds the start."""
    _, manifest, data, fs = stim
    sync = next(s for s in manifest if s["name"] == "sync_chirp")
    offset = 0.884
    rec = np.random.default_rng(3).normal(0, 1e-3, len(data) + 2 * fs)
    a = int(offset * fs)
    rec[a:a + len(data)] += 0.5 * data
    c0, c1 = a + int(sync["start_s"] * fs), a + int(sync["end_s"] * fs)
    rec[c0:c1] *= 0.001
    rec[a + int(2.6 * fs):a + int(2.8 * fs)] += 0.3 * np.sin(np.arange(int(0.2 * fs)) / 3)  # a louder distractor later on
    start, _ = diagnose.align_reference(rec, fs, data, fs, manifest, np, rt.sig)
    assert start == pytest.approx(offset, abs=0.01)


def write_pair(tmp_path, rt, ref, test, fs=48000):
    r, t = tmp_path / "ref.wav", tmp_path / "test.wav"
    rt.sf.write(str(r), np.column_stack([ref, 0.5 * ref]), fs, subtype="PCM_16")
    rt.sf.write(str(t), np.column_stack([test, 0.5 * test]), fs, subtype="PCM_16")
    return r, t


def test_buffer_continuity_maps_every_dropped_block(tmp_path, rt):
    ref, test, keep = spliced()
    # the test capture also starts ~0.3 s (28 whole buffers) before the reference, like ffmpeg started first
    lead = np.random.default_rng(9).normal(0, 0.2, 28 * 512)
    r, t = write_pair(tmp_path, rt, ref, np.concatenate([lead, test]))
    res = diagnose.buffer_continuity(r, t, rt.sf, np, rt.sig)
    runs = sum(1 for k in range(1, len(keep)) if not keep[k] and keep[k - 1])
    assert res["comparable"] and res["all_multiples_of_block"]
    assert res["frames_missing"] == int((~keep).sum()) * 512 and res["splices"] == runs
    assert res["splice_positions_mod_block"] == [0]


def test_buffer_continuity_clean_and_incomparable(tmp_path, rt):
    ref, _, _ = spliced(drop=0)
    r, t = write_pair(tmp_path, rt, ref, ref[5 * 512:])
    res = diagnose.buffer_continuity(r, t, rt.sf, np, rt.sig)
    assert res["splices"] == 0 and res["identical_blocks"] == res["located_blocks"] == res["blocks"] and res["max_lsb_difference"] == 0
    r2, t2 = write_pair(tmp_path, rt, ref, np.random.default_rng(0).normal(0, 0.2, len(ref)))
    assert diagnose.buffer_continuity(r2, t2, rt.sf, np, rt.sig)["comparable"] is False
    rt.sf.write(str(t2), np.zeros((48000, 2)), 16000)
    assert "format differs" in diagnose.buffer_continuity(r2, t2, rt.sf, np, rt.sig)["reason"]
