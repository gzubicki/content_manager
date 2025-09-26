from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from content_manager.staticfiles import LenientCompressedManifestStaticFilesStorage


class LenientCompressedManifestStaticFilesStorageTest(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = LenientCompressedManifestStaticFilesStorage()

    def test_missing_file_is_skipped(self) -> None:
        compressor = mock.Mock()
        compressor.should_compress.return_value = True
        compressor.compress.side_effect = FileNotFoundError

        with mock.patch.object(
            self.storage,
            "create_compressor",
            return_value=compressor,
        ), mock.patch.object(
            self.storage,
            "path",
            return_value="/tmp/missing.js",
        ), mock.patch("content_manager.staticfiles.logger") as mock_logger:
            result = list(self.storage.compress_files(["admin/js/missing.js"]))

        self.assertEqual(result, [])
        compressor.should_compress.assert_called_once_with("admin/js/missing.js")
        compressor.compress.assert_called_once()
        mock_logger.warning.assert_called_once()

    def test_existing_file_is_compressed(self) -> None:
        compressor = mock.Mock()
        compressor.should_compress.return_value = True
        compressor.compress.return_value = [
            "/tmp/static/admin/js/missing.js.gz",
            "/tmp/static/admin/js/missing.js.br",
        ]

        with mock.patch.object(
            self.storage,
            "create_compressor",
            return_value=compressor,
        ), mock.patch.object(
            self.storage,
            "path",
            return_value="/tmp/static/admin/js/missing.js",
        ):
            result = list(self.storage.compress_files(["admin/js/missing.js"]))

        self.assertEqual(
            result,
            [
                ("admin/js/missing.js", "admin/js/missing.js.gz"),
                ("admin/js/missing.js", "admin/js/missing.js.br"),
            ],
        )
        compressor.should_compress.assert_called_once_with("admin/js/missing.js")
        compressor.compress.assert_called_once_with("/tmp/static/admin/js/missing.js")
