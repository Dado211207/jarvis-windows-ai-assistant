---
name: artifact-integrity
description: Prove that the artifact you inspected is the artifact that shipped, using SHA-256 digests and byte comparison between a clean build and what is published. Use when verifying a build output, an installer, a deploy preview or a production deployment against a specific commit.
---

# Artifact integrity

"The build is from this commit" is a claim about bytes. Check the bytes.

## 1. Build clean, from a known commit

A build over a dirty tree or a warm cache proves nothing about the commit.

```
git rev-parse HEAD              # record the full SHA
git status --porcelain=v1       # must be empty
<the project's clean/build commands, as the project defines them>
```

Record what you removed first (`dist/`, `build/`, `node_modules/.cache`, `__pycache__`)
and whether the build was fully cold. A warm cache is not disqualifying, but it is a
caveat that belongs in the report.

## 2. Digest everything you will refer to later

```
# Linux/macOS
find <output-dir> -type f -exec sha256sum {} \; | sort -k2

# Windows PowerShell
Get-ChildItem -Recurse -File <output-dir> | Get-FileHash -Algorithm SHA256
```

Record the digest of every artifact you intend to claim something about — the
bundle, the installer, the archive. A digest with no recorded file size and no
recorded commit is half a fact.

```
<path>   sha256=<digest>   bytes=<n>   built-from=<full SHA>
```

## 3. Compare, do not assume

Comparing a local build against what is published:

- **Same digest** → identical bytes. This is the only proof of identity.
- **Different digest** → they differ. Before calling it a problem, rule out the
  ordinary causes: embedded build timestamps, embedded commit SHA, a minifier
  version difference, gzip/brotli re-encoding by the host, line-ending translation.
  Name which one you found; do not shrug and move on.
- **Cannot fetch the published artifact** → say `not-verified`, not "matches".

For text assets served over HTTP, compare the decoded body, not the compressed
transfer. For a directory, compare the sorted digest list, not just a spot check.

## 4. Reproducibility is a separate claim

Two builds of the same commit producing different digests is normal for many
toolchains. That means digest equality proves identity, but digest inequality does
not by itself prove a different source. Say which claim you are making:

- "these are the same bytes" — proven by equal digests
- "this was built from commit X" — proven only if the build is reproducible, or if
  the artifact embeds and exposes the commit and you read it

## 5. Signatures and checksum files

If the project publishes a checksum file or a signature:

- verify the artifact against the published checksum, and say whether it matched
- report the signature status as observed: signed and valid, signed and untrusted,
  unsigned, or not checked. Never report "signed" from the presence of a file name
- an unsigned artifact is a fact to disclose, not a defect to hide

## 6. Report

```
Commit:      <full SHA>
Tree:        clean | dirty (<files>)
Build:       <exact commands> -> exit <code>   (cold | warm cache)
Artifacts:   <path> sha256=<digest> bytes=<n>
Published:   <url or path> sha256=<digest> — match: yes | no (<reason>) | not fetched
Signature:   valid | untrusted | unsigned | not checked
Unverified:  <what remains unproven>
```
