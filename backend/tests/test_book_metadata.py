from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.books import book_metadata


DOUBAN_HTML = """
<html><head>
  <meta property="og:title" content="任意书名 (豆瓣)" />
  <meta property="og:image" content="https://img2.doubanio.com/view/subject/l/public/example.jpg" />
</head><body>
  <div id="info">
    <span class="pl"> 作者</span>: <a href="/author/1/">甲作者</a> / <a href="/author/2/">乙作者</a><br/>
  </div>
</body></html>
"""


class BookMetadataContractTests(unittest.TestCase):
    def test_douban_subject_projects_author_and_remote_cover_source(self):
        result = book_metadata.parse_douban_subject(
            DOUBAN_HTML,
            fallback_title="任意书名",
            source_url="https://book.douban.com/subject/1234567/",
        )

        self.assertEqual(result.title, "任意书名")
        self.assertEqual(result.authors, ["甲作者", "乙作者"])
        self.assertEqual(
            result.cover_url,
            "https://img2.doubanio.com/view/subject/l/public/example.jpg",
        )

    def test_candidate_selection_requires_exact_book_identity_and_subject_url(self):
        candidates = [
            {
                "title": "任意书名的书评",
                "source_url": "https://book.douban.com/review/123/",
            },
            {
                "title": "《任意书名》 (豆瓣)",
                "source_url": "https://book.douban.com/subject/1234567/blockquotes?start=20",
            },
        ]

        selected = book_metadata._choose_candidate("任意书名", candidates)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["source_url"], "https://book.douban.com/subject/1234567/")

    def test_cover_route_can_only_resolve_cached_content_hash_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            filename = f"{'a' * 64}.jpg"
            (cache_dir / filename).write_bytes(b"image")
            with patch.object(book_metadata, "COVER_CACHE_DIR", cache_dir):
                self.assertEqual(book_metadata.cached_cover_path(filename), cache_dir / filename)
                self.assertIsNone(book_metadata.cached_cover_path("../secret.jpg"))
                self.assertIsNone(book_metadata.cached_cover_path("remote-url.jpg"))

    def test_persisted_cover_contract_never_exposes_douban_image_host(self):
        self.assertEqual(book_metadata.COVER_URL_PREFIX, "/api/v1/books/covers")
        self.assertNotIn("douban", book_metadata.COVER_URL_PREFIX)

    def test_metadata_component_has_no_shelf_or_database_write_authority(self):
        source = Path(book_metadata.__file__).read_text(encoding="utf-8")
        self.assertNotIn("UserBookShelf", source)
        self.assertNotIn("AsyncSession", source)
        self.assertNotIn("sqlalchemy", source)

    def test_reading_service_is_the_only_metadata_persistence_owner(self):
        root = Path(__file__).resolve().parents[1]
        reading_source = (root / "app/services/books/reading_service.py").read_text(
            encoding="utf-8-sig"
        )
        api_source = (root / "app/api/v1/books.py").read_text(encoding="utf-8-sig")
        frontend_source = (
            root.parent
            / "frontend/src/features/bookshelf/components/bookshelf-view.tsx"
        ).read_text(encoding="utf-8-sig")

        self.assertIn("resolve_book_metadata", reading_source)
        self.assertIn("enrich_missing_metadata", api_source)
        self.assertNotIn("resolve_book_metadata", api_source)
        self.assertNotIn("doubanio.com", frontend_source)


if __name__ == "__main__":
    unittest.main()
