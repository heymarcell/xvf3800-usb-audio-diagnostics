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
    cont = {"comparable": True, "bit_exact_blocks": 3332, "blocks": 3336, "splices": 413, "missing_pct": 11.0, "splice_positions_mod_block": [0]}
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
    assert "| xvf48k_homebrew_ab1 | 3332/3336 | 413 | 11.0% | [0] | periodic | clean |" in md
    assert "| xvf48k_homebrew_duration30 | 30 s | 26.700 s | 11.0% | periodic |" in md
    assert "ffmpeg produced no recording" in md and "v2.1.1_native16k / v2.1.1_native16k" in md
