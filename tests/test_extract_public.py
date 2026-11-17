"""
Tests for scripts/extract_public.py — no network. Layout logic is tested on synthetic notes;
main() runs end-to-end against a local fake of the public API.

    python -m unittest tests.test_extract_public -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import extract_public as e  # noqa: E402

PARENT = {"id": "fol_aaaaaaaaaaaaaa", "name": "1:1", "parent_folder_id": None}
CHILD = {"id": "fol_bbbbbbbbbbbbbb", "name": "Alex", "parent_folder_id": PARENT["id"]}
PROJ = {"id": "fol_cccccccccccccc", "name": "Projects", "parent_folder_id": None}
PROJ2 = {"id": "fol_dddddddddddddd", "name": "projects", "parent_folder_id": None}


def note(suffix, title, folders, date="2026-03-01", atts=None, updated=None):
    return {"id": f"not_{suffix:0>14}", "title": title, "created_at": f"{date}T10:00:00Z",
            "updated_at": updated or f"{date}T11:00:00Z", "owner": {"name": "O", "email": "o@example.com"},
            "calendar_event": None, "attendees": [], "attachments": atts or [], "folder_membership": folders,
            "summary_markdown": "summary", "summary_text": "summary", "transcript": None}


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class TestNames(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(e.safe_name("1:1"), "1-1")
        self.assertEqual(e.safe_name("Q4 / Plan: <draft>?"), "Q4 Plan draft")
        self.assertEqual(e.safe_name("con"), "_con")
        self.assertEqual(e.safe_name(None), "Untitled")
        self.assertEqual(e.safe_name("  ...  "), "Untitled")
        self.assertLessEqual(len(e.safe_name("x" * 300)), 50)

    def test_unique_stems(self):
        ns = [note("a1", "Sync", [], "2026-01-01"), note("a2", "sync", [], "2026-01-08"),
              note("a3", "Sync", [], "2026-01-08"), note("a4", "Solo", [])]
        stems = e.unique_stems(ns)
        self.assertEqual(stems[ns[3]["id"]], "Solo")
        self.assertEqual(stems[ns[0]["id"]], "Sync (2026-01-01)")
        self.assertEqual(len({s.lower() for s in stems.values()}), 4)

    def test_leaf_folder(self):
        self.assertEqual(e.leaf_folder(note("a", "t", [PARENT, CHILD]))["id"], CHILD["id"])
        self.assertIsNone(e.leaf_folder(note("a", "t", [])))


class TestBuildTree(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_tree(self, notes, paths=None):
        e.build_tree(self.root, {n["id"]: n for n in notes}, paths or {})

    def test_layout(self):
        self.run_tree([note("a1", "Weekly", [PARENT, CHILD]), note("a2", "Weekly", [PARENT, CHILD], "2026-03-08"),
                       note("a3", "Plan", [PROJ]), note("a4", "Other", [PROJ2]), note("a5", "Loose", []),
                       note("a6", "1:1 with Alex", [PARENT])])
        files = tree(self.root)
        self.assertIn("Alex/Weekly (2026-03-01)/Weekly (2026-03-01).json", files)
        self.assertIn("Alex/Weekly (2026-03-08)/Weekly (2026-03-08).md", files)
        self.assertIn("1-1/1-1 with Alex/1-1 with Alex.md", files)
        self.assertIn("Unfiled/Loose/Loose.json", files)
        self.assertIn("Projects (cccccc)/Plan/Plan.json", files)       # case-colliding folder names
        self.assertIn("projects (dddddd)/Other/Other.json", files)
        self.assertIn("INDEX.md", files)
        self.assertFalse(any(f.startswith("1-1/Weekly") for f in files))

    def test_relocation_moves_attachments_and_prunes(self):
        n = note("a1", "Plan", [PROJ])
        self.run_tree([n])
        old = self.root / "Projects" / "Plan"
        (old / "attachments").mkdir()
        (old / "attachments" / "att_xyz.png").write_bytes(b"png")
        moved = note("a1", "Roadmap", [CHILD],
                     atts=[{"id": "att_xyz", "type": "photo", "width": 1, "height": 1, "url": "https://x"}])
        self.run_tree([moved], {n["id"]: [old / "Plan.json"]})
        files = tree(self.root)
        self.assertIn("Alex/Roadmap/attachments/att_xyz.png", files)
        self.assertFalse(any("Plan" in f for f in files))
        self.assertFalse((self.root / "Projects").exists())
        self.assertIn("attachments/att_xyz.png", (self.root / "Alex/Roadmap/Roadmap.md").read_text(encoding="utf-8"))

    def test_rerun_keeps_fresh_files(self):
        ns = [note("b1", "Same", [CHILD], "2026-04-01"), note("b2", "Same", [CHILD], "2026-04-01")]
        self.run_tree(ns)
        loaded, paths = e.load_existing(self.root)
        e.build_tree(self.root, loaded, paths)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 2)

    def test_load_existing_skips_hidden_dirs(self):
        hidden = self.root / ".git" / "objects"
        hidden.mkdir(parents=True)
        (hidden / "x.json").write_text(json.dumps(note("zz", "Hidden", [])), encoding="utf-8")
        notes, _ = e.load_existing(self.root)
        self.assertEqual(notes, {})

    def test_unrelated_meetings_folder_untouched(self):
        other = self.root / "meetings"
        other.mkdir()
        (other / "mine.md").write_text("my own file", encoding="utf-8")
        self.run_tree([note("a1", "T", [])])
        self.assertTrue((other / "mine.md").exists())


# ─── End-to-end against a fake API ─────────────────────────────────────────────

IDS = [f"not_abcdefghijklm{c}" for c in "ABC"]
FOLDER = {"id": "fol_zzzzzzzzzzzzzz", "name": "Team Sync", "parent_folder_id": None}


class FakeApi(BaseHTTPRequestHandler):
    notes_queries: list = []
    fail_ids: set = set()

    def log_message(self, *a):
        pass

    @staticmethod
    def note(i, big=False):
        n = note("x", "Standup", [FOLDER] if i != IDS[2] else [], f"2026-01-0{IDS.index(i) + 1}")
        n["id"] = i
        n["transcript"] = None if big else [{"speaker": {"source": "speaker"}, "text": "hello",
                                             "start_time": "2026-01-01T00:00:01Z", "end_time": "2026-01-01T00:00:02Z"}]
        return n

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self.headers.get("Authorization") != "Bearer grn_test":
            return self.reply(401, {})
        if u.path == "/v1/notes":
            FakeApi.notes_queries.append(q)
            if "cursor" not in q:
                return self.reply(200, {"notes": [self.note(IDS[0]), self.note(IDS[1])], "hasMore": True, "cursor": "c1"})
            return self.reply(200, {"notes": [self.note(IDS[2])], "hasMore": False, "cursor": None})
        if u.path == "/v1/folders":
            return self.reply(200, {"folders": [FOLDER], "hasMore": False, "cursor": None})
        if u.path == "/v1/spaces":
            return self.reply(404, {})
        if u.path.endswith("/transcript"):
            seg = self.note(IDS[0])["transcript"][0]
            if "cursor" not in q:
                return self.reply(200, {"transcript": [seg], "hasMore": True, "cursor": "t1"})
            return self.reply(200, {"transcript": [seg], "hasMore": False, "cursor": None})
        if u.path.startswith("/v1/notes/"):
            nid = u.path.split("/")[3]
            if nid in FakeApi.fail_ids:
                return self.reply(404, {"code": "NOTE_NOT_FOUND"})
            if nid == IDS[1] and q.get("include"):
                return self.reply(413, {"code": "TRANSCRIPT_TOO_LARGE"})
            return self.reply(200, self.note(nid, big=(nid == IDS[1])))
        self.reply(404, {})

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.dest = Path(tempfile.mkdtemp())
        FakeApi.notes_queries, FakeApi.fail_ids = [], set()
        patches = [mock.patch.object(e, "API_BASE", f"http://127.0.0.1:{self.server.server_port}/v1"),
                   mock.patch.object(e, "MIN_INTERVAL", 0),
                   mock.patch.dict(os.environ, {"GRANOLA_API_KEY": "grn_test"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def run_main(self, *extra):
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return e.main(["--dest", str(self.dest), *extra])

    def manifest(self):
        return json.loads((self.dest / "manifest.json").read_text(encoding="utf-8"))

    def test_full_then_incremental(self):
        self.assertEqual(self.run_main("--since-last-run"), e.EXIT_OK)      # no watermark yet -> full
        self.assertNotIn("updated_after", FakeApi.notes_queries[0])
        files = tree(self.dest)
        self.assertIn("Team Sync/Standup (2026-01-01)/Standup (2026-01-01).json", files)
        self.assertIn("Unfiled/Standup/Standup.md", files)
        self.assertFalse((self.dest / e.STAGING).exists())
        big = json.loads((self.dest / "Team Sync/Standup (2026-01-02)/Standup (2026-01-02).json").read_text("utf-8"))
        self.assertEqual(len(big["transcript"]), 2)                         # 413 -> paged transcript
        wm = self.manifest()["sync"]["watermark"]
        self.assertTrue(wm)

        snapshot = {f: (self.dest / f).read_bytes() for f in files if f != "manifest.json"}
        FakeApi.notes_queries = []
        self.assertEqual(self.run_main("--since-last-run"), e.EXIT_OK)
        sent = FakeApi.notes_queries[0]["updated_after"]
        self.assertLess(sent, wm)                                           # watermark minus overlap
        self.assertEqual(self.manifest()["last_run"]["cached"], 3)
        after = {f: (self.dest / f).read_bytes() for f in tree(self.dest) if f != "manifest.json"}
        self.assertEqual(snapshot, after, "unchanged notes must produce byte-identical files (clean git diffs)")

    def test_partial_failure_keeps_watermark(self):
        self.assertEqual(self.run_main(), e.EXIT_OK)
        wm = self.manifest()["sync"]["watermark"]
        FakeApi.fail_ids = {IDS[0]}
        self.assertEqual(self.run_main("--since-last-run", "--refetch"), e.EXIT_PARTIAL)
        self.assertEqual(self.manifest()["sync"]["watermark"], wm)
        self.assertEqual(self.manifest()["last_run"]["failed"], 1)
        self.assertEqual(len(list(self.dest.rglob("Standup*.json"))), 3)  # earlier copy still on disk

    def test_scoped_run_does_not_set_watermark(self):
        self.assertEqual(self.run_main("--folder-id", FOLDER["id"]), e.EXIT_OK)
        self.assertIsNone(self.manifest()["sync"]["watermark"])
        self.assertEqual(FakeApi.notes_queries[0]["folder_id"], FOLDER["id"])

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"GRANOLA_API_KEY": ""}):
            self.assertEqual(self.run_main(), e.EXIT_CONFIG)

    def test_render_only_is_offline(self):
        self.run_main()
        shutil.rmtree(self.dest / "Unfiled")
        with mock.patch.object(e, "API_BASE", "http://127.0.0.1:9/unreachable"):
            self.assertEqual(self.run_main("--render-only"), e.EXIT_OK)
        self.assertNotIn("Unfiled/Standup/Standup.md", tree(self.dest))   # JSON was deleted too


if __name__ == "__main__":
    unittest.main()
