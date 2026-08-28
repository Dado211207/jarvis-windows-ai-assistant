# Inspiration and Licensing Notes — v0.2

Concepts referenced during this milestone's design, per project instructions:
**concepts, not code.** No source code, branding, artwork, audio, or exact
interface composition was copied from any project below. Every module in
this milestone was written from scratch against JARVIS's own existing
architecture (see `docs/audit-v0.2.md`).

Sources marked **fetched** were retrieved and read during this session
(via their public GitHub pages) to ground specific design decisions, rather
than relying on general/training knowledge. Sources marked **general
knowledge** are widely-documented public libraries/frameworks whose public
APIs and common usage patterns informed the adapter boundaries below, without
a fresh line-by-line read in this session.

## Consulted and adopted

**Open.Jarvis** (`github.com/dmrr35/Open.Jarvis`, MIT — **fetched**)
Adopted: an explicit, named runtime-state model instead of implicit control
flow; a keyless "local-first" degraded mode as a first-class, tested state
rather than an error case; redacting exceptions from a cloud provider before
they reach the user; a privacy mode that suppresses persisted-context
injection into provider prompts. **Not copied**: JARVIS's state list (10
states, including `TRANSCRIBING`/`THINKING`/`AWAITING_APPROVAL`) differs
deliberately from Open.Jarvis's 8-state list — designed independently
against this milestone's own approval-gated pipeline, not ported.

**Microsoft UFO** (`github.com/microsoft/UFO`, MIT — **fetched**)
Adopted: separating *planning* (what should happen) from *execution* (doing
it) as distinct pipeline stages; preferring native OS integration (Windows
UI Automation / documented APIs) over simulated mouse/keyboard input, with
simulated input only as a last, explicitly-approved resort; treating
execution as a sequence of observable states rather than a single opaque
call. **Not adopted**: UFO's multi-agent DAG/orchestrator architecture — out
of scope for this milestone's single-assistant, single-user model, and
explicitly excluded by the task's "no multi-agent framework" scope boundary.

**OpenAdaptAI/OpenAdapt** (`github.com/OpenAdaptAI/OpenAdapt`, MIT —
**fetched**) Adopted directly into this milestone's Windows-execution rules:
verification as a separate step after an action, independent of the action
call itself succeeding; a fail-closed halt (return evidence, stop) when a
target's identity can't be established, instead of guessing or retrying
blindly. This maps directly onto this milestone's "verify identity before
acting, verify effect after acting, halt on ambiguity" requirement.

**isair/jarvis** (`github.com/isair/jarvis`, **personal-use-only license —
fetched**) Concepts only, and read carefully because of the license:
graceful degradation from semantic to keyword search when richer retrieval
is unavailable, and filtering which tools are exposed to a model call
instead of dumping the entire registry into every prompt. **License note**:
this project is *not* open source in the redistributable sense ("personal
use is free; commercial use requires contacting the developer") — nothing
from it was copied, and no key names, config shapes, or prompts were
reused; only the two general concepts above informed design.

**openWakeWord** (`github.com/dscripka/openWakeWord`, code Apache 2.0,
**pre-trained models CC BY-NC-SA 4.0 (non-commercial) — fetched**)
Adopted: the general shape of a threshold/sensitivity-tunable detector
behind a narrow adapter interface. **Explicit licensing decision**: because
the shipped pre-trained models are non-commercial-licensed, this milestone
does **not** bundle, auto-download, or depend on any openWakeWord model.
Wake word ships as a disabled-by-default adapter *interface* only (see
"Voice implementation" in the final report) — a real integration would
require either the user supplying their own appropriately-licensed model or
a separate, explicit licensing decision, which is exactly the kind of
approval this task's instructions ask for before shipping a restrictively
licensed asset.

## Referenced for adapter shape (general knowledge, not freshly fetched this session)

- **faster-whisper** (`github.com/SYSTRAN/faster-whisper`, MIT) — informed
  the STT adapter's model-size/device configuration surface and the
  requirement to fail into a clear degraded state rather than silently
  downloading a large model on first use.
- **pywinauto** (`github.com/pywinauto/pywinauto`, BSD-3-Clause) — informed
  treating UI Automation as the preferred Windows execution backend, with
  `pywinauto` itself scoped narrowly and never used for raw
  coordinate-based clicking.
- **Ollama** (`github.com/ollama/ollama`, MIT) — informed the optional local
  provider adapter's shape: loopback-only by default, model discovery via
  its own local HTTP API, no automatic model pulls.
- **Leon** (`github.com/leon-ai/leon`, MIT) — informed the deterministic
  vs. optional-reasoning framing already present in this milestone's
  "deterministic routes always take priority" rule (`CLAUDE.md`, pre-existing
  on `main`), reinforcing rather than changing that existing design.
- **FastAPI WebSockets** (`fastapi.tiangolo.com/advanced/websockets/`) —
  the standard `WebSocket`/`WebSocketDisconnect` pattern this milestone's
  event stream is built on.

## Rejected

- **Multi-agent orchestration** (UFO's `ConstellationAgent`/DAG model) —
  explicitly out of scope per the task's own exclusion list ("not a
  multi-agent framework").
- **Embedding-based semantic memory search** (isair/jarvis, OpenAdapt's
  broader retrieval stack) — JARVIS's existing SQLite `LIKE`-based memory
  search is kept as the baseline; SQLite FTS5 is added only where a tested
  fallback exists, per the task's own conditional instruction, and no vector
  embedding dependency was introduced.
- **openWakeWord's shipped pre-trained models** — rejected specifically due
  to their non-commercial license, as detailed above; the adapter interface
  is kept, the model is not bundled.
- **Any UI visual composition, color scheme, iconography, or audio** from
  any source above — this milestone's visual identity (see the final
  report) was designed originally against the task's own written direction
  (graphite/navy, cyan/teal, amber, restrained red), not derived from any
  inspiration project's actual interface.
