"""Recognize mixed input and route each video to its own collection."""

import re
from urllib.parse import urlsplit

import bilibili as bili
from library import Video
import youtube


URL_RE = re.compile(
    r"https?://[^\s<>\"'`]+|(?<![\w.@])(?:(?:www\.|m\.|music\.)?"
    r"(?:bilibili\.com/video/|b23\.tv/|youtube\.com/|youtu\.be/))[^\s<>\"'`]+", re.IGNORECASE)


def extract_items(text):
    result = []
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or line == "#" or line.startswith("# "):
            continue
        urls = [match.group().rstrip(bili.TRAILING_PUNCTUATION) for match in URL_RE.finditer(line)]
        if urls:
            result.extend(bili.InputItem(url, url) for url in urls)
        else:
            identifiers = [match.group(1) for match in bili.BV_RE.finditer(line)]
            if identifiers:
                result.extend(bili.InputItem(value, value) for value in identifiers)
            else:
                result.append(bili.InputItem(line, None))
    return result


def platform_for(target):
    if re.fullmatch(bili.BV_PATTERN, target):
        return "bilibili"
    try:
        parsed = urlsplit(target if "://" in target else "https://" + target)
        host = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise bili.ImportProblem("Invalid video link.") from exc
    if host in bili.VIDEO_HOSTS | bili.SHORT_HOSTS:
        return "bilibili"
    if host in youtube.VIDEO_HOSTS | youtube.SHORT_HOSTS:
        return "youtube"
    raise bili.ImportProblem("Use a Bilibili or YouTube video link.")


class BilibiliProvider:
    def __init__(self, client=None):
        self.client = client or bili.BiliClient()
        self.references = {}
        self.cache = {}

    def resolve(self, target):
        if target not in self.references:
            ref = self.client.resolve(target)
            if ref.bvid:
                identifier = ref.bvid
            else:
                video = self.client.fetch_video(ref)
                identifier = video.bvid
                self.cache[identifier] = self.convert(video)
            self.references[target] = identifier
        return self.references[target]

    @staticmethod
    def convert(video):
        return Video("bilibili", video.bvid, video.title, video.uploader, video.url,
                     video.cover_url, video.duration_seconds, video.parts)

    def fetch_video(self, identifier):
        if identifier not in self.cache:
            self.cache[identifier] = self.convert(self.client.fetch_video(bili.VideoRef(bvid=identifier)))
        return self.cache[identifier]

    def fetch_cover(self, video):
        return self.client.fetch_cover(video)


def default_providers():
    return {"bilibili": BilibiliProvider(), "youtube": youtube.Provider()}
