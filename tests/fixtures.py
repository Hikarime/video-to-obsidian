import json

import bilibili as bili


BV = "BV17x411w7KC"
YT = "jNQXAC9IVRw"
PNG = b"\x89PNG\r\n\x1a\nfixture"


def bili_data(identifier=BV):
    return {"bvid": identifier, "title": 'Video 标题 "quotes"', "owner": {"name": "Creator"},
            "pic": "https://i0.hdslb.com/bfs/archive/cover.png", "duration": 1, "videos": 2,
            "pages": [{"page": 1, "part": "Part one", "duration": 61},
                      {"page": 2, "part": "Part two", "duration": 3662}]}


def yt_player(identifier=YT, duration="3663"):
    return {"playabilityStatus": {"status": "OK"}, "videoDetails": {
        "videoId": identifier, "title": "YouTube title", "author": "Channel", "lengthSeconds": duration,
        "thumbnail": {"thumbnails": [
            {"url": f"https://i.ytimg.com/vi/{identifier}/default.jpg", "width": 120, "height": 90},
            {"url": f"https://i.ytimg.com/vi/{identifier}/hqdefault.jpg", "width": 480, "height": 360}]}}}


def yt_page(player=None):
    return ("<script>var ytInitialPlayerResponse = " + json.dumps(player or yt_player()) +
            ";window.untrustedScriptMustNotRun=true;</script>").encode()


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **_):
        self.calls.append(url)
        result = self.routes.get(url)
        if result is None:
            raise bili.FetchProblem("Fixture resource not found", url=url)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, bili.FetchResult) else bili.FetchResult(result, url)


def routes():
    return {
        f"https://api.bilibili.com/x/web-interface/wbi/view?bvid={BV}": json.dumps({"code": 0, "data": bili_data()}).encode(),
        "https://i0.hdslb.com/bfs/archive/cover.png": PNG,
        f"https://www.youtube.com/watch?v={YT}": yt_page(),
        f"https://i.ytimg.com/vi/{YT}/hqdefault.jpg": PNG,
        "https://b23.tv/example": bili.FetchResult(b"", f"https://www.bilibili.com/video/{BV}/"),
    }
