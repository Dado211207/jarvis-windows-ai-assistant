# A mobile companion, considered — and deliberately not built

**Nothing in this document is implemented. No code in this repository
does any of it.** This is a design record written so that the first
person who tries has the constraints in front of them rather than
discovering them by shipping something regrettable.

The one rule that is already enforced, today, in code and in tests:
**the JARVIS server binds to `127.0.0.1` and nothing else.**
`app/api/server.py::loopback_host()` refuses any other value, logs an
error, and binds loopback anyway; `JARVIS_HOST=0.0.0.0` cannot widen it.
There is no setting, no flag and no debug mode that opens it. Any mobile
design that begins with "just bind to the LAN address" is already
outside what this product does.

---

## 1. What somebody actually wants

Not "JARVIS on my phone" in the general sense. Concretely, three things,
in descending order of how often they would be used:

1. **Ask something and get an answer** when the PC is on and they are
   not sitting at it.
2. **See what happened** — the action history, what is pending
   approval — and approve or cancel it.
3. **Trigger something small** on the PC: lock it, open a folder for
   when they get back, take a note.

All three are the existing REST surface. None of them needs a new
capability; every one of them needs a way for a phone to reach a
loopback server, which is the entire problem.

---

## 2. Why "just expose the port" is not on the table

It would work in ten minutes and it is the wrong answer, for reasons
that are specific rather than generic:

- **The tool registry is reachable from the API.** Thirty-one
  registered tools, some of them approval-gated, all of them behind a
  per-session token that a browser gets by being served the page. That
  design assumes the only thing that can reach the port already sits at
  the keyboard. It is not an authentication system and was never asked
  to be one.
- **The session token is a CSRF defence, not a credential.** It is
  handed out to anything that performs a GET. On loopback that is
  exactly right; on a LAN it is a door with the key taped to it.
- **A home LAN is not a trust boundary.** A guest phone, a smart TV, a
  compromised IoT device and a neighbour on a shared flat network are
  all "the LAN".
- **Router-level exposure is worse still.** Port forwarding, UPnP and
  any hosted tunnel put a machine's assistant — with its microphone
  diagnostics, clipboard tool, note-writing and conversation history —
  on the public internet behind whatever the weakest link in that chain
  turns out to be. **None of these will be added.**

---

## 3. The three architectures that are actually possible

### A. Companion app on the same LAN, over mutual TLS

The Windows side would run a *second*, separate listener — never the
existing one — bound to the LAN, terminating TLS with a certificate
generated on the PC, and accepting only clients presenting a
certificate issued during an in-person pairing step (scan a QR code on
the PC's screen).

| | |
|---|---|
| **What it needs** | A second listener with its own, much smaller, explicitly-enumerated route surface; a pairing flow; certificate storage on both ends; revocation |
| **Reaches the PC when away from home?** | No |
| **Third party involved?** | No |
| **Honest cost** | The pairing flow is the whole product. Certificate lifecycle on a phone is where this kind of project usually dies |
| **Biggest risk** | "A second listener with a smaller surface" decays into "the same listener" the first time somebody wants a feature that already exists on the main one |

### B. Outbound-only: the PC connects out, the phone talks to a relay

The PC opens an outbound connection to a relay service and holds it.
The phone talks to the relay. Nothing listens on the PC; no port is
opened; no router configuration exists.

| | |
|---|---|
| **What it needs** | A relay service somebody runs and pays for; end-to-end encryption so the relay is a dumb pipe; a device-pairing scheme; an account system |
| **Reaches the PC when away from home?** | Yes |
| **Third party involved?** | **Yes — and this is the decisive fact.** JARVIS is local-first. A relay means there is now a service that knows when the machine is on, how often it is used, and — if the end-to-end encryption is ever wrong, or is quietly relaxed for a feature — what was said |
| **Honest cost** | This is not a feature, it is a service with an SLA, a privacy policy, an abuse story and a bill |

### C. No live link at all: a read-only digest the phone can fetch

The PC writes a small, encrypted, expiring summary — pending approvals,
recent actions, whether it is up — to a location the phone can read.
Nothing is controllable from the phone. Answering a question, approving
an action and running a tool are all out of scope by construction.

| | |
|---|---|
| **What it needs** | A file destination the user already has (their own cloud drive), a key on both ends, an expiry |
| **Reaches the PC when away from home?** | Reading, yes. Doing, never |
| **Third party involved?** | Only one the user already chose, holding only ciphertext |
| **Honest cost** | Small. It answers want #2 above and neither of the other two |

---

## 4. The recommendation

**If a mobile companion is built, build C first and stop there for a
release.** It is the only one of the three whose failure mode is
"somebody reads a stale status" rather than "somebody controls a PC",
and it is the one that would ship in weeks rather than quarters.

**B should not be built by this project.** Not because it cannot be
done well, but because doing it well means running a service, and a
local-first assistant that requires somebody else's server to be up has
stopped being the thing it said it was.

**A is defensible and expensive.** If it is ever attempted, the
non-negotiables are below.

---

## 5. Non-negotiables for anyone who builds any of this

These are not preferences. Each one exists because the alternative is a
specific, foreseeable failure:

1. **The existing `127.0.0.1` listener is never widened.** A mobile
   feature adds a *separate* listener or it does not exist. Same reason
   the clap detector cannot run a tool: keep the powerful thing and the
   reachable thing apart.
2. **Route allowlist, not route reuse.** The mobile surface enumerates
   the handful of endpoints it needs. `/command` with a full tool
   registry behind it is not one of them without its own risk review.
3. **Pairing is physical.** A code shown on the PC's screen, entered or
   scanned once. No password, no account, no recovery flow — a recovery
   flow is a remote takeover flow with better manners.
4. **Approval stays on the PC for anything a policy check calls
   risky.** Approving from a phone means approving something you cannot
   see the context of. `RiskLevel` already distinguishes; use it.
5. **No port forwarding, no UPnP, no tunnel, no hosted gateway.** Not
   as a default, not as an option, not behind an advanced setting.
6. **The microphone never crosses the boundary.** Push-to-talk and
   clap detection are local features of a machine somebody is sitting
   at. A phone that can open the PC's microphone is surveillance
   equipment, whoever built it.
7. **Privacy mode reaches it too.** Whatever this is, privacy mode
   turns it off, the same way it turns off the cloud voice and the clap
   listener.
8. **It is off until switched on, and visibly on while it is.** A
   listener the user cannot see is one they cannot turn off.

---

## 6. What would have to be true before starting

- Somebody wants it enough to accept a pairing step, which is the part
  users complain about.
- There is an answer to "what happens when the phone is stolen" that is
  not "reinstall JARVIS".
- The threat model in `docs/THREAT_MODEL.md` has been extended to cover
  a second network boundary — not amended in passing, rewritten for it.
- The Windows product is finished. It is not yet: see the physical-PC
  checklist that gates the current release candidate.
