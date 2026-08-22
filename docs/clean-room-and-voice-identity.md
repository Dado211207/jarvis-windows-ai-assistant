# Clean-room decision, and what JARVIS's voice is

Two questions this document settles, because both have legal answers and
neither should be re-litigated from memory later:

1. What we may and may not take from the external project the product
   owner pointed at.
2. What JARVIS's voice is, and what it deliberately is not.

---

## 1. The external reference project

The product owner supplied a public repository as a reference:

- `https://github.com/hectorg2211/jarvis`
- Pinned revision `4d8083186ec8ba48fe65bc7cacbf68a527933a90`

**That repository declares no licence.** "No licence" is not "public
domain" and is not "do what you like". Under the Berne Convention and
ordinary copyright law, the absence of a licence means **all rights
reserved**: the default is that nobody may copy, adapt or redistribute
it. GitHub's own Terms of Service grant a viewer the right to *view* and
*fork within GitHub*, and nothing more.

So this project treats it as read-only prior art, and:

- **No source is copied** from it — not a file, not a function, not a
  line.
- **No comments, tests, documentation or distinctive implementation
  structure** are copied or paraphrased.
- **It is not imported, vendored, or added as a dependency.**
- **No constants are lifted from it.** In particular, the clap
  detector's thresholds (`app/voice/clap.py::SENSITIVITY_PROFILES`) were
  measured here, against synthesised audio played through a real
  browser microphone — see `tests/test_clap_detection.py`, which is the
  experiment that produced them — not transcribed from anywhere.
- **Its private ElevenLabs voice ID is neither recovered nor guessed.**
  A private voice belongs to the account that created it, and a voice ID
  is a reference to somebody else's paid asset.
- **Its hard-coded application automation is not reproduced.** JARVIS
  does not open Spotify, Binance, Claude or Cursor in response to a clap
  or anything else; every action goes through the registered-tool
  allowlist and the policy engine.
- **Nothing in this repository describes JARVIS as derived from its
  code**, because it is not.

### What was taken

Ideas, at the level a person could describe over coffee, independently
reimplemented:

| Idea | How JARVIS implements it |
|---|---|
| A cloud TTS provider can sound better than a local one | `app/voice/elevenlabs.py`, written against ElevenLabs' own published API documentation |
| A double clap is a pleasant hands-free way to summon an assistant | An original amplitude/transient state machine in an `AudioWorkletProcessor` (`app/ui/static/clap-processor.js`), thresholds measured here |
| A greeting after activation is a nice touch | A configurable phrase, spoken only if spoken output is already on |

An idea is not protected by copyright; an expression of it is. What is
in this repository is our expression, written from primary sources.

### The primary sources actually used

ElevenLabs' own documentation, read directly:

- `https://elevenlabs.io/docs/api-reference/text-to-speech/convert` —
  method, path, `xi-api-key` header, body fields, `voice_settings`
  fields and their defaults, the `output_format` enum and which values
  need a paid tier.
- `https://elevenlabs.io/docs/api-reference/voices/search` — the voice
  listing endpoint and its response shape.
- ElevenLabs' speed-control documentation — the documented `speed` range
  of 0.7–1.2, and the fact that Eleven v3 does not accept it.

---

## 2. The voice

### What it is

The product owner's brief, recorded verbatim so it cannot drift:

> An original cinematic British male AI assistant voice with a refined
> modern RP accent, low-mid pitch, warm resonance and precise
> articulation. Calm, measured and intellectually confident, with
> restrained emotion, deliberate pacing and subtle dry wit. Futuristic
> but human, authoritative without aggression, polished and consistent
> over technical explanations and short status messages. This must be an
> original voice and must not imitate any existing actor, performer or
> copyrighted character.

Target characteristics: low-mid pitch, roughly **120–140 Hz**; warm
resonance; precise articulation; calm, controlled pacing.

Reference test phrase, used by the Voice page's test button:

> **"Good evening, sir. All systems are online and ready."**

Recommended starting settings, from the owner and within the ranges
ElevenLabs documents:

| Setting | Value | Documented range |
|---|---|---|
| `stability` | 0.50 | 0.0–1.0 (default 0.5) |
| `similarity_boost` | 0.75 | 0.0–1.0 (default 0.75) |
| `style` | 0.00 | 0.0–1.0 (default 0) |
| `speed` | 0.92 | **0.7–1.2**, and **not accepted by Eleven v3** |
| `use_speaker_boost` | enabled | boolean (default true) |

`speed` is omitted from the request body when a v3 model is selected,
rather than sent to be silently ignored.

### What it is not

- **Not a clone of Paul Bettany**, or of any other performer.
- **Not Marvel's JARVIS**, and never described as such. The name is the
  product owner's choice for their own assistant; the *voice* makes no
  claim to a copyrighted character.
- **Not built from an unauthorised actor recording.** No recording is
  used, uploaded, or referenced.
- **Not the external project's private voice.** Its voice ID is not in
  this repository and was not sought.

### On the reference recording

The product owner mentioned a short reference recording. **It is not
available in this development environment, and no attempt was made to
locate, download or reconstruct it.** Nothing in this repository claims
that anybody listened to it, compared against it, or reproduced it. The
written profile above is the complete reference used.

### Choosing the actual voice is the owner's job

JARVIS ships the *machinery*: provider support, secure key storage,
voice selection, tuning controls and a test button. It does **not** pick
a voice, and it deliberately does not create or clone one.

Selecting the final voice requires, and can only require:

- the owner's own ElevenLabs account;
- a real API key belonging to that account;
- the voices that account is licensed to use;
- the owner listening to them and deciding.

That is a physical-PC, human-judgement task. No automated check in this
repository can stand in for it, and none pretends to.

### Voice Design is never automatic

ElevenLabs offers voice creation and cloning features. JARVIS **does not
call them**. It creates no voice, clones no voice, and spends no credits
without a person pressing a button. Nothing to do with ElevenLabs runs
during installation, onboarding, startup, tests or CI — the only calls
that exist are made from a session-token-protected endpoint in response
to a direct action, and every one of them is refused outright while
privacy mode is on.
