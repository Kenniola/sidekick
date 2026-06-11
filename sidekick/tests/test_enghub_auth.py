"""Regression tests for the Eng Hub offerings pipeline (auth-failure visibility).

These tests assert the v0.3.x behavioural contract after the Option A refactor,
where sidekick no longer calls eng.ms directly but instead consumes an
authenticated ``search_fn`` injected by the host:

  * With no ``search_fn`` wired, a search reports an auth failure (never an
    empty success and never fabricated data).
  * An injected search returning live records yields offerings tagged
    ``source="live"`` with type/solution-play inferred from the URL.
  * Records missing a title or url are skipped.
  * A failure raised by the injected search surfaces as an error result,
    tolerating the flaky upstream backend.

Nothing here touches the network — the search source is a plain callable.
"""

from __future__ import annotations

import pytest

from sidekick.actions.enghub import EngHubAuthError, EngHubPipeline


class TestNoSourceWired:
    @pytest.mark.asyncio
    async def test_search_without_source_surfaces_as_auth_error(self):
        pipeline = EngHubPipeline()  # no search_fn injected
        result = await pipeline.search("Microsoft Fabric")
        assert result.offerings == []
        assert "auth required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_raw_search_raises_auth_error_without_source(self):
        pipeline = EngHubPipeline()
        with pytest.raises(EngHubAuthError):
            await pipeline._search_enghub("Fabric")


class TestLiveResultsTagged:
    @pytest.mark.asyncio
    async def test_injected_search_yields_live_tagged_offerings(self):
        async def search_fn(query: str) -> list[dict]:
            return [
                {
                    "title": "Fabric PoC — Data Prep",
                    "url": "https://eng.ms/.../sp03/03-microsoftfabric/poc/dataprep",
                    "contentId": "abc-123",
                },
                {
                    "title": "Database Migration — ADR",
                    "url": "https://eng.ms/.../sp01/01-databases/adr",
                    "contentId": "def-456",
                },
            ]

        pipeline = EngHubPipeline(search_fn=search_fn)
        result = await pipeline.search("Microsoft Fabric")
        assert result.error == ""
        assert len(result.offerings) == 2
        assert all(o.source == "live" for o in result.offerings)
        # PoC sorts before ADR by type priority.
        assert result.offerings[0].offering_type == "poc"
        # Solution play is inferred from the URL path.
        assert result.offerings[0].solution_play.startswith("SP03")

    @pytest.mark.asyncio
    async def test_set_search_fn_wires_source_after_construction(self):
        async def search_fn(query: str) -> list[dict]:
            return [
                {
                    "title": "WorkshopPLUS Microsoft Fabric",
                    "url": (
                        "https://eng.ms/.../azure-engagement-resource-center/"
                        "sp03/03-microsoftfabric/wsplus/index"
                    ),
                }
            ]

        pipeline = EngHubPipeline()
        pipeline.set_search_fn(search_fn)
        result = await pipeline.search("Fabric workshop")
        assert result.error == ""
        assert len(result.offerings) == 1
        assert result.offerings[0].offering_type == "wsplus"

    @pytest.mark.asyncio
    async def test_records_missing_title_or_url_are_skipped(self):
        async def search_fn(query: str) -> list[dict]:
            return [
                {"title": "", "url": "https://eng.ms/x"},
                {"title": "No URL", "url": ""},
                {"title": "Good", "url": "https://eng.ms/.../sp03/poc/good"},
            ]

        pipeline = EngHubPipeline(search_fn=search_fn)
        result = await pipeline.search("Fabric")
        assert len(result.offerings) == 1
        assert result.offerings[0].title == "Good"


class TestSearchFailureTolerated:
    @pytest.mark.asyncio
    async def test_injected_search_failure_returns_error_result(self):
        async def search_fn(query: str) -> list[dict]:
            raise ValueError("The input does not contain any JSON tokens.")

        pipeline = EngHubPipeline(search_fn=search_fn)
        result = await pipeline.search("Fabric")
        assert result.offerings == []
        assert result.error != ""
