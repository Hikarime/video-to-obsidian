#!/usr/bin/env python3
"""Public Bilibili metadata, link parsing, and safe file helpers."""

from __future__ import annotations

import gzip
import html
import http.cookiejar
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)
import zlib


BV_PATTERN = r"[Bb][Vv][A-Za-z0-9]{10}"
BV_RE = re.compile(rf"(?<![A-Za-z0-9])({BV_PATTERN})(?![A-Za-z0-9])")
TRAILING_PUNCTUATION = ".,;:!?，。；：！？、）)]】}>"
VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
SHORT_HOSTS = {"b23.tv", "www.b23.tv"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


class ImportProblem(Exception):
    """An actionable, per-item import failure."""


class FetchProblem(ImportProblem):
    def __init__(self, message: str, *, url: str = "", status: int = 0):
        super().__init__(message)
        self.url = url
        self.status = status


@dataclass(frozen=True)
class InputItem:
    source: str
    target: str | None


@dataclass(frozen=True)
class VideoRef:
    bvid: str = ""
    aid: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid or ('av' + self.aid)}/"


@dataclass(frozen=True)
class Part:
    number: int
    title: str
    duration: int


@dataclass(frozen=True)
class Video:
    bvid: str
    title: str
    uploader: str
    cover_url: str
    duration_seconds: int
    parts: tuple[Part, ...]

    @property
    def url(self) -> str:
        return VideoRef(bvid=self.bvid).url


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    url: str


def normalize_bvid(value: str) -> str:
    if not re.fullmatch(BV_PATTERN, value):
        raise ImportProblem("Use a 12-character Bilibili BV ID.")
    # Only the prefix is case-insensitive; the remaining BV characters are not.
    return "BV" + value[2:]


def parse_video_url(value: str) -> VideoRef | str:
    value = value.strip()
    if re.fullmatch(BV_PATTERN, value):
        return VideoRef(bvid=normalize_bvid(value))
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ImportProblem("Invalid video link.") from exc
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ImportProblem("Use a Bilibili video link or b23.tv short link.")
    if port not in {None, 80, 443}:
        raise ImportProblem("Video links cannot use a custom port.")
    if host in SHORT_HOSTS:
        if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
            raise ImportProblem("Invalid b23.tv short link.")
        return urlunsplit(("https", host, parsed.path, parsed.query, ""))
    if host not in VIDEO_HOSTS:
        raise ImportProblem("Use a bilibili.com video link or b23.tv short link.")
    match = re.fullmatch(rf"/video/({BV_PATTERN}|[Aa][Vv][0-9]+)/?", unquote(parsed.path))
    if not match:
        raise ImportProblem("Use a Bilibili video page containing /video/BV.")
    identifier = match.group(1)
    if identifier[:2].lower() == "bv":
        return VideoRef(bvid=normalize_bvid(identifier))
    return VideoRef(aid=str(int(identifier[2:])))


def allowed_remote_host(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 80, 443}
            and any(host == domain or host.endswith("." + domain)
                    for domain in ("bilibili.com", "b23.tv", "hdslb.com"))
        )
    except ValueError:
        return False


class BiliRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_remote_host(newurl):
            raise FetchProblem("Bilibili redirected to an unsupported website.", url=newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def __init__(self, *, timeout: float = 20, attempts: int = 3, sleep=time.sleep):
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.last_request = 0.0
        self.opener = build_opener(
            BiliRedirectHandler(), HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def get(self, url: str, *, read_body: bool = True) -> FetchResult:
        if not allowed_remote_host(url):
            raise FetchProblem("Unsupported Bilibili resource.", url=url)
        for attempt in range(self.attempts):
            wait = 0.5 - (time.monotonic() - self.last_request)
            if wait > 0:
                self.sleep(wait)
            self.last_request = time.monotonic()
            request = Request(url, headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json,text/html,image/*;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            })
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    if not read_body:
                        return FetchResult(b"", response.geturl())
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise FetchProblem("Bilibili's response is too large.", url=url)
                    encoding = response.headers.get("Content-Encoding", "").lower()
                    try:
                        if encoding == "gzip":
                            body = gzip.decompress(body)
                        elif encoding == "deflate":
                            body = zlib.decompress(body)
                    except (OSError, EOFError, zlib.error) as exc:
                        raise FetchProblem("Could not decompress Bilibili's response.", url=url) from exc
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise FetchProblem("Bilibili's decompressed response is too large.", url=url)
                    return FetchResult(body, response.geturl())
            except HTTPError as exc:
                status = exc.code
                final_url = exc.geturl()
                exc.close()
                if (status == 429 or 500 <= status < 600) and attempt + 1 < self.attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise FetchProblem(f"Bilibili returned HTTP {status}. Try again later.", url=final_url, status=status) from exc
            except (URLError, TimeoutError, socket.timeout, OSError) as exc:
                if attempt + 1 < self.attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise FetchProblem("Could not connect to Bilibili. Check your connection and try again.", url=url) from exc
        raise FetchProblem("Could not complete the Bilibili request.", url=url)


def integer(value, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ImportProblem(f"Missing video {label}.")
    try:
        number = int(value)
    except ValueError as exc:
        raise ImportProblem(f"Invalid video {label}.") from exc
    if number < minimum:
        raise ImportProblem(f"Invalid video {label}.")
    return number


def video_from_data(data: dict) -> Video:
    if not isinstance(data, dict):
        raise ImportProblem("Bilibili returned invalid video metadata.")
    bvid = normalize_bvid(str(data.get("bvid", "")))
    title = data.get("title")
    cover = data.get("pic")
    if not isinstance(title, str) or not title.strip():
        raise ImportProblem("The video title is missing.")
    if not isinstance(cover, str) or not cover:
        raise ImportProblem("The video cover is missing.")
    if cover.startswith("//"):
        cover = "https:" + cover
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]
    if not allowed_remote_host(cover):
        raise ImportProblem("Unsupported Bilibili cover URL.")
    parts = []
    raw_pages = data.get("pages") or []
    if not isinstance(raw_pages, list):
        raise ImportProblem("Bilibili returned an invalid part list.")
    for index, page in enumerate(raw_pages, 1):
        if not isinstance(page, dict):
            raise ImportProblem("The video part list is incomplete.")
        number = integer(page.get("page", index), "part number", minimum=1)
        duration = integer(page.get("duration"), "part duration")
        part_title = str(page.get("part") or f"P{number}")
        parts.append(Part(number, part_title, duration))
    declared_count = integer(data.get("videos", len(parts) or 1), "part count", minimum=1)
    if parts:
        parts.sort(key=lambda part: part.number)
        if [part.number for part in parts] != list(range(1, len(parts) + 1)):
            raise ImportProblem("The video part list is incomplete.")
        if declared_count > len(parts):
            raise ImportProblem("Some video parts are missing. Try again later.")
        total_duration = sum(part.duration for part in parts)
    else:
        if declared_count > 1:
            raise ImportProblem("The multi-part video list is unavailable.")
        total_duration = integer(data.get("duration"), "duration", minimum=1)
        parts = [Part(1, title.strip(), total_duration)]
    if total_duration <= 0:
        raise ImportProblem("The video duration is missing or invalid.")
    owner = data.get("owner") or {}
    uploader = owner.get("name", "") if isinstance(owner, dict) else ""
    if not isinstance(uploader, str) or not uploader.strip():
        raise ImportProblem("The uploader information is missing.")
    return Video(bvid, title.strip(), uploader.strip(), cover, total_duration, tuple(parts))


def data_from_webpage(body: bytes) -> dict:
    text = body.decode("utf-8-sig", errors="replace")
    match = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", text)
    if not match:
        raise ImportProblem("The video page does not contain complete metadata.")
    try:
        state, _ = json.JSONDecoder().raw_decode(text[match.end():])
    except json.JSONDecodeError as exc:
        raise ImportProblem("Could not read the video page metadata.") from exc
    if not isinstance(state, dict) or not isinstance(state.get("videoData"), dict):
        raise ImportProblem("The video page metadata is incomplete.")
    return state["videoData"]


class BiliClient:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()

    def resolve(self, target: str) -> VideoRef:
        parsed = parse_video_url(target)
        if isinstance(parsed, VideoRef):
            return parsed
        try:
            destination = self.http.get(parsed, read_body=False).url
        except FetchProblem as exc:
            # A short link may resolve correctly even when its final video page
            # declines the subsequent GET. Only use an actual final video URL.
            destination = exc.url
            try:
                result = parse_video_url(destination)
            except ImportProblem:
                raise exc
            if isinstance(result, VideoRef):
                return result
            raise exc
        result = parse_video_url(destination)
        if not isinstance(result, VideoRef):
            raise ImportProblem("The short link did not lead to a video page.")
        return result

    def fetch_video(self, ref: VideoRef) -> Video:
        query = urlencode({"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid})
        errors = []
        # The current WBI endpoint accepts these public metadata requests.
        # Keep the previous endpoint and public-page state as compatibility paths.
        for endpoint in ("wbi/view", "view"):
            try:
                response = self.http.get(f"https://api.bilibili.com/x/web-interface/{endpoint}?{query}")
                try:
                    payload = json.loads(response.body.decode("utf-8-sig"))
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise ImportProblem("Bilibili returned unreadable metadata.") from exc
                if not isinstance(payload, dict):
                    raise ImportProblem("Bilibili returned invalid metadata.")
                if payload.get("code") != 0:
                    raise ImportProblem(f"Video unavailable (Bilibili code {payload.get('code', 'unknown')}).")
                video = video_from_data(payload.get("data"))
                if ref.bvid and video.bvid != ref.bvid:
                    raise ImportProblem("Bilibili returned a different video ID.")
                return video
            except ImportProblem as exc:
                errors.append(str(exc))
        try:
            data = data_from_webpage(self.http.get(ref.url).body)
            video = video_from_data(data)
            if ref.bvid and video.bvid != ref.bvid:
                raise ImportProblem("The page returned a different video ID.")
            return video
        except ImportProblem as exc:
            errors.append(str(exc))
            raise ImportProblem(" ".join(dict.fromkeys(errors))) from exc

    def fetch_cover(self, video: Video) -> tuple[bytes, str]:
        body = self.http.get(video.cover_url).body
        return body, image_extension(body)


def image_extension(body: bytes) -> str:
    if body.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    if len(body) >= 12 and body[4:8] == b"ftyp" and body[8:12] in {b"avif", b"avis"}:
        return ".avif"
    raise ImportProblem("The cover response is not a supported image.")


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def yaml_scalar(value) -> str:
    # JSON scalar/array syntax is also valid YAML; this safely quotes titles,
    # colons, hashes, quotes, and line breaks without a third-party dependency.
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def markdown_text(value: str) -> str:
    value = html.escape(value, quote=False).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_[\]{}|])", r"\\\1", value)


def atomic_write(path: Path, data: bytes, *, replace: bool = True) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".bili-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp_path, path)
        elif os.name == "nt":
            # Windows rename fails if the destination exists. This is both
            # atomic and non-overwriting, including with simultaneous imports.
            try:
                os.rename(temp_path, path)
            except FileExistsError:
                return False
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError:
                return False
        return True
    finally:
        temp_path.unlink(missing_ok=True)
