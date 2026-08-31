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
- **Secrets live in the OS credential store, not in a file.** In the
  packaged Windows product every API key is held in Windows Credential
  Manager through `app/core/credentials.py` — Anthropic, ElevenLabs and
  OpenAI each in their own entry. `ANTHROPIC_API_KEY` as an environment
  variable (from the shell or a gitignored `.env`) still takes precedence
  when it is set, because that is what development and CI use and a
  credential store that may not exist in those contexts must never
  shadow it; the store is the fallback for the installed app, where
  onboarding and Settings write it. See
  `app/config.py::Settings.effective_api_key`.
- No key is ever returned by an endpoint, rendered into a template,
  written to the database or logged — the UI learns only whether one
  exists. An ordinary uninstall deliberately **leaves** the credentials
  in place; removing them is the explicit `--purge-data` choice, carried
  out by the application itself (`JARVIS.exe --uninstall-cleanup`)
  because only it knows how the key was stored — an installer guessing at
  a Credential Manager target name is how an uninstall leaves a secret
  behind while reporting success.

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
