"""The committed voice fixture and its metadata must agree byte-for-byte."""
import json
import wave

import diagnose
from conftest import CANONICAL_VOICE, REPO_ROOT

VOICE_DIR = REPO_ROOT / "assets" / "voice"
EXPECTED_SHA = "2a94eeb70177de6c22db7a1253c5dacf41a7b6203d28dd5bf4027cbc6e61b15b"


def test_fixture_hash_matches_every_record():
    actual = diagnose.sha256_file(CANONICAL_VOICE)
    assert actual == EXPECTED_SHA
    assert (VOICE_DIR / "SHA256SUMS.txt").read_text(encoding="utf-8").split() == [EXPECTED_SHA, "xvf3800-test-vo.wav"]
    assert json.loads((VOICE_DIR / "provenance.json").read_text(encoding="utf-8"))["sha256"] == EXPECTED_SHA
    for doc in ("README.md", "PUBLISHING.md", "assets/voice/README.md"):
        assert EXPECTED_SHA in (REPO_ROOT / doc).read_text(encoding="utf-8"), doc


def test_provenance_matches_wav_header():
    prov = json.loads((VOICE_DIR / "provenance.json").read_text(encoding="utf-8"))
    with wave.open(str(CANONICAL_VOICE)) as w:
        assert w.getframerate() == prov["audio"]["sample_rate_hz"] == 48000
        assert w.getnchannels() == prov["audio"]["channels"] == 1
        assert w.getsampwidth() * 8 == prov["audio"]["bits_per_sample"] == 16
        assert round(w.getnframes() / w.getframerate(), 2) == prov["audio"]["duration_seconds"]
    assert CANONICAL_VOICE.stat().st_size == prov["size_bytes"]
    assert prov["canonical_file"] == CANONICAL_VOICE.name
    for key in ("script_file",):
        assert (VOICE_DIR / prov[key]).is_file()
    assert (VOICE_DIR / prov["voice_design"]["prompt_file"]).is_file()


def test_embedded_script_matches_committed_script():
    assert (VOICE_DIR / "script.txt").read_text(encoding="utf-8") == diagnose.VOICE_SCRIPT + "\n"


def test_write_assets_restores_missing_script(tmp_path):
    diagnose.write_assets(tmp_path)
    assert (tmp_path / "assets" / "voice" / "script.txt").read_text(encoding="utf-8") == diagnose.VOICE_SCRIPT + "\n"
    (tmp_path / "assets" / "voice" / "script.txt").write_text("keep", encoding="utf-8")
    diagnose.write_assets(tmp_path)
    assert (tmp_path / "assets" / "voice" / "script.txt").read_text(encoding="utf-8") == "keep"
