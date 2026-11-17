# Scheduled backup to a private GitHub repo

This guide shows how to keep a continuously updated archive of your Granola notes in a **private** GitHub repository. The archive is built with `scripts/extract_public.py` from this repo, run on a schedule by GitHub Actions. This public repo holds only code. Your notes only ever live in the private repo.

```
kknapp1/granola-export (public)         your private backup repo
  scripts/extract_public.py   ──────►    .github/workflows/granola-backup.yml   (runs daily)
  (pinned by tag, e.g. v0.2.0)           notes/
                                           <Granola folder>/<Note>/<Note>.json + .md
                                           INDEX.md  folders.json  spaces.json  manifest.json
```

## Setup

1. Create a private repository on GitHub. It can be empty.
2. Copy [`examples/github-actions/granola-backup.yml`](../examples/github-actions/granola-backup.yml) into it as `.github/workflows/granola-backup.yml`.
3. Add a repository secret named `GRANOLA_API_KEY` that holds your `grn_...` key (Settings → Secrets and variables → Actions). Create the key in the Granola desktop app under Settings → Connectors → API keys.
4. Add this `.gitignore` to the private repo:
   ```
   notes/_incoming/
   .exporter/
   ```
5. Run the workflow once by hand (Actions → Granola backup → Run workflow). The first run has no watermark yet, so it does a full export. Every later run is incremental.

Alternatively, run the first full export on your own machine with `--dest <private repo>/notes`, then commit and push it. The scheduled runs carry on from that copy's watermark.

## How the incremental sync works

- The workflow runs `extract_public.py --dest notes --since-last-run`.
- `notes/manifest.json` stores a **watermark**: the start time of the last run that listed everything without errors.
- On the next run, the exporter asks the API only for notes updated after the watermark minus 15 minutes (to absorb clock skew). New notes and edited notes both match.
- Any note whose `updated_at` matches the saved copy is skipped. The rest are fetched with their transcripts and laid out into the tree.
- The watermark advances only when the run had **no failures** and was **not scoped** with `--folder-id`, `--space-id`, `--created-*` or `--updated-after`. A failed or partial run therefore never causes later runs to skip a note.
- Output is deterministic: an unchanged note produces byte-identical files, so git only records real changes. The workflow skips commits where only `manifest.json` changed.

## Exit codes

| Code | Meaning | Data written? |
|---|---|---|
| 0 | Success | yes |
| 1 | Some notes failed (listed in the log); everything else saved, watermark not advanced | yes, commit it |
| 2 | Configuration problem, such as a missing or invalid `GRANOLA_API_KEY` or bad arguments | no |
| 3 | Fatal API error, such as an invalid key (401) or an unreachable API | possibly partial; safe to re-run |
| 130 | Interrupted | partial; safe to re-run |

The template commits whatever was saved, then fails the job if the exit code was non-zero, so you get an Actions failure notification.

## Behaviors to know

- **Deletions are not propagated.** A note deleted in Granola stays in the archive. This is intentional for a backup.
- **Renames and moves are.** If a note's title or folder changes in Granola, its directory is moved on the next sync, and git shows it as a rename.
- **Only notes with a generated AI summary** are returned by the public API. Other notes cannot be backed up this way.
- **Transcripts** can be disabled for API keys by a Granola workspace admin. In that case the notes are saved without them.
- **Attachments**, with `--attachments`, are downloaded during the same run because their URLs expire. They are binary files in git, so the repo grows with every screenshot.
- **Key expiry or revocation** shows up as exit code 3 with HTTP 401 in the log.

## Upgrading the exporter

The template pins the exporter to a tag (`ref: v0.2.0`). To upgrade, read [CHANGELOG.md](../CHANGELOG.md), change the tag, and run the workflow by hand once. Layout changes are applied by the exporter itself on the next run, using the same relocation logic as renames. If you ever need to rebuild the Markdown or the layout without calling the API, run `python scripts/extract_public.py --dest notes --render-only` locally.

## Local Windows alternative

To run the same backup from a Windows PC instead of Actions, use Task Scheduler:

```powershell
# One-time: store the key for your user (plain text in your user environment; prefer a secret store if available)
[Environment]::SetEnvironmentVariable("GRANOLA_API_KEY", "grn_...", "User")

# Daily at 07:30. Replace the paths with your clone of this repo and your backup folder.
schtasks /Create /SC DAILY /ST 07:30 /TN "Granola backup" /TR "py C:\src\granola-export\scripts\extract_public.py --dest C:\Backups\granola --since-last-run --attachments"
```
