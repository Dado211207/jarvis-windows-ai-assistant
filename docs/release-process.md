# JARVIS Release Process

## How to create a release

1. Go to **GitHub → Actions → Release** workflow.
2. Click **Run workflow**.
3. Enter the version tag (e.g. `v0.1.0`) and click **Run workflow**.

The workflow will:
- Run all tests (must pass before the release is created)
- Build the Windows executable via PyInstaller
- Create a versioned ZIP: `JARVIS-Windows-v0.1.0.zip`
- Publish a GitHub Release with the ZIP attached as a release asset

## Version naming

Follow [Semantic Versioning](https://semver.org/) with a `v` prefix:

| Example | When to use |
|---|---|
| `v0.1.0` | Initial / early development releases |
| `v0.2.0` | New phase or significant feature set |
| `v0.1.1` | Bug-fix patch on an existing release |
| `v1.0.0` | First stable, signed production release |

## Where the release appears

After the workflow completes:
- **GitHub → Releases** tab of the repository
- The ZIP asset is attached directly to the release

## What to verify before triggering a release

- All CI checks are green on `main`
- `pytest` passes locally (80/80)
- No secrets or `.env` are present in the repo
- The version tag does not already exist on GitHub (re-using a tag will fail)

## What is NOT included in the release ZIP

| Excluded item | Reason |
|---|---|
| `.env` / API keys | Never in source; never in build |
| `data/jarvis.db` | Created fresh on first run |
| `data/logs/` | Created fresh on first run |
| `data/screenshots/` | Created on demand |
| Test files | Not bundled by PyInstaller |
| `.git` history | PyInstaller never includes it |
| GitHub workflow files | Not bundled by PyInstaller |

## Security reminders

- Never add `ANTHROPIC_API_KEY` to the workflow environment.
- Never commit `.env` to the repository.
- FastAPI must remain bound to `127.0.0.1` only.
- The release workflow makes no real Anthropic API calls — tests mock all external AI calls.
