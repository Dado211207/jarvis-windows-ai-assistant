"""Tests for app/launcher/runtime_check.py.

The registry read itself is Windows-only and cannot run here, so it is
exercised through an injected fake `winreg`. What matters most is the
part that is fully testable: that a *reported* failure is always
described honestly, and that "could not confirm" never becomes
"available".
"""

import sys

import pytest

from app.launcher import ipc, runtime_check


# ---------------------------------------------------------------------------
# Off Windows: not applicable, never "available"
# ---------------------------------------------------------------------------

def test_off_windows_nothing_is_claimed_available(monkeypatch):
    monkeypatch.setattr(runtime_check.sys, "platform", "linux")

    webview2 = runtime_check.webview2_status()
    dotnet = runtime_check.dotnet_status()

    assert webview2.applicable is False and webview2.available is False
    assert dotnet.applicable is False and dotnet.available is False


def test_off_windows_no_window_runtime_error_is_invented(monkeypatch):
    """A Linux test run must not report a missing WebView2 — the window
    child does not use one there."""
    monkeypatch.setattr(runtime_check.sys, "platform", "linux")
    assert runtime_check.window_runtime_error() is None


# ---------------------------------------------------------------------------
# WebView2 detection
# ---------------------------------------------------------------------------

class _FakeKey:
    def __init__(self, values):
        self._values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """The three-hive lookup, in memory. *installed* maps (hive, path) to
    the value the `pv` name holds there."""

    HKEY_LOCAL_MACHINE = 1
    HKEY_CURRENT_USER = 2

    def __init__(self, installed=None, raises=False):
        self._installed = installed or {}
        self._raises = raises
        self.opened = []

    def OpenKey(self, hive, path):  # noqa: N802 — mirrors the stdlib name
        self.opened.append((hive, path))
        if self._raises:
            raise OSError("registry is unavailable")
        if (hive, path) not in self._installed:
            raise FileNotFoundError(path)
        return _FakeKey(self._installed[(hive, path)])

    def QueryValueEx(self, key, name):  # noqa: N802
        return key._values[name], 1


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(runtime_check.sys, "platform", "win32")


def _install_fake_winreg(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "winreg", fake)


def test_a_real_version_in_the_machine_hive_counts_as_installed(monkeypatch, on_windows):
    fake = _FakeWinreg({
        (_FakeWinreg.HKEY_LOCAL_MACHINE, runtime_check.WEBVIEW2_CLIENT_KEY): {"pv": "120.0.2210.91"},
    })
    _install_fake_winreg(monkeypatch, fake)

    status = runtime_check.webview2_status()

    assert status.available is True
    assert "120.0.2210.91" in status.detail


def test_a_per_user_install_is_found_too(monkeypatch, on_windows):
    """A per-user WebView2 install is invisible in the machine hive; only
    checking one of the two would call it missing on a machine where it
    works."""
    fake = _FakeWinreg({
        (_FakeWinreg.HKEY_CURRENT_USER, runtime_check.WEBVIEW2_CLIENT_KEY): {"pv": "121.0.0.1"},
    })
    _install_fake_winreg(monkeypatch, fake)

    assert runtime_check.webview2_status().available is True


def test_the_documented_not_installed_sentinel_is_treated_as_absent(monkeypatch, on_windows):
    """Microsoft documents a "0.0.0.0" pv as explicitly meaning "not
    installed" — a present-but-empty key is not an install."""
    fake = _FakeWinreg({
        (_FakeWinreg.HKEY_LOCAL_MACHINE, runtime_check.WEBVIEW2_CLIENT_KEY): {"pv": "0.0.0.0"},
    })
    _install_fake_winreg(monkeypatch, fake)

    status = runtime_check.webview2_status()

    assert status.available is False
    assert status.fix_url == runtime_check.WEBVIEW2_DOWNLOAD_URL


def test_an_absent_key_reports_missing_with_a_download_link(monkeypatch, on_windows):
    _install_fake_winreg(monkeypatch, _FakeWinreg({}))

    status = runtime_check.webview2_status()

    assert status.available is False
    assert status.fix_url == runtime_check.WEBVIEW2_DOWNLOAD_URL
    assert "WebView2" in status.detail


def test_a_registry_error_is_not_treated_as_installed(monkeypatch, on_windows):
    """"Could not confirm" and "confirmed present" are different answers.
    Only the second may ever produce available=True."""
    _install_fake_winreg(monkeypatch, _FakeWinreg(raises=True))

    assert runtime_check.webview2_status().available is False


def test_a_registry_error_never_escapes(monkeypatch, on_windows):
    """A crash inside a diagnostic must not take the launcher down."""
    _install_fake_winreg(monkeypatch, _FakeWinreg(raises=True))

    assert runtime_check._read_webview2_version() is None  # must not raise


# ---------------------------------------------------------------------------
# Which cause is reported
# ---------------------------------------------------------------------------

def test_webview2_is_reported_before_dotnet(monkeypatch, on_windows):
    """When both are missing, the user is told about the one they can fix
    themselves in a minute."""
    monkeypatch.setattr(runtime_check, "webview2_status",
                        lambda: runtime_check.RuntimeStatus("WebView2 Runtime", False, "missing"))
    monkeypatch.setattr(runtime_check, "dotnet_status",
                        lambda: runtime_check.RuntimeStatus(".NET runtime", False, "missing"))

    assert runtime_check.window_runtime_error() == ipc.ERROR_WEBVIEW2_MISSING


def test_dotnet_is_reported_when_only_it_is_missing(monkeypatch, on_windows):
    monkeypatch.setattr(runtime_check, "webview2_status",
                        lambda: runtime_check.RuntimeStatus("WebView2 Runtime", True, "ok"))
    monkeypatch.setattr(runtime_check, "dotnet_status",
                        lambda: runtime_check.RuntimeStatus(".NET runtime", False, "missing"))

    assert runtime_check.window_runtime_error() == ipc.ERROR_DOTNET_MISSING


def test_nothing_is_reported_when_both_runtimes_are_present(monkeypatch, on_windows):
    monkeypatch.setattr(runtime_check, "webview2_status",
                        lambda: runtime_check.RuntimeStatus("WebView2 Runtime", True, "ok"))
    monkeypatch.setattr(runtime_check, "dotnet_status",
                        lambda: runtime_check.RuntimeStatus(".NET runtime", True, "ok"))

    assert runtime_check.window_runtime_error() is None


# ---------------------------------------------------------------------------
# describe() — the sentence the user actually reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("detail", sorted(ipc.VALID_ERROR_DETAILS))
def test_every_known_cause_has_its_own_message(detail):
    assert runtime_check.describe(detail).strip() != ""


def test_the_three_causes_do_not_share_one_message():
    messages = {runtime_check.describe(detail) for detail in ipc.VALID_ERROR_DETAILS}
    assert len(messages) == len(ipc.VALID_ERROR_DETAILS), (
        "collapsing distinct causes into one message is the defect this "
        "module exists to prevent"
    )


def test_an_unknown_cause_still_produces_an_honest_sentence():
    assert runtime_check.describe("something-nobody-defined") == runtime_check.WINDOW_FAILED_MESSAGE


def test_describe_does_not_re_probe_the_machine(monkeypatch):
    """describe() explains a failure the window child already reported.
    Re-probing would let the dialog contradict the report — a runtime
    installed in the seconds between the two would produce "Installed
    (version …)" as the explanation for why nothing opened."""
    monkeypatch.setattr(runtime_check, "webview2_status",
                        lambda: pytest.fail("describe() must not re-probe WebView2"))
    monkeypatch.setattr(runtime_check, "dotnet_status",
                        lambda: pytest.fail("describe() must not re-probe .NET"))

    assert runtime_check.describe(ipc.ERROR_WEBVIEW2_MISSING) == runtime_check.WEBVIEW2_MISSING_MESSAGE
    assert runtime_check.describe(ipc.ERROR_DOTNET_MISSING) == runtime_check.DOTNET_MISSING_MESSAGE


def test_only_webview2_offers_a_download(monkeypatch):
    """A broken .NET/pythonnet load is a repair-JARVIS problem, not a
    go-download-this one; offering a link that fixes nothing is worse
    than offering none."""
    assert runtime_check.fix_url_for(ipc.ERROR_WEBVIEW2_MISSING) == runtime_check.WEBVIEW2_DOWNLOAD_URL
    assert runtime_check.fix_url_for(ipc.ERROR_DOTNET_MISSING) is None
    assert runtime_check.fix_url_for(ipc.ERROR_WINDOW_FAILED) is None


def test_no_message_tells_the_user_the_browser_is_the_product():
    """The browser is an explicitly named advanced action, never the
    thing a failure dialog silently redirects people to."""
    for detail in ipc.VALID_ERROR_DETAILS:
        assert "browser" not in runtime_check.describe(detail).lower()
