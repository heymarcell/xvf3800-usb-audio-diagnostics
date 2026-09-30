"""Discontinuity/periodicity detection, alignment, classification and report writers."""
import csv
import json

import numpy as np
import pytest

import diagnose


def stepped(n: int, every: int = 512 * 40, amp: float = 0.4, phase: int = 511, noise: float = 1e-3, seed: int = 0):
    x = np.random.default_rng(seed).normal(0, noise, n)
    steps = np.zeros(n)
    at = np.arange(phase, n - 1, every)
    steps[at] = np.where(np.arange(len(at)) % 2 == 0, 1.0, -1.0)
    return x + amp * np.cumsum(steps), at


def as_test(channels, name="t"):
    return {"firmware": name, "route": "r", "analysis": {"channels": channels}}


def test_candidate_period_stats():
    rows = {r["period"]: r for r in diagnose.candidate_period_stats(np.array([511, 1023, 1535, 2047]), [256, 512, 1024], np)}
    assert rows[512] == {"period": 512, "events": 4, "dominant_phase": 511, "dominant_count": 4, "concentration": 1.0, "enrichment": 512.0}
    assert rows[256]["dominant_phase"] == 255 and rows[256]["concentration"] == 1.0
    assert rows[1024]["dominant_count"] == 2 and rows[1024]["enrichment"] == 512.0
    empty = diagnose.candidate_period_stats(np.array([], dtype=int), [512], np)[0]
    assert empty["events"] == 0 and empty["dominant_phase"] is None and empty["enrichment"] == 0.0


def test_step_discontinuities_report_phase_511():
    x, at = stepped(48000 * 10)
    ch = diagnose.analyze_channel(x, 48000, np)
    assert ch["jumps"]["gt_0.20"] == len(at)
    r512 = next(r for r in ch["periodicity_gt_0_20"] if r["period"] == 512)
    assert r512["dominant_phase"] == 511 and r512["concentration"] == 1.0
    assert all(t["mod_512"] == 511 for t in ch["top_jumps"][: len(at)])
    assert set(ch["bands"]) == {"0_300", "300_3400", "3400_8000", "8000_16000", "16000_24000"}
    assert sum(ch["bands"].values()) == pytest.approx(1.0, abs=1e-6)
    assert diagnose.classify_test(as_test([ch])) == "periodic discontinuity signature"


def test_broadband_noise_is_not_a_click_signature():
    x = np.clip(np.random.default_rng(5).normal(0, 0.15, 48000 * 10), -1, 1)
    ch = diagnose.analyze_channel(x, 48000, np)
    assert ch["jumps"]["gt_0.20"] > 1000, "noise must produce many raw threshold crossings"
    assert diagnose.classify_test(as_test([ch])) == "no strong periodic-click signature"


def test_16k_bands_skip_above_nyquist():
    ch = diagnose.analyze_channel(np.zeros(16000), 16000, np)
    assert set(ch["bands"]) == {"0_300", "300_3400", "3400_8000"}
    assert ch["jumps"]["gt_0.05"] == 0 and ch["clipped_samples"] == 0


@pytest.mark.parametrize("count,enrichment,expected", [
    (4, 20.0, "periodic discontinuity signature"),
    (3, 500.0, "no strong periodic-click signature"),
    (40, 19.9, "no strong periodic-click signature"),
])
def test_classification_thresholds(count, enrichment, expected):
    ch = {"periodicity_gt_0_20": [{"period": 512, "dominant_count": count, "enrichment": enrichment}]}
    assert diagnose.classify_test(as_test([{"periodicity_gt_0_20": []}, ch])) == expected


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
    assert set(plots) == {"waveform", "jumps", "phase512", "spectrum", "spectrogram", "click_zoom"}
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
    assert rows[0]["ch1_512_phase"] == "511"
    comment = diagnose.generate_github_comment(sess)
    assert "| `<script>alert(1)</script>` | `normal` | periodic discontinuity signature |" in comment
    assert "modulo-512" in comment
    json.dumps({k: v for k, v in sess.items() if k != "tests"})
