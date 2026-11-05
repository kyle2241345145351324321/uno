"""Web-gateway tests for the add-to-home-screen assets."""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import net  # noqa: E402
import webserver  # noqa: E402


class PwaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = net.HostSession("Kyle", port=45897, discovery=False)
        self.host.start()
        self.web = webserver.WebServer(self.host.port, 18091)
        self.wport = self.web.start()

    def tearDown(self) -> None:
        self.web.close()
        self.host.close()

    def get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.wport}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_manifest(self):
        status, ctype, body = self.get("/manifest.webmanifest")
        manifest = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("application/manifest+json"))
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/"))

    def test_icons_are_png(self):
        manifest = json.loads(self.get("/manifest.webmanifest")[2])
        paths = [i["src"] for i in manifest["icons"]] + ["/apple-touch-icon.png", "/favicon.ico"]
        for path in paths:
            status, ctype, body = self.get(path)
            self.assertEqual((status, ctype), (200, "image/png"), path)
            self.assertTrue(body.startswith(b"\x89PNG"), path)

    def test_page_links_manifest_and_icon(self):
        html = self.get("/")[2].decode("utf-8")
        for needle in ('rel="manifest"', 'rel="apple-touch-icon"', 'id="btnPwa"'):
            self.assertIn(needle, html)

    def test_no_path_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/icons/../net.py")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
