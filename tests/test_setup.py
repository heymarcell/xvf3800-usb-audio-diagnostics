"""Upstream download/pinning, firmware verification, dfu-util provisioning and test matrix."""
import io
import tarfile
import zipfile

import pytest

import diagnose


def test_upstream_is_pinned_to_a_commit():
    assert len(diagnose.RESPEAKER_REPO_COMMIT) == 40
    assert diagnose.REPO_ZIP_URL.endswith(f"/archive/{diagnose.RESPEAKER_REPO_COMMIT}.zip")
    assert "refs/heads" not in diagnose.REPO_ZIP_URL


def test_firmware_table_is_well_formed():
    assert list(diagnose.FIRMWARES) == ["v2.1.0_48k2ch", "v2.1.1_48k2ch", "v2.1.1_native16k"]
    for meta in diagnose.FIRMWARES.values():
        assert len(meta["sha256"]) == 64 and int(meta["sha256"], 16) >= 0
        assert meta["rel"].startswith("xmos_firmwares/usb/") and meta["rel"].endswith(".bin")
        assert meta["rate"] in (16000, 48000)


def make_archive(path, commit: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"reSpeaker_XVF3800_USB_4MIC_ARRAY-{commit}/python_control/xvf_host.py", "# host\n")
        zf.comment = commit.encode()
    path.write_bytes(buf.getvalue())


def test_ensure_repo_downloads_pinned_archive_once(tmp_path, monkeypatch):
    calls = []

    def fake_download(url, dest):
        calls.append(url)
        make_archive(dest, diagnose.RESPEAKER_REPO_COMMIT)

    monkeypatch.setattr(diagnose, "download", fake_download)
    repo = diagnose.ensure_repo(tmp_path)
    assert repo.name == f"respeaker-repo-{diagnose.RESPEAKER_REPO_COMMIT[:12]}"
    assert (repo / "python_control" / "xvf_host.py").is_file()
    assert diagnose.ensure_repo(tmp_path) == repo
    assert calls == [diagnose.REPO_ZIP_URL]
    assert diagnose.locate_xvf_host(repo)[1] == str(repo / "python_control/xvf_host.py")


def test_ensure_repo_rejects_wrong_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose, "download", lambda url, dest: make_archive(dest, "f" * 40))
    with pytest.raises(RuntimeError, match="expected " + diagnose.RESPEAKER_REPO_COMMIT):
        diagnose.ensure_repo(tmp_path)


def test_locate_xvf_host_requires_script(tmp_path):
    with pytest.raises(RuntimeError, match="xvf_host.py not found"):
        diagnose.locate_xvf_host(tmp_path)


def test_firmware_paths_verifies_hashes(fake_repo):
    deep = diagnose.firmware_paths(fake_repo, True)
    assert [r[0] for r in deep] == ["v2.1.0_48k2ch", "v2.1.1_48k2ch", "v2.1.1_native16k"]
    assert [r[2] for r in deep] == [48000, 48000, 16000]
    assert [r[0] for r in diagnose.firmware_paths(fake_repo, False)] == ["v2.1.1_48k2ch", "v2.1.1_native16k"]
    (fake_repo / diagnose.FIRMWARES["v2.1.1_48k2ch"]["rel"]).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="Refusing to flash"):
        diagnose.firmware_paths(fake_repo, False)
    (fake_repo / diagnose.FIRMWARES["v2.1.1_48k2ch"]["rel"]).unlink()
    with pytest.raises(RuntimeError, match="Firmware missing"):
        diagnose.firmware_paths(fake_repo, False)


def names(routes):
    return [r["name"] for r in routes]


def test_route_matrix():
    assert names(diagnose.routes_for("v2.1.0_48k2ch", 48000, True)) == ["normal"]
    assert names(diagnose.routes_for("v2.1.1_48k2ch", 48000, True)) == [
        "normal", "raw", "pre_shf", "shf_input", "processed_direct", "processed_no_upsample"]
    assert names(diagnose.routes_for("v2.1.1_native16k", 16000, True)) == ["normal", "raw"]
    assert names(diagnose.routes_for("v2.1.1_48k2ch", 48000, False)) == ["normal", "raw", "pre_shf", "shf_input"]
    assert names(diagnose.routes_for("v2.1.1_native16k", 16000, False)) == ["normal"]


def test_routes_use_documented_categories():
    # python_control/readme.md: 1 raw mics, 3 mics+gain+delay (SHF input), 6 processed beams,
    # 7 AEC residual/ASR beams, 8 user-selected output, 11 mics+gain before delay.
    by = {r["name"]: r for r in diagnose.ROUTES_48K}
    assert by["raw"]["left"][0] == 1 and by["shf_input"]["left"][0] == 3 and by["pre_shf"]["left"][0] == 11
    assert by["normal"]["left"] == (8, 0) and by["normal"]["right"] == (7, 3)
    assert by["processed_direct"]["left"] == (6, 3) and by["processed_no_upsample"]["upsample"] == (0, 0)


def test_ensure_dfu_util_prefers_path(monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda name: "/opt/bin/dfu-util")
    assert diagnose.ensure_dfu_util(None) == "/opt/bin/dfu-util"


def test_ensure_dfu_util_windows_download(tmp_path, monkeypatch):
    def fake_download(url, dest):
        assert url == diagnose.DFU_BINARIES_URL
        with tarfile.open(dest, "w:xz") as tf:
            for arch in ("win32", "win64"):
                data = f"{arch} exe".encode()
                info = tarfile.TarInfo(f"dfu-util-0.11-binaries/{arch}/dfu-util.exe")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    monkeypatch.setattr(diagnose.shutil, "which", lambda name: None)
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Windows")
    monkeypatch.setattr(diagnose, "download", fake_download)
    exe = diagnose.ensure_dfu_util(tmp_path)
    assert exe.endswith("dfu-util.exe") and "win64" in exe
    assert diagnose.ensure_dfu_util(tmp_path) == exe


def test_windows_dfu_util_choice_ignores_listing_order(tmp_path):
    for arch in ("win32", "win64", "aarch64"):
        (tmp_path / arch).mkdir()
        (tmp_path / arch / "dfu-util.exe").write_text(arch)
    assert diagnose.find_windows_dfu_util(tmp_path) == tmp_path / "win64" / "dfu-util.exe"
    (tmp_path / "win64" / "dfu-util.exe").unlink()
    assert diagnose.find_windows_dfu_util(tmp_path) == tmp_path / "aarch64" / "dfu-util.exe"
    assert diagnose.find_windows_dfu_util(tmp_path / "missing") is None


def test_ensure_dfu_util_mac_without_homebrew(monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda name: None)
    monkeypatch.setattr(diagnose.platform, "system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="Homebrew"):
        diagnose.ensure_dfu_util(None)


def test_in_managed_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("XVFDIAG_VENV", raising=False)
    assert not diagnose.in_managed_venv(tmp_path)
    monkeypatch.setenv("XVFDIAG_VENV", str(tmp_path / diagnose.VENV_DIR_NAME))
    assert diagnose.in_managed_venv(tmp_path)
    diagnose.bootstrap_venv(tmp_path / "diagnose.py")  # already inside: must return, not exec


def test_run_reports_failures():
    import sys
    ok = diagnose.run([sys.executable, "-c", "print('hi')"])
    assert ok.stdout.strip() == "hi"
    with pytest.raises(RuntimeError, match=r"Command failed \(3\)"):
        diagnose.run([sys.executable, "-c", "import sys; print('bad'); sys.exit(3)"])
    assert diagnose.run([sys.executable, "-c", "raise SystemExit(4)"], check=False).returncode == 4
