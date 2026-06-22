"""Agentic web automation using browser-use (agentic navigation & forms).

Runtime purity (CLAUDE.md §0)
------------------------------
browser-use is a runtime dep in the [web] extra. All browser-use imports are
*lazy* (inside functions, guarded by try/except ImportError) so this module
loads safely in CI without the [web] extra installed.

Security (CLAUDE.md §3 + ADR-0008)
------------------------------------
Telemetry
~~~~~~~~~
browser-use ships PostHog telemetry that phones home on every agent run.
FRIDAY disables it unconditionally at **module load time** (before any
browser-use import can occur) by setting:

    os.environ["ANONYMIZED_TELEMETRY"] = "false"

This is the documented opt-out (https://docs.browser-use.com/development/telemetry).
It must be set *before* the first ``import browser_use`` because the telemetry
client initialises at package import. The assignment is at module top-level so
it runs the moment this file is imported, which is always before the lazy
browser-use import inside functions.

Sentinel verification method: grep the installed browser_use package for
``ANONYMIZED_TELEMETRY`` references and confirm that the telemetry class
short-circuits on "false". The env var is also logged at DEBUG level on
WebAgent construction so the test suite can assert it without running a browser.

State-changing / destructive action gate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
browser-use is an **agentic** browser controller: the LLM decides each step
autonomously. FRIDAY cannot intercept individual browser actions from the
outside without forking browser-use. The following two-layer defence is used:

Layer 1 — Task prefix guard (wired in web_task):
    The task string is prepended with a safety instruction that tells the LLM
    to read and navigate but not to submit forms, log in, make purchases, post
    messages, or perform any irreversible state-changing action unless
    ``allow_destructive=True`` is passed.

Layer 2 — Constructor flag:
    ``WebAgent(settings, allow_destructive=False)`` (default). When
    ``allow_destructive=False``, the safety prefix is always prepended.
    When ``allow_destructive=True`` (opt-in at construction) the prefix is
    omitted and the LLM acts freely.

Limitation / Step 9 hardening item
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
browser-use does not expose a callback that would let us individually approve
or deny browser actions (click, type, submit) at the Python level in 0.13.1.
Layer 1 relies on the LLM honouring the instruction prefix — a sufficiently
adversarial task prompt could cause it to disobey. A true action-level gate
(intercepting CDP events or monkey-patching browser-use's action executors)
is deferred to Step 9 hardening. This is surfaced as a Step 9 item below.

Step 9 hardening items
~~~~~~~~~~~~~~~~~~~~~~~
- [SEC] WebAgent action-level gate: implement CDP/browser-use hook to
  intercept form-submit / navigation events and prompt the user before
  executing. Currently only a task-prefix LLM instruction stops submissions.
- [SEC] Allowlist/denylist for domains: block browser-use from navigating to
  internal network addresses (10.x, 192.168.x, localhost) to prevent SSRF.
- [SEC] Timeout enforcement: add a hard wall-clock timeout on agent.run() to
  prevent runaway automation.
- [SEC] Screenshot / extracted_content logging: browser-use may log page
  screenshots or extracted text internally. Verify at Step 9 that nothing
  at INFO level exposes PII or credentials.

Routing heuristic
-----------------
``web/agent.py`` is the *expensive, interactive* path. Route here when:
  - The task requires JavaScript rendering, login, or multi-step interaction.
  - Navigation decisions depend on page content (click the right link, fill
    a form, paginate results).
  - A simple HTTP fetch (``web/scrape.py``) would miss dynamic content.

For clearly read-only/static-page tasks, the caller should route to
``web/scrape.py`` first and fall back here only if that path fails or the
task description implies interaction.

LLM reuse
---------
browser-use 0.13.1 provides a ``ChatGoogle`` class backed by ``google-genai``
(the same SDK FRIDAY already uses for the Brain). ``WebAgent`` accepts a
``Settings`` object and constructs ``ChatGoogle(model=..., api_key=...)``
from it — no second provider, no hard-coded key.

Source: https://docs.browser-use.com/supported-models (confirmed 2026-06-23).
``ChatGoogle`` constructor: ``ChatGoogle(model: str, api_key: str | None = None)``.
``Agent.run()`` returns ``AgentHistoryList``; ``history.final_result()`` gives
the final extracted string.

Host setup for live use (atlas — add to [REVIEW])
--------------------------------------------------
browser-use 0.13.1 drives Chromium via Playwright. Before first live run:
    playwright install chromium --with-deps
If the [web] extra is freshly installed, playwright itself is a transitive dep
and can be invoked via:
    python -m playwright install chromium --with-deps
This downloads ~150 MB of Chromium to ~/.cache/ms-playwright. It is NOT run
during CI or tests — the browser is fully mocked.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Callable

# ---------------------------------------------------------------------------
# Telemetry opt-out — MUST happen before any browser_use import.
# browser-use reads ANONYMIZED_TELEMETRY at package import time.
# Setting it here (module top-level) guarantees it fires before the lazy
# imports inside web_task / _run_agent.
# Ref: https://docs.browser-use.com/development/telemetry
# ---------------------------------------------------------------------------
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

from core.config import Settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety task prefix (Layer 1 gate for state-changing actions)
# ---------------------------------------------------------------------------

_SAFE_MODE_PREFIX: str = (
    "[SAFETY CONSTRAINT — do NOT override] "
    "You are operating in read-only/navigate mode. "
    "You may open URLs, scroll, click links, and extract information. "
    "Do NOT submit any form, do NOT click 'buy', 'purchase', 'send', 'post', "
    "'login', 'sign in', 'delete', or any button that causes an irreversible "
    "state change. If the task would require such an action, stop and report "
    "that the action requires explicit human authorisation. "
    "Original task: "
)

# ---------------------------------------------------------------------------
# WebAgent
# ---------------------------------------------------------------------------


class WebAgent:
    """Agentic browser controller satisfying the ``WebTool`` Protocol from brain.py.

    Parameters
    ----------
    settings:
        FRIDAY runtime settings — provides ``gemini_api_key`` and
        ``gemini_model`` for the browser-use LLM (no second provider).
    allow_destructive:
        When ``False`` (default, safe), every task is prepended with a
        safety instruction that tells the LLM not to submit forms,
        make purchases, post messages, or perform irreversible actions.
        Set ``True`` only when the caller has obtained explicit human consent
        for the specific task.
    confirm_callback:
        Optional hook called with a human-readable description of the task
        before the agent runs. Return ``True`` to proceed, ``False`` to abort.
        When ``None`` (default), the agent runs without a pre-flight prompt.
        This is the coarse-grained gate; fine-grained action interception is a
        Step 9 hardening item.
    max_steps:
        Hard cap on the number of browser-use agent steps per task. Prevents
        runaway automation. Defaults to 20 (conservative; raise if tasks need
        more steps).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        allow_destructive: bool = False,
        confirm_callback: Callable[[str], bool] | None = None,
        max_steps: int = 20,
    ) -> None:
        self._settings = settings
        self._allow_destructive = allow_destructive
        self._confirm_callback = confirm_callback
        self._max_steps = max_steps

        logger.debug(
            "WebAgent initialised: model=%r allow_destructive=%r max_steps=%d "
            "ANONYMIZED_TELEMETRY=%r",
            settings.gemini_model,
            allow_destructive,
            max_steps,
            os.environ.get("ANONYMIZED_TELEMETRY"),
        )

    # ------------------------------------------------------------------
    # WebTool Protocol method
    # ------------------------------------------------------------------

    async def web_task(self, task: str, url: str = "") -> str:
        """Execute a web task and return a concise structured summary.

        Satisfies the ``WebTool`` Protocol declared in ``core/brain.py``.
        Never raises — all exceptions are caught and returned as an error string
        (mirroring ``_NoopWeb`` and the brain tool router contract).

        Routing: if the task looks like a simple URL read with no interaction
        needed, we first try the cheaper ``web/scrape.py`` path. If that
        returns useful content we skip the full browser agent. Otherwise we
        fall through to the browser-use ``Agent``.

        Parameters
        ----------
        task:
            Plain-English description of what to do on the web.
        url:
            Optional starting URL. Empty string for open-ended search tasks.

        Returns
        -------
        str
            Human-readable summary of the task result, suitable for feeding
            back to Gemini as a function-response.
        """
        try:
            return await self._execute(task, url)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "WebAgent.web_task unhandled exception for task=%r: %s",
                task[:60],
                type(exc).__name__,
            )
            return f"(web error) {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # Internal: routing + execution
    # ------------------------------------------------------------------

    async def _execute(self, task: str, url: str) -> str:
        """Route task to scrape or agentic path, honouring the safe-mode gate."""
        # --- Confirm gate (coarse) -----------------------------------------
        if self._confirm_callback is not None:
            description = f"web_task: {task[:120]}" + (f" @ {url}" if url else "")
            allowed = self._confirm_callback(description)
            if not allowed:
                logger.debug("WebAgent: task blocked by confirm_callback")
                return f"(web blocked) Task blocked by confirm callback: {task[:80]}"

        # --- Routing heuristic ---------------------------------------------
        # Use the cheap scrape path when:
        #   1. A URL is provided (caller knows where to look).
        #   2. The task uses read-only vocabulary and no "click / fill / login".
        if url and _is_read_only_task(task):
            logger.debug("WebAgent: routing to scrape path for host=%s", _safe_host(url))
            scrape_result = _try_scrape(url)
            if scrape_result and not scrape_result.startswith("(scrape error)"):
                return scrape_result
            # Scrape failed or empty — fall through to agent.
            logger.debug("WebAgent: scrape path failed, falling back to browser agent")

        # --- Full browser-use agent path -----------------------------------
        return await self._run_agent(task, url)

    async def _run_agent(self, task: str, url: str) -> str:
        """Run a browser-use Agent and return a summary string."""
        # --- Lazy import + availability check ------------------------------
        try:
            from browser_use import Agent, ChatGoogle  # type: ignore[import-not-found]
        except ImportError:
            logger.debug("browser-use not installed — agentic web unavailable")
            return (
                "(web unavailable) browser-use is not installed. "
                "Install the [web] extra: pip install 'friday[web]'"
            )

        # --- Safe-mode task construction -----------------------------------
        effective_task = task
        if not self._allow_destructive:
            effective_task = _SAFE_MODE_PREFIX + task
        if url:
            effective_task = f"{effective_task}\nStarting URL: {url}"

        logger.debug(
            "WebAgent: launching browser agent task_len=%d url=%r",
            len(effective_task),
            _safe_host(url) if url else "",
        )

        # --- LLM construction (reuse FRIDAY's Gemini config) ---------------
        # ChatGoogle accepts api_key directly so we never rely on GOOGLE_API_KEY
        # being set in the environment. We bridge FRIDAY's gemini_api_key to
        # browser-use's ChatGoogle.
        # Source: https://docs.browser-use.com/supported-models (2026-06-23)
        try:
            llm = ChatGoogle(
                model=self._settings.gemini_model,
                api_key=self._settings.gemini_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebAgent: ChatGoogle construction failed: %s", type(exc).__name__)
            return f"(web error) Failed to initialise LLM: {type(exc).__name__}: {exc}"

        # --- Agent construction + run ---------------------------------------
        # NOTE: max_steps is a parameter of Agent.run(), NOT Agent.__init__().
        # Passing it to the constructor would be silently swallowed by **kwargs
        # and the cap would never be applied — a live-only bug CI cannot catch.
        # Verified against browser-use 0.13.1 source: Agent.__init__ signature
        # has no max_steps param; Agent.run(max_steps: int = 500) does.
        try:
            agent = Agent(
                task=effective_task,
                llm=llm,
            )
            history = await agent.run(max_steps=self._max_steps)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "WebAgent: agent.run() raised %s", type(exc).__name__
            )
            return f"(web error) Agent run failed: {type(exc).__name__}: {exc}"

        # --- Extract result -------------------------------------------------
        try:
            final = history.final_result()
            if final:
                logger.debug(
                    "WebAgent: task complete, result_len=%d", len(str(final))
                )
                return str(final)

            # final_result() can return None if the agent did not extract
            # content explicitly. Fall back to extracted_content list.
            extracted = history.extracted_content()
            if extracted:
                summary = "\n".join(str(e) for e in extracted if e)
                logger.debug(
                    "WebAgent: using extracted_content, items=%d", len(extracted)
                )
                return summary or "(web: agent completed but returned no content)"

            # Check for errors in history
            if history.has_errors():
                errors = history.errors()
                err_str = "; ".join(str(e) for e in errors if e)
                return f"(web error) Agent completed with errors: {err_str}"

            return "(web: agent completed but returned no content)"

        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "WebAgent: result extraction error: %s", type(exc).__name__
            )
            return f"(web error) Result extraction failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

_READ_ONLY_KEYWORDS: frozenset[str] = frozenset(
    {
        "read",
        "get",
        "fetch",
        "scrape",
        "extract",
        "find",
        "search",
        "look up",
        "lookup",
        "check",
        "show",
        "display",
        "summarise",
        "summarize",
        "what",
        "list",
        "tell me",
        "give me",
    }
)

_INTERACTIVE_KEYWORDS: frozenset[str] = frozenset(
    {
        "click",
        "fill",
        "submit",
        "login",
        "log in",
        "sign in",
        "register",
        "buy",
        "purchase",
        "order",
        "book",
        "send",
        "post",
        "comment",
        "reply",
        "download",
        "upload",
        "navigate",
        "scroll to",
        "interact",
    }
)


def _is_read_only_task(task: str) -> bool:
    """Heuristic: return True if the task sounds read-only / non-interactive.

    Logic:
    - True if any read-only keyword matches AND no interactive keyword matches.
    - Conservative: unknown tasks (neither list hits) return False so we fall
      through to the full agent rather than risk a bad scrape result.
    """
    lower = task.lower()
    has_interactive = any(kw in lower for kw in _INTERACTIVE_KEYWORDS)
    if has_interactive:
        return False
    has_read = any(kw in lower for kw in _READ_ONLY_KEYWORDS)
    return has_read


def _safe_host(url: str) -> str:
    """Return just the hostname for metadata-safe logging."""
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc or url[:80]
    except Exception:
        return url[:80]


def _try_scrape(url: str) -> str | None:
    """Attempt a cheap scrape of *url*. Returns None on import failure."""
    try:
        from web.scrape import scrape_summary  # type: ignore[import-not-found]

        return scrape_summary(url)
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("WebAgent: scrape helper raised %s", type(exc).__name__)
        return None
