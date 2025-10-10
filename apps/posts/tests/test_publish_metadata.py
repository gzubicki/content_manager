import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import TestCase

from apps.posts import tasks
from apps.posts.models import Channel, Post, PostMedia


def _u32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")


def _u16(value: int) -> bytes:
    return int(value).to_bytes(2, "big")


def _make_atom(name: str, payload: bytes) -> bytes:
    size = len(payload) + 8
    return _u32(size) + name.encode("ascii") + payload


def _fixed_16_16(value: float) -> int:
    return int(round(float(value) * 65536))


def _build_minimal_mp4(*, width: int, height: int, duration_seconds: int) -> bytes:
    timescale = 1000
    duration_ticks = int(duration_seconds * timescale)

    mvhd = bytearray()
    mvhd += b"\x00\x00\x00\x00"
    mvhd += _u32(0)
    mvhd += _u32(0)
    mvhd += _u32(timescale)
    mvhd += _u32(duration_ticks)
    mvhd += _u32(0x00010000)
    mvhd += _u16(0x0100)
    mvhd += _u16(0)
    mvhd += b"\x00" * 8
    matrix = [
        0x00010000,
        0,
        0,
        0,
        0x00010000,
        0,
        0,
        0,
        0x40000000,
    ]
    for value in matrix:
        mvhd += _u32(value)
    mvhd += b"\x00" * 24
    mvhd += _u32(2)
    mvhd_atom = _make_atom("mvhd", bytes(mvhd))

    tkhd = bytearray()
    tkhd += b"\x00\x00\x00\x07"
    tkhd += _u32(0)
    tkhd += _u32(0)
    tkhd += _u32(1)
    tkhd += _u32(0)
    tkhd += _u32(duration_ticks)
    tkhd += b"\x00" * 8
    tkhd += _u16(0)
    tkhd += _u16(0)
    tkhd += _u16(0)
    tkhd += _u16(0)
    for value in matrix:
        tkhd += _u32(value)
    tkhd += _u32(_fixed_16_16(width))
    tkhd += _u32(_fixed_16_16(height))
    tkhd_atom = _make_atom("tkhd", bytes(tkhd))

    mdhd = bytearray()
    mdhd += b"\x00\x00\x00\x00"
    mdhd += _u32(0)
    mdhd += _u32(0)
    mdhd += _u32(timescale)
    mdhd += _u32(duration_ticks)
    mdhd += _u16(0)
    mdhd += _u16(0)
    mdhd_atom = _make_atom("mdhd", bytes(mdhd))

    hdlr = bytearray()
    hdlr += b"\x00\x00\x00\x00"
    hdlr += _u32(0)
    hdlr += b"vide"
    hdlr += _u32(0)
    hdlr += _u32(0)
    hdlr += _u32(0)
    hdlr += b"VideoHandler\x00"
    hdlr_atom = _make_atom("hdlr", bytes(hdlr))

    mdia_atom = _make_atom("mdia", mdhd_atom + hdlr_atom)
    trak_atom = _make_atom("trak", tkhd_atom + mdia_atom)
    moov_atom = _make_atom("moov", mvhd_atom + trak_atom)

    ftyp_payload = b"isom" + _u32(0) + b"isomiso2"
    ftyp_atom = _make_atom("ftyp", ftyp_payload)
    mdat_atom = _make_atom("mdat", b"")

    return ftyp_atom + moov_atom + mdat_atom


class PublishMetadataTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Test channel",
            slug="test-metadata",
            tg_channel_id="@test_metadata",
            bot_token="token",
        )

    def test_video_metadata_from_reference_prefers_video_section(self):
        metadata, flags = tasks._video_metadata_from_reference(
            {"video_metadata": {"width": 1280, "height": 720, "duration": 12}}
        )

        self.assertEqual(metadata, {"width": 1280, "height": 720, "duration": 12})
        self.assertTrue(all(flags.values()))

    @patch("apps.posts.tasks._probe_video_metadata", return_value={})
    def test_video_metadata_for_media_normalises_reference(self, mock_probe):
        post = Post.objects.create(channel=self.channel, text="Video post")
        media = PostMedia.objects.create(
            post=post,
            type="video",
            reference_data={"width": "1920", "height": "1080", "duration": "15"},
        )

        metadata, changed = tasks._video_metadata_for_media(media)

        self.assertFalse(mock_probe.called)
        self.assertTrue(changed)
        self.assertEqual(metadata, {"width": 1920, "height": 1080, "duration": 15})
        self.assertIn("video_metadata", media.reference_data)
        self.assertEqual(media.reference_data["video_metadata"], metadata)

    @patch("apps.posts.tasks._probe_video_metadata", return_value={"width": 640, "height": 360, "duration": 9})
    def test_video_metadata_for_media_uses_probe_when_missing(self, mock_probe):
        post = Post.objects.create(channel=self.channel, text="Video post")
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(b"fake")
        try:
            media = PostMedia.objects.create(
                post=post,
                type="video",
                cache_path=temp_file.name,
                reference_data={},
            )

            metadata, changed = tasks._video_metadata_for_media(media)
        finally:
            os.unlink(temp_file.name)

        self.assertTrue(mock_probe.called)
        self.assertTrue(changed)
        self.assertEqual(metadata, {"width": 640, "height": 360, "duration": 9})
        self.assertEqual(media.reference_data["video_metadata"], metadata)

    @patch("apps.posts.tasks._probe_video_metadata_ffprobe", return_value={})
    def test_probe_video_metadata_mp4_fallback(self, mock_ffprobe):
        sample = _build_minimal_mp4(width=1920, height=1080, duration_seconds=5)
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(sample)
        try:
            metadata = tasks._probe_video_metadata(temp_file.name)
        finally:
            os.unlink(temp_file.name)

        self.assertTrue(mock_ffprobe.called)
        self.assertEqual(metadata, {"width": 1920, "height": 1080, "duration": 5})

    @patch("apps.posts.tasks._probe_video_metadata", return_value={})
    @patch("apps.posts.tasks.services._bot_for")
    def test_publish_async_includes_video_metadata(self, mock_bot_for, mock_probe):
        post = Post.objects.create(channel=self.channel, text="Caption text")
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(b"fake video")

        media = PostMedia.objects.create(
            post=post,
            type="video",
            cache_path=temp_file.name,
            reference_data={"width": 800, "height": 600, "duration": 5},
        )
        media.tg_file_id = "cached"
        media.save(update_fields=["tg_file_id"])

        class _FakeMessage:
            def __init__(self, message_id: int):
                self.message_id = message_id
                self.video = SimpleNamespace(file_id="cached")

        class _FakeBot:
            def __init__(self):
                self.media_calls = []
                self.message_calls = []

            async def send_media_group(self, chat_id, media):
                self.media_calls.append((chat_id, media))
                return [_FakeMessage(111)]

            async def send_message(self, chat_id, text):
                self.message_calls.append((chat_id, text))
                return SimpleNamespace(message_id=222)

        fake_bot = _FakeBot()
        mock_bot_for.return_value = fake_bot

        try:
            result = asyncio.run(tasks._publish_async(post, [media]))
        finally:
            os.unlink(temp_file.name)

        self.assertEqual(result, ([111], 111))
        self.assertEqual(len(fake_bot.media_calls), 1)
        _, media_payload = fake_bot.media_calls[0]
        self.assertEqual(len(media_payload), 1)
        video = media_payload[0]
        self.assertEqual(video.width, 800)
        self.assertEqual(video.height, 600)
        self.assertEqual(video.duration, 5)
        self.assertTrue(video.supports_streaming)
        self.assertTrue(getattr(media, "_reference_data_dirty", False))
        self.assertEqual(media.reference_data.get("video_metadata", {}), {"width": 800, "height": 600, "duration": 5})

    @patch("apps.posts.tasks.services.compute_dupe", return_value=0.42)
    @patch("apps.posts.tasks._publish_async", new_callable=AsyncMock)
    def test_publish_updates_metadata_and_status(self, mock_publish_async, mock_compute):
        mock_publish_async.return_value = ([101, 202], 303)
        post = Post.objects.create(
            channel=self.channel,
            text="Hello world",
            status=Post.Status.SCHEDULED,
        )

        result = tasks.publish_post(post.id)

        mock_publish_async.assert_awaited()
        mock_compute.assert_called_once()
        self.assertEqual(result, {"group": [101, 202], "text": 303})

        post.refresh_from_db()
        self.assertEqual(post.status, Post.Status.PUBLISHED)
        self.assertIsNotNone(post.scheduled_at)
        self.assertEqual(post.dupe_score, 0.42)

        publication = post.source_metadata.get("publication", {})
        self.assertEqual(publication.get("status"), "completed")
        self.assertEqual(publication.get("message_id"), "303")
        self.assertEqual(publication.get("group_message_ids"), [101, 202])
        self.assertTrue(publication.get("requested_at"))
        self.assertTrue(publication.get("completed_at"))
