"""End-to-end: simulation, emulated hardware matrix, CLI entry points and result import."""
import argparse
import csv
import json
import subprocess
import sys
import zipfile

import numpy as np
import pytest

import diagnose
from conftest import REPO_ROOT, FakeSoundDevice, read_log

PERIODIC = "periodic discontinuity signature"
CLEAN = "no strong periodic-click signature"
SESSION_FILES = {"report.html", "summary.csv", "analysis.json", "session.json", "github_comment.md", "github-share-bundle.zip"}


@pytest.fixture(scope="module")
def simulated(tmp_path_factory, rt):
    out = tmp_path_factory.mktemp("sim")
    voice = out / "voice.wav"
    t = np.arange(48000) / 48000
    rt.sf.write(str(voice), 0.3 * np.sin(2 * np.pi * 300 * t), 48000, subtype="PCM_16")
    return diagnose.simulate_run(out, voice, rt.np, rt.sig, rt.sf)


def test_simulation_detects_injected_phase_511(simulated):
    rows = list(csv.DictReader((simulated / "summary.csv").open(encoding="utf-8")))
    got = {(r["firmware"], r["route"]): r for r in rows}
    bad = got[("v2.1.1_48k2ch", "normal")]
    assert bad["classification"] == PERIODIC
    assert bad["ch0_periodicity"].startswith("P=512 phase 511") and bad["ch1_periodicity"].startswith("P=512 phase 511")
    assert ", sharp;" in bad["ch1_periodicity"]
    assert got[("v2.1.1_48k2ch", "raw")]["ch0_48k_signature"] == "full-band"
    assert got[("v2.1.1_48k2ch", "raw")]["classification"] == CLEAN
    assert got[("v2.1.1_native16k", "normal")]["classification"] == CLEAN
    assert all(float(r["alignment_score"]) > 0.99 for r in rows)


def test_simulation_outputs(simulated):
    assert SESSION_FILES <= {p.name for p in simulated.iterdir()}
    analysis = json.loads((simulated / "analysis.json").read_text())
    assert all("plots" not in t for t in analysis["tests"])
    assert analysis["voice_fixture"]["playback_gain_db"] == -6.0
    with zipfile.ZipFile(simulated / "github-share-bundle.zip") as zf:
        names = set(zf.namelist())
    assert {"report.html", "analysis.json", "summary.csv", "session.json", "stimulus_manifest.json",
            "github_comment.md", "diagnose.py", "VOICE_SCRIPT.txt",
            "v2.1.1_48k2ch__normal.flac", "v2.1.1_48k2ch__raw.flac", "v2.1.1_native16k__normal.flac"} == names
    assert (simulated / "github_comment.md").read_text() == (simulated / "share" / "github_comment.md").read_text()


def import_run(*args):
    return subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "import_run.py"), *map(str, args)],
                          capture_output=True, text=True)


def test_reanalyze_rebuilds_outputs_from_recordings(simulated, tmp_path, rt):
    import shutil
    copy = tmp_path / simulated.name
    shutil.copytree(simulated, copy)  # relative recording paths keep a moved session usable
    for f in ("report.html", "summary.csv", "github-share-bundle.zip"):
        (copy / f).unlink()
    diagnose.reanalyze(copy, rt.np, rt.sig, rt.sf)
    assert (copy / "summary.csv").read_text() == (simulated / "summary.csv").read_text()
    assert json.loads((copy / "analysis.json").read_text())["analyzed_with"] == diagnose.APP_VERSION
    assert (copy / "report.html").is_file() and (copy / "github-share-bundle.zip").is_file()


def test_import_run_publishes_hashed_evidence(simulated, tmp_path):
    proc = import_run(simulated, "--dest-root", tmp_path)
    assert proc.returncode == 0, proc.stderr
    dest = tmp_path / simulated.name
    manifest = json.loads((dest / "manifest.json").read_text())
    assert {f["path"] for f in manifest["files"]} == SESSION_FILES
    for f in manifest["files"]:
        assert diagnose.sha256_file(dest / f["path"]) == f["sha256"]
        assert (dest / f["path"]).stat().st_size == f["bytes"]
        assert f["sha256"] in (dest / "README.md").read_text(encoding="utf-8")
    again = import_run(simulated, "--dest-root", tmp_path)
    assert again.returncode == 1 and "Already imported" in again.stderr and "Traceback" not in again.stderr


def test_import_run_errors(tmp_path):
    assert "Run directory not found" in import_run(tmp_path / "missing").stderr
    (tmp_path / "empty").mkdir()
    proc = import_run(tmp_path / "empty", "--dest-root", tmp_path / "out")
    assert "No expected report artifacts" in proc.stderr and not (tmp_path / "out").exists()


def test_import_run_default_destination_is_repo_relative():
    import importlib.util
    spec = importlib.util.spec_from_file_location("import_run", REPO_ROOT / "tools" / "import_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.REPO_ROOT == REPO_ROOT


# --- emulated hardware ---------------------------------------------------------------------

EXPECTED_MATRIX = [
    ("v2.1.0_48k2ch", "normal_ambient", CLEAN),
    ("v2.1.0_48k2ch", "normal", CLEAN),
    ("v2.1.1_48k2ch", "normal_ambient", PERIODIC),
    ("v2.1.1_48k2ch", "normal", PERIODIC),
    ("v2.1.1_48k2ch", "raw", CLEAN),
    ("v2.1.1_48k2ch", "pre_shf", CLEAN),
    ("v2.1.1_48k2ch", "shf_input", CLEAN),
    ("v2.1.1_48k2ch", "processed_direct", PERIODIC),
    ("v2.1.1_48k2ch", "processed_no_upsample", CLEAN),
    ("v2.1.1_native16k", "normal_ambient", CLEAN),
    ("v2.1.1_native16k", "normal", CLEAN),
    ("v2.1.1_native16k", "raw", CLEAN),
]


def test_real_run_against_emulated_hardware(tmp_path, monkeypatch, rt, fake_sd, fake_repo, fake_dfu, host_cmd, xvf_state, short_voice):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda msg="": prompts.append(msg) or "")
    monkeypatch.setattr(diagnose, "ensure_repo", lambda cache: fake_repo)
    monkeypatch.setattr(diagnose, "ensure_dfu_util", lambda cache: fake_dfu)
    monkeypatch.setattr(diagnose, "locate_xvf_host", lambda repo: host_cmd)
    # Behave like macOS dfu-util 0.11: non-zero exit after every successful flash.
    xvf_state.write_text(json.dumps({**json.loads(xvf_state.read_text()), "reset_exit": 251}))
    args = argparse.Namespace(output=str(tmp_path / "runs"), voice=str(short_voice), output_device=None, deep=True, yes=False, host_control=False)

    sess = diagnose.real_run(args, tmp_path, rt.np, rt.sig, rt.sf, fake_sd)

    analysis = json.loads((sess / "analysis.json").read_text())
    got = [(t["firmware"], t["route"], diagnose.classify_test(t)) for t in analysis["tests"]]
    assert got == EXPECTED_MATRIX
    version = {"v2.1.0_48k2ch": "VERSION: [2, 1, 0]", "v2.1.1_48k2ch": "VERSION: [2, 1, 1]", "v2.1.1_native16k": "VERSION: [2, 1, 1]"}
    routes = {r["name"]: r for r in diagnose.ROUTES_48K}
    for t in analysis["tests"]:
        assert t["xvf_before"]["VERSION"] == version[t["firmware"]]
        assert t["xvf_before"]["BLD_MSG"] == f"BLD_MSG: '{t['firmware']}'"
        assert t["recording_path"] == f"recordings/{t['firmware']}__{t['route']}.wav"
        assert t["analysis"]["sample_rate"] == (16000 if "16k" in t["firmware"] else 48000)
        route = routes[t["route"].replace("_ambient", "")]
        assert t["xvf_before"]["AUDIO_MGR_OP_L"] == "AUDIO_MGR_OP_L: [{}, {}]".format(*route["left"])
        assert t["xvf_before"]["AUDIO_MGR_OP_UPSAMPLE"] == "AUDIO_MGR_OP_UPSAMPLE: [{}, {}]".format(*route["upsample"])
        if t["route"] != "normal_ambient":
            assert t["analysis"]["alignment_score"] > 0.99
            assert t["capture_meta"]["output_device_name"] == FakeSoundDevice.SPEAKER

    # One PortAudio rescan per reflash, and the same physical speaker despite index shifts.
    assert fake_sd.inits == 3
    assert {p["device"] for p in fake_sd.plays} == {FakeSoundDevice.SPEAKER}
    assert len(prompts) == 1  # pre-flight only: the fake exposes DFU while running

    flashes = [c for c in read_log(xvf_state.with_suffix(".dfu.log")) if "-D" in c]
    assert [c[:5] for c in flashes] == [["-R", "-e", "-a", "1", "-D"]] * 3
    assert json.loads(xvf_state.read_text())["firmware"] == "v2.1.1_native16k"

    host_calls = read_log(xvf_state.with_suffix(".host.log"))
    assert not any(c and c[0] in ("SAVE_CONFIGURATION", "CLEAR_CONFIGURATION") for c in host_calls)
    assert all(c[1] == "--values" for c in host_calls if len(c) > 1)

    session = json.loads((sess / "session.json").read_text())
    assert not any(str(tmp_path) in json.dumps(v) for v in (session, analysis)), "no local absolute paths"
    assert session["firmware_order"] == ["v2.1.0_48k2ch", "v2.1.1_48k2ch", "v2.1.1_native16k"]
    assert session["environment"]["respeaker_repo_commit"] == diagnose.RESPEAKER_REPO_COMMIT
    assert SESSION_FILES <= {p.name for p in sess.iterdir()}
    with zipfile.ZipFile(sess / "github-share-bundle.zip") as zf:
        flacs = sorted(n for n in zf.namelist() if n.endswith(".flac"))
    assert flacs == ["v2.1.1_48k2ch__normal.flac", "v2.1.1_48k2ch__normal_ambient.flac", "v2.1.1_48k2ch__pre_shf.flac",
                     "v2.1.1_48k2ch__raw.flac", "v2.1.1_native16k__normal.flac", "v2.1.1_native16k__normal_ambient.flac"]


def test_real_run_without_portaudio_refresh_would_fail(tmp_path, monkeypatch, rt, fake_sd, fake_repo, fake_dfu, host_cmd, short_voice):
    """Guards the refresh: with a stale device list the 16 kHz capture cannot open."""
    monkeypatch.setattr("builtins.input", lambda msg="": "")
    monkeypatch.setattr(diagnose, "ensure_repo", lambda cache: fake_repo)
    monkeypatch.setattr(diagnose, "ensure_dfu_util", lambda cache: fake_dfu)
    monkeypatch.setattr(diagnose, "locate_xvf_host", lambda repo: host_cmd)
    monkeypatch.setattr(diagnose, "refresh_audio_devices", lambda sd: None)
    monkeypatch.setattr(diagnose, "routes_for", lambda *a: [])
    real_wait = diagnose.wait_for_audio_input
    monkeypatch.setattr(diagnose, "wait_for_audio_input", lambda sd, rate: real_wait(sd, rate, timeout_s=0.2))
    args = argparse.Namespace(output=str(tmp_path / "runs"), voice=str(short_voice), output_device=None, deep=False, yes=True, host_control=False)
    with pytest.raises(RuntimeError, match="did not become available at 16000 Hz: Invalid sample rate 16000"):
        diagnose.real_run(args, tmp_path, rt.np, rt.sig, rt.sf, fake_sd)


# --- CLI -----------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, rt):
    sd = FakeSoundDevice()
    monkeypatch.setattr(diagnose, "bootstrap_venv", lambda path: None)
    monkeypatch.setattr(diagnose, "import_runtime", lambda: (rt.np, rt.sig, sd, rt.sf))

    def main(*argv):
        monkeypatch.setattr(sys, "argv", ["diagnose.py", *map(str, argv)])
        return diagnose.main()
    return main


def test_cli_devices(cli, capsys):
    assert cli("--devices") == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["name"] for r in rows if r["max_input_channels"] >= 2] == [FakeSoundDevice.XVF]


def test_cli_make_stimulus_uses_canonical_voice(cli, tmp_path, capsys):
    assert cli("--make-stimulus", "--output", tmp_path) == 0
    out = capsys.readouterr().out
    assert "Using repository test VO" in out and str(tmp_path / "stimulus" / "diagnostic_stimulus_48k.wav") in out
    manifest = json.loads((tmp_path / "stimulus" / "stimulus_manifest.json").read_text())
    assert any(s["name"] == "english_voice" for s in manifest["segments"])


def test_resolve_voice(tmp_path, capsys):
    assert diagnose.resolve_voice(None, REPO_ROOT) == (REPO_ROOT / "assets/voice/xvf3800-test-vo.wav").resolve()
    assert "Using repository test VO: assets/voice/xvf3800-test-vo.wav" in capsys.readouterr().out
    assert diagnose.resolve_voice(None, tmp_path) is None
    with pytest.raises(RuntimeError, match="Voice file not found"):
        diagnose.resolve_voice(str(tmp_path / "nope.wav"), REPO_ROOT)


def test_voice_fixture_path_is_repo_relative():
    info = diagnose.voice_fixture_info(REPO_ROOT / "assets" / "voice" / "xvf3800-test-vo.wav")
    assert info["path"] == "assets/voice/xvf3800-test-vo.wav"
    assert diagnose.voice_fixture_info(None) is None


# --- host capture-path control ----------------------------------------------------------

class DroppingHostCapture:
    """Behaves like FFmpeg/AVFoundation at 48 kHz: the stream it records is the direct capture
    with random whole 512-frame buffers missing (and it starts a little earlier)."""
    instances = []

    def __init__(self, ffmpeg, index, out_wav):
        self.out_wav = out_wav
        DroppingHostCapture.instances.append(self)

    def stop(self):
        import soundfile as sf
        rec = self.out_wav.with_name(self.out_wav.name.replace("__ffmpeg", ""))
        x, fs = sf.read(str(rec), always_2d=True)
        if fs == 48000:
            keep = np.random.default_rng(3).random(len(x) // 512) > 0.12
            x = np.concatenate([x[k * 512:(k + 1) * 512] for k in range(len(x) // 512) if keep[k]])
        sf.write(str(self.out_wav), x, fs, subtype="PCM_16")
        return {"tool": "fake ffmpeg", "returncode": 0, "stderr": ""}


def test_real_run_with_host_path_control(tmp_path, monkeypatch, rt, fake_sd, fake_repo, fake_dfu, host_cmd, short_voice):
    monkeypatch.setattr("builtins.input", lambda msg="": "")
    monkeypatch.setattr(diagnose, "ensure_repo", lambda cache: fake_repo)
    monkeypatch.setattr(diagnose, "ensure_dfu_util", lambda cache: fake_dfu)
    monkeypatch.setattr(diagnose, "locate_xvf_host", lambda repo: host_cmd)
    monkeypatch.setattr(diagnose, "host_control_ffmpeg", lambda: ("ffmpeg", "6"))
    monkeypatch.setattr(diagnose, "HostPathCapture", DroppingHostCapture)
    DroppingHostCapture.instances = []
    args = argparse.Namespace(output=str(tmp_path / "runs"), voice=str(short_voice), output_device=None, deep=False, yes=True, host_control=True)

    sess = diagnose.real_run(args, tmp_path, rt.np, rt.sig, rt.sf, fake_sd)

    tests = {(t["firmware"], t["route"]): t for t in json.loads((sess / "analysis.json").read_text())["tests"]}
    assert len(DroppingHostCapture.instances) == 2  # the "normal" route of each firmware
    hp48 = tests[("v2.1.1_48k2ch", "normal")]["host_path_control"]
    cont = hp48["continuity"]
    assert cont["comparable"] and cont["splices"] > 10 and cont["all_multiples_of_block"]
    assert cont["splice_positions_mod_block"] == [0] and cont["bit_exact_blocks"] >= cont["blocks"] - 2
    assert hp48["classification"] == PERIODIC
    hp16 = tests[("v2.1.1_native16k", "normal")]["host_path_control"]
    assert hp16["continuity"]["splices"] == 0 and hp16["classification"] == CLEAN
    assert "host_path_control" not in tests[("v2.1.1_48k2ch", "raw")]
    comment = (sess / "github_comment.md").read_text()
    assert "FFmpeg/AVFoundation" in comment and "buffers dropped on the host side" in comment
    with zipfile.ZipFile(sess / "github-share-bundle.zip") as zf:
        assert "v2.1.1_48k2ch__normal__ffmpeg.flac" in zf.namelist()
    rows = {(r["firmware"], r["route"]): r for r in csv.DictReader((sess / "summary.csv").open())}
    assert int(rows[("v2.1.1_48k2ch", "normal")]["host_path_splices"]) == cont["splices"]
    # a reanalysis recomputes the control from the stored recordings
    before = cont["splices"]
    diagnose.reanalyze(sess, rt.np, rt.sig, rt.sf)
    again = json.loads((sess / "analysis.json").read_text())["tests"]
    assert next(t for t in again if t["route"] == "normal" and "48k" in t["firmware"])["host_path_control"]["continuity"]["splices"] == before
