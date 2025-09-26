"""Niestandardowe klasy do obsługi statycznych zasobów."""

from __future__ import annotations

import logging
from typing import Iterable, Iterator, Tuple

from django.conf import settings

from whitenoise.storage import CompressedManifestStaticFilesStorage

logger = logging.getLogger(__name__)


class LenientCompressedManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Wersja przechowalni, która ignoruje brakujące pliki podczas kompresji.

    W praktyce WhiteNoise potrafi zgłosić FileNotFoundError, gdy manifest zawiera
    wpis dla pliku, którego nie da się już otworzyć (np. z powodu dubletów
    ścieżek lub równoległego czyszczenia katalogu). Zamiast przerywać cały
    proces ``collectstatic`` ignorujemy taki wpis i jedynie logujemy ostrzeżenie.
    """

    def compress_files(self, paths: Iterable[str]) -> Iterator[Tuple[str, str]]:
        """Kompresuj pliki w sposób odporny na FileNotFoundError."""

        extensions = getattr(settings, "WHITENOISE_SKIP_COMPRESS_EXTENSIONS", None)
        compressor = self.create_compressor(extensions=extensions, quiet=True)

        for path in paths:
            if not compressor.should_compress(path):
                continue

            full_path = self.path(path)
            prefix_len = len(full_path) - len(path)

            try:
                for compressed_path in compressor.compress(full_path):
                    compressed_name = compressed_path[prefix_len:]
                    yield path, compressed_name
            except FileNotFoundError:
                logger.warning(
                    "Pomijam kompresję brakującego pliku statycznego: %s (pełna ścieżka: %s)",
                    path,
                    full_path,
                )
                continue
