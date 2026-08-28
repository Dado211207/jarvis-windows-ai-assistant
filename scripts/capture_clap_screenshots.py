"""Capture the Voice page in every double-clap state, deterministically.

Run:

    JARVIS_TEST_CHROMIUM_PATH=/path/to/chrome \\
        python scripts/capture_clap_screenshots.py

Writes PNGs into docs/screenshots/. Every one is produced by driving the
real page in a real Chromium against a real in-process JARVIS server —
these are screenshots, not mock-ups.

**No personal data.** The microphone list is replaced, in the page,
before anything reads it, with two invented devices ("Headset Microphone
(Studio 2)" and "Built-in Microphone Array"). Nothing here can capture a
real device name, an API key, a username or a filesystem path: the
ElevenLabs card is photographed in its unconfigured state, and no key is
ever entered.

The tray section is different and is labelled as such. A Windows
notification-area menu cannot be photographed from a Linux container, so
what is written is the exact set of menu strings
`app/launcher/tray.py::build_menu_entries` produces for each state,
rendered as text. It is evidence about the strings, not about the
Windows shell; the real menu stays on the physical-PC checklist.
"""

import math
import random
import struct
import sys
import threading
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "docs" / "screenshots"
PORT = 5561
BASE_URL = f"http://127.0.0.1:{PORT}"
RATE = 48000

# Invented, so a screenshot can never carry somebody's hardware.
FAKE_DEVICES = [
    ("clap-demo-device-a", "Headset Microphone (Studio 2)"),
    ("clap-demo-device-b", "Built-in Microphone Array"),
]


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def _write_wav(path: Path, samples) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples
        ))
    return path


def _floor(n, seed=3):
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) * 0.004 for _ in range(n)]


def _clap(seed=1):
    rng = random.Random(seed)
    n = int(RATE * 0.15)
    return [rng.uniform(-1, 1) * math.exp(-i / (RATE * 0.012)) * 0.95 for i in range(n)]


def _mix(base, overlay, at_seconds):
    start = int(RATE * at_seconds)
    for i, value in enumerate(overlay):
        if start + i < len(base):
            base[start + i] += value
    return base


def quiet_clip(directory: Path) -> Path:
    return _write_wav(directory / "quiet.wav", _floor(int(RATE * 40)))


def one_clap_clip(directory: Path) -> Path:
    """A single clap, eight seconds in: enough for the "first clap
    detected" frame, and never a pair."""
    audio = _floor(int(RATE * 40))
    return _write_wav(directory / "one_clap.wav", _mix(audio, _clap(seed=5), 8.0))


def pair_clip(directory: Path) -> Path:
    audio = _floor(int(RATE * 40))
    audio = _mix(audio, _clap(seed=11), 8.00)
    audio = _mix(audio, _clap(seed=12), 8.26)
    return _write_wav(directory / "pair.wav", audio)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

# Runs before any of the page's own scripts. Replacing enumerateDevices
# rather than editing the screenshot afterwards is what makes "no real
# device name can appear" a property of the capture rather than a promise
# about it.
FAKE_DEVICE_SCRIPT = """
(() => {
  const fakes = __DEVICES__;
  const md = navigator.mediaDevices;
  const real = md.enumerateDevices.bind(md);
  md.enumerateDevices = async function () {
    const actual = await real();
    const kept = actual.filter(d => d.kind !== "audioinput");
    return kept.concat(fakes.map(f => ({
      kind: "audioinput", deviceId: f[0], groupId: "demo", label: f[1],
      toJSON() { return this; },
    })));
  };
})();
"""

DENY_MICROPHONE_SCRIPT = """
(() => {
  const md = navigator.mediaDevices;
  md.getUserMedia = function () {
    const err = new Error("Permission denied");
    err.name = "NotAllowedError";
    return Promise.reject(err);
  };
})();
"""


def start_server():
    import uvicorn

    from app.config import settings
    settings.jarvis_port = PORT
    from app.api.server import app as jarvis_app

    config = uvicorn.Config(jarvis_app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="screenshot-server")
    thread.start()

    import httpx
    for _ in range(75):
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=1.0).status_code == 200:
                return server, thread
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit("the screenshot server did not start")


def shot(page, selector, name):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    page.locator(selector).screenshot(path=str(OUT_DIR / f"{name}.png"))
    print(f"  wrote docs/screenshots/{name}.png")


def new_page(playwright, wav, deny_microphone=False):
    import json
    import os

    browser = playwright.chromium.launch(
        executable_path=os.environ.get("JARVIS_TEST_CHROMIUM_PATH") or None,
        args=[
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}%noloop",
            "--autoplay-policy=no-user-gesture-required",
        ],
    )
    context = browser.new_context(
        permissions=["microphone"], viewport={"width": 900, "height": 1400},
    )
    page = context.new_page()
    page.add_init_script(FAKE_DEVICE_SCRIPT.replace("__DEVICES__", json.dumps(FAKE_DEVICES)))
    if deny_microphone:
        page.add_init_script(DENY_MICROPHONE_SCRIPT)
    return browser, page


def goto_voice(page, wait_listening=True):
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    if wait_listening:
        page.wait_for_function("clapListening() === true", timeout=20000)


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------

def capture(playwright, tmp: Path):
    from app.core.privacy import privacy_mode
    from app.voice import clap

    quiet = quiet_clip(tmp)

    # 1. Off — the state it ships in.
    clap.reset_for_tests()
    clap.set_enabled(False)
    privacy_mode.set(False)
    browser, page = new_page(playwright, quiet)
    goto_voice(page, wait_listening=False)
    page.wait_for_timeout(1500)
    shot(page, "#clap-card", "01-clap-disabled")
    browser.close()

    # 2. Listening. 3. Temporarily paused. 4. Privacy-paused.
    clap.set_enabled(True)
    browser, page = new_page(playwright, quiet)
    goto_voice(page)
    page.wait_for_timeout(800)
    shot(page, "#clap-card", "02-clap-listening")

    page.evaluate("clapSuspend('speaking')")
    page.wait_for_function("clapState() === 'suspended'", timeout=10000)
    shot(page, "#clap-card", "03-clap-temporarily-paused")
    page.evaluate("clapResume('speaking')")
    page.wait_for_function("clapListening() === true", timeout=15000)

    # The selected microphone, and the one actually open.
    page.evaluate(f"setSharedMicrophone('{FAKE_DEVICES[0][0]}')")
    page.wait_for_timeout(1500)
    page.click("#diag-refresh")
    page.wait_for_timeout(1500)
    shot(page, "#voice-diagnostics-card", "11-selected-microphone")
    page.evaluate("setSharedMicrophone('')")
    page.wait_for_function("clapListening() === true", timeout=15000)

    page.evaluate("setPrivacyMode(true)")
    page.wait_for_function("clapState() === 'privacy-blocked'", timeout=15000)
    shot(page, "#clap-card", "04-clap-privacy-paused")
    page.evaluate("setPrivacyMode(false)")
    page.wait_for_function("clapListening() === true", timeout=20000)
    browser.close()
    privacy_mode.set(False)

    # 5. The chosen microphone is gone — fallback, said out loud.
    clap.set_device_id("a-microphone-that-is-not-plugged-in")
    browser, page = new_page(playwright, quiet)
    goto_voice(page)
    page.wait_for_timeout(1000)
    shot(page, "#clap-card", "05-clap-fallback-device")
    page.click("#diag-refresh")
    page.wait_for_timeout(1500)
    shot(page, "#voice-diagnostics-card", "05b-diagnostics-missing-device")
    browser.close()
    clap.set_device_id("")

    # 6. Microphone unavailable — the permission refused outright.
    browser, page = new_page(playwright, quiet, deny_microphone=True)
    goto_voice(page, wait_listening=False)
    page.wait_for_function("clapState() === 'microphone-unavailable'", timeout=20000)
    page.wait_for_timeout(500)
    shot(page, "#clap-card", "06-clap-microphone-unavailable")
    browser.close()

    # 7. Calibration ready. 8. First clap detected.
    browser, page = new_page(playwright, one_clap_clip(tmp))
    goto_voice(page)
    shot(page, "#clap-card", "07-calibration-ready")
    page.click("#clap-cal-start")
    page.wait_for_function("clapState() === 'calibrating'", timeout=15000)
    page.wait_for_timeout(300)
    shot(page, "#clap-card", "08-calibration-listening")
    page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent"
        ".indexOf('First clap detected.') === 0",
        timeout=25000,
    )
    shot(page, "#clap-card", "09-calibration-first-clap")
    # 10. Then it runs out of time, on its own, and lets go.
    page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent"
        ".indexOf('microphone was released') !== -1",
        timeout=40000,
    )
    shot(page, "#clap-card", "10-calibration-timeout")
    browser.close()

    # 11. A real pair, measured, with a proposal nothing has saved yet.
    browser, page = new_page(playwright, pair_clip(tmp))
    goto_voice(page)
    page.click("#clap-cal-start")
    page.wait_for_function(
        "document.getElementById('clap-cal-proposal').textContent.indexOf('Proposed:') === 0",
        timeout=30000,
    )
    shot(page, "#clap-card", "12-calibration-pair-accepted")
    page.click("#clap-cal-save")
    page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent.indexOf('Saved.') === 0",
        timeout=15000,
    )
    shot(page, "#clap-card", "13-calibration-saved")
    page.click("#clap-cal-reset")
    page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent.indexOf('Reset') === 0",
        timeout=15000,
    )
    shot(page, "#clap-card", "14-calibration-reset")
    browser.close()

    # 12. The optional cloud tier, unconfigured. No key is ever entered
    # here, and the page has no endpoint that would return one.
    browser, page = new_page(playwright, quiet)
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    page.wait_for_timeout(2000)
    shot(page, "#cloud-voice-card", "15-elevenlabs-unconfigured")
    browser.close()

    clap.set_enabled(False)
    clap.reset_for_tests()


# ---------------------------------------------------------------------------
# The tray — strings, not a photograph
# ---------------------------------------------------------------------------

def write_tray_menus() -> None:
    from app.launcher.tray import TrayState, build_menu_entries
    from app.voice import clap

    noop = lambda: None  # noqa: E731
    cases = [
        ("On", "listening", False),
        ("Paused by Privacy Mode", "listening", True),
        ("Temporarily paused", "suspended", False),
        ("Microphone unavailable", "unknown", False),
        ("Off", "disabled", False),
    ]

    lines = [
        "# Tray menu — what it says in each double-clap state",
        "",
        "Generated by `scripts/capture_clap_screenshots.py`. These are the",
        "exact strings `app/launcher/tray.py::build_menu_entries` produces,",
        "with the clap line composed by `app/voice/clap.py::tray_label()`.",
        "",
        "**This is not a photograph of the Windows notification area.** A",
        "Windows tray menu cannot be captured from the Linux container this",
        "was generated in, so what is shown is the menu's content, not the",
        "shell's rendering of it. Seeing the real menu is item 48 on",
        "`docs/physical-pc-checklist.md`.",
        "",
        "A `·` marks a disabled row — a status line, never a control.",
        "",
    ]

    for title, label_state, privacy in cases:
        clap.reset_for_tests()
        clap.set_enabled(label_state != "disabled")
        if label_state not in ("unknown", "disabled"):
            clap.report_listener_state(label_state)
        from app.core.privacy import privacy_mode
        privacy_mode.set(privacy)
        state = TrayState(status="Running", privacy_active=privacy, clap_label=clap.tray_label())
        privacy_mode.set(False)

        lines.append(f"## {title}")
        lines.append("")
        lines.append("```")
        for entry in build_menu_entries(state, noop, noop, noop, noop, noop, noop):
            lines.append(("· " if not entry.enabled else "  ") + entry.label)
        lines.append("```")
        lines.append("")

    clap.set_enabled(False)
    clap.reset_for_tests()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "tray-menu-states.md").write_text("\n".join(lines), encoding="utf-8")
    print("  wrote docs/screenshots/tray-menu-states.md")


def main() -> int:
    import tempfile

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)

        # Settings are written here, not into whatever this machine's
        # real JARVIS config happens to say. Running this script must not
        # change how the developer's own copy behaves.
        settings_dir = tmp / "config"
        settings_dir.mkdir()
        import app.core.preferences as preferences
        preferences.config_dir = lambda: settings_dir

        server, thread = start_server()
        try:
            with sync_playwright() as playwright:
                capture(playwright, tmp)
            write_tray_menus()
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
