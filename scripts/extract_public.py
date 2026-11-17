#!/usr/bin/env python3
"""
granola-export (public API edition) — cross-platform, stdlib only.

Targets Granola's official public API (docs.granola.ai, OpenAPI v1.7.0) with a
key generated in the app: Settings -> Connectors -> API keys (Business/Enterprise).

The key is read from the GRANOLA_API_KEY environment variable (never argv).

    PowerShell:  $env:GRANOLA_API_KEY = "grn_..."
    bash:        export GRANOLA_API_KEY=grn_...

Usage:
    python scripts/extract_public.py                       # everything your key can see
    python scripts/extract_public.py --list-folders        # show folder ids
    python scripts/extract_public.py --folder-id <fol_id> --attachments
    python scripts/extract_public.py --dest D:\\granola --created-after 2026-01-01
    python scripts/extract_public.py --render-only         # re-layout from saved JSON, offline

Output layout (one Granola folder -> one directory; one note -> one directory):
    <dest>/<Granola folder>/<Note title>/<Note title>.json   raw API response (source of truth)
    <dest>/<Granola folder>/<Note title>/<Note title>.md     agent-readable rendering
    <dest>/<Granola folder>/<Note title>/attachments/        only with --attachments
    <dest>/INDEX.md, folders.json, spaces.json, manifest.json
Notes in no folder go to "Unfiled"; a note in nested folders goes in its most specific one.

Network: only https://public-api.granola.ai, plus (only with --attachments) the
         https attachment URLs the API returns. The API key is never sent to
         attachment hosts, and API requests never follow redirects.
Disk:    writes only under --dest. All names derive from validated ids / sanitized titles.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.2.0"

API_BASE = "https://public-api.granola.ai/v1"
DEFAULT_DEST = Path.cwd() / "granola-data"
NOTE_ID_RE = re.compile(r"^not_[A-Za-z0-9]{14}$", re.I)
FOLDER_ID_RE = re.compile(r"^fol_[A-Za-z0-9]{14}$", re.I)
SPACE_ID_RE = re.compile(r"^spa_[A-Za-z0-9]{14}$", re.I)
ATT_ID_RE =re.compile(r"^att_[A-Za-z0-9]{1,40}$", re.I)
MIN_INTERVAL = 0.22          # ~4.5 req/s, under the 5/s sustained limit
SYNC_OVERLAP = timedelta(minutes=15)   # re-list a little before the watermark (clock skew)
EXIT_OK, EXIT_PARTIAL, EXIT_CONFIG, EXIT_API = 0, 1, 2, 3
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


class ApiError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects on API calls, so the Bearer key can't be forwarded."""
    def redirect_request(self, *a, **k):
        return None


_api_opener = urllib.request.build_opener(_NoRedirect)


def api_get(path: str, key: str, params: dict | None = None, retries: int = 5):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "granola-export-public/2.0",
    })
    for attempt in range(retries):
        time.sleep(MIN_INTERVAL)
        try:
            with _api_opener.open(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise ApiError(f"HTTP {e.code} on {path}: {body}", e.code)
        except urllib.error.URLError as e:
            raise ApiError(f"Network error on {path}: {e}")
    raise ApiError(f"Gave up after {retries} retries on {path}")


def paginate(path: str, key: str, item_key: str, params: dict | None = None):
    """Yield items across cursor pages. Cursor requests must not repeat filters."""
    page = api_get(path, key, params)
    while True:
        yield from page.get(item_key) or []
        cursor = page.get("cursor")
        if not page.get("hasMore") or not cursor:
            return
        page = api_get(path, key, {"cursor": cursor, **({"page_size": params["page_size"]}
                                                       if params and "page_size" in params else {})})


def fetch_transcript(key: str, note_id: str) -> list[dict]:
    return list(paginate(f"/notes/{note_id}/transcript", key, "transcript", {"page_size": 100}))


def fetch_note(key: str, note_id: str, want_transcript: bool) -> dict:
    if not want_transcript:
        return api_get(f"/notes/{note_id}", key)
    try:
        return api_get(f"/notes/{note_id}", key, {"include": "transcript"})
    except ApiError as e:
        if e.code != 413:
            raise
        note = api_get(f"/notes/{note_id}", key)  # TRANSCRIPT_TOO_LARGE -> page it
        note["transcript"] = fetch_transcript(key, note_id)
        return note


# ─── Rendering ────────────────────────────────────────────────────────────────

def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_user(u) -> str:
    if not isinstance(u, dict):
        return str(u)
    return f"{u.get('name') or ''} <{u.get('email') or ''}>".strip()


def fmt_speaker(sp) -> str:
    if not isinstance(sp, dict):
        return "?"
    if sp.get("name"):
        return sp["name"]
    if sp.get("attribution"):                       # "me" | "them"
        return {"me": "Me", "them": "Them"}.get(sp["attribution"], sp["attribution"])
    return sp.get("diarization_label") or {"microphone": "Mic", "speaker": "Speaker"}.get(
        sp.get("source"), sp.get("source") or "?")


def render(note: dict, att_files: dict[str, str]) -> str:
    title = (note.get("title") or "untitled").strip()
    ev0 = note.get("calendar_event") or {}
    q = json.dumps  # JSON strings are valid YAML scalars
    md = ["---", f"id: {q(note.get('id'))}", f"title: {q(title, ensure_ascii=False)}",
          f"meeting_at: {q(ev0.get('scheduled_start_time'))}",
          f"created_at: {q(note.get('created_at'))}", f"updated_at: {q(note.get('updated_at'))}",
          f"owner: {q((note.get('owner') or {}).get('email'))}",
          "folders: " + q([f.get("name") for f in note.get("folder_membership") or [] if isinstance(f, dict)],
                          ensure_ascii=False),
          "---", ""]
    md += [f"# {title}\n", f"- **ID:** `{note.get('id')}`"]
    if note.get("web_url"):
        md.append(f"- **Link:** {note['web_url']}")
    ev = note.get("calendar_event") or {}
    when = ev.get("scheduled_start_time")
    for label, v in (("Meeting", when), ("Created", note.get("created_at")),
                     ("Updated", note.get("updated_at")), ("Deleted", note.get("deleted_at"))):
        if v:
            md.append(f"- **{label}:** {v}")
    if note.get("owner"):
        md.append(f"- **Owner:** {fmt_user(note['owner'])}")
    if ev.get("event_title"):
        md.append(f"- **Calendar event:** {ev['event_title']}")
    folders = [f.get("name") for f in note.get("folder_membership") or [] if isinstance(f, dict)]
    spaces = [s.get("name") for s in note.get("space_membership") or [] if isinstance(s, dict)]
    if folders:
        md.append(f"- **Folders:** {', '.join(map(str, folders))}")
    if spaces:
        md.append(f"- **Spaces:** {', '.join(map(str, spaces))}")
    md.append("")

    attendees = note.get("attendees") or []
    if attendees:
        md += ["## Attendees\n"] + [f"- {fmt_user(a)}" for a in attendees] + [""]

    private = note.get("private_notes_markdown") or note.get("private_notes_text")
    if private:
        md += ["## My Notes\n", private.strip(), ""]
    summary = note.get("summary_markdown") or note.get("summary_text")
    if summary:
        md += ["## Summary\n", summary.strip(), ""]

    atts = [a for a in note.get("attachments") or [] if isinstance(a, dict)]
    if atts:
        md.append("## Attachments\n")
        for a in atts:
            aid = a.get("id", "?")
            line = f"- `{aid}` ({a.get('type')}, {a.get('width')}×{a.get('height')})"
            if a.get("description"):
                line += f" — {a['description']}"
            if aid in att_files:
                line += f" → `attachments/{att_files[aid]}`"
            md.append(line)
        md.append("")

    tr = note.get("transcript")
    if isinstance(tr, list) and tr:
        md += ["---\n", "## Transcript\n"]
        for seg in tr:
            if not isinstance(seg, dict) or not (seg.get("text") or "").strip():
                continue
            t = parse_ts(seg.get("start_time"))
            md.append(f"`{t.strftime('%H:%M:%S') if t else ''}` **[{fmt_speaker(seg.get('speaker'))}]** "
                      f"{seg['text'].strip()}")
        md.append("")
    return "\n".join(md)


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
NO_FOLDER = "_none"
STAGING = "_incoming"


def safe_name(title: str | None, maxlen: int = 50) -> str:
    """Human-friendly, Windows-safe file/folder name (keeps case and spaces)."""
    s = re.sub(r"(?<=\d):(?=\d)", "-", title or "")          # "1:1" -> "1-1"
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .") or "Untitled"
    if s.split(".")[0].upper() in WIN_RESERVED:
        s = "_" + s
    return s[:maxlen].rstrip(" .")


def note_date(note: dict) -> str:
    ev = note.get("calendar_event") or {}
    t = parse_ts(ev.get("scheduled_start_time") or note.get("created_at"))
    return t.strftime("%Y-%m-%d") if t else "0000-00-00"


def unique_stems(notes: list[dict]) -> dict[str, str]:
    """
    note id -> folder/file stem, unique within one Granola folder. A title used once is
    just '<Title>'; repeats (recurring meetings) become '<Title> (YYYY-MM-DD)', then add a
    short id. Case-insensitive, because Windows names are.
    """
    groups: dict[str, list[dict]] = {}
    for n in notes:
        groups.setdefault(safe_name(n.get("title")).lower(), []).append(n)
    stems: dict[str, str] = {}
    for group in groups.values():
        stem = safe_name(group[0].get("title"))
        if len(group) == 1:
            stems[group[0]["id"]] = stem
            continue
        by_date: dict[str, list[dict]] = {}
        for n in group:
            by_date.setdefault(note_date(n), []).append(n)
        for date, same in by_date.items():
            for n in same:
                stems[n["id"]] = f"{stem} ({date})" + ("" if len(same) == 1 else f" {n['id'][-6:]}")
    seen: set[str] = set()                    # guarantee uniqueness
    for nid in sorted(stems):
        if stems[nid].lower() in seen:
            stems[nid] += f" {nid}"
        seen.add(stems[nid].lower())
    return stems


def leaf_folder(note: dict) -> dict | None:
    """Most specific Granola folder (membership also lists ancestors). Ties: first by name."""
    members = [f for f in note.get("folder_membership") or [] if isinstance(f, dict) and f.get("id")]
    if not members:
        return None
    parents = {f.get("parent_folder_id") for f in members}
    leaves = [f for f in members if f["id"] not in parents] or members
    return sorted(leaves, key=lambda f: ((f.get("name") or "").lower(), f["id"]))[0]


def folder_dirnames(notes: list[dict]) -> dict[str, str]:
    """folder id -> directory name (NO_FOLDER -> 'Unfiled'); same-named folders get an id suffix."""
    names = {NO_FOLDER: "Unfiled"}
    for n in notes:
        f = leaf_folder(n)
        if f:
            names[f["id"]] = f.get("name") or "Untitled folder"
    by_lower: dict[str, list[str]] = {}
    for fid in sorted(names):
        by_lower.setdefault(safe_name(names[fid], 40).lower(), []).append(fid)
    out = {}
    for fids in by_lower.values():
        for fid in fids:
            base = safe_name(names[fid], 40)
            out[fid] = base if len(fids) == 1 else f"{base} ({fid[-6:]})"
    return out


def load_existing(dest: Path) -> tuple[dict[str, dict], dict[str, list[Path]]]:
    """Find every saved note JSON under dest (current tree, staging, or the old json/ layout)."""
    notes: dict[str, dict] = {}
    paths: dict[str, list[Path]] = {}
    found: list[Path] = []
    for root, dirs, files in os.walk(dest):
        # Skip hidden dirs (.git, .github, a checked-out .exporter, ...) so dest can live in a repo.
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if Path(root) == dest:                  # folders.json / spaces.json / manifest.json
            continue
        found += [Path(root) / f for f in sorted(files) if f.endswith(".json")]
    for p in found:
        try:
            n = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"  ! unreadable {p}", file=sys.stderr)
            continue
        if isinstance(n, dict) and NOTE_ID_RE.match(str(n.get("id", ""))):
            nid = n["id"]
            paths.setdefault(nid, []).append(p)
            if nid not in notes or (n.get("updated_at") or "") >= (notes[nid].get("updated_at") or ""):
                notes[nid] = n
    return notes, paths


def existing_attachments(note: dict, att_dir: Path) -> dict[str, str]:
    out = {}
    if att_dir.is_dir():
        for a in note.get("attachments") or []:
            aid = a.get("id", "") if isinstance(a, dict) else ""
            if ATT_ID_RE.match(aid):
                hit = next(att_dir.glob(f"{aid}.*"), None)
                if hit:
                    out[aid] = hit.name
    return out


def build_tree(dest: Path, notes: dict[str, dict], old_paths: dict[str, list[Path]],
               write_md: bool = True, download_ids: set[str] | None = None) -> int:
    """
    Lay notes out as  <dest>/<Granola folder>/<Note>/<Note>.json + <Note>.md  and clean up
    whatever older location (staging, json/, meetings/, renamed folders) they came from.
    """
    ordered = sorted(notes.values(), key=lambda n: (note_date(n), n["id"]))
    fdirs = folder_dirnames(ordered)
    groups: dict[str, list[dict]] = {}
    for n in ordered:
        f = leaf_folder(n)
        groups.setdefault(f["id"] if f else NO_FOLDER, []).append(n)

    written: set[Path] = set()
    index: list[tuple[str, str, str, str]] = []
    for fid, group in groups.items():
        stems = unique_stems(group)
        for n in group:
            stem, nid = stems[n["id"]], n["id"]
            ndir = dest / fdirs[fid] / stem
            ndir.mkdir(parents=True, exist_ok=True)
            jpath = ndir / f"{stem}.json"
            for p in old_paths.get(nid, []):                 # carry attachments to the new home
                att = p.parent / "attachments"
                if p.parent != ndir and att.is_dir() and not (ndir / "attachments").exists():
                    shutil.move(str(att), str(ndir / "attachments"))
            jpath.write_text(json.dumps(n, indent=2, ensure_ascii=False), encoding="utf-8")
            written.add(jpath)
            if download_ids and nid in download_ids:
                att_files = download_attachments(n, ndir / "attachments")
            else:
                att_files = existing_attachments(n, ndir / "attachments")
            if write_md:
                mpath = ndir / f"{stem}.md"
                mpath.write_text(render(n, att_files), encoding="utf-8")
                written.add(mpath)
            index.append((fdirs[fid], note_date(n), n.get("title") or "Untitled", f"{fdirs[fid]}/{stem}/{stem}.md"))

    for nid, plist in old_paths.items():                    # remove superseded copies
        for p in plist:
            if p in written:
                continue
            p.unlink(missing_ok=True)
            if p.parent.parent != dest or p.parent.name not in ("json", STAGING):
                m = p.with_suffix(".md")
                if m not in written:
                    m.unlink(missing_ok=True)
            for d in (p.parent, p.parent.parent):
                if d != dest:
                    try:
                        d.rmdir()                           # only succeeds if empty
                    except OSError:
                        pass

    if write_md:
        legacy = dest / "meetings"                          # previous flat layout (v0.2 dev builds)
        legacy_index = legacy / "INDEX.md"
        if legacy_index.is_file() and legacy_index.read_text(encoding="utf-8", errors="replace") \
                .startswith("# Granola Export"):          # only clean up output we generated
            for old in legacy.glob("*.md"):
                old.unlink()
            try:
                legacy.rmdir()
            except OSError:
                pass
        lines = ["# Granola Export — Index\n"]
        for folder in sorted({i[0] for i in index}, key=str.lower):
            lines.append(f"\n## {folder}\n")
            for _, d, t, rel in sorted((i for i in index if i[0] == folder), key=lambda i: (i[1], i[2])):
                lines.append(f"- {d} [{t.replace('[', '(').replace(']', ')')}]({urllib.parse.quote(rel)})")
        (dest / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(notes)


# ─── Attachments (opt-in) ─────────────────────────────────────────────────────

def download_attachments(note: dict, att_dir: Path) -> dict[str, str]:
    """https-only, size-capped, validated filenames, no API key sent."""
    saved: dict[str, str] = {}
    if any(isinstance(a, dict) for a in note.get("attachments") or []):
        att_dir.mkdir(parents=True, exist_ok=True)
    for a in note.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        aid, url = a.get("id", ""), a.get("url", "")
        if not ATT_ID_RE.match(aid) or urllib.parse.urlparse(url).scheme != "https":
            print(f"  ! skipping attachment {aid!r}: bad id or non-https URL", file=sys.stderr)
            continue
        existing = next(att_dir.glob(f"{aid}.*"), None)
        if existing:
            saved[aid] = existing.name
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "granola-export-public/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(MAX_ATTACHMENT_BYTES + 1)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    print(f"  ! {aid}: exceeds size cap", file=sys.stderr)
                    continue
                ext = IMAGE_EXT.get(resp.headers.get_content_type(), ".bin")
            (att_dir / f"{aid}{ext}").write_bytes(data)
            saved[aid] = f"{aid}{ext}"
        except Exception as e:
            print(f"  ! attachment {aid}: {e}", file=sys.stderr)
    return saved


# ─── Main ─────────────────────────────────────────────────────────────────────

def read_manifest(dest: Path) -> dict:
    try:
        m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--since-last-run", action="store_true",
                    help="only list notes updated since the last fully successful run (watermark in "
                         "manifest.json); falls back to a full export when there is no watermark")
    ap.add_argument("--created-after")
    ap.add_argument("--created-before")
    ap.add_argument("--updated-after")
    ap.add_argument("--folder-id")
    ap.add_argument("--space-id")
    ap.add_argument("--no-transcripts", action="store_true")
    ap.add_argument("--attachments", action="store_true", help="download image attachments (opt-in)")
    ap.add_argument("--skip-render", action="store_true", help="write .json only, no .md")
    ap.add_argument("--refetch", action="store_true", help="ignore cache and re-fetch every note")
    ap.add_argument("--render-only", action="store_true",
                    help="re-lay-out and re-render from saved JSON (no key or network)")
    ap.add_argument("--list-folders", action="store_true", help="print folder ids and names, then exit")
    args = ap.parse_args(argv)

    if args.folder_id and not FOLDER_ID_RE.match(args.folder_id):
        ap.error(f"--folder-id must look like fol_ + 14 letters/digits (got {args.folder_id!r}); "
                 "run with --list-folders to see yours")
    if args.space_id and not SPACE_ID_RE.match(args.space_id):
        ap.error(f"--space-id must look like spa_ + 14 letters/digits (got {args.space_id!r})")
    if args.since_last_run and args.updated_after:
        ap.error("--since-last-run and --updated-after are mutually exclusive")

    # Windows consoles default to cp1252; avoid crashes on non-ASCII output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.render_only:   # no key or network needed
        notes, paths = load_existing(args.dest)
        build_tree(args.dest, notes, paths, write_md=not args.skip_render)
        print(f"Laid out {len(notes)} notes under {args.dest}")
        return EXIT_OK

    key = os.environ.get("GRANOLA_API_KEY", "").strip()
    if not key.startswith("grn_"):
        print("Set GRANOLA_API_KEY to your grn_... key (see --help).", file=sys.stderr)
        return EXIT_CONFIG

    if args.list_folders:
        for f in paginate("/folders", key, "folders", {"page_size": 30}):
            print(f"{f.get('id')}  {f.get('name')}")
        return EXIT_OK

    run_started = datetime.now(timezone.utc)
    args.dest.mkdir(parents=True, exist_ok=True)
    stage = args.dest / STAGING          # crash-safe: each fetched note is saved here immediately
    prev = read_manifest(args.dest)
    watermark = (prev.get("sync") or {}).get("watermark")

    updated_after = args.updated_after
    if args.since_last_run:
        wm = parse_ts(watermark) if watermark else None
        if wm:
            updated_after = iso_z(wm - SYNC_OVERLAP)
            print(f"Incremental: notes updated after {updated_after} "
                  f"(last successful run {watermark}, minus {SYNC_OVERLAP})", flush=True)
        else:
            print("Incremental: no watermark yet, doing a full export", flush=True)

    # Folder/space catalogs (useful context; spaces 404 when not enabled)
    for name, item_key in (("folders", "folders"), ("spaces", "spaces")):
        try:
            items = list(paginate(f"/{name}", key, item_key, {"page_size": 30}))
            (args.dest / f"{name}.json").write_text(json.dumps(items, indent=2), encoding="utf-8")
            print(f"{name}: {len(items)}", flush=True)
        except ApiError as e:
            print(f"{name}: skipped ({e})", flush=True)

    notes, old_paths = load_existing(args.dest)
    params = {"page_size": 30, "created_after": args.created_after, "created_before": args.created_before,
              "updated_after": updated_after, "folder_id": args.folder_id, "space_id": args.space_id}
    run_ids: set[str] = set()
    fetched, cached, failed = 0, 0, 0
    print("Listing notes...", flush=True)
    for stub in paginate("/notes", key, "notes", params):
        nid = stub.get("id", "")
        if not NOTE_ID_RE.match(nid):
            print(f"  ! skipping unexpected note id {nid!r}", file=sys.stderr)
            continue
        have = notes.get(nid)
        if have and not args.refetch and have.get("updated_at") == stub.get("updated_at"):
            cached += 1                  # unchanged since our saved copy
        else:
            try:
                note = fetch_note(key, nid, not args.no_transcripts)
            except ApiError as e:
                failed += 1
                print(f"  ! {nid}: {e}", file=sys.stderr)
                continue
            stage.mkdir(exist_ok=True)
            sp = stage / f"{nid}.json"
            sp.write_text(json.dumps(note, indent=2, ensure_ascii=False), encoding="utf-8")
            notes[nid] = note
            old_paths.setdefault(nid, []).append(sp)
            fetched += 1
            print(f"  ✓ {nid}", flush=True)
        run_ids.add(nid)

    build_tree(args.dest, notes, old_paths, write_md=not args.skip_render,
               download_ids=run_ids if args.attachments else None)

    # Advance the watermark only after a complete, unscoped run: a run limited to one folder or
    # date range (or one with failures) must not make the next --since-last-run skip notes.
    scoped = any((args.created_after, args.created_before, args.folder_id, args.space_id, args.updated_after))
    if failed == 0 and not scoped:
        watermark = iso_z(run_started)
    filters = {k: v for k, v in params.items() if v and k != "page_size"}
    manifest = {
        "tool": "granola-export/extract_public.py",
        "version": __version__,
        "api": API_BASE,
        "sync": {"watermark": watermark},
        "last_run": {
            "started_at": iso_z(run_started),
            "finished_at": iso_z(datetime.now(timezone.utc)),
            "mode": "since-last-run" if args.since_last_run else ("scoped" if scoped else "full"),
            "filters": filters,
            "listed": len(run_ids) + failed, "fetched": fetched, "cached": cached, "failed": failed,
            "transcripts_requested": not args.no_transcripts,
            "attachments_downloaded": args.attachments,
        },
        "notes_on_disk": len(notes),
    }
    (args.dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nDone: {fetched} fetched, {cached} unchanged, {failed} failed; "
          f"{len(notes)} notes on disk. Output: {args.dest}")
    if failed:
        print(f"{failed} note(s) failed; everything else was saved. Watermark not advanced, "
              "so the next --since-last-run retries them.", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"\nAPI error: {e}", file=sys.stderr)
        sys.exit(EXIT_API)
    except KeyboardInterrupt:
        print("\nInterrupted (re-run to resume; saved notes are kept).", file=sys.stderr)
        sys.exit(130)
