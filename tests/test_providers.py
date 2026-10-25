import unittest

import bilibili as bili
import catalog
import youtube
from fixtures import BV, YT, FakeHttp, bili_data, routes, yt_page, yt_player


class ProviderTests(unittest.TestCase):
    def test_mixed_share_text_extracts_both_platforms(self):
        items = catalog.extract_items(f"# ignored\n[分享]https://b23.tv/example。 https://youtu.be/{YT}?t=6\n{BV}\nbad line")
        self.assertEqual(len(items), 4)
        self.assertEqual([catalog.platform_for(item.target) for item in items[:3]], ["bilibili", "youtube", "bilibili"])
        self.assertIsNone(items[-1].target)

    def test_youtube_variants_have_one_canonical_id(self):
        for url in [f"https://www.youtube.com/watch?v={YT}&list=PLtest&t=20", f"https://youtu.be/{YT}?si=test",
                    f"https://youtube.com/shorts/{YT}", f"https://m.youtube.com/live/{YT}",
                    f"https://www.youtube.com/embed/{YT}", f"youtube.com/watch?v={YT}"]:
            with self.subTest(url=url):
                ref = youtube.parse_url(url)
                self.assertEqual(ref.identifier, YT)
                self.assertEqual(ref.url, f"https://www.youtube.com/watch?v={YT}")

    def test_invalid_and_nonvideo_links_are_rejected(self):
        for url in ["https://youtube.com/playlist?list=123", "https://youtube.com/@channel", "https://youtube.com/watch?v=bad",
                    f"https://youtube.com.evil.example/watch?v={YT}", f"https://u:p@youtube.com/watch?v={YT}",
                    f"https://youtube.com:9999/watch?v={YT}", f"file://youtube.com/watch?v={YT}"]:
            with self.subTest(url=url), self.assertRaises(bili.ImportProblem):
                youtube.parse_url(url)

    def test_player_json_is_parsed_without_running_scripts(self):
        player = youtube.player_from_page(yt_page())
        video = youtube.video_from_player(player, youtube.Ref(YT))
        self.assertEqual(video.title, "YouTube title")
        self.assertEqual(video.uploader, "Channel")
        self.assertEqual(video.duration_seconds, 3663)
        self.assertIn("hqdefault", video.cover_url)

    def test_bracket_player_assignment_is_supported(self):
        import json
        body = ('<script>window["ytInitialPlayerResponse"] = ' + json.dumps(yt_player()) + ";</script>").encode()
        self.assertEqual(youtube.player_from_page(body)["videoDetails"]["videoId"], YT)

    def test_restricted_live_missing_and_mismatched_metadata_fail(self):
        fixtures = [yt_player("M7lc1UVf-VE"), yt_player(duration="0"), yt_player(duration="unknown"),
                    {"playabilityStatus": {"status": "LOGIN_REQUIRED"}},
                    {"playabilityStatus": {"status": "OK"}, "videoDetails": {}}]
        live = yt_player()
        live["microformat"] = {"playerMicroformatRenderer": {"liveBroadcastDetails": {"isLiveNow": True}}}
        fixtures.append(live)
        for player in fixtures:
            with self.subTest(player=player), self.assertRaises(bili.ImportProblem):
                youtube.video_from_player(player, youtube.Ref(YT))

    def test_thumbnail_hosts_and_image_bytes_are_validated(self):
        player = yt_player()
        player["videoDetails"]["thumbnail"]["thumbnails"] = [{"url": "https://evil.example/cover.jpg"}]
        with self.assertRaises(bili.ImportProblem):
            youtube.video_from_player(player, youtube.Ref(YT))
        http = FakeHttp(routes())
        provider = youtube.Provider(http)
        video = provider.fetch_video(YT)
        http.routes[video.cover_url] = b"<html>not an image</html>"
        with self.assertRaises(bili.ImportProblem):
            provider.fetch_cover(video)

    def test_optional_nested_fields_are_validated_without_aborting_a_batch(self):
        player = yt_player()
        player["microformat"] = None
        self.assertEqual(youtube.video_from_player(player, youtube.Ref(YT)).identifier, YT)
        player["videoDetails"]["thumbnail"] = None
        with self.assertRaises(bili.ImportProblem):
            youtube.video_from_player(player, youtube.Ref(YT))
        with self.assertRaises(bili.ImportProblem):
            youtube.video_from_player([], youtube.Ref(YT))

    def test_bilibili_total_parts_and_short_link_are_preserved(self):
        provider = catalog.BilibiliProvider(bili.BiliClient(FakeHttp(routes())))
        self.assertEqual(provider.resolve("https://b23.tv/example"), BV)
        video = provider.fetch_video(BV)
        self.assertEqual(video.duration_seconds, 3723)
        self.assertEqual(len(video.parts), 2)
        self.assertEqual(video.platform, "bilibili")
        self.assertEqual(bili.format_duration(video.duration_seconds), "01:02:03")

    def test_incomplete_bilibili_parts_are_not_saved_with_wrong_duration(self):
        data = bili_data()
        data["pages"].pop()
        with self.assertRaises(bili.ImportProblem):
            bili.video_from_data(data)
