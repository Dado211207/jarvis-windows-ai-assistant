# Security

JARVIS is a private, local-first, single-user personal project. It is not
a public service and does not accept external vulnerability reports in
the usual open-source sense — there is one intended user, running it on
their own machine.

## Summary

- The API binds to `127.0.0.1` only, by default, and changing that
  requires explicit approval and a security review (see `CLAUDE.md`).
- The language model never directly executes anything. Every action goes
  through a fixed policy engine (`app/core/policy.py`) before it can run;
  see `docs/THREAT_MODEL.md` for the full pipeline.
- Destructive capabilities (arbitrary shell execution, credential access,
  mass file deletion, registry/service changes, and more) are not
  implemented at all — not sandboxed, not gated, simply absent. See
  CLAUDE.md's "Do NOT implement" list.
- Secrets (`ANTHROPIC_API_KEY` and friends) live only in `.env`, which is
  gitignored, and are never sent to the browser or logged.

## What this document is not

It is not a claim that JARVIS is fully sandboxed, that its local database
is encrypted, or that it is safe to run on a shared or malicious machine.
`docs/THREAT_MODEL.md` states plainly what is and is not protected —
read that before assuming a guarantee this file doesn't make explicit.

## If you find a problem

Since this is a private single-user project: open an issue (or, if it's
sensitive, fix it directly) rather than following a public disclosure
process built for a multi-user service. If a real gap doesn't already
appear in `docs/THREAT_MODEL.md`, treat the missing documentation itself
as part of the bug.
