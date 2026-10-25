"""Read public YouTube metadata and thumbnails without downloading video."""

from dataclasses import dataclass
import gzip
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib

import bilibili as bili
from library import Video


VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class Ref:
    identifier: str

    @property
    def url(self):
        return f"https://www.youtube.com/watch?v={self.identifier}"


def parse_url(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError
    except ValueError as exc:
        raise bili.ImportProblem("Invalid YouTube link.") from exc
    path = unquote(parsed.path)
    identifier = ""
    if host in SHORT_HOSTS:
        identifier = path.strip("/")
    elif host in VIDEO_HOSTS:
        if path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            identifier = values[0] if len(values) == 1 else ""
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", path)
            identifier = match.group(1) if match else ""
    else:
        raise bili.ImportProblem("Use a youtube.com or youtu.be video link.")
    if not ID_RE.fullmatch(identifier):
        raise bili.ImportProblem("Use a single YouTube video link, not a channel or playlist.")
    return Ref(identifier)


def allowed_host(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
                and parsed.port in {None, 80, 443}
                and any(host == domain or host.endswith("." + domain) for domain in ("youtube.com", "youtu.be", "ytimg.com")))
    except ValueError:
        return False


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not allowed_host(newurl):
            raise bili.ImportProblem("YouTube redirected to an unsupported resource.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


class HttpClient:
    def __init__(self, timeout=20, sleep=time.sleep):
        self.timeout, self.sleep = timeout, sleep
        self.opener = build_opener(Redirects())
        self.last_request = 0

    def get(self, url):
        if not allowed_host(url):
            raise bili.ImportProblem("Unsupported YouTube resource.")
        for attempt in range(3):
            wait = .4 - (time.monotonic() - self.last_request)
            if wait > 0:
                self.sleep(wait)
            self.last_request = time.monotonic()
            request = Request(url, headers={"User-Agent": bili.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    if not allowed_host(response.url):
                        raise bili.ImportProblem("YouTube redirected to an unsupported resource.")
                    body = response.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise bili.ImportProblem("YouTube's response is too large.")
                    encoding = response.headers.get("Content-Encoding", "")
                    if encoding == "gzip":
                        body = gzip.decompress(body)
                    elif encoding == "deflate":
                        body = zlib.decompress(body)
                    if len(body) > MAX_BYTES:
                        raise bili.ImportProblem("YouTube's response is too large.")
                    return bili.FetchResult(body, response.url)
            except HTTPError as exc:
                if exc.code >= 500 and attempt < 2:
                    self.sleep(attempt + 1)
                    continue
                raise bili.ImportProblem(f"YouTube returned HTTP {exc.code}. Try again later.") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    self.sleep(attempt + 1)
                    continue
                raise bili.ImportProblem("Could not connect to YouTube. Check your connection and try again.") from exc
        raise bili.ImportProblem("Could not read YouTube metadata.")


def player_from_page(body):
    text = body.decode("utf-8-sig", errors="replace")
    pattern = r'(?:var\s+)?ytInitialPlayerResponse\s*=\s*|window\[\s*["\']ytInitialPlayerResponse["\']\s*\]\s*=\s*'
    for match in re.finditer(pattern, text):
        try:
            player, _ = json.JSONDecoder().raw_decode(text[match.end():])
            if isinstance(player, dict) and ("videoDetails" in player or "playabilityStatus" in player):
                return player
        except json.JSONDecodeError:
            pass
    raise bili.ImportProblem("YouTube did not return video metadata. Try a public video link.")


def video_from_player(player, ref):
    def object_value(value):
        return value if isinstance(value, dict) else {}
    if not isinstance(player, dict):
        raise bili.ImportProblem("YouTube returned invalid video metadata.")
    status = object_value(player.get("playabilityStatus")).get("status")
    if status and status != "OK":
        raise bili.ImportProblem("This YouTube video is unavailable, restricted, or requires sign-in.")
    data = player.get("videoDetails")
    if not isinstance(data, dict) or data.get("videoId") != ref.identifier:
        raise bili.ImportProblem("YouTube returned a different or missing video ID.")
    title, author = data.get("title"), data.get("author")
    if not isinstance(title, str) or not title.strip() or not isinstance(author, str) or not author.strip():
        raise bili.ImportProblem("YouTube's title or channel information is incomplete.")
    duration = data.get("lengthSeconds")
    microformat = object_value(object_value(player.get("microformat")).get("playerMicroformatRenderer"))
    live = object_value(microformat.get("liveBroadcastDetails"))
    if live.get("isLiveNow") or not re.fullmatch(r"[0-9]+", str(duration)) or int(duration) <= 0:
        raise bili.ImportProblem("A finished video with a known duration is required.")
    thumbnails = object_value(data.get("thumbnail")).get("thumbnails", [])
    if not isinstance(thumbnails, list):
        thumbnails = []
    images = [image for image in thumbnails if isinstance(image, dict) and isinstance(image.get("url"), str)
              and allowed_host(image["url"]) and (urlsplit(image["url"]).hostname or "").endswith(".ytimg.com")]
    if not images:
        raise bili.ImportProblem("YouTube did not return a usable thumbnail.")
    def size(image):
        try:
            return int(image.get("width", 0)) * int(image.get("height", 0))
        except (TypeError, ValueError):
            return 0
    cover = max(images, key=size)["url"]
    return Video("youtube", ref.identifier, title.strip(), author.strip(), ref.url, cover, int(duration))


class Provider:
    def __init__(self, http=None):
        self.http = http or HttpClient()
        self.cache = {}

    def resolve(self, target):
        return parse_url(target).identifier

    def fetch_video(self, identifier):
        if identifier not in self.cache:
            ref = Ref(identifier)
            self.cache[identifier] = video_from_player(player_from_page(self.http.get(ref.url).body), ref)
        return self.cache[identifier]

    def fetch_cover(self, video):
        body = self.http.get(video.cover_url).body
        return body, bili.image_extension(body)
