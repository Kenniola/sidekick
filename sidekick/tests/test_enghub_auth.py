"""Regression tests for Eng Hub auth-failure visibility (bug: silent fallback).

These tests assert the v0.3.x behavioural contract:
  * An Entra sign-in redirect is reported as an auth failure (never success).
  * A 200 response with a non-JSON (HTML sign-in) body is an auth failure,
    not a parsed-but-empty result.
  * A genuine 200 JSON response yields offerings tagged ``source="live"``.

The eng.ms HTTP call is stubbed with ``httpx.MockTransport`` so nothing
touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from sidekick.actions.enghub import EngHubAuthError, EngHubPipeline


def _pipeline_with_handler(handler) -> EngHubPipeline:
    """Build a pipeline whose client routes through a mock transport."""
    pipeline = EngHubPipeline()
    pipeline._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return pipeline


class TestAuthRedirectDetected:
    @pytest.mark.asyncio
    async def test_login_redirect_surfaces_as_auth_error_result(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://login.microsoftonline.com/common/oauth2/"
                        "v2.0/authorize?client_id=91c02195-fbbd-4bc8-8c69-"
                        "5b75dadc5672&redirect_uri=https%3A%2F%2Feng.ms%2F"
                        "signin-oidc"
                    )
                },
            )

        pipeline = _pipeline_with_handler(handler)
        result = await pipeline.search("Microsoft Fabric")
        assert result.offerings == []
        assert "auth required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_raw_search_raises_auth_error_on_redirect(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"location": "https://eng.ms/signin-oidc?code=x"}
            )

        pipeline = _pipeline_with_handler(handler)
        with pytest.raises(EngHubAuthError):
            await pipeline._search_enghub("Fabric")


class TestHtmlMasqueradeDetected:
    @pytest.mark.asyncio
    async def test_200_html_login_page_is_auth_error_not_success(self):
        """The sign-in page returns HTTP 200 text/html — must not be trusted."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><title>Sign in to your account</title></html>",
            )

        pipeline = _pipeline_with_handler(handler)
        result = await pipeline.search("Microsoft Fabric")
        assert result.offerings == []
        assert "auth required" in result.error.lower()


class TestLiveResultsTagged:
    @pytest.mark.asyncio
    async def test_valid_json_yields_live_tagged_offerings(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "value": [
                        {
                            "title": "Fabric PoC — Data Prep",
                            "url": "https://eng.ms/.../sp03/03-microsoftfabric/poc/dataprep",
                        },
                        {
                            "title": "Database Migration — ADR",
                            "url": "https://eng.ms/.../sp03/01-databases/adr",
                        },
                    ]
                },
            )

        pipeline = _pipeline_with_handler(handler)
        result = await pipeline.search("Microsoft Fabric")
        assert result.error == ""
        assert len(result.offerings) == 2
        assert all(o.source == "live" for o in result.offerings)
        # ADR sorts after PoC by type priority
        assert result.offerings[0].offering_type == "poc"

    @pytest.mark.asyncio
    async def test_non_200_returns_empty_without_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        pipeline = _pipeline_with_handler(handler)
        result = await pipeline.search("Fabric")
        assert result.offerings == []
        assert result.error == ""
