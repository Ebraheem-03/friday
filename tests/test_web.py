"""Unit tests for web/agent.py and web/scrape.py.

CI constraints
--------------
- No browser-use or scrapling installed (the [web] extra is NOT installed in CI).
- No real browser launched, no network, no Playwright binary required.
- browser-use Agent and Scrapling fetchers are fully mocked.
- All tests run without FRIDAY_LIVE or any real API key.

Coverage
--------
- web_task happy path (mocked browser-use Agent returns a summary).
- web_task never raises: mocked agent raises → returns error string.
- Scrape path returns typed ScrapeResult dicts (mocked Scrapling Fetcher).
- Graceful degradation when browser-use is not installed.
- Graceful degradation when scrapling is not installed.
- Safe-mode / confirm gate for state-changing actions.
- Telemetry opt-out env var is set before any browser-use import.
- isinstance(agent, WebTool) against the runtime_checkable Protocol.

Baseline: 234 passed, 2 skipped.  This file must only add to the passed count.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.brain import WebTool
from core.config import Settings


# ---------------------------------------------------------------------------
# Helpers: fake Settings (no .env required)
# ---------------------------------------------------------------------------


def _fake_settings(model: str = "gemini-test") -> Settings:
    return Settings(
        gemini_api_key="fake-key-for-testing",
        gemini_model=model,
        sample_rate=24000,
        tts_voice="af_heart",
        telemetry_ws_port=8765,
        log_level="DEBUG",
    )


# ---------------------------------------------------------------------------
# Helper: build a fake AgentHistoryList (returned by browser-use agent.run())
# ---------------------------------------------------------------------------


def _make_history(
    final: str | None = "Task completed successfully.",
    extracted: list[str] | None = None,
    has_errors: bool = False,
    errors: list[str] | None = None,
) -> MagicMock:
    """Build a minimal fake of browser-use's AgentHistoryList."""
    history = MagicMock()
    history.final_result.return_value = final
    history.extracted_content.return_value = extracted or []
    history.has_errors.return_value = has_errors
    history.errors.return_value = errors or []
    return history


# ---------------------------------------------------------------------------
# Helper: patch browser-use and scrapling into sys.modules so lazy imports work
# ---------------------------------------------------------------------------


def _patch_browser_use(agent_run_return: Any = None, agent_run_raises: Exception | None = None):
    """Context manager: inject a mock browser_use module into sys.modules.

    Returns a context manager that patches ``browser_use`` globally so the lazy
    ``from browser_use import Agent, ChatGoogle`` inside ``web.agent`` succeeds
    in CI without the real package installed.
    """
    mock_bu = MagicMock()

    if agent_run_raises is not None:
        mock_bu.Agent.return_value.run = AsyncMock(side_effect=agent_run_raises)
    else:
        history = agent_run_return or _make_history()
        mock_bu.Agent.return_value.run = AsyncMock(return_value=history)

    mock_bu.ChatGoogle.return_value = MagicMock()
    return patch.dict("sys.modules", {"browser_use": mock_bu})


def _patch_scrapling(
    *,
    title: str = "Test Page",
    text: str = "Hello from scrapling.",
    links: list[str] | None = None,
    raises: Exception | None = None,
) -> Any:
    """Context manager: inject a mock scrapling.fetchers module into sys.modules."""
    mock_scrapling = MagicMock()
    mock_scrapling_fetchers = MagicMock()

    if raises is not None:
        mock_scrapling_fetchers.Fetcher.return_value.get.side_effect = raises
    else:
        # Build a fake response object with .css() selector support.
        fake_response = MagicMock()

        def _css(selector: str) -> list[MagicMock]:
            if selector == "title":
                el = MagicMock()
                el.text = title
                return [el]
            elif selector == "body":
                el = MagicMock()
                el.text = text
                return [el]
            elif selector == "a[href]":
                link_els = []
                for href in (links or ["https://example.com/a", "https://example.com/b"]):
                    el = MagicMock()
                    el.attrib = {"href": href}
                    link_els.append(el)
                return link_els
            return []

        fake_response.css.side_effect = _css
        fake_response.status = 200
        fake_response.content = b"<html>...</html>"
        mock_scrapling_fetchers.Fetcher.return_value.get.return_value = fake_response

    return patch.dict(
        "sys.modules",
        {
            "scrapling": mock_scrapling,
            "scrapling.fetchers": mock_scrapling_fetchers,
        },
    )


# ---------------------------------------------------------------------------
# Tests: Protocol conformance
# ---------------------------------------------------------------------------


class TestWebAgentProtocol:
    def test_web_agent_satisfies_webtool_protocol(self) -> None:
        """WebAgent must satisfy the runtime_checkable WebTool Protocol."""
        from web.agent import WebAgent

        agent = WebAgent(_fake_settings())
        assert isinstance(agent, WebTool)

    def test_web_agent_has_web_task_method(self) -> None:
        """WebAgent.web_task must exist and be callable."""
        from web.agent import WebAgent
        import inspect

        agent = WebAgent(_fake_settings())
        assert hasattr(agent, "web_task")
        assert inspect.iscoroutinefunction(agent.web_task)


# ---------------------------------------------------------------------------
# Tests: Telemetry opt-out
# ---------------------------------------------------------------------------


class TestTelemetryDisable:
    def test_anonymized_telemetry_env_var_is_set_on_import(self) -> None:
        """ANONYMIZED_TELEMETRY must be 'false' after importing web.agent.

        The assignment at module top-level fires before any browser-use import
        and before any test runs.
        """
        import web.agent  # noqa: F401

        # The module sets os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
        # at load time, so the value must be "false" (or an existing override).
        value = os.environ.get("ANONYMIZED_TELEMETRY", "")
        assert value.lower() == "false", (
            f"ANONYMIZED_TELEMETRY should be 'false', got {value!r}"
        )

    def test_telemetry_disabled_before_browser_use_import(self) -> None:
        """The env var is set at module level — it fires before any lazy import."""
        # Simulate a fresh import by temporarily removing web.agent from sys.modules.
        saved = sys.modules.pop("web.agent", None)
        # Also clear ANONYMIZED_TELEMETRY so we can observe the module setting it.
        old_val = os.environ.pop("ANONYMIZED_TELEMETRY", None)
        try:
            import web.agent  # noqa: F401

            assert os.environ.get("ANONYMIZED_TELEMETRY") == "false"
        finally:
            if old_val is not None:
                os.environ["ANONYMIZED_TELEMETRY"] = old_val
            # Restore the cached module to avoid disturbing other tests.
            if saved is not None:
                sys.modules["web.agent"] = saved


# ---------------------------------------------------------------------------
# Tests: web_task happy path (mocked browser-use)
# ---------------------------------------------------------------------------


class TestWebAgentHappyPath:
    @pytest.mark.asyncio
    async def test_web_task_returns_string(self) -> None:
        """web_task returns a non-empty string on a successful mocked agent run."""
        from web.agent import WebAgent

        history = _make_history(final="The page title is 'Example'.")
        with _patch_browser_use(agent_run_return=history):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Get the title of https://example.com")

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_web_task_with_url_returns_result(self) -> None:
        """web_task with a URL passes it to the agent task string."""
        from web.agent import WebAgent

        history = _make_history(final="Scraped content here.")
        with _patch_browser_use(agent_run_return=history):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Read the page", url="https://example.com")

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_web_task_uses_extracted_content_when_final_result_is_none(self) -> None:
        """Falls back to extracted_content() when final_result() returns None."""
        from web.agent import WebAgent

        history = _make_history(final=None, extracted=["item1", "item2"])
        with _patch_browser_use(agent_run_return=history):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("List items")

        assert isinstance(result, str)
        assert "item1" in result or "item2" in result

    @pytest.mark.asyncio
    async def test_web_task_reports_agent_errors(self) -> None:
        """When agent has errors and no content, returns error description."""
        from web.agent import WebAgent

        history = _make_history(
            final=None, extracted=[], has_errors=True, errors=["timeout on step 3"]
        )
        with _patch_browser_use(agent_run_return=history):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Do something")

        assert isinstance(result, str)
        assert "error" in result.lower() or "timeout" in result.lower()


# ---------------------------------------------------------------------------
# Tests: web_task never raises (error-return contract)
# ---------------------------------------------------------------------------


class TestWebAgentNeverRaises:
    @pytest.mark.asyncio
    async def test_agent_run_raises_returns_error_string(self) -> None:
        """If agent.run() raises, web_task returns an error string, never raises."""
        from web.agent import WebAgent

        with _patch_browser_use(agent_run_raises=RuntimeError("browser crash")):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Do a task")

        assert isinstance(result, str)
        assert "error" in result.lower() or "browser" in result.lower()

    @pytest.mark.asyncio
    async def test_agent_run_raises_exception_returns_error_string(self) -> None:
        """Any exception type from agent.run() is caught and returned as string."""
        from web.agent import WebAgent

        with _patch_browser_use(agent_run_raises=ValueError("unexpected value")):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Search something")

        assert isinstance(result, str)
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_web_task_returns_string_not_raises_on_any_exception(self) -> None:
        """The outer try/except in web_task must absorb arbitrary exceptions."""
        from web.agent import WebAgent

        agent = WebAgent(_fake_settings(), allow_destructive=True)

        # Patch _execute to raise directly.
        async def _boom(*a: Any, **kw: Any) -> str:
            raise MemoryError("OOM")

        agent._execute = _boom  # type: ignore[method-assign]
        result = await agent.web_task("anything")
        assert isinstance(result, str)
        assert "error" in result.lower() or "oom" in result.lower()


# ---------------------------------------------------------------------------
# Tests: graceful degradation when browser-use is not installed
# ---------------------------------------------------------------------------


class TestBrowserUseNotInstalled:
    @pytest.mark.asyncio
    async def test_web_task_without_browser_use_returns_unavailable(self) -> None:
        """When browser-use is not importable, web_task returns a clear message."""
        from web.agent import WebAgent

        # Remove browser_use from sys.modules and make it unimportable.
        with patch.dict("sys.modules", {"browser_use": None}):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Do something interactive")

        assert isinstance(result, str)
        assert "unavailable" in result.lower() or "not installed" in result.lower()

    @pytest.mark.asyncio
    async def test_web_task_without_browser_use_is_string(self) -> None:
        """Return value is always a str even when browser-use is missing."""
        from web.agent import WebAgent

        with patch.dict("sys.modules", {"browser_use": None}):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("Anything")

        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: safe-mode / confirm gate
# ---------------------------------------------------------------------------


class TestSafeModeGate:
    @pytest.mark.asyncio
    async def test_safe_mode_prepends_safety_prefix(self) -> None:
        """In safe mode (default), the safety prefix is added to the task."""
        from web.agent import WebAgent, _SAFE_MODE_PREFIX

        captured_task: list[str] = []

        mock_bu = MagicMock()
        history = _make_history(final="done")

        def _capture_agent(**kwargs: Any) -> MagicMock:
            captured_task.append(kwargs.get("task", ""))
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=history)
            return mock_agent

        mock_bu.Agent.side_effect = _capture_agent
        mock_bu.ChatGoogle.return_value = MagicMock()

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            agent = WebAgent(_fake_settings(), allow_destructive=False)
            await agent.web_task("Submit this form", url="https://example.com/form")

        assert len(captured_task) == 1
        assert captured_task[0].startswith(_SAFE_MODE_PREFIX)
        # Original task must still be present
        assert "Submit this form" in captured_task[0]

    @pytest.mark.asyncio
    async def test_allow_destructive_omits_safety_prefix(self) -> None:
        """When allow_destructive=True, the safety prefix is NOT added."""
        from web.agent import WebAgent, _SAFE_MODE_PREFIX

        captured_task: list[str] = []

        mock_bu = MagicMock()
        history = _make_history(final="done")

        def _capture_agent(**kwargs: Any) -> MagicMock:
            captured_task.append(kwargs.get("task", ""))
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=history)
            return mock_agent

        mock_bu.Agent.side_effect = _capture_agent
        mock_bu.ChatGoogle.return_value = MagicMock()

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            await agent.web_task("Buy the product", url="https://example.com/buy")

        assert len(captured_task) == 1
        assert not captured_task[0].startswith(_SAFE_MODE_PREFIX)
        assert "Buy the product" in captured_task[0]

    @pytest.mark.asyncio
    async def test_confirm_callback_deny_blocks_execution(self) -> None:
        """When confirm_callback returns False, the task is blocked."""
        from web.agent import WebAgent

        deny_callback_called: list[bool] = []

        def deny(desc: str) -> bool:
            deny_callback_called.append(True)
            return False

        mock_bu = MagicMock()
        mock_bu.Agent.return_value.run = AsyncMock(return_value=_make_history())
        mock_bu.ChatGoogle.return_value = MagicMock()

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            agent = WebAgent(_fake_settings(), confirm_callback=deny)
            result = await agent.web_task("Do something")

        assert deny_callback_called == [True]
        # Agent.run must NOT have been called.
        mock_bu.Agent.return_value.run.assert_not_called()
        assert isinstance(result, str)
        assert "blocked" in result.lower()

    @pytest.mark.asyncio
    async def test_confirm_callback_allow_proceeds(self) -> None:
        """When confirm_callback returns True, the task executes normally."""
        from web.agent import WebAgent

        mock_bu = MagicMock()
        history = _make_history(final="allowed result")
        mock_bu.Agent.return_value.run = AsyncMock(return_value=history)
        mock_bu.ChatGoogle.return_value = MagicMock()

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            agent = WebAgent(
                _fake_settings(),
                allow_destructive=True,
                confirm_callback=lambda desc: True,
            )
            result = await agent.web_task("allowed task")

        assert isinstance(result, str)
        assert "allowed" in result.lower()

    @pytest.mark.asyncio
    async def test_default_safe_mode_is_false(self) -> None:
        """WebAgent defaults to allow_destructive=False (safe mode on)."""
        from web.agent import WebAgent

        agent = WebAgent(_fake_settings())
        assert agent._allow_destructive is False


# ---------------------------------------------------------------------------
# Tests: scrape path (mocked Scrapling)
# ---------------------------------------------------------------------------


class TestScrapePath:
    def test_fetch_page_returns_typed_dict(self) -> None:
        """fetch_page returns a ScrapeResult typed dict with expected keys."""
        with _patch_scrapling(title="My Title", text="Hello world"):
            from web.scrape import fetch_page

            result = fetch_page("https://example.com")

        assert isinstance(result, dict)
        assert "url" in result
        assert "title" in result
        assert "text" in result
        assert "links" in result
        assert "error" in result

    def test_fetch_page_returns_correct_title(self) -> None:
        """fetch_page extracts the page title correctly."""
        with _patch_scrapling(title="FRIDAY Test Page"):
            from web.scrape import fetch_page

            result = fetch_page("https://example.com")

        assert result["title"] == "FRIDAY Test Page"

    def test_fetch_page_returns_text_content(self) -> None:
        """fetch_page extracts body text."""
        with _patch_scrapling(text="This is the body text."):
            from web.scrape import fetch_page

            result = fetch_page("https://example.com")

        assert "body text" in result["text"]

    def test_fetch_page_returns_links(self) -> None:
        """fetch_page returns a list of href strings."""
        links = ["https://example.com/page1", "https://example.com/page2"]
        with _patch_scrapling(links=links):
            from web.scrape import fetch_page

            result = fetch_page("https://example.com")

        assert isinstance(result["links"], list)
        assert "https://example.com/page1" in result["links"]

    def test_fetch_page_error_is_empty_string_on_success(self) -> None:
        """error field is empty string when extraction succeeds."""
        with _patch_scrapling():
            from web.scrape import fetch_page

            result = fetch_page("https://example.com")

        assert result["error"] == ""

    def test_fetch_page_url_matches_input(self) -> None:
        """url field reflects the input URL."""
        with _patch_scrapling():
            from web.scrape import fetch_page

            result = fetch_page("https://test.example.org/path")

        assert result["url"] == "https://test.example.org/path"


# ---------------------------------------------------------------------------
# Tests: graceful degradation when scrapling is not installed
# ---------------------------------------------------------------------------


class TestScraplingNotInstalled:
    def test_fetch_page_without_scrapling_returns_error_dict(self) -> None:
        """fetch_page gracefully returns an error dict when scrapling is absent."""
        with patch.dict("sys.modules", {"scrapling": None, "scrapling.fetchers": None}):
            # Force reimport of the module without scrapling
            saved = sys.modules.pop("web.scrape", None)
            try:
                from web.scrape import fetch_page

                result = fetch_page("https://example.com")
            finally:
                if saved is not None:
                    sys.modules["web.scrape"] = saved

        assert isinstance(result, dict)
        assert result["error"] != ""
        assert "unavailable" in result["error"].lower() or "not installed" in result["error"].lower()

    def test_fetch_page_without_scrapling_does_not_raise(self) -> None:
        """fetch_page must return a dict, not raise, when scrapling is absent."""
        with patch.dict("sys.modules", {"scrapling": None, "scrapling.fetchers": None}):
            saved = sys.modules.pop("web.scrape", None)
            try:
                from web.scrape import fetch_page

                # Must not raise
                result = fetch_page("https://example.com")
                assert isinstance(result, dict)
            finally:
                if saved is not None:
                    sys.modules["web.scrape"] = saved

    def test_scrape_summary_without_scrapling_returns_string(self) -> None:
        """scrape_summary returns a string error when scrapling is absent."""
        with patch.dict("sys.modules", {"scrapling": None, "scrapling.fetchers": None}):
            saved = sys.modules.pop("web.scrape", None)
            try:
                from web.scrape import scrape_summary

                result = scrape_summary("https://example.com")
            finally:
                if saved is not None:
                    sys.modules["web.scrape"] = saved

        assert isinstance(result, str)
        assert "error" in result.lower() or "unavailable" in result.lower()

    def test_fetch_page_fetch_error_returns_error_dict(self) -> None:
        """fetch_page returns error dict when fetcher.get() raises."""
        with _patch_scrapling(raises=ConnectionError("network unreachable")):
            from web.scrape import fetch_page

            result = fetch_page("https://example.com")

        assert isinstance(result, dict)
        assert result["error"] != ""
        assert "error" in result["error"].lower() or "connectionerror" in result["error"].lower()


# ---------------------------------------------------------------------------
# Tests: routing heuristic
# ---------------------------------------------------------------------------


class TestRoutingHeuristic:
    def test_read_only_task_with_read_keyword(self) -> None:
        """Tasks with read-only vocabulary are classified as read-only."""
        from web.agent import _is_read_only_task

        assert _is_read_only_task("read the article at this URL") is True
        assert _is_read_only_task("get the price of the product") is True
        assert _is_read_only_task("find the author of this article") is True
        assert _is_read_only_task("what is on the homepage") is True
        assert _is_read_only_task("summarize this page") is True

    def test_interactive_task_not_classified_as_read_only(self) -> None:
        """Tasks with interactive vocabulary are NOT classified as read-only."""
        from web.agent import _is_read_only_task

        assert _is_read_only_task("click the submit button") is False
        assert _is_read_only_task("fill out the form") is False
        assert _is_read_only_task("login with my credentials") is False
        assert _is_read_only_task("buy the item") is False
        assert _is_read_only_task("send the message") is False

    def test_ambiguous_task_is_not_read_only(self) -> None:
        """Tasks with neither keyword default to interactive (conservative)."""
        from web.agent import _is_read_only_task

        assert _is_read_only_task("do the thing on the website") is False

    @pytest.mark.asyncio
    async def test_read_only_task_with_url_tries_scrape_first(self) -> None:
        """A read-only task + URL routes through the scrape path first."""
        from web.agent import WebAgent

        mock_bu = MagicMock()
        mock_bu.Agent.return_value.run = AsyncMock(return_value=_make_history())
        mock_bu.ChatGoogle.return_value = MagicMock()

        with _patch_scrapling(text="Scrapling result here"):
            with patch.dict("sys.modules", {"browser_use": mock_bu}):
                # Patch _try_scrape to track calls
                with patch("web.agent._try_scrape") as mock_scrape:
                    mock_scrape.return_value = "Scrapling result here"
                    agent = WebAgent(_fake_settings(), allow_destructive=True)
                    result = await agent.web_task(
                        "read the article", url="https://example.com"
                    )

        mock_scrape.assert_called_once_with("https://example.com")
        # Agent.run should NOT have been called (scrape succeeded)
        mock_bu.Agent.return_value.run.assert_not_called()
        assert "Scrapling result here" in result

    @pytest.mark.asyncio
    async def test_interactive_task_skips_scrape(self) -> None:
        """An interactive task bypasses the scrape path entirely."""
        from web.agent import WebAgent

        mock_bu = MagicMock()
        history = _make_history(final="form submitted")
        mock_bu.Agent.return_value.run = AsyncMock(return_value=history)
        mock_bu.ChatGoogle.return_value = MagicMock()

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            with patch("web.agent._try_scrape") as mock_scrape:
                agent = WebAgent(_fake_settings(), allow_destructive=True)
                await agent.web_task("click the login button", url="https://example.com")

        mock_scrape.assert_not_called()

    @pytest.mark.asyncio
    async def test_scrape_fail_falls_back_to_agent(self) -> None:
        """When the scrape path returns None, the agent path is used."""
        from web.agent import WebAgent

        mock_bu = MagicMock()
        history = _make_history(final="agent result")
        mock_bu.Agent.return_value.run = AsyncMock(return_value=history)
        mock_bu.ChatGoogle.return_value = MagicMock()

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            with patch("web.agent._try_scrape", return_value=None):
                agent = WebAgent(_fake_settings(), allow_destructive=True)
                result = await agent.web_task("read the page", url="https://example.com")

        mock_bu.Agent.return_value.run.assert_called_once()
        assert "agent result" in result


# ---------------------------------------------------------------------------
# Tests: LLM construction — reuse of FRIDAY's Gemini config
# ---------------------------------------------------------------------------


class TestLLMReuse:
    @pytest.mark.asyncio
    async def test_chatgoogle_receives_gemini_api_key(self) -> None:
        """ChatGoogle is initialised with FRIDAY's gemini_api_key, not env lookup."""
        from web.agent import WebAgent

        mock_bu = MagicMock()
        history = _make_history(final="ok")
        mock_bu.Agent.return_value.run = AsyncMock(return_value=history)

        captured_kwargs: list[dict] = []

        def _capture_llm(**kwargs: Any) -> MagicMock:
            captured_kwargs.append(kwargs)
            return MagicMock()

        mock_bu.ChatGoogle.side_effect = _capture_llm

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            settings = _fake_settings(model="gemini-2.5-flash")
            agent = WebAgent(settings, allow_destructive=True)
            await agent.web_task("get the weather")

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0].get("api_key") == "fake-key-for-testing"
        assert captured_kwargs[0].get("model") == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_chatgoogle_construction_failure_returns_error_string(self) -> None:
        """If ChatGoogle() raises, web_task returns an error string."""
        from web.agent import WebAgent

        mock_bu = MagicMock()
        mock_bu.ChatGoogle.side_effect = ImportError("google-genai not available")

        with patch.dict("sys.modules", {"browser_use": mock_bu}):
            agent = WebAgent(_fake_settings(), allow_destructive=True)
            result = await agent.web_task("anything")

        assert isinstance(result, str)
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Tests: scrape_summary convenience wrapper
# ---------------------------------------------------------------------------


class TestScrapeSummary:
    def test_scrape_summary_returns_string(self) -> None:
        """scrape_summary always returns a plain string."""
        with _patch_scrapling(title="My Blog", text="Content here"):
            from web.scrape import scrape_summary

            result = scrape_summary("https://blog.example.com")

        assert isinstance(result, str)

    def test_scrape_summary_includes_title(self) -> None:
        """scrape_summary includes the page title in its output."""
        with _patch_scrapling(title="Important Article", text="Article body."):
            from web.scrape import scrape_summary

            result = scrape_summary("https://example.com/article")

        assert "Important Article" in result

    def test_scrape_summary_includes_text(self) -> None:
        """scrape_summary includes extracted text."""
        with _patch_scrapling(title="T", text="The quick brown fox."):
            from web.scrape import scrape_summary

            result = scrape_summary("https://example.com")

        assert "quick brown fox" in result

    def test_scrape_summary_on_error_returns_error_string(self) -> None:
        """scrape_summary returns a '(scrape error)' string on fetch failure."""
        with _patch_scrapling(raises=ConnectionError("no network")):
            from web.scrape import scrape_summary

            result = scrape_summary("https://unreachable.example.com")

        assert isinstance(result, str)
        assert "error" in result.lower()
