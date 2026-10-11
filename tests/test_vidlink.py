from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch
import httpx

from dion.metadata.models import StreamSource, SubtitleTrack
from dion.providers.manager import ProviderManager
from dion.providers.vidlink import (
    VidLinkProvider,
    decrypt_vidlink_payload,
    decrypt_vidlink_token,
    encrypt_vidlink_token,
    resolve_tmdb_id,
    _TMDB_CACHE,
)


class TestVidLinkCrypto(unittest.TestCase):
    def test_token_encrypt_and_decrypt_roundtrip(self):
        media_id = "533535"
        token = encrypt_vidlink_token(media_id)
        self.assertIsInstance(token, str)
        self.assertNotIn("=", token)  # stripped padding

        decrypted_id, timestamp = decrypt_vidlink_token(token)
        self.assertEqual(decrypted_id, media_id)
        self.assertGreater(timestamp, int(time.time()))

    def test_payload_decrypt(self):
        from dion.providers.vidlink import BOX
        import base64

        sample_dict = {"stream": {"qualities": {"1080": {"url": "https://cdn.example.com/stream.mp4"}}}}
        raw_text = json.dumps(sample_dict)
        nonce = bytes(24)
        encrypted = BOX.encrypt(raw_text.encode("utf-8"), nonce)
        token = base64.urlsafe_b64encode(nonce + encrypted.ciphertext).decode("utf-8")

        decrypted_text = decrypt_vidlink_payload(token)
        self.assertEqual(decrypted_text, raw_text)
        parsed = json.loads(decrypted_text)
        self.assertEqual(parsed["stream"]["qualities"]["1080"]["url"], "https://cdn.example.com/stream.mp4")


class TestTmdbMapping(unittest.TestCase):
    def setUp(self):
        _TMDB_CACHE.clear()

    def test_numeric_id_passthrough(self):
        result = resolve_tmdb_id("12345", "movie")
        self.assertEqual(result, "12345")

    def test_cached_lookup(self):
        _TMDB_CACHE["tt9999999:movie"] = "77777"
        result = resolve_tmdb_id("tt9999999", "movie")
        self.assertEqual(result, "77777")

    @patch("httpx.Client.get")
    def test_mapping_endpoint_primary(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "movie_results": [{"id": 533535, "title": "Deadpool & Wolverine"}]
        }
        mock_get.return_value = mock_resp

        result = resolve_tmdb_id("tt6263850", "movie")
        self.assertEqual(result, "533535")
        self.assertEqual(_TMDB_CACHE.get("tt6263850:movie"), "533535")

    @patch("httpx.Client.get")
    def test_cinemeta_fallback(self, mock_get):
        # First call fails, second call (cinemeta) succeeds
        resp1 = MagicMock()
        resp1.status_code = 404

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {"meta": {"moviedb_id": 1396, "name": "Breaking Bad"}}

        mock_get.side_effect = [resp1, resp2]

        result = resolve_tmdb_id("tt0903747", "tv")
        self.assertEqual(result, "1396")
        self.assertEqual(_TMDB_CACHE.get("tt0903747:tv"), "1396")


class TestVidLinkProvider(unittest.TestCase):
    def setUp(self):
        self.provider = VidLinkProvider()

    def tearDown(self):
        self.provider.close()

    def test_parse_stream_data_qualities_and_subtitles(self):
        raw_data = {
            "sourceId": "mwVault",
            "stream": {
                "id": "primary",
                "type": "file",
                "qualities": {
                    "360": {"type": "mp4", "url": "https://cdn.example.com/360.mp4"},
                    "1080": {"type": "mp4", "url": "https://cdn.example.com/1080.mp4"},
                    "720": {"type": "mp4", "url": "https://cdn.example.com/720.mp4"},
                },
                "captions": [
                    {"url": "https://cdn.example.com/en.srt", "language": "English", "type": "srt"},
                    {"url": "https://cdn.example.com/es.srt", "language": "Spanish", "type": "srt"},
                ],
            },
        }

        sources = self.provider._parse_stream_data(raw_data)
        self.assertEqual(len(sources), 3)

        # Ensure sorted highest resolution first
        self.assertEqual(sources[0].quality, "1080p")
        self.assertEqual(sources[0].url, "https://cdn.example.com/1080.mp4")
        self.assertFalse(sources[0].is_hls)
        self.assertEqual(sources[0].server, "vidlink")

        self.assertEqual(sources[1].quality, "720p")
        self.assertEqual(sources[2].quality, "360p")

        # Subtitles attached
        self.assertEqual(len(sources[0].subtitles), 2)
        self.assertEqual(sources[0].subtitles[0].label, "English")
        self.assertEqual(sources[0].subtitles[1].label, "Spanish")

    def test_parse_stream_data_hls_playlist(self):
        raw_data = {
            "sourceId": "mwVault",
            "stream": {
                "id": "primary",
                "type": "hls",
                "playlist": "https://cdn.example.com/master.m3u8",
                "captions": [],
            },
        }

        sources = self.provider._parse_stream_data(raw_data)
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0].is_hls)
        self.assertEqual(sources[0].quality, "1080p")
        self.assertEqual(sources[0].url, "https://cdn.example.com/master.m3u8")

    @patch("dion.providers.vidlink.resolve_tmdb_id")
    def test_resolve_movie_mocked(self, mock_resolve_tmdb):
        mock_resolve_tmdb.return_value = "533535"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({
            "sourceId": "mwVault",
            "stream": {
                "qualities": {
                    "1080": {"type": "mp4", "url": "https://cdn.example.com/deadpool.mp4"}
                },
                "captions": [],
            },
        })
        mock_resp.json.return_value = json.loads(mock_resp.text)

        with patch.object(self.provider.client, "get", return_value=mock_resp):
            sources = self.provider.resolve_movie("tt6263850", "Deadpool & Wolverine")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].quality, "1080p")
            self.assertEqual(sources[0].url, "https://cdn.example.com/deadpool.mp4")

    @patch("dion.providers.vidlink.resolve_tmdb_id")
    def test_resolve_episode_mocked(self, mock_resolve_tmdb):
        mock_resolve_tmdb.return_value = "1396"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({
            "sourceId": "mwVault",
            "stream": {
                "qualities": {
                    "1080": {"type": "mp4", "url": "https://cdn.example.com/bb_s1e1.mp4"}
                },
                "captions": [],
            },
        })
        mock_resp.json.return_value = json.loads(mock_resp.text)

        with patch.object(self.provider.client, "get", return_value=mock_resp):
            sources = self.provider.resolve_episode("tt0903747", 1, 1, "Pilot")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].quality, "1080p")
            self.assertEqual(sources[0].url, "https://cdn.example.com/bb_s1e1.mp4")

    def test_provider_manager_includes_vidlink(self):
        manager = ProviderManager()
        provider_names = [p.name for p in manager.providers]
        self.assertIn("vidlink", provider_names)


if __name__ == "__main__":
    unittest.main()
