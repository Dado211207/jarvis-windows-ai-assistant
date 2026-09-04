# The physical-PC checklist

This document has two halves, and the split is the point.

**Part 1 — Proven automatically.** Things a test run has actually
demonstrated, with the test that demonstrates each one named. You do not
need to re-verify these, and no claim belongs here unless a green test
backs it.

**Part 2 — Must be tested by you.** Things no environment this project
builds in can observe: real speakers, a real microphone in a real room,
Windows' own microphone indicator, the Credential Manager, a UAC prompt,
a GPU, a sleeping laptop. **None of it has been performed by any
automated run**, and this release candidate is not finished until
somebody has done it on a Windows machine and said so.

If anything fails, capture this before changing anything:

```
%LOCALAPPDATA%\JARVIS\data\logs\jarvis.log
%LOCALAPPDATA%\JARVIS\boot_trace.log
Get-Process | Where-Object { $_.Name -match 'JARVIS|msedgewebview2' }
```

---

# Part 1 — Proven automatically

Every row is a real assertion in a real run. Where the row says *real
browser*, the test drives Chromium through Playwright against the actual
page; where it says *real audio*, Chromium's capture device is a
synthesised WAV, so the whole path from the microphone down is genuine.

## Double-clap detection

| Claim | Proved by |
|---|---|
| Two claps activate; one clap, mistimed claps, speech, a hum and near-silence do not | `tests/test_clap_detection.py` (real browser, real audio) |
| The detector computes RMS and peak only — no FFT, no recogniser, no recorder | `tests/test_clap.py::test_the_worklet_never_reads_frequency_content` |
| The worklet keeps no buffer or history of audio | `tests/test_clap.py::test_the_worklet_keeps_no_audio_buffer` |
| Ordinary listening emits one payload-free message and nothing else | `tests/test_clap.py::test_the_worklet_posts_nothing_but_the_bare_fact` |
| The microphone stream is never connected to the speakers | `tests/test_clap.py::test_the_clap_stream_is_never_connected_to_the_speakers` |
| `POST /voice/clap/activate` takes no request body | `tests/test_clap.py::test_activate_takes_no_request_body` |

## Privacy mode and the microphone

| Claim | Proved by |
|---|---|
| Enabling privacy mode stops every live `MediaStreamTrack`, closes every `AudioContext` and disconnects the worklet node — asserted on the real objects, not a flag | `tests/test_clap_controller.py::test_privacy_mode_stops_the_microphone_not_just_the_label` |
| The same holds when privacy is switched from the Settings page | `…::test_privacy_mode_from_the_settings_page_stops_the_microphone` |
| The listener does not come back while privacy stays on | `…::test_the_listener_does_not_come_back_while_privacy_stays_on` |
| A clap during privacy mode reaches nothing — there is no worklet, and the server refuses | `…::test_a_clap_during_privacy_mode_cannot_reach_anything` |
| Leaving privacy mode does not switch the feature on by itself | `…::test_privacy_off_does_not_switch_the_feature_on_by_itself` |
| Starting up with privacy already on never opens the microphone at all | `…::test_startup_with_privacy_already_on_never_opens_the_microphone` |
| Three privacy on/off cycles leak no track, context, worklet, `devicechange` listener or timer | `…::test_repeated_privacy_cycles_leak_nothing` |

## The selected microphone

| Claim | Proved by |
|---|---|
| The chosen device id reaches `getUserMedia` as `deviceId: {exact: …}` — asserted on the constraints object itself | `tests/test_clap_controller.py::test_the_selected_microphone_reaches_get_user_media` |
| Changing device stops the previous stream first; there are never two | `…::test_changing_the_microphone_stops_the_previous_stream` |
| A missing device falls back to the default, keeps working, and says on screen that it fell back | `…::test_a_missing_microphone_falls_back_and_says_so` |
| The fallback happens once — no restart loop | `…::test_a_missing_microphone_does_not_produce_a_restart_loop` |
| Diagnostics shows a saved-but-absent microphone as not connected, not as selected | `…::test_a_missing_saved_device_is_shown_as_missing_in_diagnostics` |
| An unrelated `devicechange` does not restart the listener | `…::test_an_unrelated_device_change_does_not_restart_the_listener` |
| Losing the *active* device restarts cleanly onto the default with one live stream | `…::test_losing_the_active_microphone_restarts_cleanly` |

## Suspension while something else owns the audio

| Claim | Proved by |
|---|---|
| Two suspension reasons in, one out, still silent — reference-counted, not a boolean | `tests/test_clap_controller.py::test_overlapping_suspension_reasons_are_reference_counted` |
| Releasing the same reason twice does not resume early | `…::test_releasing_the_same_reason_twice_does_not_resume_early` |
| An exception inside a suspended operation still releases its reason | `…::test_an_exception_inside_a_suspended_operation_still_releases_it` |
| Push-to-talk suspends the clap listener and releases it | `…::test_push_to_talk_suspends_and_releases_the_clap_listener` |
| A failed transcription still releases it | `…::test_a_failed_transcription_still_releases_the_listener` |
| A microphone test that could not open a device still releases it | `…::test_a_failed_microphone_test_still_releases_the_clap_listener` |

## Calibration

| Claim | Proved by |
|---|---|
| A real synthesised double clap is measured through the real worklet and produces a proposal | `tests/test_clap_controller.py::test_calibration_measures_a_real_pair_and_proposes_settings` |
| No request made during a calibration session carries an onset, level or sample | `…::test_calibration_never_sends_a_measurement_anywhere` |
| Nothing is saved until Save is pressed, and what is saved is inside `SAFE_BOUNDS` | `…::test_saving_a_calibration_stores_only_clamped_tuning` |
| Reset returns to the standard settings | `…::test_resetting_a_calibration_returns_to_the_standard_settings` |
| Cancel releases the microphone; privacy mode during calibration stops it immediately | `…::test_cancelling_calibration_releases_the_microphone`, `…::test_privacy_mode_during_calibration_stops_it_immediately` |
| A calibration session is bounded and ends itself | `…::test_calibration_is_bounded_and_stops_itself` |
| An unusable proposed value is clamped, not stored; a non-number is dropped | `tests/test_clap.py::test_an_unusable_value_is_clamped_rather_than_stored`, `…::test_a_value_that_is_not_a_number_is_dropped` |

## What the tray is allowed to say

| Claim | Proved by |
|---|---|
| "On" only while a live track and an active worklet exist | `tests/test_clap_controller.py::test_the_tray_only_says_on_when_a_microphone_is_really_open` |
| "Paused by Privacy Mode", "Temporarily paused" and "Off" each appear for the right reason | `…::test_the_tray_says_paused_by_privacy_mode`, `…::test_the_tray_says_temporarily_paused_during_a_suspension`, `…::test_the_tray_stops_saying_on_when_the_feature_is_switched_off` |
| A page that stops reporting goes stale rather than leaving a false "On" | `tests/test_clap.py::test_a_stale_report_is_not_evidence_of_a_live_microphone` |
| A page that stays open keeps proving it, so the tray does not decay to a false "unavailable" | `tests/test_clap_controller.py::test_the_page_keeps_proving_the_microphone_is_open` |
| Every reported state maps to the right tray line | `tests/test_clap.py::test_every_tray_status_transition` |

## Page lifecycle

| Claim | Proved by |
|---|---|
| Leaving the page releases every resource | `tests/test_clap_controller.py::test_leaving_the_page_releases_the_microphone` |
| A page restored from the back/forward cache listens again | `…::test_a_page_restored_from_the_back_forward_cache_listens_again` |
| A microphone plugged in later is picked up without a reload | `…::test_a_microphone_that_appears_later_is_picked_up` |
| A quitting page does not reopen the microphone | `…::test_a_quitting_page_does_not_reopen_the_microphone` |
| Navigating away and back leaves exactly one listener | `…::test_navigating_away_and_back_leaves_exactly_one_listener` |
| A reload leaves one listener and an honest report | `…::test_a_reload_leaves_one_listener_and_an_honest_report` |
| Switching the feature off releases everything | `…::test_switching_the_feature_off_releases_every_resource` |

## Installer and process lifecycle

| Claim | Proved by |
|---|---|
| Ten install/launch/quit cycles leave no orphaned JARVIS or WebView2 process | `scripts/test_clean_install.py` (Windows CI) |
| Uninstall preserving data, reinstall, and full purge | `scripts/test_clean_install.py` phases B and C |
| A v0.1 ZIP database is carried forward once, without being modified | `scripts/test_clean_install.py` phase H |
| The installer's checksum file matches the executable it ships | the Windows Installer workflow |

## Security invariants

| Claim | Proved by |
|---|---|
| A credential-shaped memory is refused before any database write | `tests/test_secret_guard.py::test_the_guard_runs_before_the_privacy_check` |
| The database layer refuses even when called directly, and no rejected secret leaves a byte behind | `…::test_the_database_layer_refuses_even_when_called_directly`, `…::test_no_rejected_secret_leaves_a_single_byte_in_the_database` |
| There is exactly one place a memory row can be created | `…::test_the_database_layer_is_the_only_place_memory_rows_are_created` |
| Tool inputs are redacted before a log line, the audit trail or a WebSocket event | `tests/test_redaction.py`, `tests/test_audit_and_log_redaction.py` (non-empty log fixtures, pinned level) |
| `/api/pull` is reachable from one module only; `model_puller.start()` from one endpoint only | `tests/test_local_ai.py` (AST walk over every module in `app/`) |
| The API binds to `127.0.0.1` only, and no mutating endpoint is unprotected | `tests/test_security_invariants.py` |
| Only processes provably descended from a JARVIS this launcher started are ever terminated | `tests/test_launcher_process_tree.py` |
| No ElevenLabs request is made in any test, ever | `tests/test_elevenlabs.py` (every call through a mock transport; the real host is never contacted) |
| Cloud audio never reaches the disk | `tests/test_elevenlabs.py::test_cloud_audio_never_reaches_the_disk` |

---

# Part 2 — Must be tested by you

Nothing below has been verified by any automated run. **Do not mark an
audio item complete on the strength of a synthetic signal** — the whole
reason these are here is that synthesised audio through a fake capture
device is not a room.

## A0. Before you install — confirm the WebView2 Runtime is present

JARVIS's native window **is** a WebView2 control. Windows 11 normally
includes the Evergreen Runtime, but Microsoft's own distribution guide
says in the same breath that *"some devices might not have the Runtime
pre-installed, so it's a good practice to check whether the Runtime is
present on the client"* — and a customized or stripped Windows
installation can be missing or have a damaged one. Setup checks the
registry rather than assuming; do the same before you start.

Run these three read-only queries. Microsoft documents these exact
locations, and they are the ones `packaging/jarvis.iss` and
`app/launcher/runtime_check.py` both read:

```bat
reg query "HKLM\Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv
reg query "HKLM\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv
reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv
```

- **Installed** means at least one of them returns a `pv` value that is
  **neither empty nor `0.0.0.0`**. Microsoft documents `0.0.0.0` as
  explicitly meaning *not installed*, so a key that exists is not on its
  own an answer.
- **Looking in Settings → Installed Apps is not sufficient.** An entry
  can be present while the Runtime is broken or partially removed, and it
  can be absent while a per-user install is fine. The `pv` value is what
  both JARVIS and Microsoft's own detection use.

**If no query returns a usable `pv`, stop — do not run JARVIS yet.**
Install the Runtime from Microsoft's official page,
<https://developer.microsoft.com/microsoft-edge/webview2/>, then **repeat
the three queries above** and only continue once one returns a real
version.

Setup will also try to fetch Microsoft's bootstrapper itself if the
Runtime is missing, and a failure there never aborts the installation —
so an unchecked machine can end up with JARVIS installed and no native
window. Checking first is what turns that into a decision instead of a
surprise.

## A. Install and lifecycle

1. Verify the downloaded installer against **its own** `.sha256` file.
   Two builds of the same commit are not byte-identical — Inno Setup
   stamps a build time into the file — so never check one build against
   another build's checksum.
2. This installer is **unsigned**. SmartScreen will warn about an
   unrecognised publisher. That warning is accurate, and clicking through
   it is your decision to make.
3. **Upgrade directly over the previously installed RC**, without
   uninstalling first. Settings, chat history and any downloaded voice or
   speech model must survive.
4. The **native JARVIS window opens and actually shows the dashboard** —
   not a blank panel, not a WebView2 error page — and no unwanted browser
   tab opens with it.

   Check this with your eyes, because nothing in the product can.
   `GET /desktop/ready` proves four *process* facts — the server answered
   `/health`, the window child answered a ping on its control channel,
   the tray's message loop dispatched, the parent owns the lock — and
   **none of them is evidence that WebView2 painted anything**. A blank
   window reporting `ready: true` is a state the automated suite cannot
   tell apart from success. See `app/launcher/desktop_ready.py` for why
   pywebview's `loaded` event is not the missing proof either.
5. A **second launch brings the existing window forward** rather than
   starting anything new.
6. **Start with Windows**, if you switch it on: reboot and confirm JARVIS
   comes back the way you left it.
7. **Restart** from the UI and from the tray. **Quit** from the UI and
   from the tray.
8. After Quit, open Task Manager and confirm no `JARVIS.exe`,
   `msedgewebview2.exe` or port-owning process remains.
9. **Uninstall, preserving data.** Confirm settings and history survive a
   reinstall. Only when everything else on this list is done, **uninstall
   with full data removal** and confirm `%LOCALAPPDATA%\JARVIS` is gone.
10. Confirm Ollama and its models are **still installed** after an
    uninstall, even if JARVIS installed Ollama.

## B. First run, keys and the AI

11. Complete **onboarding** on a clean machine: your name, then the
    Anthropic API key.
12. Confirm the key is in **Windows Credential Manager** and survives a
    restart of the machine. Confirm it appears in no settings field, no
    log line and nothing on the Diagnostics page.
13. Ask JARVIS a real question and get a **real Anthropic response**.
14. Deliberately save a **wrong key** and confirm the error says the key
    was rejected — not "add an API key", and not "offline".
14a. **Identity-linked key.** With a personal or service account key that
    is *not* scoped to a single workspace, save it with the **Workspace ID
    box empty**. It must be refused with *"This Anthropic API key requires
    a Workspace ID"*, and the Settings status must **not** afterwards
    claim the key is working. Then supply the workspace and save again:
    for a **non-default** workspace the ID is in the Console's
    Settings → Workspaces **ID** column; the **Default Workspace is not
    listed there**, and its ID comes from the `anthropic-workspace-id`
    response header (see `docs/WINDOWS_INSTALLER.md`). Confirm chat works,
    and that the Workspace ID appears in no log line, no Diagnostics field
    and no API response — only "Set"/"Not set".
14b. **Scoped key — the route that needs no ID.** Create a key scoped to a
    single workspace (the Default Workspace is fine) and confirm it works
    with the Workspace ID box **blank**. This is the setup the docs
    recommend, and it must not require creating an extra workspace.
14c. **Legacy workspace-scoped key**, if you have one: it must still work
    with the Workspace ID box left empty. Nothing about it changed.
14d. **Remove the key** and confirm the Workspace ID clears with it, so
    the next key does not inherit it.
14e. **A check that cannot run is not a rejection.** Disconnect the
    network and save a key. It must be stored and reported as *not yet
    confirmed* — the Settings status must **not** say Anthropic rejected
    it, and must **not** say chat is available.
14f. **The Logs page must show the failure.** After any failed key save
    in 14a or 14e, open **Logs** and confirm there is one `ai_provider`
    row naming the category with a reference id — and that it contains no
    key, no Workspace ID and no Anthropic response text.

> ### Two things that are deliberately *not* on this list
>
> **"Save a key while offline and check the previous one survived."**
> That contradicts the product's own contract and would fail correctly.
> A network timeout is in `key_check._KEY_IS_PROBABLY_FINE`, so the key
> being saved is *worth storing*: JARVIS keeps the **new** key and labels
> it unconfirmed, which is what 14e asks you to confirm. An unreachable
> Anthropic is not a Credential Manager write failure and implies nothing
> about the credential already on the machine.
>
> **"Make the settings file unwritable and press Remove twice."**
> Credential-store and metadata write failures are not states a person
> should be asked to manufacture on their own PC, and a half-finished
> removal is not something to practise on a real credential. Every
> ordering — refused write, timed-out write, mutate-then-raise, failed
> metadata write, and the idempotent second Remove that recovers from it —
> is covered by `tests/test_credential_replacement_safety.py` and
> `tests/test_credential_backend_targets.py`, deterministically, against a
> backend fake with fault injection.
>
> Real-PC acceptance stays limited to what an ordinary user actually does.
15. On a machine without Ollama, run the **guided local-AI install**.
    Confirm the plan screen names source, publisher, licence, size and
    free space *before* anything is fetched; that the **Authenticode
    signature check** passes and the SHA-256 is shown; and that Ollama's
    own installer runs **visibly**, with a UAC prompt.
16. On a machine that already has Ollama, confirm JARVIS uses it and
    **never reinstalls over it**.

## C. Voice — output

17. Test **Kokoro speech** (`bm_george`): Test Voice, Stop, and
    interrupting one reply with another.
18. Select and test an available **Windows natural voice**.
19. Turn **Speak replies aloud** on and confirm a chat reply is spoken.
20. Use the per-message **Listen**, **Stop** and **Replay** controls.
21. Confirm an **approval prompt is never read aloud**.

## D. Voice — the premium cloud tier

Everything here needs a real ElevenLabs account. No automated test in
this repository has ever contacted ElevenLabs, and none ever will.

22. Paste a real API key and press **Save key**. Confirm the field
    clears, the status reads *Saved in the Windows Credential Manager*,
    and the key appears in Credential Manager under **JARVIS /
    elevenlabs_api_key**.
23. Press **Check key**. A valid key reports success; a key with the
    wrong characters reports *invalid*, not *offline*.
24. Press **Load voices**. Confirm the list is the voices **that
    account** actually has.
25. **Choose the voice.** This is a human judgement and nothing automated
    can stand in for it — listen to the candidates and pick the one that
    matches the brief in `docs/clean-room-and-voice-identity.md`:
    original, cinematic, calm, polished British male, warm baritone,
    restrained, highly intelligible — **not** an imitation of any actor,
    performer or copyrighted character.
26. Press **Test cloud voice** and listen to *"Good evening, sir. All
    systems are online and ready."* Adjust stability / similarity /
    style / speed and re-test. Suggested starting point:
    0.50 / 0.75 / 0.00 / 0.92 with speaker boost on.
27. **Compare** the cloud voice against Kokoro on the same sentence, and
    decide whether the premium tier earns its cost.
28. Switch **Speak with** to the ElevenLabs voice, ask JARVIS something,
    and confirm the reply is spoken in that voice.
29. **Turn privacy mode on.** Confirm the cloud voice refuses, says
    privacy mode is why, and that nothing is sent.
30. Turn the **fallback toggle off**, then make the cloud voice fail
    (turn off Wi-Fi). Confirm JARVIS stays silent and reports the reason
    rather than quietly using the local voice. Turn it back on and
    confirm the local voice covers **and says so**.
31. Press **Remove key**. Confirm the Credential Manager entry is gone
    and the local voice still works perfectly.
32. Check the log file and the Diagnostics page: **the key must appear in
    neither**, in any form.

## E. Voice — input

33. Test **push-to-talk** on the Chat page with your physical
    microphone, and judge the **transcription quality** on ordinary
    speech.
34. On the Voice page, run **Test microphone** and confirm the level
    meter moves and the result is accurate.
35. Try to store a secret — *"remember my API key is sk-…"* — and confirm
    JARVIS **refuses in the UI** and says what it refused, without
    echoing the value.

## F. Clap to activate

The detector is verified against synthesised audio in a real browser.
What that cannot tell us is how it behaves in a real room, with a real
microphone, in a hidden WebView2 window on Windows.

36. Switch **Clap to bring JARVIS forward** on. Confirm Windows asks for
    microphone permission if it has not already, and that the Voice page
    reports *Listening for claps*.
37. Confirm the **Windows microphone indicator** (taskbar / privacy
    settings) shows JARVIS using the microphone while it is listening —
    and that it goes out within a second when you turn the feature off.
38. **Pick a specific physical microphone** in the dropdown. Confirm the
    Voice page says it is using the selected microphone, and that
    speaking into *that* microphone (not another one) is what the level
    meter responds to.
39. Run the **calibration** flow in the room you will use. Clap twice
    when asked; read the proposal; press **Save**; then clap for real and
    confirm it works better than before. Then press **Reset** and confirm
    it goes back.
40. Minimise JARVIS to the tray, wait a minute, then **clap twice**. The
    window should come forward. *This is the item that cannot be verified
    anywhere but here:* Chromium does not throttle the audio thread and
    does not freeze a capturing page, but that is reasoning from
    documented behaviour, not a measurement of WebView2 on a real
    desktop.
41. **False positives.** For at least ten minutes each: talk near the
    microphone, play music, let a door close, type hard, and play audio
    out of the speakers JARVIS is next to. Nothing should activate. If
    something does, drop the sensitivity and try again — and report it,
    because that is a real result.
42. Clap twice from the **actual distance** you will use, at each
    sensitivity. Note which one works.
43. **Sleep and wake** the machine. Confirm the listener comes back
    honestly — either listening, or saying why not.
44. **Unplug and replug** the selected USB microphone while listening.
    Confirm JARVIS falls back to the default and says so, then recovers
    when it comes back — with no duplicate listener and no crash loop.
45. With **Speak replies aloud** on, confirm the greeting is spoken on
    activation, and that clapping during that greeting does not
    re-trigger anything. Turn *Say something when it activates* off and
    confirm the window still appears, silently.
46. **Turn privacy mode on.** Confirm the Voice page stops reporting
    *Listening*, the tray says *Paused by Privacy Mode*, the Windows
    microphone indicator goes out, and clapping does nothing.
47. **Quit JARVIS and clap.** Nothing should happen — this is expected
    and documented: the listener lives in the window. Confirm the Windows
    microphone indicator is out.
48. Check the **tray menu** at each of these moments and confirm the line
    it shows matches what is actually happening.

## G. Diagnostics and the whole point

49. Open **Diagnostics** and confirm every row is accurate on this
    machine — and that nothing on it is a credential or a filesystem path
    that names you.
50. Use it for an hour as a person, not a tester. Ask it things. Have it
    speak. Clap at it. The bar is not "no errors" — it is whether it
    feels like a product.

---

**Report back what failed and what surprised you.** An item that passed
but felt wrong is worth as much as one that failed.
