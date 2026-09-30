"""tools/validate.py: importable without side effects, summary rendering, stop without a run."""
import importlib.util

from conftest import REPO_ROOT


def load():
    spec = importlib.util.spec_from_file_location("validate", REPO_ROOT / "tools" / "validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stages_and_pinned_fix():
    v = load()
    assert v.STAGES[:4] == v.SILENT_STAGES == ["unit", "upstream", "readonly", "ffmpeg"]
    assert len(v.FFMPEG_FIX_COMMIT) == 40 and "--enable-indev=avfoundation" in v.FFMPEG_CONFIGURE
    assert v.ISSUE_ROUTE["left"] == (8, 0) and v.ISSUE_ROUTE["right"] == (7, 3)


def test_stop_without_a_run(monkeypatch, tmp_path, capsys):
    v = load()
    monkeypatch.setattr(v, "PIDFILE", tmp_path / "running.pid")
    assert v.stop_running() == 0 and "No validation run" in capsys.readouterr().out


def test_render_summary():
    v = load()
    cont = {"comparable": True, "identical_blocks": 3332, "located_blocks": 3332, "within_1lsb_blocks": 3332, "blocks": 3336, "splices": 413,
            "missing_pct": 11.0, "splice_positions_mod_block": [0]}
    results = {
        "started": "20260930-120000", "env": {"platform": "macOS", "python": "3.14", "system_ffmpeg": {"version": "ffmpeg version 8.1.1"}},
        "firmware_before": "v2.1.1_native16k", "firmware_after": "v2.1.1_native16k", "errors": [],
        "stages": {
            "unit": {"ok": True, "summary": "120 passed"},
            "matrix": {"ok": True, "session": "s", "tests": [{"firmware": "v2.1.1_48k2ch", "route": "normal", "result": "clean",
                                                                "periodicity": ["none", "none"], "signature": "band-limited", "host_path": "periodic"}]},
            "hostpath": {"ok": True, "trials": [
                {"name": "xvf48k_homebrew_ab1", "kind": "ab", "continuity": cont,
                 "ffmpeg": {"classification": "periodic"}, "direct": {"classification": "clean"}},
                {"name": "xvf48k_homebrew_duration30", "kind": "duration", "requested_s": 30.0, "held_s": 26.7, "missing_pct": 11.0,
                 "ffmpeg": {"classification": "periodic"}},
                {"name": "broken", "kind": "ab", "error": "ffmpeg produced no recording"},
            ]},
        },
    }
    md = v.render_summary(results)
    assert "| unit | ✅ 120 passed |" in md
    assert "| xvf48k_homebrew_ab1 | 3332/3332 (3332) | 413 | 11.0% | [0] | periodic | clean |" in md
    assert "| xvf48k_homebrew_duration30 | 30 s | 26.700 s | 11.0% | periodic |" in md
    assert "ffmpeg produced no recording" in md and "v2.1.1_native16k / v2.1.1_native16k" in md


def load_report():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("validation_report", REPO_ROOT / "tools" / "validation_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_html_report_from_trials(tmp_path, rt):
    """Two synthetic trials (one with dropped buffers, one clean), a duration check and a failed trial."""
    import numpy as np
    import diagnose
    rep = load_report()
    rng = np.random.default_rng(0)
    b, a = rt.sig.butter(4, 0.1)
    trials = []
    for name, drop in (("xvf48k_homebrew_ab1", 0.12), ("xvf16k_homebrew_ab", 0.0)):
        ref = rt.sig.lfilter(b, a, rng.normal(0, 0.4, 512 * 900))
        keep = rng.random(900) > drop
        keep[:4] = True
        test = np.concatenate([ref[k * 512:(k + 1) * 512] for k in range(900) if keep[k]])
        tdir = tmp_path / "hostpath" / name
        tdir.mkdir(parents=True)
        rt.sf.write(str(tdir / "direct.wav"), np.column_stack([ref, 0.5 * ref]), 48000, subtype="PCM_16")
        rt.sf.write(str(tdir / "ffmpeg.wav"), np.column_stack([test, 0.5 * test]), 48000, subtype="PCM_16")
        cont = diagnose.buffer_continuity(tdir / "direct.wav", tdir / "ffmpeg.wav", rt.sf, np, rt.sig)
        cont["missing_pct"] = 100 * cont["frames_missing"] / (cont["test_frames"] + cont["frames_missing"])
        trials.append({"name": name, "kind": "ab", "dir": str(tdir), "continuity": cont,
                       "ffmpeg": {"classification": diagnose.PERIODIC if drop else diagnose.CLEAN, "periodicity": ["x", "y"]},
                       "direct": {"classification": diagnose.CLEAN, "periodicity": ["x", "y"]}})
        if drop:
            # offsets reconstructed from the anchor and splice list map FFmpeg frames to the direct capture
            f = cont["splice_list"][3][0]
            off = rep.reference_offset(cont, f)
            ffx, _ = rt.sf.read(str(tdir / "ffmpeg.wav"), always_2d=True)
            dix, _ = rt.sf.read(str(tdir / "direct.wav"), always_2d=True)
            assert np.array_equal(ffx[f:f + 100], dix[f + off:f + off + 100])
    trials.append({"name": "xvf48k_homebrew_duration30", "kind": "duration", "requested_s": 30.0, "held_s": 26.6, "missing_pct": 11.3,
                   "ffmpeg": {"classification": diagnose.PERIODIC}})
    trials.append({"name": "builtin_mic_homebrew_ab", "kind": "ab", "error": "ffmpeg produced no recording"})
    results = {"started": "x", "env": {}, "errors": [], "stages": {"hostpath": {"ok": True, "trials": trials, "playback_device": "Speakers"}}}
    doc = rep.render(results, tmp_path).read_text()
    assert doc.count("<img") == 10  # 5 for the spliced trial, 4 for the clean one (no splice to zoom on), 1 duration chart
    assert "XVF3800 v2.1.1 48 kHz · Homebrew FFmpeg" in doc and "ffmpeg produced no recording" in doc
    assert "no audio missing in 1 trial(s)" in doc and "whole 512-frame buffers only" in doc
    assert trials[0]["stats"]["splices_per_min"] > 0 and trials[0]["stats"]["run_lengths"]
