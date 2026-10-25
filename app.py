#!/usr/bin/env python3
"""Local Bilibili and YouTube inbox for an Obsidian vault."""

from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

import bilibili as bili
from catalog import default_providers, extract_items, platform_for
from desktop import open_gallery
from library import load_libraries


# Keep the protocol ID stable so existing local sessions remain compatible.
APP_ID = "obsidian-video-collection-v1"
MAX_TEXT_BYTES = 128 * 1024
MAX_BODY_BYTES = 6 * MAX_TEXT_BYTES + 1024
MAX_ITEMS = 500
STATUSES = ("success", "existing", "duplicate", "failed")
PAGE_CLOSE_GRACE = 1.5
PAGE_START_TIMEOUT = 20
SESSION_PULSE = .35


class BusyError(ValueError):
    pass


def public_error(error):
    if isinstance(error, OSError):
        return "Could not access a local file. Check the folder permissions and try again."
    return str(error)


class ImportApp:
    def __init__(self, inbox: Path, *, providers_factory=default_providers, config_path=None):
        self.inbox = Path(inbox).resolve()
        self.libraries = load_libraries(self.inbox, config_path)
        self.vault = self.libraries["bilibili"].vault
        self.providers_factory = providers_factory
        self.key = hashlib.sha256(str(self.inbox).casefold().encode()).hexdigest()[:20]
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active_job = None
        self.stop_requested = threading.Event()
        self.started_at = time.monotonic()
        self.pages = {}
        self.had_page = False
        self.empty_since = None

    def valid_token(self, value):
        return isinstance(value, str) and secrets.compare_digest(value.encode(), self.token.encode())

    def connect_page(self, client_id):
        if not isinstance(client_id, str) or not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise ValueError("Invalid page session.")
        with self.lock:
            if self.stop_requested.is_set():
                raise BusyError("The tool is closing.")
            connection = secrets.token_hex(12)
            self.pages[client_id] = connection
            self.had_page = True
            self.empty_since = None
            return connection

    def page_connected(self, client_id, connection):
        with self.lock:
            return self.pages.get(client_id) == connection

    def disconnect_page(self, client_id, connection=None):
        with self.lock:
            if client_id not in self.pages:
                if connection is None and not self.pages and not self.had_page:
                    self.had_page = True
                    self.empty_since = time.monotonic()
                return
            if connection is not None and self.pages[client_id] != connection:
                return
            self.pages.pop(client_id)
            if not self.pages:
                self.empty_since = time.monotonic()

    def page_timeout(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.pages:
                return False
            if self.had_page:
                return self.empty_since is not None and now - self.empty_since >= PAGE_CLOSE_GRACE
            return now - self.started_at >= PAGE_START_TIMEOUT

    def count_notes(self):
        return sum(library.count() for library in self.libraries.values())

    def info(self):
        with self.lock:
            return {
                "app_id": APP_ID,
                "collection_key": self.key,
                "count": self.count_notes(),
                "active_job": self.active_job,
            }

    def start_job(self, text):
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Input is too long. Keep each batch under 128 KB.")
        items = extract_items(text)
        if not items:
            raise ValueError("Paste a video link or share text first.")
        if len(items) > MAX_ITEMS:
            raise ValueError(f"Import up to {MAX_ITEMS} links per batch.")
        with self.lock:
            if self.stop_requested.is_set():
                raise BusyError("The tool is closing.")
            if self.active_job:
                raise BusyError("Another import is running. Try again when it finishes.")
            while len(self.jobs) >= 20:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = secrets.token_hex(12)
            self.jobs[job_id] = {
                "id": job_id, "state": "running", "total": len(items),
                "processed": 0, "results": [], "input_text": text,
                "counts": {status: 0 for status in STATUSES}, "remaining_text": "", "error": "",
            }
            self.active_job = job_id
        threading.Thread(target=self.process, args=(job_id, items), daemon=True).start()
        return job_id

    def snapshot(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return json.loads(json.dumps(self.jobs[job_id]))

    def process(self, job_id, items):
        seen = set()
        try:
            providers = self.providers_factory()
            for index, item in enumerate(items, 1):
                if self.stop_requested.is_set():
                    break
                row = {"source": item.source, "platform": "", "video_id": "", "url": "", "title": "", "status": "failed", "message": ""}
                try:
                    if not item.target:
                        raise bili.ImportProblem("No video link found. Paste a Bilibili or YouTube link.")
                    platform = platform_for(item.target)
                    provider = providers[platform]
                    library = self.libraries[platform]
                    identifier = provider.resolve(item.target)
                    key = (platform, identifier)
                    row.update(platform=platform, video_id=identifier,
                               url=(f"https://www.bilibili.com/video/{identifier}/" if platform == "bilibili" else
                                    f"https://www.youtube.com/watch?v={identifier}"))
                    existing = library.has_video(identifier)
                    row["title"] = library.title(identifier) if existing else identifier
                    if key in seen:
                        row["status"] = "duplicate"
                    elif existing:
                        seen.add(key)
                        row["status"] = "existing"
                    else:
                        seen.add(key)
                        video = provider.fetch_video(identifier)
                        if video.platform != platform or video.identifier != identifier:
                            raise bili.ImportProblem("The returned video does not match the link.")
                        cover, extension = provider.fetch_cover(video)
                        if self.stop_requested.is_set():
                            break
                        saved = library.save(video, cover, extension)
                        row.update(status="success" if saved else "existing", title=video.title)
                except (bili.ImportProblem, OSError, UnicodeError) as exc:
                    row.update(status="failed", message=public_error(exc))
                with self.lock:
                    job = self.jobs[job_id]
                    job["results"].append(row)
                    job["processed"] = index
                    job["counts"][row["status"]] += 1
        except Exception:
            # Keep both completed rows and every unprocessed source retryable.
            with self.lock:
                self.jobs[job_id]["error"] = "Import interrupted. Saved videos are safe; retry the remaining links."
        finally:
            with self.lock:
                job = self.jobs[job_id]
                remaining = [row["source"] for row in job["results"] if row["status"] == "failed"]
                remaining.extend(item.source for item in items[job["processed"]:])
                job.update(state="done", remaining_text="\n".join(dict.fromkeys(remaining)))
                self.active_job = None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, app):
        self.app = app
        self.stopped = threading.Event()
        self.shutdown_lock = threading.Lock()
        self.shutdown_requested = False
        super().__init__(address, Handler)

    def request_shutdown(self):
        with self.shutdown_lock:
            if self.shutdown_requested:
                return
            self.shutdown_requested = True
            self.app.stop_requested.set()
            self.stopped.set()
            threading.Thread(target=self.shutdown, daemon=True).start()

    def shutdown(self):
        self.app.stop_requested.set()
        self.stopped.set()
        super().shutdown()

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoToObsidian/1"

    def log_message(self, *_):
        pass

    def reply(self, status, payload, *, mime="application/json; charset=utf-8", headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, dict) else payload
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self, *, mutation=False):
        expected = urlsplit(self.server.origin).netloc
        origin = self.headers.get("Origin")
        if (self.headers.get("Host") != expected or
                (origin and origin != self.server.origin) or
                self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.reply(403, {"error": "Access is limited to the local tool page."})
            return False
        if mutation and not self.server.app.valid_token(self.headers.get("X-Bili-Token", "")):
            self.reply(403, {"error": "Session updated. Refresh the page and try again."})
            return False
        return True

    def page_session(self, query):
        app = self.server.app
        parameters = parse_qs(query)
        if not app.valid_token(parameters.get("token", [""])[0]):
            self.reply(403, {"error": "Invalid page session."})
            return
        client_id = parameters.get("client", [""])[0]
        try:
            connection = app.connect_page(client_id)
        except ValueError as exc:
            self.reply(400, {"error": str(exc)})
            return
        try:
            self.connection.settimeout(2)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 200\nevent: ready\ndata: {}\n\n")
            self.wfile.flush()
            while not self.server.stopped.wait(SESSION_PULSE):
                if not app.page_connected(client_id, connection):
                    break
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.close_connection = True
            app.disconnect_page(client_id, connection)

    def do_GET(self):
        if not self.trusted():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        app = self.server.app
        if path == "/":
            nonce = secrets.token_urlsafe(20)
            config = json.dumps({"token": app.token, "client": secrets.token_hex(16)}, ensure_ascii=False).replace("<", "\\u003c")
            page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
            page = page.replace("__NONCE__", nonce).replace("__BOOTSTRAP__", config)
            self.reply(200, page.encode(), mime="text/html; charset=utf-8", headers={
                "Content-Security-Policy": "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            })
        elif path == "/api/session":
            self.page_session(parsed.query)
        elif path == "/api/info":
            self.reply(200, app.info())
        elif path.startswith("/api/jobs/"):
            try:
                self.reply(200, app.snapshot(path.removeprefix("/api/jobs/")))
            except KeyError:
                self.reply(404, {"error": "Task ended or tool restarted. Import again to continue."})
        elif path == "/favicon.ico":
            self.reply(204, b"", mime="image/x-icon")
        else:
            self.reply(404, {"error": "Page not found."})

    def do_POST(self):
        path = urlsplit(self.path).path
        beacon_action = path in {"/api/client-close", "/api/shutdown"}
        if not self.trusted(mutation=not beacon_action):
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "Use the import page to perform this action."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("Request is empty or too large.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Invalid request format.")
            app = self.server.app
            if beacon_action and not app.valid_token(self.headers.get("X-Bili-Token") or payload.get("token")):
                self.reply(403, {"error": "Invalid page session."})
                return
            if path == "/api/client-close":
                client_id = payload.get("client")
                if not isinstance(client_id, str) or not re.fullmatch(r"[a-f0-9]{32}", client_id):
                    raise ValueError("Invalid page session.")
                app.disconnect_page(client_id)
                self.reply(200, {"ok": True})
            elif path == "/api/jobs":
                if payload.get("action", "import") != "import":
                    raise ValueError("Unsupported action.")
                job_id = app.start_job(payload.get("text"))
                self.reply(202, {"id": job_id})
            elif path == "/api/open-gallery":
                platform = payload.get("platform", "bilibili")
                if not isinstance(platform, str) or platform not in app.libraries:
                    raise ValueError("Unknown video platform.")
                library = app.libraries[platform]
                library.prepare()
                open_gallery(library.root / "Gallery.base", library.vault.name)
                self.reply(200, {"ok": True})
            elif path == "/api/shutdown":
                try:
                    self.reply(200, {"ok": True})
                finally:
                    self.server.request_shutdown()
            else:
                self.reply(404, {"error": "Action not found."})
        except BusyError as exc:
            self.reply(409, {"error": str(exc)})
        except (ValueError, UnicodeError, OSError) as exc:
            self.reply(400, {"error": public_error(exc)})


def find_server(app, preferred_port):
    """Reuse this collection's running tool, without a persistent PID or port file."""
    opener = build_opener(ProxyHandler({}))
    candidate = None
    for port in range(preferred_port, preferred_port + 20):
        try:
            server = LocalServer(("127.0.0.1", port), app)
            if candidate is None:
                candidate = server
            else:
                server.server_close()
        except OSError:
            try:
                with opener.open(Request(f"http://127.0.0.1:{port}/api/info"), timeout=0.4) as response:
                    info = json.loads(response.read(8192))
                if info.get("app_id") == APP_ID and info.get("collection_key") == app.key:
                    if candidate:
                        candidate.server_close()
                    return None, f"http://127.0.0.1:{port}/"
            except (OSError, ValueError, URLError):
                pass
    if candidate:
        return candidate, None
    raise OSError("No local port is available. Try again later.")


def page_monitor(server):
    while not server.stopped.wait(.1):
        if server.app.page_timeout():
            server.request_shutdown()
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description="Save Bilibili and YouTube videos to Obsidian.")
    parser.add_argument("--inbox", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65515:
        parser.error("--port must be between 1024 and 65515.")
    app = ImportApp(args.inbox, config_path=args.config)
    server, existing_url = find_server(app, args.port)
    url = existing_url or server.origin + "/"
    if not args.no_browser:
        webbrowser.open(url)
    if sys.stdout:
        print(f"Video to Obsidian: {url}", flush=True)
    if not server:
        return 0
    threading.Thread(target=page_monitor, args=(server,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_requested.set()
        server.stopped.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
