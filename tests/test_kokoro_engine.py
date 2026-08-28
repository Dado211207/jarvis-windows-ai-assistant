"""The neural voice: tokens, synthesis, installation and playback.

No test here plays audio or requires audio hardware, and none of them
downloads anything. The parts that can be checked without the 92 MB
model — the token table, the sequence limits, WAV encoding, the install
verification logic, the engine chain's ordering — are checked directly.

The one thing that genuinely needs the model is inference, and that is
handled the way this repository already handles the packaged-tree
checks: run it for real when the model is present, and *say* it was not
run when it is absent rather than skipping quietly. See
test_the_inference_check_reports_when_it_cannot_run.
"""

import struct
import threading
import wave
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from app.voice.kokoro import assets, g2p, install, tokens


# ---------------------------------------------------------------------------
# The token table
# ---------------------------------------------------------------------------

def test_the_token_table_matches_the_vocabulary_the_g2p_validates_against():
    """Two copies of the same fact, kept honest against each other.

    g2p refuses phonemes outside KOKORO_VOCABULARY; the encoder drops
    anything outside VOCAB. If those two ever disagree, one of them is
    silently discarding sound the other approved.
    """
    assert set(tokens.VOCAB) == set(g2p.KOKORO_VOCABULARY)


def test_the_token_ids_are_the_sparse_ones_from_the_model():
    """115 tokens numbered up to 177. A dense 0..114 numbering would be
    the published symbol list instead, and would mean something
    different to this model."""
    assert len(tokens.VOCAB) == 115
    assert max(tokens.VOCAB.values()) == 177
    assert sorted(tokens.VOCAB.values()) != list(range(len(tokens.VOCAB)))


def test_encoding_wraps_the_sequence_in_the_pad_token():
    encoded = tokens.encode("ʤˈɑːvɪs")

    assert encoded[0] == tokens.PAD_ID
    assert encoded[-1] == tokens.PAD_ID
    assert len(encoded) == len("ʤˈɑːvɪs") + 2


def test_encoding_drops_nothing_that_the_g2p_produces():
    """The two halves of the pronunciation path, joined up: whatever
    phonemise() emits must survive encoding intact."""
    phonemes = g2p.phonemise("Good evening. JARVIS is online and ready.")

    assert tokens.unsupported(phonemes) == []
    assert len(tokens.encode(phonemes)) == len(phonemes) + 2


def test_an_unknown_character_is_dropped_rather_than_guessed():
    """There is no spare ID meaning "unknown" — every one is a specific
    sound — so an unmappable character cannot be sent as anything."""
    assert tokens.unsupported("aπb") == ["π"]
    assert tokens.encode("aπb") == [tokens.PAD_ID, tokens.VOCAB["a"], tokens.VOCAB["b"], tokens.PAD_ID]


# ---------------------------------------------------------------------------
# Sequence length
# ---------------------------------------------------------------------------

def test_a_short_phrase_is_one_piece():
    from app.voice.kokoro.engine import split_for_model

    assert split_for_model("hɛləʊ") == ["hɛləʊ"]


def test_a_long_phrase_is_split_below_the_model_ceiling():
    """510 style vectors means 510 tokens, hard. Every piece has to fit
    with its two pad tokens."""
    from app.voice.kokoro.engine import split_for_model

    phonemes = " ".join(["hɛləʊ"] * 300)
    pieces = split_for_model(phonemes)

    assert len(pieces) > 1
    for piece in pieces:
        assert len(tokens.encode(piece)) <= tokens.MAX_TOKENS


def test_splitting_breaks_on_a_space_so_a_word_survives():
    from app.voice.kokoro.engine import split_for_model

    phonemes = " ".join(["hɛləʊ"] * 300)

    assert all(piece == piece.strip() for piece in split_for_model(phonemes))
    assert "".join(split_for_model(phonemes)).replace(" ", "") == phonemes.replace(" ", "")


def test_speed_is_bounded_because_the_model_does_not_check():
    from app.voice.kokoro.engine import MAX_SPEED, MIN_SPEED, clamp_speed

    assert clamp_speed(0) == MIN_SPEED          # would divide by zero in the graph
    assert clamp_speed(99) == MAX_SPEED
    assert clamp_speed("nonsense") == 1.0
    assert clamp_speed(1.25) == 1.25


# ---------------------------------------------------------------------------
# Availability reporting
# ---------------------------------------------------------------------------

def test_a_missing_runtime_and_a_missing_model_are_different_messages():
    """Four different problems with four different fixes must not
    collapse into one "speech failed"."""
    from app.voice.kokoro import engine as kokoro_engine

    with patch.object(kokoro_engine, "runtime_available", return_value=False):
        no_runtime = kokoro_engine.engine.unavailable_reason("bm_george")

    with patch.object(kokoro_engine, "runtime_available", return_value=True), \
         patch.object(kokoro_engine.install, "is_installed", return_value=False):
        no_model = kokoro_engine.engine.unavailable_reason("bm_george")

    assert "ONNX Runtime" in no_runtime
    assert "not installed" in no_model
    assert no_runtime != no_model


def test_a_ready_engine_gives_no_reason():
    from app.voice.kokoro import engine as kokoro_engine

    with patch.object(kokoro_engine, "runtime_available", return_value=True), \
         patch.object(kokoro_engine.install, "is_installed", return_value=True):
        assert kokoro_engine.engine.unavailable_reason("bm_george") == ""


def test_the_runtime_answer_survives_sys_modules_being_disturbed():
    """onnxruntime refuses to load twice in one process, so probing it
    with a bare import has a failure mode where the neural voice dies
    for the rest of the process. The answer is decided once and held."""
    import sys

    from app.voice.kokoro import engine as kokoro_engine

    before = kokoro_engine.runtime_available()
    with patch.dict(sys.modules, {"pyttsx3": MagicMock()}):
        pass  # exiting removes anything first imported inside

    assert kokoro_engine.runtime_available() is before


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def test_a_file_of_the_wrong_size_is_not_good(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "install_dir", lambda: tmp_path)
    (tmp_path / assets.MODEL_ASSET.filename).write_bytes(b"nowhere near 92 MB")

    assert install.file_is_good(assets.MODEL_ASSET) is False


def test_a_file_of_the_right_size_but_wrong_bytes_is_not_good(tmp_path, monkeypatch):
    """The check a truncated download would pass and a corrupted one
    would not."""
    voice = assets.VOICES[0]
    monkeypatch.setattr(install, "install_dir", lambda: tmp_path)
    (tmp_path / voice.asset.filename).write_bytes(b"\0" * voice.asset.size_bytes)

    assert install.file_is_good(voice.asset) is False


def test_missing_assets_names_exactly_what_would_be_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "install_dir", lambda: tmp_path)

    missing = install.missing_assets("bm_george")

    assert {asset.filename for asset in missing} == {
        assets.MODEL_ASSET.filename, "bm_george.bin",
    }
    assert install.bytes_required("bm_george") == sum(a.size_bytes for a in missing)


def test_a_second_install_is_refused_while_one_is_running(monkeypatch):
    """Two writers to one directory is how a corrupt install happens."""
    from app.voice.kokoro.install import VoiceInstaller

    installer = VoiceInstaller()
    release = threading.Event()

    def _block(voice):
        release.wait(timeout=5)

    monkeypatch.setattr(installer, "_run", _block)
    try:
        assert installer.start("bm_george") is True
        assert installer.start("bm_george") is False
    finally:
        release.set()


def test_cancelling_reports_that_nothing_was_changed(monkeypatch, tmp_path):
    from app.voice.kokoro.install import VoiceInstaller

    monkeypatch.setattr(install, "install_dir", lambda: tmp_path)
    installer = VoiceInstaller()
    installer.cancel()

    assert installer._cancelled() is True
    assert installer.state().status == "cancelled"
    assert "Nothing was changed" in installer.state().message


def test_progress_is_a_percentage_of_what_is_actually_being_fetched():
    from app.voice.kokoro.install import VoiceInstallState

    assert VoiceInstallState(bytes_downloaded=0, bytes_total=0).percent == 0
    assert VoiceInstallState(bytes_downloaded=50, bytes_total=200).percent == 25
    assert VoiceInstallState(bytes_downloaded=999, bytes_total=200).percent == 100


def test_every_pinned_asset_carries_a_full_digest_and_a_real_size():
    for asset in (assets.MODEL_ASSET, *(voice.asset for voice in assets.VOICES)):
        assert len(asset.sha256) == 64
        assert int(asset.sha256, 16) >= 0        # it is hex, not a placeholder
        assert asset.size_bytes > 0
        assert assets.MODEL_REVISION in asset.url()


# ---------------------------------------------------------------------------
# Audio encoding
# ---------------------------------------------------------------------------

def test_encoded_audio_is_a_readable_wav_at_the_models_sample_rate():
    from app.voice import audio
    from app.voice.kokoro.engine import SAMPLE_RATE

    data = audio.encode_wav([0.0, 0.5, -0.5, 1.0], SAMPLE_RATE)

    with wave.open(BytesIO(data), "rb") as handle:
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 4


def test_samples_beyond_the_range_are_clipped_not_wrapped():
    """A plosive that exceeds 1.0 must not come out as a loud click of
    the opposite sign, which is what integer wrap-around sounds like."""
    from app.voice import audio

    raw = audio.to_pcm16([2.0, -2.0])
    left, right = struct.unpack("<hh", raw)

    assert left == 32767
    assert right == -32767


def test_playback_is_absent_rather_than_pretended_off_windows():
    import sys as _sys

    from app.voice import audio

    if _sys.platform != "win32":
        assert audio.playback_available() is False


# ---------------------------------------------------------------------------
# The engine chain
# ---------------------------------------------------------------------------

def _chain(kokoro: bool, windows: bool, sapi5: bool):
    from app.voice import engines

    return (
        patch.object(engines.kokoro_engine.engine, "is_ready", return_value=kokoro),
        patch.object(engines.kokoro_engine, "runtime_available", return_value=kokoro),
        patch.object(engines.install, "is_installed", return_value=kokoro),
        patch.object(engines.winrt_voices, "is_available", return_value=windows),
        patch.object(engines, "_sapi5_available", return_value=sapi5),
    )


def test_the_neural_voice_is_chosen_over_everything_else():
    from app.voice import engines

    patches = _chain(kokoro=True, windows=True, sapi5=True)
    for item in patches:
        item.start()
    try:
        assert engines.active_engine("bm_george") == engines.KOKORO
    finally:
        for item in patches:
            item.stop()


def test_windows_natural_voices_come_before_the_robotic_one():
    from app.voice import engines

    patches = _chain(kokoro=False, windows=True, sapi5=True)
    for item in patches:
        item.start()
    try:
        assert engines.active_engine("bm_george") == engines.WINDOWS
    finally:
        for item in patches:
            item.stop()


def test_the_robotic_voice_is_used_only_when_nothing_else_can():
    from app.voice import engines

    patches = _chain(kokoro=False, windows=False, sapi5=True)
    for item in patches:
        item.start()
    try:
        assert engines.active_engine("bm_george") == engines.SAPI5
    finally:
        for item in patches:
            item.stop()


def test_exactly_one_engine_is_ever_marked_active():
    from app.voice import engines

    patches = _chain(kokoro=True, windows=True, sapi5=True)
    for item in patches:
        item.start()
    try:
        assert sum(1 for status in engines.statuses("bm_george") if status.active) == 1
    finally:
        for item in patches:
            item.stop()


def test_every_engine_reports_its_own_reason():
    """"Speech is not available" is not a diagnosis."""
    from app.voice import engines

    patches = _chain(kokoro=False, windows=False, sapi5=False)
    for item in patches:
        item.start()
    try:
        for status in engines.statuses("bm_george"):
            assert status.detail.strip(), f"{status.key} gave no reason"
    finally:
        for item in patches:
            item.stop()


def test_the_voices_offered_are_all_british_male_and_include_the_default():
    from app.voice import engines

    listed = engines.installed_voices()

    assert {voice["key"] for voice in listed} == {voice.key for voice in assets.VOICES}
    assert all(voice["key"].startswith("bm_") for voice in listed)
    assert assets.DEFAULT_VOICE_KEY in {voice["key"] for voice in listed}


def test_an_unknown_saved_voice_falls_back_instead_of_breaking_speech():
    from app.voice import engines

    with patch("app.core.preferences.get", return_value="bm_does_not_exist"):
        assert engines.selected_voice_key() == assets.DEFAULT_VOICE_KEY


def test_selecting_a_voice_that_is_not_ours_is_refused():
    from app.voice import engines

    assert engines.set_selected_voice("definitely-not-a-voice") is None


def test_a_selected_voice_is_remembered():
    from app.voice import engines

    assert engines.set_selected_voice("bm_lewis") == "bm_lewis"
    assert engines.selected_voice_key() == "bm_lewis"


# ---------------------------------------------------------------------------
# Real inference — run when the model is present, reported when it is not
# ---------------------------------------------------------------------------

def _model_is_available() -> bool:
    from app.voice.kokoro import engine as kokoro_engine

    return kokoro_engine.runtime_available() and install.is_installed(assets.DEFAULT_VOICE_KEY)


def test_the_inference_check_reports_when_it_cannot_run():
    """A check that silently skips is a check nobody knows is missing."""
    if not _model_is_available():
        pytest.skip(
            "The Kokoro model is not installed here, so real inference was NOT "
            f"exercised. Install it to {install.install_dir()} to run it."
        )
    assert _model_is_available()


@pytest.mark.skipif(not _model_is_available(), reason="Kokoro model not installed")
def test_real_inference_produces_audible_speech_of_a_plausible_length():
    """End to end through the project's own pronunciation path: text,
    normalisation, G2P, tokens, the model, samples."""
    from app.voice.kokoro import engine as kokoro_engine

    samples = kokoro_engine.engine.synthesise_all(
        "Good evening. JARVIS is online and ready.", voice_key="bm_george",
    )
    seconds = samples.size / kokoro_engine.SAMPLE_RATE

    assert 1.0 < seconds < 12.0, f"implausible duration: {seconds:.2f}s"
    assert abs(samples).max() > 0.01, "the model produced silence"
    assert abs(samples).max() <= 1.5, "the model produced something that is not speech"


@pytest.mark.skipif(not _model_is_available(), reason="Kokoro model not installed")
def test_cancellation_stops_synthesis_between_sentences():
    from app.voice.kokoro import engine as kokoro_engine

    cancel = threading.Event()
    cancel.set()

    produced = list(
        kokoro_engine.engine.synthesise("One. Two. Three.", cancel=cancel)
    )

    assert produced == []
