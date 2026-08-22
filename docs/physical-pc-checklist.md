# The physical-PC checklist

Everything here needs the real hardware. **None of it has been performed
by any automated run**, and this release candidate is not finished until
somebody has done it on a Windows machine and said so.

Two things this list is not: it is not a substitute for the automated
suite (2,313 tests, 104 real-browser tests including accessibility, a
Windows CI smoke job and a ten-cycle installed-artifact lifecycle test
all pass), and it is not a formality. Every item on it is something no
environment this project builds in can observe.

If anything fails, capture this before changing anything:

```
%LOCALAPPDATA%\JARVIS\data\logs\jarvis.log
%LOCALAPPDATA%\JARVIS\boot_trace.log
Get-Process | Where-Object { $_.Name -match 'JARVIS|msedgewebview2' }
```

---

## A. Install and lifecycle

1. Verify the downloaded installer against **its own** `.sha256` file.
   Two builds of the same commit are not byte-identical — Inno Setup
   stamps a build time into the file — so never check one build against
   another build's checksum.
2. **Upgrade directly over the previously installed RC**, without
   uninstalling first. Settings, chat history and any downloaded voice
   or speech model must survive.
3. The **native JARVIS window opens**, and no unwanted browser tab opens
   with it.
4. A **second launch brings the existing window forward** rather than
   starting anything new.
5. **Restart** from the UI and from the tray.
6. **Quit** from the UI and from the tray, then confirm no `JARVIS.exe`,
   `msedgewebview2.exe` or port-owning process remains.
7. **Uninstall, preserving data.** Reinstall, then **uninstall with full
   data removal** and confirm `%LOCALAPPDATA%\JARVIS` is gone.
8. Confirm Ollama and its models are **still installed** after an
   uninstall, even if JARVIS installed Ollama.

## B. Voice — the local side

9. Test **Kokoro speech**: `bm_george`, Test Voice, Stop, and
   interrupting one reply with another.
10. Select and test an available **Windows natural voice**.
11. Test the **physical microphone** and push-to-talk transcription on
    the Chat page.
12. On the Voice page, run **Test microphone** and confirm the level
    meter moves and the result is accurate.

## C. Voice — the premium cloud tier (new this pass)

Everything here needs a real ElevenLabs account. No automated test in
this repository has ever contacted ElevenLabs, and none ever will.

13. Paste a real API key and press **Save key**. Confirm the field
    clears, the status reads *Saved in the Windows Credential Manager*,
    and the key appears in Windows Credential Manager under **JARVIS /
    elevenlabs_api_key**.
14. Press **Check key**. A valid key reports success; a key with the
    wrong characters reports *invalid*, not *offline*.
15. Press **Load voices**. Confirm the list is the voices **that
    account** actually has.
16. **Choose the voice.** This is a human judgement and nothing
    automated can stand in for it — listen to the candidates and pick
    the one that matches the brief in
    `docs/clean-room-and-voice-identity.md`: cinematic British male,
    refined modern RP, low-mid pitch (~120–140 Hz), warm, precise,
    calm, restrained.
17. Press **Test cloud voice** and listen to *"Good evening, sir. All
    systems are online and ready."* Adjust stability / similarity /
    style / speed and re-test. The suggested starting point is
    0.50 / 0.75 / 0.00 / 0.92 with speaker boost on.
18. Switch **Speak with** to the ElevenLabs voice, ask JARVIS something
    on the Chat page, and confirm the reply is spoken in that voice.
19. **Turn privacy mode on.** Confirm the cloud voice refuses, says
    privacy mode is why, and that nothing is sent.
20. Turn the **fallback toggle off**, then make the cloud voice fail
    (turn off Wi-Fi). Confirm JARVIS stays silent and reports the
    reason rather than quietly using the local voice. Turn it back on
    and confirm the local voice covers and says so.
21. Press **Remove key**. Confirm the Credential Manager entry is gone
    and the local voice still works perfectly.
22. Check the log file and the Diagnostics page: **the key must appear
    in neither**, in any form.

## D. Clap to activate (new this pass)

The detector is verified against synthesised audio in a real browser
(`tests/test_clap_detection.py`). What that cannot tell us is how it
behaves in a real room, with a real microphone, in a hidden WebView2
window on Windows — which is exactly what items 24 and 26 are for.

23. Switch **Clap to bring JARVIS forward** on. Confirm Windows asks for
    microphone permission if it has not already, and that the Voice page
    reports *Listening*.
24. Minimise JARVIS to the tray, wait a minute, then **clap twice**.
    The window should come forward. *This is the item that cannot be
    verified anywhere but here:* Chromium does not throttle the audio
    thread and does not freeze a page that is capturing, but that is
    reasoning from documented behaviour, not a measurement of WebView2
    on a real desktop.
25. **Talk near the microphone for a minute.** Play music. Type loudly.
    Nothing should activate. If something does, drop the sensitivity to
    Low and try again — and report it, because that is a real result.
26. Clap twice in the **actual room** it will be used in, from the
    **actual distance**, at each sensitivity. Find the one that works
    and note it.
27. With **Speak replies aloud** on, confirm the greeting is spoken on
    activation. Turn *Say something when it activates* off and confirm
    the window still appears, silently.
28. **Turn privacy mode on.** Confirm the Voice page stops reporting
    *Listening* and clapping does nothing.
29. Switch the feature **off**. Confirm Windows no longer shows JARVIS
    as using the microphone (the taskbar microphone indicator).
30. **Quit JARVIS and clap.** Nothing should happen — this is expected
    and documented: the listener lives in the window.

## E. Local AI (unchanged this pass, worth re-checking after the download changes)

31. On a machine without Ollama, run the guided install. Confirm the
    plan screen names the source, publisher, licence, size and free
    space *before* anything is fetched.
32. Confirm the signature check passes and the SHA-256 is shown, and
    that Ollama's own installer runs **visibly** with a UAC prompt.
33. On a machine that already has Ollama, confirm JARVIS uses it and
    never reinstalls over it.

## F. The whole point

34. Use it for an hour as a person, not a tester. Ask it things. Have it
    speak. Clap at it. The bar is not "no errors" — it is whether it
    feels like a product.

---

**Report back what failed and what surprised you.** An item that passed
but felt wrong is worth as much as one that failed.
