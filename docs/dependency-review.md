# Dependency and supply-chain review

Two questions this file answers, because both were asked and both have
checkable answers rather than reassuring ones:

1. What did this pass add?
2. What is in the product now, under what licence, and how big is it?

---

## 1. What this pass added: nothing

**No new runtime, packaging or test dependency was introduced by the
premium-voice / double-clap pass.** Checkable:

```
$ git diff --stat <pass-start>..HEAD -- requirements.txt \
      requirements-windows.txt requirements-test.txt
(no output)
```

Every module added or changed in this pass imports only the standard
library (`json`, `re`, `threading`, `time`, `wave`, `struct`,
`contextlib`, `dataclasses`, `typing`, `urllib.parse`), something
already in `requirements.txt` (`httpx`), or another JARVIS module.

This was a design constraint, not luck:

| Feature | Obvious dependency | What was used instead |
|---|---|---|
| ElevenLabs cloud voice | the `elevenlabs` SDK | `httpx`, already present. Two endpoints and one audio stream do not justify a client library, a transitive tree, and a second HTTP stack whose redirect and timeout behaviour would have to be audited separately. `app/voice/elevenlabs.py` is ~490 lines written against ElevenLabs' own published API reference. |
| Double-clap listening | PyAudio / sounddevice (PortAudio) | Nothing. The detector is an `AudioWorkletProcessor` in the page that is already open. See `docs/double-clap-activation.md` for why a native audio-input dependency was rejected: it could be tested neither on GitHub's Windows runners (no audio device) nor in this project's Linux build container, and the requirement was that its lifecycle be tested. |
| Clap detector's test | a recorded WAV fixture | Audio synthesised in the test itself, played through Chromium's `--use-file-for-fake-audio-capture`. No binary fixture to trust; every waveform is described by the code that makes it. |

**JARVIS still has no native audio *input* dependency of any kind.** No
PortAudio, no PyAudio, no sounddevice. Playback remains stdlib
`winsound`, output only. A test asserts their absence
(`tests/test_speak_intent.py`).

---

## 2. The inventory

`docs/THIRD_PARTY_NOTICES.md` carries the full reasoning for each of the
packaging-only dependencies — why `pywin32` and not `pystray`, why
`pywebview` needs `pythonnet`, what ONNX Runtime brings with it. This is
the summary table.

### Runtime (`requirements.txt`) — installed everywhere

| Package | Licence | Why it is here |
|---|---|---|
| fastapi | MIT | The loopback HTTP API |
| uvicorn[standard] | BSD-3-Clause | Serves it |
| jinja2 | BSD-3-Clause | Dashboard templates |
| pydantic, pydantic-settings | MIT | Typed settings and request models |
| httpx | BSD-3-Clause | Every outbound HTTP call: Ollama, Hugging Face, ElevenLabs |
| psutil | BSD-3-Clause | System metrics, and the process-tree walk that shutdown depends on |
| pillow | MIT-CMU | The tray icon |
| python-dotenv | BSD-3-Clause | `.env` in development |
| anthropic | MIT | Claude, the default provider |
| onnxruntime | MIT | The local neural voice |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Pulled in by onnxruntime; not optional |
| pyttsx3 | MPL-2.0 | The last-resort speech tier only |
| python-multipart | Apache-2.0 | The push-to-talk audio upload |
| pytest | MIT | Tests (in `requirements.txt` historically; not shipped) |

### Windows packaging only (`requirements-windows.txt`)

| Package | Pin | Licence | Why |
|---|---|---|---|
| pywin32 | 312 | PSF-2.0 | Tray icon via raw `Shell_NotifyIcon`; chosen over pystray to keep LGPL out of the build entirely |
| keyring | 25.7.0 | MIT | Windows Credential Manager, for both API keys |
| winsdk | 1.0.0b10 | MIT | Windows' own natural voices (speech tier 2) |
| pywebview | 6.2.1 | BSD-3-Clause | The native window |
| pythonnet | 3.1.0 | MIT | pywebview's only Windows backend needs the CLR |
| clr-loader | ≥0.2.6 | MIT | How pythonnet loads it |
| faster-whisper | 1.2.0 | MIT | Push-to-talk transcription |
| ctranslate2 | (via faster-whisper) | MIT | Its inference engine |
| huggingface-hub | ≥0.13 | Apache-2.0 | Imported by faster-whisper at package-import time |
| requests | ≥2.31.0 | Apache-2.0 | Likewise, transitively; named here because a packaged app that cannot start is not a dependency to leave to resolution |

### Test only (`requirements-test.txt`) — never shipped

| Package | Licence | Why |
|---|---|---|
| playwright | Apache-2.0 | The real-browser suite, including the clap detector's audio tests |
| axe-playwright-python | MIT | Accessibility assertions |
| axe-core (`axe.min.js`, vendored inside the above) | MPL-2.0 | Deque's own engine. File-level copyleft, unmodified, and it never leaves the test environment |

The licences in both tables above were read from the installed
distributions' own metadata in this repository's environment
(`importlib.metadata`, and the packaged `LICENSE` file where the
metadata carries none), not from memory. `huggingface-hub` is the one
entry that could not be checked that way — it is a Windows-packaging
dependency and is not installed in the Linux dev/CI environment — so its
Apache-2.0 is taken from the project's own published licence and is
marked here as the one unverified-locally row.

### Downloaded on request, never bundled

| Component | Licence | Verified how |
|---|---|---|
| Kokoro neural voice model + voice packs | Apache-2.0 | SHA-256 pinned in `app/voice/kokoro/assets.py`, checked before install |
| CMU Pronouncing Dictionary | CMU's own text (`docs/licences/CMUDICT-LICENSE.txt`) | Licence text pinned by hash; the acknowledgement it asks for ships |
| faster-whisper speech model | MIT (model card) | `model.bin` by SHA-256 from Hugging Face's API; three small non-LFS files by byte count — stated plainly in `app/voice/model_installer.py` rather than presented as equally verified |
| Ollama (optional) | MIT | Host-pinned HTTPS, every redirect checked before it is followed, Authenticode signature must name Ollama, SHA-256 shown. Never bundled, never removed by JARVIS's uninstaller |

### Shipped by the installer but never removed by it

WebView2 Runtime and the Visual C++ redistributable are shared Windows
components. JARVIS will install them if absent and will never uninstall
them — see `app/core/ownership.py`.

---

## 3. Licensing posture, and what enforces it

- **No copyleft in the shipped tree.** `pystray` (LGPL-3.0) was removed
  rather than shipped with an unreviewed compliance argument; the
  reasoning is in `docs/THIRD_PARTY_NOTICES.md` and is worth reading
  before anyone reintroduces an LGPL library.
- `pyttsx3` (MPL-2.0) is file-level copyleft: unmodified use imposes no
  obligation on the rest of the product, and it is not modified. The
  same applies to axe-core, which is MPL-2.0 and vendored inside the
  MIT-licensed `axe-playwright-python` — and which is a test-only
  dependency that never reaches the installer.
- **`tests/test_licence_policy.py` enforces this mechanically**, not by
  convention: it reads the requirement files, refuses a forbidden
  package, refuses a forbidden import anywhere under `app/`, checks the
  PyInstaller spec collects nothing forbidden, checks every download URL
  is pinned and checksummed, verifies the CMU licence text against a
  hash, and — in the Windows Installer job, where a packaged tree
  exists — checks no forbidden binary or package directory shipped.

## 4. Supply-chain posture

- **Everything downloaded at runtime is verified before use**, by
  checksum (models) or code signature (the one executable). Nothing is
  fetched on startup, on a status read, or as a side effect.
- **`app/core/safe_fetch.py` checks each redirect before following it.**
  The consent screens name a source; this is what makes that sentence
  true rather than aspirational.
- **Pins are exact where the wheel matters** (pywin32, keyring, winsdk,
  pywebview, pythonnet, faster-whisper) and floors where a security
  update should be picked up (httpx, requests, huggingface-hub).
- **No new package was added by this pass**, which is the cheapest
  supply-chain outcome available and the reason section 1 leads.
