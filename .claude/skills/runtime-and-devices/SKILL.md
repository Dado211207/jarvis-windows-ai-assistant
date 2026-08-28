---
name: runtime-and-devices
description: Check runtime dependencies a Windows desktop app relies on - browser engine and WebView2 discovery, audio devices, microphone permission, speech-to-text and text-to-speech, and Credential Manager patterns - while stating clearly what cannot be tested without real hardware. Use when reviewing device, speech, credential or embedded-browser code, or before claiming any of it works.
---

# Runtime dependencies and devices

This skill is as much about what you **cannot** claim as about what to check.

## The hardware rule

A headless session, a CI runner and a VM without devices cannot test a microphone, a
speaker, a camera, a GPU, an antivirus reaction, or how a real user experiences
speech. Any statement about those from such a session is fabrication.

Everything below is split into **checkable** (code and configuration, discovery
logic, failure handling) and **requires-manual-acceptance** (does it actually work
with real hardware). Keep them separate in the report.

## 1. Browser engine and WebView2 discovery

If the app embeds a browser view:

**Checkable**

- discovery does not assume a fixed install path. WebView2 ships as the Evergreen
  runtime, a fixed-version distribution, or is absent entirely — the app must detect
  which, in that order, and handle absent
- the "runtime not installed" path produces a clear message with a next step, not a
  crash or a blank window
- the WebView is disposed on close; its child processes are terminated with the app
- the app does not silently fall back to a system browser for content that was meant
  to stay in-app
- content loaded into the WebView is trusted, or is isolated: no host-object bridge
  exposed to remote content, no `AllowExternalDrop` where it is not needed
- a version floor is documented if the app depends on a specific WebView2 feature

**Requires manual acceptance**

- rendering correctness on a real machine, on a machine with the runtime missing, and
  on a machine where an administrator has blocked its installation

## 2. Audio devices

**Checkable**

- device enumeration handles **zero devices** without raising
- the app reacts to a device being added, removed or changed while running — a
  desktop app that holds a handle to an unplugged headset is the usual failure
- the default device is read at use time, not cached at startup
- sample rate and channel-count mismatches are converted, not assumed
- streams are closed on exit; no audio device is left held after quit

**Requires manual acceptance**

- that sound is actually audible, at a sensible volume, without distortion
- behaviour with Bluetooth devices, with a device switch mid-playback, and with
  exclusive-mode applications holding the device

## 3. Microphone permission

**Checkable**

- Windows privacy settings can deny microphone access per-app; the failure surfaces
  as an access error, and the app must present it as "microphone access is blocked in
  Windows Settings", with the path to fix it — not as a generic error
- the app requests the device only when it needs it, and releases it after
- a denied microphone does not leave the app in a state where it thinks it is
  recording

**Requires manual acceptance**

- the actual permission prompt, the actual denial path, and recovery after the user
  grants access

## 4. Speech-to-text and text-to-speech

**Checkable**

- which engine is used, and whether it is local or a network service. If it is a
  network service, the app must handle offline, timeout and rate-limit responses, and
  the user must be told their audio leaves the machine
- audio format required by the engine is produced explicitly (sample rate, encoding,
  channels), not assumed from the device default
- long inputs are chunked; the app does not block the UI thread while transcribing
- TTS voice selection handles the requested voice or language being unavailable
- errors surface to the user; a failed transcription is not silently rendered as
  empty text

**Requires manual acceptance**

- transcription accuracy, latency, voice quality, and whether speech output is
  intelligible. No automated check in a headless session establishes any of this.
  A fixed audio file transcribed correctly proves the pipeline, not the microphone.

## 5. Credential storage (Windows Credential Manager)

**Checkable — as patterns, never as values**

- credentials are stored in Windows Credential Manager (or an equivalent OS keystore),
  not in a config file, a `.env`, the registry in plain text, or a log
- a stable, namespaced target name is used, so credentials survive an upgrade and are
  removable on purge
- read failures are handled: no credential stored yet, access denied, a different
  Windows user, a credential deleted behind the app's back
- the credential is fetched at use time and not held in memory longer than needed
- **nothing logs the value.** Check that error paths do not include the secret in a
  message, a traceback, or a telemetry payload
- uninstall keeps the credential; full purge removes it (see
  `/install-lifecycle-test`)

**Never do this while testing**

- do not read, print, enumerate or export stored credentials
- do not create a real credential with a real secret to test the path — use an
  obvious placeholder and delete it afterwards
- check only *whether* an entry exists and whether the code path handles each outcome

## 6. Report

Two sections, always both:

```
Checked (static or automated)
  [webview|audio|microphone|speech|credentials]  <file>:<line>
    Observed: <the code path>
    Fails when: <the concrete condition>
    Fix:      <the change>

Requires manual acceptance (not tested here)
  <capability> — needs: <real device / real Windows machine / a human listening>
  Why it cannot be automated here: <reason>
```

Never merge the two. A reader must be able to see at a glance which claims rest on
evidence and which rest on nothing.
