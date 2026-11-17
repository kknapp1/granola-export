# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Dependency-free (stdlib only, Python 3.10+) tools that export a user's own Granola.ai data to disk. There are **two independent exporters**:

| | [scripts/extract_public.py](scripts/extract_public.py) | [scripts/extract.py](scripts/extract.py) + [scripts/api.py](scripts/api.py) |
|---|---|---|
| API | Official public API, `public-api.granola.ai/v1` (docs.granola.ai, OpenAPI v1.7.0) | Undocumented private API, `api.granola.ai` + sub-service hosts |
| Auth | `GRANOLA_API_KEY` env var (`grn_...`, Business/Enterprise plans) | Token scraped from the Granola Mac app's `supabase.json` |
| Platform | Any (this is the one used on Windows) | macOS only (the Windows app stores `supabase.json.enc`, encrypted) |
| Output | `<Granola folder>/<Note>/<Note>.json + .md` | `docs.json`, `transcripts/`, `panels/`, `aux/`, `meetings/*.md` |

The repo also doubles as a Claude Code skill: [SKILL.md](SKILL.md) documents the **legacy macOS exporter only**. Keep it in sync if `extract.py`/`api.py` behavior changes.

## Commands

```powershell
# Public-API exporter (Windows/any OS). Key comes from the environment, never argv.
$env:GRANOLA_API_KEY = "grn_..."
python scripts/extract_public.py --list-folders          # print folder ids
python scripts/extract_public.py --folder-id <fol_id>    # small first test
python scripts/extract_public.py --attachments           # full backup incl. image attachments
python scripts/extract_public.py --render-only           # rebuild layout/markdown from saved JSON, offline
# other flags: --dest, --created-after/--created-before/--updated-after, --space-id,
#              --no-transcripts, --skip-render (json only), --refetch

# Legacy macOS exporter
python3 scripts/extract.py [--dest DIR --index-only --skip-render --no-extended --workers N]
python3 scripts/api.py                      # quick auth + API health check
./scripts/refresh_token.sh                  # needs jq

# Tests (no network; extract_public tests run main() against a local fake API server)
python -m unittest discover -s tests -v
python -m unittest tests.test_extract_public.TestEndToEnd -v                  # one class
python -m unittest tests.test_extract_public.TestEndToEnd.test_full_then_incremental
```

No build or lint steps. CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the tests on Ubuntu + Windows, Python 3.10 and 3.13 — keep code 3.10-compatible. In tests, call `extract_public.main([...argv])` (it returns the exit code) with `API_BASE`/`MIN_INTERVAL` patched.

## Repo / release workflow

This is a fork: `origin` = `kknapp1/granola-export` (public), `upstream` = `moona3k/granola-export`. A separate **private** repo runs [examples/github-actions/granola-backup.yml](examples/github-actions/granola-backup.yml) on a schedule and checks this repo out **at a pinned tag**. To release: bump `__version__` in `extract_public.py`, add a CHANGELOG section, update the `ref:` in the workflow template and [docs/scheduled-backup.md](docs/scheduled-backup.md), then tag `vX.Y.Z`. Never commit exported notes or keys here.

## Architecture: extract_public.py

Single file, flow: `api_get` (GET, gzip, retries on 429/500, **redirects disabled** so the Bearer key can't be forwarded) → `paginate` (cursor pages; cursor requests must not repeat filters) → `fetch_note` (falls back to the paged `/transcript` endpoint on HTTP 413) → `build_tree` (layout + render).

Key design points that span functions:
- **JSON is the source of truth; the tree is derived.** `load_existing` scans `dest` recursively for note JSON (current tree, `_incoming/` staging, or the legacy `json/` dir), so cache checks (`updated_at` equality) work wherever a note currently lives. Each freshly fetched note is written to `_incoming/<id>.json` immediately (crash safety); `build_tree` then writes the final layout and deletes superseded copies and empty directories.
- **Layout:** one directory per Granola folder (flat — nested folders become peers), note placed in its *leaf* folder via `leaf_folder` (the API's `folder_membership` includes ancestors); notes with no folder go to `Unfiled`. Names come from `safe_name` (Windows-safe, `1:1` → `1-1`, 40/50 char caps to stay under MAX_PATH); `unique_stems` disambiguates repeats with the date, then a short id. Because names depend on the whole set of notes, they can change between runs; the cleanup pass handles relocation and must never delete a path that was just written (`written` set).
- **Contract the private backup pipeline depends on — don't break without a major version bump:**
  - **Exit codes:** `0` = ok, `1` = partial failure with data saved, `2` = configuration error, `3` = fatal API error.
  - **`--since-last-run`:** uses `manifest.json` → `sync.watermark`. That watermark is the start time of the last run that was unscoped and had zero failures; 15 minutes are subtracted as overlap.
  - **Deterministic output:** unchanged notes must produce byte-identical files, because git diffs depend on it, and a test checks this. Only `manifest.json` may change on every run.
  - **Hidden directories are never scanned:** `load_existing` prunes dirs starting with `.`, so `dest` can sit inside a git repo next to `.git` and a checked-out `.exporter`.
  - **Legacy `meetings/` cleanup:** only runs if that dir's `INDEX.md` was generated by us.
- **Security properties to preserve:** note/attachment/folder ids are regex-validated before use in paths; attachments are opt-in, https-only, size-capped, extension chosen from content-type, and the API key is never sent to attachment hosts.
- API caveats: only notes with a generated AI summary are returned; transcripts can be disabled by an admin (403); `private_notes_*` appear only for the note owner's key; attachment URLs are short-lived, so download them in the same run (`--attachments`).

## Architecture: legacy extract.py / api.py

- [scripts/api.py](scripts/api.py) — token loading from `supabase.json` (values are JSON-stringified inner JSON), JWT expiry check, `api_post()`, one thin wrapper per endpoint (some on sub-service hosts via `base=`), and `pm_to_md()` (ProseMirror → Markdown, tolerant of strings/None/lists).
- [scripts/extract.py](scripts/extract.py) — pipeline: index → per-doc transcripts + panels (ThreadPoolExecutor) → aux endpoints + attachments → Markdown. Idempotent skip-if-exists; failed fetches are cached as `{"error": ...}` and not retried unless the file is deleted. Known weaknesses (reviewed, unfixed): attachment URLs and ids from server responses are used unvalidated (any-scheme fetch incl. `file://`, path traversal via ids), and `aux/integrations.json` may contain third-party credentials.
- [references/](references/) — reverse-engineered notes on the private API.

## Gotchas

- Tokens/keys and exported data must never be committed: `.gitignore` covers `granola-data/`, `extracted/`, `my-data/`, `*supabase*.json`. `.sf/` is an unrelated untracked Salesforce directory.
- Windows console is cp1252: `extract_public.py` reconfigures stdout/stderr to UTF-8; keep that if adding non-ASCII output.
- Never use placeholder ids in docs/examples that look valid; the API rejects them with HTTP 400.
