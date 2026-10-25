from __future__ import annotations
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import bilibili as bili
from catalog import extract_items
BV = "BV17x411w7KC"
SECOND_BV = "BV1GJ411x7h7"
PNG = (b"\x89PNG\r\n\x1a\n" + b"test-image")


def metadata(bvid=BV):
    return {
        "bvid": bvid,
        "title": '测试：标题 # 标签 "引号" [链接] | 表格\n换行',
        "owner": {"name": "测试UP主"},
        "pic": "https://i0.hdslb.com/bfs/archive/cover.png",
        "duration": 111,  # Per-part durations, rather than this stale value, win.
        "videos": 2,
        "pages": [
            {"page": 1, "part": "第一部分 | A", "duration": 61},
            {"page": 2, "part": "第二部分 [B]", "duration": 3662},
        ],
    }


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, *, read_body=True):
        self.calls.append((url, read_body))
        result = self.routes.get(url)
        if result is None:
            raise bili.FetchProblem("fixture not found", url=url)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bili.FetchResult):
            return result
        return bili.FetchResult(result, url)


def api_url(bvid=BV, endpoint="wbi/view"):
    return f"https://api.bilibili.com/x/web-interface/{endpoint}?bvid={bvid}"


class ParsingTests(unittest.TestCase):
    def test_share_text_multiple_urls_and_bom(self):
        text = ("\ufeff# 说明\n\n【分享】https://www.bilibili.com/video/" + BV +
                "/?p=2&spm_id_from=123）\nhttps://b23.tv/AbCd9。\n" + SECOND_BV)
        items = extract_items(text)
        self.assertEqual([item.target for item in items], [
            f"https://www.bilibili.com/video/{BV}/?p=2&spm_id_from=123",
            "https://b23.tv/AbCd9", SECOND_BV,
        ])

    def test_markdown_link_and_multiple_links_on_one_line(self):
        items = extract_items(f"[收藏](https://www.bilibili.com/video/{BV}/) https://b23.tv/abcd")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].target, f"https://www.bilibili.com/video/{BV}/")

    def test_bvid_suffix_case_is_preserved(self):
        self.assertEqual(bili.parse_video_url("bv17x411w7KC").bvid, BV)

    def test_tracking_and_p_parameter_produce_whole_video(self):
        ref = bili.parse_video_url(f"https://m.bilibili.com/video/{BV}?p=2&share_source=copy_link#t=20")
        self.assertEqual(ref.url, f"https://www.bilibili.com/video/{BV}/")

    def test_unsupported_host_and_nonvideo_url_are_rejected(self):
        for value in (f"https://evil.example/video/{BV}/", "https://www.bilibili.com/",
                      f"https://www.bilibili.com.evil.example/video/{BV}/",
                      f"https://u:p@www.bilibili.com/video/{BV}/"):
            with self.subTest(value=value), self.assertRaises(bili.ImportProblem):
                bili.parse_video_url(value)

    def test_invalid_line_remains_visible_as_failure(self):
        items = extract_items("这不是视频链接\n")
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].target)

    def test_duration_boundaries(self):
        for seconds, text in [(0, "00:00"), (1, "00:01"), (59, "00:59"), (60, "01:00"),
                              (3599, "59:59"), (3600, "01:00:00"), (7323, "02:02:03"), (360000, "100:00:00")]:
            with self.subTest(seconds=seconds):
                self.assertEqual(bili.format_duration(seconds), text)

    def test_total_duration_comes_from_all_parts(self):
        video = bili.video_from_data(metadata())
        self.assertEqual(video.duration_seconds, 3723)
        self.assertEqual([part.number for part in video.parts], [1, 2])

    def test_missing_part_never_creates_a_shorter_whole_video(self):
        data = metadata()
        data["pages"] = data["pages"][:1]
        with self.assertRaises(bili.ImportProblem):
            bili.video_from_data(data)

    def test_single_part_without_pages_uses_declared_duration(self):
        data = metadata()
        data.update(videos=1, pages=[], duration=3723)
        video = bili.video_from_data(data)
        self.assertEqual(video.duration_seconds, 3723)
        self.assertEqual(len(video.parts), 1)

    def test_bad_metadata_fails_cleanly(self):
        for update in [{"duration": None, "pages": [], "videos": 1}, {"owner": {}},
                       {"pic": "https://example.org/image.jpg"}, {"bvid": "../bad"},
                       {"pages": [{"page": 1, "duration": True}]}]:
            with self.subTest(update=update), self.assertRaises(bili.ImportProblem):
                data = metadata()
                data.update(update)
                bili.video_from_data(data)


class NetworkCompatibilityTests(unittest.TestCase):
    def test_short_link_can_resolve_even_when_destination_page_returns_412(self):
        url = "https://b23.tv/AbCd9"
        http = FakeHttp({url: bili.FetchProblem("HTTP 412", url=f"https://www.bilibili.com/video/{BV}/", status=412)})
        self.assertEqual(bili.BiliClient(http).resolve(url).bvid, BV)

    def test_short_link_does_not_accept_unrelated_redirect(self):
        url = "https://b23.tv/AbCd9"
        http = FakeHttp({url: bili.FetchResult(b"", "https://example.org/")})
        with self.assertRaises(bili.ImportProblem):
            bili.BiliClient(http).resolve(url)

    def test_previous_endpoint_is_a_fallback(self):
        http = FakeHttp({api_url(): bili.FetchProblem("HTTP 412"),
                         api_url(endpoint="view"): json.dumps({"code": 0, "data": metadata()}).encode()})
        self.assertEqual(bili.BiliClient(http).fetch_video(bili.VideoRef(bvid=BV)).bvid, BV)
        self.assertEqual(len(http.calls), 2)

    def test_public_page_state_is_a_fallback_and_scripts_are_not_executed(self):
        payload = json.dumps({"videoData": metadata()}, ensure_ascii=False)
        page = ("<script>window.__INITIAL_STATE__=" + payload +
                ";(function(){throw 'must not execute';}());</script>").encode("utf-8")
        http = FakeHttp({f"https://www.bilibili.com/video/{BV}/": page})
        self.assertEqual(bili.BiliClient(http).fetch_video(bili.VideoRef(bvid=BV)).duration_seconds, 3723)

    def test_mismatched_video_id_is_rejected(self):
        http = FakeHttp({api_url(): json.dumps({"code": 0, "data": metadata(SECOND_BV)}).encode()})
        with self.assertRaises(bili.ImportProblem):
            bili.BiliClient(http).fetch_video(bili.VideoRef(bvid=BV))

    def test_html_error_page_is_not_saved_as_cover(self):
        with self.assertRaises(bili.ImportProblem):
            bili.image_extension(b"<html>Access denied</html>")

    def test_transient_network_error_retries_but_412_does_not(self):
        url = api_url()
        http = bili.HttpClient(sleep=lambda _: None)
        response = io.BytesIO(b"{}")
        response.headers = {}
        response.geturl = lambda: url
        with patch.object(http.opener, "open", side_effect=[URLError("timeout"), response]) as opened:
            self.assertEqual(http.get(url).body, b"{}")
            self.assertEqual(opened.call_count, 2)
        with patch.object(http.opener, "open", side_effect=HTTPError(url, 412, "blocked", {}, None)) as opened:
            with self.assertRaises(bili.FetchProblem):
                http.get(url)
            self.assertEqual(opened.call_count, 1)
