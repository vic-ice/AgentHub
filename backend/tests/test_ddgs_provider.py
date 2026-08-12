from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.external_search.contracts import SearchRequest
from app.services.external_search.providers.ddgs import DDGSProvider
from app.services.provider_config import (
    get_provider_registry,
    reset_provider_registry,
)


def _raw_result(
    *,
    title: str = "标题",
    url: str = "https://book.douban.com/subject/123/",
    body: str = "内容",
) -> dict:
    return {"title": title, "href": url, "body": body}


class DDGSProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_projects_hits_and_domains(self) -> None:
        raw = [
            _raw_result(),
            _raw_result(
                url="https://www.goodreads.com/book/1",
                title="Goodreads",
            ),
        ]
        with patch(
            "app.services.external_search.providers.ddgs._run_ddgs_text",
            return_value=raw,
        ) as mocked:
            result = await DDGSProvider().search(
                SearchRequest(
                    query="类似《失控》风格的书",
                    include_domains=["book.douban.com"],
                    include_url_prefixes=["https://book.douban.com/subject/"],
                )
            )
        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.provider, "ddgs")
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].url, "https://book.douban.com/subject/123/")
        self.assertEqual(result.hits[0].provider, "ddgs")
        self.assertEqual(result.attempts[0].outcome, "found")
        mocked.assert_called_once()
        call_kwargs = mocked.call_args.kwargs
        self.assertEqual(call_kwargs["region"], "cn-zh")
        self.assertEqual(call_kwargs["max_results"], 5)

    async def test_empty_results_map_to_empty_outcome(self) -> None:
        with patch(
            "app.services.external_search.providers.ddgs._run_ddgs_text",
            return_value=[],
        ):
            result = await DDGSProvider().search(
                SearchRequest(query="没有结果")
            )
        self.assertEqual(result.outcome, "empty")
        self.assertEqual(result.hits, [])

    async def test_adapter_exception_maps_to_unavailable(self) -> None:
        with patch(
            "app.services.external_search.providers.ddgs._run_ddgs_text",
            side_effect=RuntimeError("ConnectError: connection refused"),
        ):
            result = await DDGSProvider().search(
                SearchRequest(query="书")
            )
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual(result.attempts[0].error_type, "network")

    async def test_timeout_maps_to_unavailable_timeout(self) -> None:
        import time

        def _slow(*args, **kwargs):
            time.sleep(3)
            return []

        with patch(
            "app.services.external_search.providers.ddgs._run_ddgs_text",
            side_effect=_slow,
        ), patch(
            "app.services.external_search.providers.ddgs.resolve_search_provider",
        ) as resolve:
            resolve.return_value = _config(
                enabled=True,
                settings={"timeout_seconds": 1},
            )
            result = await DDGSProvider().search(
                SearchRequest(query="书", detail="deep")
            )
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual(result.attempts[0].error_type, "timeout")

    def test_registry_exposes_enabled_ddgs(self) -> None:
        reset_provider_registry()
        providers = get_provider_registry().list_configs().providers
        ddgs = next(
            item for item in providers if item.provider_key == "ddgs"
        )
        self.assertTrue(ddgs.enabled)
        self.assertEqual(ddgs.provider_type, "web_search")
        self.assertIn("web_search", ddgs.capabilities)
        self.assertTrue(ddgs.settings["allow_anonymous"])


def _config(*, enabled: bool, settings: dict | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(
        name="ddgs",
        enabled=enabled,
        api_key="",
        api_base_url="",
        settings=settings or {},
        source="anonymous",
        error="",
    )


if __name__ == "__main__":
    unittest.main()
