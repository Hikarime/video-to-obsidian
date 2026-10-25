from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import ImportApp, LocalServer


class GalleryTests(unittest.TestCase):
    def setUp(self):
        output = Path(__file__).resolve().parents[1] / ".test-output" / "gallery-unit"
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(self.temp.cleanup)
        vault = Path(self.temp.name)
        (vault / ".obsidian").mkdir()
        self.app = ImportApp(vault / "Inbox")
        self.server = LocalServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request("POST", path, json.dumps(payload),
                               {"Content-Type": "application/json", "X-Bili-Token": self.app.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_both_platforms_launch_their_own_gallery(self):
        with patch("app.open_gallery") as launch:
            for platform in ("bilibili", "youtube"):
                with self.subTest(platform=platform):
                    status, result = self.request("/api/open-gallery", {"platform": platform})
                    self.assertEqual((status, result), (200, {"ok": True}))
                    document = self.app.libraries[platform].root / "Gallery.base"
                    launch.assert_called_with(document, self.app.vault.name)
                    self.assertTrue(document.is_file())
            self.assertEqual(launch.call_count, 2)

    def test_invalid_platform_does_not_launch_anything(self):
        with patch("app.open_gallery") as launch:
            status, result = self.request("/api/open-gallery", {"platform": "unknown"})
            self.assertEqual(status, 400)
            self.assertEqual(result, {"error": "Unknown video platform."})
            launch.assert_not_called()

    def test_removed_check_action_cannot_accidentally_import(self):
        with patch.object(self.app, "start_job") as start:
            status, _ = self.request("/api/jobs", {"action": "check", "text": "BV17x411w7KC"})
            self.assertEqual(status, 400)
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
