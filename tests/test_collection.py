import json
from pathlib import Path
import tempfile
import time
import unittest

import app
import bilibili as bili
from catalog import BilibiliProvider
from library import load_libraries
import youtube
from fixtures import BV, YT, FakeHttp, routes


PROJECT = Path(__file__).resolve().parents[1]


class CollectionTests(unittest.TestCase):
    def setUp(self):
        output = PROJECT / ".test-output" / "unit"
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "Obsidian vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.inbox = self.vault / "Inbox"
        self.http = FakeHttp(routes())
        self.providers = {"bilibili": BilibiliProvider(bili.BiliClient(self.http)), "youtube": youtube.Provider(self.http)}
        self.app = app.ImportApp(self.inbox, providers_factory=lambda: self.providers)

    def job(self, text):
        identifier = self.app.start_job(text)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.app.snapshot(identifier)
            if job["state"] == "done":
                return job
            time.sleep(.01)
        self.fail("Import did not finish")

    def test_mixed_batch_uses_separate_folders_and_deduplicates_each_platform(self):
        result = self.job(f"{BV}\nhttps://youtu.be/{YT}\nhttps://b23.tv/example\nhttps://youtube.com/shorts/{YT}")
        self.assertEqual([row["status"] for row in result["results"]], ["success", "success", "duplicate", "duplicate"])
        self.assertEqual(result["remaining_text"], "")
        for platform, identifier in [("bilibili", BV), ("youtube", YT)]:
            library = self.app.libraries[platform]
            self.assertTrue(library.note_path(identifier).is_file())
            self.assertTrue((library.root / "Gallery.base").is_file())
            self.assertFalse((library.root / "tools").exists())
            gallery = (library.root / "Gallery.base").read_text(encoding="utf-8")
            self.assertIn("imageAspectRatio: 0.5625", gallery)
        self.assertEqual(self.app.count_notes(), 2)

    def test_note_schema_progress_and_notes_match_existing_collection(self):
        self.job(f"{BV}\nhttps://youtu.be/{YT}")
        for platform, identifier, duration in [("bilibili", BV, "01:02:03"), ("youtube", YT, "01:01:03")]:
            library = self.app.libraries[platform]
            note = library.note_path(identifier).read_text(encoding="utf-8")
            self.assertIn(f'duration: "{duration}"', note)
            self.assertIn('watched_time: "00:00"', note)
            self.assertIn("## Notes", note)
            self.assertIn(f"Inbox/{platform}/covers/", note)
            self.assertNotIn("duration_seconds:", note)
            self.assertNotIn("added_at:", note)
            self.assertNotIn("aliases:", note)
        multi = self.app.libraries["bilibili"].note_path(BV).read_text(encoding="utf-8")
        self.assertIn("[!info]- Parts (2)", multi)
        self.assertIn("?p=2", multi)
        self.assertNotIn("Parts", self.app.libraries["youtube"].note_path(YT).read_text(encoding="utf-8"))

    def test_reimport_preserves_notes_tags_progress_and_custom_gallery_without_network(self):
        self.job(f"{BV}\nhttps://youtu.be/{YT}")
        originals = {}
        for platform, identifier in [("bilibili", BV), ("youtube", YT)]:
            library = self.app.libraries[platform]
            note = library.note_path(identifier)
            text = note.read_text(encoding="utf-8").replace('watched_time: "00:00"', 'watched_time: "05:06"').replace("tags: []", 'tags: ["AI"]')
            note.write_text(text + "Personal [[Topic]] ^reference\n", encoding="utf-8")
            (library.root / "Gallery.base").write_text("custom user gallery", encoding="utf-8")
            originals[note] = note.read_bytes()
        before = len(self.http.calls)
        result = self.job(f"{BV}\nhttps://youtube.com/watch?v={YT}&t=20")
        self.assertEqual(result["counts"]["existing"], 2)
        self.assertEqual(len(self.http.calls), before)
        for note, original in originals.items():
            self.assertEqual(note.read_bytes(), original)
            self.assertEqual((note.parent.parent / "Gallery.base").read_text(encoding="utf-8"), "custom user gallery")

    def test_one_failure_continues_other_platform_and_preserves_retry_input(self):
        self.http.routes[f"https://www.youtube.com/watch?v={YT}"] = bili.ImportProblem("Unavailable")
        result = self.job(f"https://youtu.be/{YT}\n{BV}\nhttps://example.org/")
        self.assertEqual([row["status"] for row in result["results"]], ["failed", "success", "failed"])
        self.assertEqual(result["remaining_text"], f"https://youtu.be/{YT}\nhttps://example.org/")

    def test_invalid_links_and_paths_cannot_write_outside_the_vault(self):
        result = self.job("https://example.org/")
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertFalse(self.inbox.exists())
        with self.assertRaises(bili.ImportProblem):
            self.app.libraries["youtube"].note_path("../bad")
        config = self.vault / "config.json"
        config.write_text(json.dumps({"collections": {"youtube": "../outside"}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_libraries(self.inbox, config)

    def test_configuration_allows_relative_paths_but_never_overlapping_collections(self):
        project = self.inbox / "video-to-obsidian"
        project.mkdir(parents=True)
        config = project / "config.json"
        config.write_text(json.dumps({"collections": {"bilibili": "../bilibili", "youtube": "../youtube"}}), encoding="utf-8")
        self.assertEqual(load_libraries(self.inbox, config)["youtube"].root, self.inbox / "youtube")
        config.write_text(json.dumps({"collections": {"youtube": "../bilibili"}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_libraries(self.inbox, config)

    def test_pages_keep_process_alive_and_shutdown_stops_new_imports(self):
        client = "a" * 32
        old = self.app.connect_page(client)
        new = self.app.connect_page(client)
        self.app.disconnect_page(client, old)
        self.assertTrue(self.app.page_connected(client, new))
        self.app.disconnect_page(client)
        self.assertTrue(self.app.page_timeout(self.app.empty_since + app.PAGE_CLOSE_GRACE + .01))
        self.app.stop_requested.set()
        with self.assertRaises(app.BusyError):
            self.app.start_job(BV)
