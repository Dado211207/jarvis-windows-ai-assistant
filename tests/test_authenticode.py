"""Who signed it?

`app/core/authenticode.py` answers two questions before JARVIS runs the
one executable it downloads and did not write: is the signature valid on
this machine (WinVerifyTrust's answer, and only reachable on Windows),
and does the certificate name the publisher we expect.

This file covers the second one, which is pure logic and runs anywhere.
The first is Windows-only ctypes and is exercised on the real platform;
what is asserted here is that a non-Windows machine reports "cannot
verify" as a *failure*, never as a pass.
"""

import sys

import pytest

from app.core import authenticode


# ---------------------------------------------------------------------------
# The publisher comparison.
#
# This used to be `expected.lower() in signer.lower()`. The cases below
# marked "the reason this is not a substring test" are the ones that
# accepted: an attacker who can buy a code-signing certificate does not
# need to compromise anything else if a name merely containing the right
# word is enough.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signer", [
    "Ollama",
    "ollama",
    "OLLAMA",
    "Ollama Inc.",
    "Ollama, Inc.",
    "Ollama Inc",
    "Ollama LLC",
    "Ollama Ltd.",
    "  Ollama  ",
])
def test_ollamas_own_certificate_is_accepted(signer):
    assert authenticode.publisher_matches(signer, "Ollama") is True


@pytest.mark.parametrize("signer", [
    "NotOllama Ltd",              # the reason this is not a substring test
    "Ollama Fan Club",            # the reason this is not a substring test
    "Evil Corp (ollama)",         # the reason this is not a substring test
    "Ollama Community Builds",
    "Ollamaa",
    "Olla ma",
    "Microsoft Corporation",
    "",
])
def test_anybody_else_is_refused(signer):
    assert authenticode.publisher_matches(signer, "Ollama") is False


def test_no_expected_publisher_matches_nothing():
    """An empty expectation must never read as "anyone will do"."""
    assert authenticode.publisher_matches("Ollama", "") is False
    assert authenticode.publisher_matches("", "") is False


def test_a_verdict_is_only_from_a_publisher_when_it_is_also_trusted():
    untrusted = authenticode.SignatureVerdict(
        trusted=False, signer="Ollama", detail="The signing certificate has expired.",
    )
    trusted = authenticode.SignatureVerdict(
        trusted=True, signer="Ollama", detail="Signed by Ollama.",
    )

    assert untrusted.is_from("Ollama") is False
    assert trusted.is_from("Ollama") is True
    assert trusted.is_from("Somebody Else") is False


# ---------------------------------------------------------------------------
# Unknown is not a pass
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="this asserts the non-Windows path")
def test_a_machine_that_cannot_check_reports_failure(tmp_path):
    """"We could not verify this" and "this is fine" are different
    answers, and only one of them may lead to running the file."""
    candidate = tmp_path / "OllamaSetup.exe"
    candidate.write_bytes(b"not really an executable")

    verdict = authenticode.verify(candidate, expected_publisher="Ollama")

    assert verdict.trusted is False
    assert verdict.is_from("Ollama") is False
    assert "Windows" in verdict.detail


def test_the_sha256_of_a_file_is_reported(tmp_path):
    import hashlib

    candidate = tmp_path / "thing.bin"
    candidate.write_bytes(b"hello")

    assert authenticode.sha256(candidate) == hashlib.sha256(b"hello").hexdigest()


def test_a_missing_file_has_no_digest_rather_than_raising(tmp_path):
    assert authenticode.sha256(tmp_path / "absent.bin") in ("", None)
