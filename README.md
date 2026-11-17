# granola-export

> **Fork of [moona3k/granola-export](https://github.com/moona3k/granola-export)** that adds a cross-platform exporter built on Granola's official public API. It is the one to use on **Windows**, and it supports [scheduled backups to a private GitHub repo](docs/scheduled-backup.md). Jump to [Windows setup](#windows-and-linuxmacos-the-public-api-exporter). The original macOS exporter (`scripts/extract.py`) is unchanged.

Recover your own [Granola.ai](https://granola.ai) data — meeting notes, full transcripts, AI summaries, and attachments — via Granola's REST API, using the auth tokens already stored locally by the Mac app.

## What Granola offers natively, and where this script fits

Granola has been shipping aggressively in 2026 and now has **three official ways** to get your data out. Worth knowing about all of them before deciding what you need.

**1. CSV export.** Settings → Profile → "Generate CSV". Per Granola's docs: "enabled by default on Business and Basic plans" (Basic = the free tier; Enterprise admin-toggle). Emailed within a few hours. Includes note titles and short summaries; **does not** include full transcripts, attachments, or AI panel content. Rate-limited to 1 export per 24h.

**2. Personal API.** A documented public REST API at `docs.granola.ai/introduction` — `GET /v1/notes` (paginated) and `GET /v1/notes/{id}`. Returns full notes with transcript and summary as JSON. Auth via `grn_` API keys. Rate-limited to 5 req/sec, 300/min, 25 burst. **Available on Business and Enterprise plans only.**

**3. Granola MCP server** (launched Feb 4, 2026). Official MCP server at `docs.granola.ai/help-center/sharing/integrations/mcp`. Connects directly to Claude Desktop, Claude Code, ChatGPT (Plus/Pro/Business/Enterprise), Cursor, and any MCP-compatible client. OAuth-based (Dynamic Client Registration — no API keys to juggle). **Free tier gets the last 30 days of notes; paid plans get full history + transcripts + shared folders.** The `get_meeting_transcript` tool is paid-only.

> **Recommendation**: If you're on Business or Enterprise, **start with Granola's official MCP server** for AI agent access. It's their supported path, OAuth-handled for you, and exposes the same notes + transcripts. Use the Personal API if you want raw JSON. Use this script only for the specific corners those paths don't reach.

What this script covers that the official paths don't:

| | Granola CSV | Granola Personal API | Granola MCP | this script |
|---|---|---|---|---|
| Plan | Basic + Business by default | Business + Enterprise only | Basic gets 30-day window; paid gets full | Any tier — uses your local app's session |
| Format | CSV emailed | JSON via REST | MCP tool calls in your AI agent | **Markdown files, one per meeting**, on disk |
| Auth | none — UI button | API keys (`grn_...`) | OAuth (DCR) | locally-cached app session |
| Notes content | titles + short summaries | full notes + transcript + summary | full notes + transcripts (paid) | full notes + AI panels (with `original_content` + edited) |
| Transcripts | ❌ | ✅ | ✅ (paid only) | ✅ (with mic-vs-system audio diarization) |
| Attachments | ❌ | ❌ | unclear | ✅ inline images downloaded |
| Audio | — Granola doesn't store audio (real-time transcription, then deleted — a deliberate privacy choice) | — same | — same | — same |
| Folders / recipes / panel templates / people profiles | ❌ | ❌ | folders yes; rest unclear | ✅ all captured as auxiliary endpoints |
| Free-tier full history | Subject to 30-day UI gate (probably) | N/A (paid only) | ❌ 30 days only | ✅ full history (the API returns everything) |
| Local file output | ❌ (CSV only, emailed) | ❌ (JSON via API) | ❌ (lives in agent context) | ✅ `meetings/<date>_<slug>_<id>.md` per meeting |
| Idempotent backup | manual every 24h | hand-rolled | not designed for backup | ✅ skip-if-exists, runs as cron |

Built for users who:

- Want one Markdown file per meeting on disk — for grep, Obsidian/Logseq import, periodic backups, or feeding to your own LLM workflows that prefer file-based input
- Are on the **free tier** and want full history (Granola MCP only gives free users the last 30 days; this script gets everything the API will serve)
- Want auxiliary data (recipes, panel templates, calendar sync state, attendee profiles) the official `/v1/notes` API and current MCP tool surface don't expose
- Want a courtesy script for one-shot or scheduled extraction without setting up API keys or OAuth flows

## Prior art

This is a community space, not unprecedented work. Several existing tools cover similar ground:

- [theantichris/granola](https://github.com/theantichris/granola) — Markdown exporter
- [magarcia/granola-cli](https://magarcia.io/reverse-engineered-meeting-notes-into-terminal/) — CLI with a thoughtful reverse-engineering writeup
- [wassimk/granary](https://github.com/wassimk/granary) — cache-aware Markdown export, preserves transcripts even after Granola's local cache rotates them
- [pedramamini/granola-mcp](https://lobehub.com/mcp/pedramamini-granola-mcp) — MCP server exposing Granola data to AI agents
- [Joseph Thacker's writeup](https://josephthacker.com/hacking/2025/05/08/reverse-engineering-granola-notes.html) on getting Granola notes into Obsidian

What this one tries to add: Claude Code skill packaging (auto-loadable for AI coding agents), paid-tier endpoint coverage that auto-stubs on free accounts, idempotent skip-if-exists for incremental backups, single-file Python install with no dependencies beyond stdlib. None of it makes the others wrong; it's just a different point in the design space.

## What you get

A single command produces:

```
~/reference/granola/extracted/my-data/
├── docs.json                 all document metadata (46 fields per doc)
├── transcripts/<id>.json     word-level transcripts with diarization (mic vs system audio)
├── panels/<id>.json          AI summary panels (ProseMirror JSON, original + edited versions)
├── attachments/              inline image attachments
├── aux/                      folders, recipes, templates, calendar, integrations, ...
└── meetings/                 rendered Markdown — one file per meeting
    └── INDEX.md              chronological master index
```

Run time scales with account size — expect minutes, not hours, for typical accounts. The script is idempotent: re-runs skip already-fetched data.

## What you don't get

Honest about limits — verified by reverse-engineering Granola's client code:

- **Audio recordings.** Granola intentionally doesn't store audio — it's transcribed in real time and then deleted. A deliberate privacy choice on their part. The `audio_file_handle` field on document records is a vestige of the upload-then-transcribe pipeline, not a pointer to a persistent file. So no tool, anywhere, can recover the actual audio.
- **Chat history (Chat with documents).** No discoverable list endpoint via REST. Persisted client-side in IndexedDB; would require separate disk-parsing tooling.
- **Action items, pre-meeting briefs, follow-up emails, ambient context.** Server-paywalled — return `403 "feature not enabled"` on free-tier accounts. They're often summarized inside the AI summary panels we *do* fetch.

If your account is on a paid tier, the paywalled endpoints work automatically — no code changes needed.

## Quick start

Requirements (macOS exporter; on Windows/Linux see the public-API exporter below):
- macOS with the Granola desktop app installed and logged in
- Python 3.10+
- ~10 minutes

```bash
git clone https://github.com/kknapp1/granola-export
cd granola-export
python3 scripts/extract.py
```

Output appears in `~/reference/granola/extracted/my-data/`.

The script is **idempotent** — re-run anytime to top up new documents without re-fetching existing ones.

## Windows (and Linux/macOS): the public-API exporter

`scripts/extract.py` (above) is **macOS-only**: on Windows the Granola app stores its session encrypted (`%APPDATA%\Granola\supabase.json.enc`), so there is no token file to read. `scripts/extract_public.py` is a separate, cross-platform exporter. It is built on Granola's **official** public API ([docs.granola.ai](https://docs.granola.ai)) and authenticates with an API key you create yourself. It uses no local app files, only the Python standard library.

### Requirements

- A Granola **Business or Enterprise** plan, which is required to create API keys.
- Python 3.10 or newer.

### Windows setup

1. **Install Python** if `py --version` doesn't print 3.10 or newer:
   ```powershell
   winget install Python.Python.3.12
   ```
   Open a new terminal afterwards. On Windows, `py` (the Python launcher) is the most reliable command, and `python` also works when it's on PATH.
2. **Get the code:**
   ```powershell
   git clone https://github.com/kknapp1/granola-export
   cd granola-export
   ```
3. **Create an API key** in the Granola desktop app under Settings → Connectors → API keys.
   - **Scopes:** A personal key can read notes you own, notes shared with you (the "Personal notes" scope), and notes in workspace-visible folders ("Public notes"). Select both for a full backup.
   - **Workspace keys:** Admins can create these instead (Settings → Connectors → Workspace API keys).
4. **Make the key available.** Never pass the key on the command line, where it would land in your shell history. Use one of these:
   ```powershell
   # For this terminal session only:
   $env:GRANOLA_API_KEY = "grn_..."

   # Or persist it for your Windows user (stored in plain text in your user environment;
   # open a new terminal, or restart VS Code, before it takes effect):
   [Environment]::SetEnvironmentVariable("GRANOLA_API_KEY", "grn_...", "User")
   ```
5. **Run it:**
   ```powershell
   py scripts/extract_public.py --list-folders           # show your folder ids
   py scripts/extract_public.py --folder-id <fol_id>     # try one small folder first
   py scripts/extract_public.py --attachments            # full backup, including image attachments
   py scripts/extract_public.py --since-last-run         # afterwards: only what changed since the last run
   ```
   Output goes to `.\granola-data` by default. Use `--dest D:\Backups\granola` to put it elsewhere. Avoid deeply nested destinations, because Windows limits full paths to 260 characters.

### Output

There is one directory per Granola folder, and one directory per note. Each note directory holds the raw JSON, which is the source of truth, and an agent-readable Markdown version:

```
granola-data/
├── Team Sync/
│   └── Weekly planning (2026-03-08)/
│       ├── Weekly planning (2026-03-08).json   raw API response: summary, your notes, transcript, attendees, calendar, folders
│       └── Weekly planning (2026-03-08).md     metadata header, summary, notes, attendees, transcript
├── 1-1/ ...
├── Unfiled/              notes that are in no Granola folder
├── INDEX.md   folders.json   spaces.json   manifest.json
```

- **Nested folders are flattened.** Each note goes in its most specific Granola folder.
- **Names:** Repeated titles get a date suffix, and names are Windows-safe (`1:1` becomes `1-1`).
- **Re-runs are incremental.** A note is re-fetched only when its `updated_at` changed. `--since-last-run` also limits the listing to notes changed since the last successful run.
- **Renames and moves follow Granola.** If a title or folder changes in Granola, the files are moved to match.
- **Offline re-render.** `--render-only` rebuilds the layout and Markdown from the saved JSON with no key or network.
- **Interruptions are safe.** Each fetched note is saved immediately, so an interrupted run loses nothing.
- **Attachments** (`--attachments`) are opt-in, https-only and size-capped. They are saved under `<Note>/attachments/`. Their URLs expire, so they must be downloaded during the run.
- **Rate limits** (5 requests per second) are respected, with retries on HTTP 429.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | Success |
  | 1 | Some notes failed; the rest were saved |
  | 2 | Configuration problem, such as a missing key |
  | 3 | Fatal API error |

**Limits versus the macOS exporter:**

- The public API returns only notes that have a generated AI summary.
- It has no endpoints for recipes, templates, people profiles, or the original-vs-edited summary versions.
- Enterprise admins can disable transcript access for member keys.

For a backup, compare the number of notes in the app with the number of `.json` files exported.

### Scheduled backups

To keep an always-current archive in a **private GitHub repo**, see [docs/scheduled-backup.md](docs/scheduled-backup.md). It covers a ready-to-copy GitHub Actions workflow, how the incremental watermark works, and a Windows Task Scheduler alternative.

## Token expiry (macOS exporter)

Granola's WorkOS access token has a ~1-hour TTL. If `extract.py` errors with "WorkOS access token expired":

```bash
./scripts/refresh_token.sh
```

…or, simplest path: open the Granola desktop app for a few seconds. It silently refreshes its token on launch and writes a fresh one back to `supabase.json`. Then re-run `extract.py`.

## Install as an agent skill (optional)

The repo doubles as a Claude-Code-format skill. Drop it where your agent looks:

```bash
# Claude Code
git clone https://github.com/kknapp1/granola-export ~/.claude/skills/granola-export

# Codex (or any agent following the same skill convention)
git clone https://github.com/kknapp1/granola-export
ln -s "$PWD/granola-export/SKILL.md" "${CODEX_HOME:-$HOME/.codex}/skills/granola-export/SKILL.md"
```

Trigger phrases like "extract granola data", "backup granola", "recover granola notes" auto-load it.

## How it works

1. Reads your active **WorkOS** access token from `~/Library/Application Support/Granola/supabase.json` (Granola migrated from Cognito; the Cognito tokens in the file are vestigial)
2. Calls `/v2/get-documents` paginated to enumerate every document
3. Fetches `/v1/get-document-transcript` + `/v1/get-document-panels` per document, parallel
4. Walks doc records for inline image attachments and downloads them from S3 presigned URLs
5. Fetches 15+ auxiliary endpoints (folders, recipes, templates, people, calendar, ...)
6. Renders one Markdown file per meeting + a master index

Full technical detail in [`SKILL.md`](SKILL.md), [`references/auth.md`](references/auth.md), [`references/endpoints.md`](references/endpoints.md), and [`references/data-shapes.md`](references/data-shapes.md).

## Status

Built and tested April 2026 against Granola Mac app v7.155.1.

| | |
|--|--|
| Auth (WorkOS Bearer) | ✅ |
| Document enumeration | ✅ |
| Transcripts (with diarization) | ✅ |
| AI summary panels | ✅ |
| Image attachments | ✅ |
| Auxiliary endpoints (folders, recipes, etc.) | ✅ |
| Token refresh | ⚠️ JWT decode works; the WorkOS token URL is inferred but not live-verified. Easy fallback: open the desktop app. |
| Audio download | ❌ No endpoint exists in Granola's codebase |
| Chat history | ❌ No discoverable REST endpoint; lives in IndexedDB |

Granola's API is undocumented. **Expect occasional breakage** as they change things server-side. Issues + PRs welcome when this happens.

## Compliance

This script extracts data the user already owns, using the user's own authenticated session, against endpoints the user's own desktop app calls. Personal data portability is a recognized right under [GDPR Article 20](https://gdpr-info.eu/art-20-gdpr/), [CCPA](https://oag.ca.gov/privacy/ccpa), [UK GDPR](https://ico.org.uk/), and [LGPD](https://lgpd-brazil.info/) in their respective jurisdictions.

It does **not**:
- Bypass authentication
- Scrape data from other users' accounts
- Modify Granola's behavior on disk or in transit
- Make any request the user could not make by clicking around the app

**It is your responsibility** to ensure your use complies with [Granola's Terms of Service](https://www.granola.ai/policies/terms) and any applicable laws.

See [DISCLAIMER.md](DISCLAIMER.md) for full terms.

## Contributing

PRs welcome for:
- New endpoint discoveries (Granola changes, new categories of recoverable data)
- Drift fixes when the API changes
- Output format improvements (e.g. better INDEX.md, JSON-Lines option, etc.)
- Cross-platform support for the private-API exporter (Windows / Linux token locations — currently macOS-only; `extract_public.py` already runs anywhere)

Out of scope:
- Anything that runs against accounts the user does not own
- Anything that violates Granola's ToS in spirit (rate-limit abuse, redistribution of others' content, etc.)

## License

[MIT](LICENSE) — do whatever you want with this code, as long as you keep the copyright notice and don't sue me.

## Author

This fork (public-API exporter, Windows support, scheduled backups) is maintained by [@kknapp1](https://github.com/kknapp1). The original project is by [@moona3k](https://github.com/moona3k), built collaboratively with [Claude Code](https://docs.claude.com/en/docs/claude-code/) — discovery, framing, and decisions on this end; most of the Python and docs on Claude's. Also maintainer of [MacParakeet](https://github.com/moona3k/macparakeet) — a fast, private, local-first voice app for Mac.

If you're switching off Granola for privacy or cost reasons, MacParakeet is one option; there are others. The principle is the same: your meetings, your data, your machine.
