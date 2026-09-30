"""XVF control (xvf_host.py CLI contract), DFU flashing and audio-device handling."""
import json
import subprocess
import sys

import numpy as np
import pytest

import diagnose
from conftest import FakeSoundDevice, read_log

UPSTREAM_READ = "ReadCMD: cmdid: 128, resid: 48, payload: [0, 2, 1, 1]\nVERSION: [2, 1, 1]\nDone!"


def regs(state):
    return json.loads(state.read_text())["regs"]


def test_fake_host_rejects_positional_values_like_upstream(host_cmd):
    proc = subprocess.run([*host_cmd, "AUDIO_MGR_OP_L", "8", "0"], capture_output=True, text=True)
    assert proc.returncode == 2 and "unrecognized arguments: 8 0" in proc.stderr


def test_xvf_call_writes_with_values_flag(host_cmd, xvf_state):
    out = diagnose.xvf_call(host_cmd, "AUDIO_MGR_OP_L", 6, 3)
    assert out.endswith("Done!")
    assert regs(xvf_state)["AUDIO_MGR_OP_L"] == [6, 3]
    assert read_log(xvf_state.with_suffix(".host.log"))[-1] == ["AUDIO_MGR_OP_L", "--values", "6", "3"]
    line = diagnose.parse_last_line_value(diagnose.xvf_call(host_cmd, "AUDIO_MGR_OP_L"), "AUDIO_MGR_OP_L")
    assert line == "AUDIO_MGR_OP_L: [6, 3]"


def test_xvf_call_raises_on_host_error(host_cmd):
    with pytest.raises(RuntimeError, match="read-only"):
        diagnose.xvf_call(host_cmd, "VERSION", 1, 2, 3)


@pytest.mark.parametrize("output,expected", [
    (UPSTREAM_READ, "VERSION: [2, 1, 1]"),
    ("VERSION 2 1 1", "VERSION 2 1 1"),
    ("junk\nError executing command VERSION: timeout\nDone!", "Error executing command VERSION: timeout"),
    ("", ""),
])
def test_parse_last_line_value(output, expected):
    assert diagnose.parse_last_line_value(output, "VERSION") == expected


def test_parse_does_not_confuse_prefixed_commands():
    out = "AEC_ASROUTONOFF: [1]\nAEC_ASROUTGAIN: [1.000]\nDone!"
    assert diagnose.parse_last_line_value(out, "AEC_ASROUTONOFF") == "AEC_ASROUTONOFF: [1]"


@pytest.mark.parametrize("text,ok", [(UPSTREAM_READ, True), ("VERSION 2 1 1", True), ("No device found", False),
                                     ("usage: xvf_host.py\nerror: VERSION", False)])
def test_version_regex(text, ok):
    assert bool(diagnose.VERSION_RE.search(text)) is ok


@pytest.mark.parametrize("line,expected", [
    ("BLD_MSG: ['u', 'a', '-', 'i', 'o', '1', '6']", "BLD_MSG: 'ua-io16'"),
    ("X: [',', ''', 'a']", "X: ','a'"),  # upstream prints a quote character as '''
    ("VERSION: [2, 1, 1]", "VERSION: [2, 1, 1]"),
    ("AEC_RT60: [0.300]", "AEC_RT60: [0.300]"),
])
def test_join_char_values(line, expected):
    assert diagnose.join_char_values(line) == expected


@pytest.mark.parametrize("route", diagnose.ROUTES_48K + diagnose.ROUTES_16K, ids=lambda r: r["name"])
def test_set_route_programs_every_register(host_cmd, xvf_state, route):
    diagnose.set_route(host_cmd, route)
    r = regs(xvf_state)
    assert r["AUDIO_MGR_OP_L"] == list(route["left"]) and r["AUDIO_MGR_OP_R"] == list(route["right"])
    assert r["AUDIO_MGR_OP_UPSAMPLE"] == list(route["upsample"]) and r["AUDIO_MGR_OP_PACKED"] == [0, 0]
    assert r["AEC_ASROUTONOFF"] == [1] and r["AEC_ASROUTGAIN"] == [1.0]


def test_snapshot_reads_every_value(host_cmd, xvf_state):
    diagnose.set_route(host_cmd, diagnose.ROUTES_48K[1])
    snap = diagnose.snapshot_xvf(host_cmd)
    assert len(snap) == 21
    for cmd, line in snap.items():
        assert line.startswith(cmd + ": "), (cmd, line)
    assert snap["VERSION"] == "VERSION: [2, 1, 1]"
    assert snap["BLD_MSG"] == "BLD_MSG: 'v2.1.1_48k2ch'" and snap["BLD_REPO_HASH"] == "BLD_REPO_HASH: '" + "0" * 40 + "'"
    assert snap["AUDIO_MGR_OP_L"] == "AUDIO_MGR_OP_L: [1, 0]"
    written = [c for c in read_log(xvf_state.with_suffix(".host.log")) if "--values" in c]
    assert not any(c[0] in ("SAVE_CONFIGURATION", "CLEAR_CONFIGURATION") for c in written)


def test_snapshot_and_wait_without_device(host_cmd, xvf_state):
    xvf_state.write_text(json.dumps({"firmware": "v2.1.1_48k2ch", "regs": {}, "present": False}))
    snap = diagnose.snapshot_xvf(host_cmd)
    assert all(v.startswith("ERROR:") and "No device found" in v for v in snap.values())
    with pytest.raises(RuntimeError, match="did not return"):
        diagnose.wait_for_xvf(host_cmd, timeout_s=0.5)


def test_wait_for_xvf(host_cmd):
    assert "VERSION: [2, 1, 1]" in diagnose.wait_for_xvf(host_cmd, timeout_s=10)


def test_flash_firmware_uses_runtime_dfu_without_prompting(fake_dfu, fake_repo, xvf_state, monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda msg="": prompts.append(msg) or "")
    rel = diagnose.FIRMWARES["v2.1.0_48k2ch"]["rel"]
    listing = diagnose.flash_firmware(fake_dfu, fake_repo / rel, "v2.1.0_48k2ch")
    assert "alt=1" in listing and prompts == []
    assert json.loads(xvf_state.read_text())["firmware"] == "v2.1.0_48k2ch"
    assert read_log(xvf_state.with_suffix(".dfu.log"))[-1] == ["-R", "-e", "-a", "1", "-D", str(fake_repo / rel)]


def set_state(xvf_state, **kw):
    xvf_state.write_text(json.dumps({**json.loads(xvf_state.read_text()), **kw}))


def test_flash_tolerates_macos_reset_exit_code(fake_dfu, fake_repo, xvf_state, capsys):
    set_state(xvf_state, reset_exit=251)
    rel = diagnose.FIRMWARES["v2.1.0_48k2ch"]["rel"]
    diagnose.flash_firmware(fake_dfu, fake_repo / rel, "v2.1.0_48k2ch")
    assert json.loads(xvf_state.read_text())["firmware"] == "v2.1.0_48k2ch"
    assert "exited 251 during the post-download USB reset" in capsys.readouterr().out


def test_flash_tolerates_hang_after_complete_download(fake_dfu, fake_repo, xvf_state, capsys, monkeypatch):
    set_state(xvf_state, reset_hang=5)
    real_run = diagnose.run
    monkeypatch.setattr(diagnose, "run", lambda cmd, **kw: real_run(cmd, **{**kw, "timeout": 2 if "-D" in cmd else kw.get("timeout")}))
    rel = diagnose.FIRMWARES["v2.1.0_48k2ch"]["rel"]
    diagnose.flash_firmware(fake_dfu, fake_repo / rel, "v2.1.0_48k2ch")
    assert json.loads(xvf_state.read_text())["firmware"] == "v2.1.0_48k2ch"
    assert "did not exit within 120 s; verifying the running firmware instead" in capsys.readouterr().out


def test_flash_raises_on_incomplete_download(fake_dfu, fake_repo, xvf_state):
    set_state(xvf_state, fail_download=True)
    rel = diagnose.FIRMWARES["v2.1.0_48k2ch"]["rel"]
    with pytest.raises(RuntimeError, match=r"dfu-util failed \(74\) flashing v2.1.0_48k2ch"):
        diagnose.flash_firmware(fake_dfu, fake_repo / rel, "v2.1.0_48k2ch")
    assert json.loads(xvf_state.read_text())["firmware"] == "v2.1.1_48k2ch"


def test_verify_firmware_version(host_cmd, xvf_state, capsys):
    diagnose.verify_firmware_version(host_cmd, "v2.1.1_48k2ch", (2, 1, 1))
    assert "verified: v2.1.1_48k2ch (VERSION 2.1.1)" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match=r"reports VERSION \(2, 1, 1\), expected \(2, 1, 0\)"):
        diagnose.verify_firmware_version(host_cmd, "v2.1.0_48k2ch", (2, 1, 0))


@pytest.mark.parametrize("text,expected", [(UPSTREAM_READ, (2, 1, 1)), ("VERSION 2 1 0", (2, 1, 0)), ("No device found", None)])
def test_parse_version(text, expected):
    assert diagnose.parse_version(text) == expected


def test_flash_firmware_falls_back_to_button_procedure(fake_dfu, fake_repo, xvf_state, monkeypatch, capsys):
    state = json.loads(xvf_state.read_text())
    xvf_state.write_text(json.dumps({**state, "dfu_visible": False}))

    def press_mute(msg=""):
        xvf_state.write_text(json.dumps({**json.loads(xvf_state.read_text()), "dfu_visible": True}))
        return ""
    monkeypatch.setattr("builtins.input", press_mute)
    rel = diagnose.FIRMWARES["v2.1.1_48k2ch"]["rel"]
    diagnose.flash_firmware(fake_dfu, fake_repo / rel, "v2.1.1_48k2ch")
    assert "Hold MUTE" in capsys.readouterr().out
    assert json.loads(xvf_state.read_text())["firmware"] == "v2.1.1_48k2ch"


def test_wait_for_dfu_timeout(tmp_path):
    missing = tmp_path / ("nodfu.cmd" if diagnose.os.name == "nt" else "nodfu")
    missing.write_text("@exit /b 0\r\n" if diagnose.os.name == "nt" else "#!/bin/sh\nexit 0\n")
    missing.chmod(0o755)
    with pytest.raises(RuntimeError, match="DFU device not found"):
        diagnose.wait_for_dfu(str(missing), timeout_s=0.2)


# --- audio devices -------------------------------------------------------------------------

def test_find_input_device():
    sd = FakeSoundDevice()
    assert sd.devices[diagnose.find_input_device(sd)]["name"] == sd.XVF
    sd.devices = [d for d in sd.devices if d["name"] != sd.XVF]
    with pytest.raises(RuntimeError, match="Could not find"):
        diagnose.find_input_device(sd)


def test_choose_output_device():
    sd = FakeSoundDevice()
    xvf = diagnose.find_input_device(sd)
    names = [d["name"] for d in sd.devices]
    assert diagnose.choose_output_device(sd, None, xvf) == names.index(sd.SPEAKER)
    assert diagnose.choose_output_device(sd, "display", xvf) == names.index("Display Audio")
    assert diagnose.choose_output_device(sd, str(names.index("Display Audio")), xvf) == names.index("Display Audio")
    with pytest.raises(RuntimeError, match="No output device matched"):
        diagnose.choose_output_device(sd, "nope", xvf)
    with pytest.raises(RuntimeError, match="ambiguous"):
        diagnose.choose_output_device(sd, "a", xvf)
    sd.default.device = [xvf, xvf]  # default output is the XVF itself: fall back to a speaker
    assert diagnose.choose_output_device(sd, None, xvf) == names.index(sd.SPEAKER)


def test_refresh_follows_reenumeration():
    sd = FakeSoundDevice()
    before = diagnose.find_output_by_name(sd, sd.SPEAKER)
    diagnose.refresh_audio_devices(sd)
    assert (sd.terminates, sd.inits) == (1, 1)
    after = diagnose.find_output_by_name(sd, sd.SPEAKER)
    assert after != before and sd.devices[after]["name"] == sd.SPEAKER
    with pytest.raises(RuntimeError, match="no longer available"):
        diagnose.find_output_by_name(sd, "Unplugged DAC")


def test_wait_for_audio_input_retries_until_device_appears(fake_sd, xvf_state):
    xvf = [d for d in fake_sd.devices if d["name"] == fake_sd.XVF]
    fake_sd.devices = [d for d in fake_sd.devices if d["name"] != fake_sd.XVF]
    real_init = fake_sd._initialize

    def init_late():  # the XVF only shows up on the second rescan
        real_init()
        if fake_sd.inits < 2:
            fake_sd.devices = [d for d in fake_sd.devices if d["name"] != fake_sd.XVF]
    fake_sd._initialize = init_late
    idx = diagnose.wait_for_audio_input(fake_sd, 48000, timeout_s=10)
    assert fake_sd.devices[idx]["name"] == fake_sd.XVF and fake_sd.inits == 2 and xvf
    with pytest.raises(RuntimeError, match="did not become available at 16000 Hz"):
        diagnose.wait_for_audio_input(fake_sd, 16000, timeout_s=0.2)


def test_refresh_tolerates_portaudio_errors(capsys):
    class Broken:
        def _terminate(self):
            raise OSError("boom")
    diagnose.refresh_audio_devices(Broken())
    assert "boom" in capsys.readouterr().err


def test_list_audio_devices():
    rows = diagnose.list_audio_devices(FakeSoundDevice())
    assert [r["index"] for r in rows] == list(range(4)) and rows[0]["hostapi"] == "Fake Audio"


def test_capture_and_play(fake_sd, tmp_path, rt):
    stim = tmp_path / "stim.wav"
    rt.sf.write(str(stim), 0.3 * np.sin(np.arange(48000) / 5), 48000, subtype="PCM_16")
    out = tmp_path / "rec" / "cap.wav"
    meta = diagnose.capture_and_play(fake_sd, rt.sf, np, stim, fake_sd.default.device[1], 48000, out)
    data, fs = rt.sf.read(str(out))
    assert fs == 48000 and data.shape == (int(2.5 * 48000), 2)
    assert meta["captured_frames"] == len(data) and meta["output_device_name"] == fake_sd.SPEAKER
    assert meta["input_rate"] == 48000 and meta["output_rate"] == 48000 and meta["statuses"] == []


def test_capture_rejects_wrong_rate(fake_sd, tmp_path, rt):
    with pytest.raises(RuntimeError, match="Invalid sample rate 16000"):
        diagnose.capture_only(fake_sd, rt.sf, np, 16000, tmp_path / "x.wav", duration_s=1)


def test_capture_only(fake_sd, tmp_path, rt):
    meta = diagnose.capture_only(fake_sd, rt.sf, np, 48000, tmp_path / "amb.wav", duration_s=20)
    assert meta["captured_frames"] == 20 * 48000 and meta["playback"] is False
    assert rt.sf.info(str(tmp_path / "amb.wav")).channels == 2


# --- host capture-path control ----------------------------------------------------------------

AVF_LISTING = """[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] [1] reSpeaker XVF3800 (not audio)
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] Immersed
[AVFoundation indev @ 0x1] [6] reSpeaker XVF3800 4-Mic Array
Error opening input file ."""


def test_parse_avfoundation_audio_index():
    assert diagnose.parse_avfoundation_audio_index(AVF_LISTING) == "6"
    master = ("[AVFoundation indev @ 0x1] AVFoundation audio devices:\n"
              "[AVFoundation indev @ 0x1] [0] reSpeaker XVF3800 4-Mic Array  [uid:AppleUSBAudioEngine:Seeed Studio:x:1,2] [serial:1149]\n"
              "[AVFoundation indev @ 0x1] [2] MacBook Pro Microphone  [uid:BuiltInMicrophoneDevice]")
    assert diagnose.parse_avfoundation_audio_index(master) == "0"
    assert diagnose.parse_avfoundation_audio_index(master, lambda n: n == "MacBook Pro Microphone") == "2"
    assert diagnose.parse_avfoundation_audio_index(AVF_LISTING.replace("reSpeaker XVF3800 4-Mic Array", "USB Mic")) is None


def make_fake_ffmpeg(tmp_path, body):
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(body, encoding="utf-8")
    if diagnose.os.name == "nt":
        exe = tmp_path / "ffmpeg.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "ffmpeg"
        exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
        exe.chmod(0o755)
    return str(exe)


def test_host_control_discovery(tmp_path, monkeypatch):
    exe = make_fake_ffmpeg(tmp_path, f"import sys\nsys.stderr.write({AVF_LISTING!r})\nsys.exit(1)\n")
    monkeypatch.setattr(diagnose.shutil, "which", lambda name: exe)
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Linux")
    assert diagnose.host_control_ffmpeg() is None
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Darwin")
    assert diagnose.host_control_ffmpeg() == (exe, "6")


def test_host_path_capture_stops_ffmpeg_with_q(tmp_path):
    body = ("import sys, wave\nout = sys.argv[-1]\nassert sys.argv[sys.argv.index('-i') + 1] == ':6'\n"
            "assert sys.stdin.read(1) == 'q'\nw = wave.open(out, 'wb'); w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)\n"
            "w.writeframes(b'\\x00' * 4 * 4800); w.close()\n")
    cap = diagnose.HostPathCapture(make_fake_ffmpeg(tmp_path, body), "6", tmp_path / "rec" / "x__ffmpeg.wav")
    meta = cap.stop()
    assert meta["returncode"] == 0 and (tmp_path / "rec" / "x__ffmpeg.wav").stat().st_size > 1000
