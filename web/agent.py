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

browser-use 0.13.1 also performs a PyPI version-check HTTP call on every
``agent.run()`` (``check_latest_browser_use_version`` in utils.py, gated by
``CONFIG.BROWSER_USE_VERSION_CHECK``).  We disable this with the second
module-level env var:

    os.environ["BROWSER_USE_VERSION_CHECK"] = "false"

Both must be set *before* the first ``import browser_use`` because
``CONFIG`` is constructed at package import time and the property reads
the env var fresh each time. The assignments are at module top-level so
they run the moment this file is imported, always before the lazy
browser-use import inside functions.
Ref telemetry: https://docs.browser-use.com/development/telemetry
Ref version-check: browser_use/config.py ``BROWSER_USE_VERSION_CHECK`` property.

Sentinel verification method: grep the installed browser_use package for
``ANONYMIZED_TELEMETRY`` and ``BROWSER_USE_VERSION_CHECK`` references to
confirm that both the telemetry class and the version-check gate
short-circuit on "false". The env vars are also logged at DEBUG level on
WebAgent construction so the test suite can assert them without running a
browser.

SSRF / private-network filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
With ``allow_destructive=True`` the agent can submit forms, follow
redirects, and POST to any URL the LLM decides to visit.  An attacker (or
a confused LLM) could direct the agent to ``http://169.254.169.254/`` (the
AWS/GCP/Azure metadata endpoint) or to ``http://192.168.1.1/admin`` —
giving the remote Gemini model read access to internal services.

The ``_check_url_allowed`` helper runs a three-layer check on every entry-
point URL *before* the browser agent is launched and before the cheap
scrape path fetches:

1. Scheme allowlist: only ``http`` and ``https`` are allowed.  ``file:``,
   ``ftp:``, ``data:``, etc. are rejected immediately.
2. IP literal check: the host is parsed as an ``ipaddress`` object. If it
   parses as a private/loopback/link-local/reserved address it is rejected.
3. DNS resolution + IP check: ``socket.getaddrinfo`` resolves the hostname
   and every returned IP is checked with ``ipaddress``. If *any* resolved
   IP is private/loopback/link-local/reserved the URL is rejected (DNS-
   rebinding defence). If resolution itself fails the URL is also rejected
   (fail-closed).

Rejected URLs return a ``"(web blocked) …"`` string immediately; the
browser agent and scrape fetcher are never called.

Residual risk — mid-task navigation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The pre-flight filter covers the *entry-point* URL passed to ``web_task``.
It does NOT prevent the agent from navigating to a private IP mid-task
(e.g. after the LLM decides to ``click`` a link that resolves to 10.x.x.x,
or follow a redirect from a public page to an internal service).  A complete
defence would require a CDP ``Network.requestWillBeSent`` intercept that
fires for every sub-navigation.  browser-use 0.13.1 does not expose a
per-request CDP hook.  Mitigations in place:

- Safe-mode task prefix (Layer 1) instructs the LLM not to navigate to
  unusual URLs; still applied when ``allow_destructive=False``.
- Hard wall-clock timeout prevents prolonged exfiltration even if the LLM
  does navigate somewhere unexpected.
- ``on_step_end`` hook logs the current URL after each step so anomalous
  navigation is visible in DEBUG logs.

Wiring a full CDP ``requestWillBeSent`` filter is deferred as a future
hardening item requiring either a browser-use fork or a custom
``BrowserSession`` subclass.

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

Action-level hook investigation — browser-use 0.13.1
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
**Finding:** browser-use 0.13.1 DOES expose per-step hooks:

- ``Agent.run(on_step_start, on_step_end)`` — both accept an async callable
  ``Callable[[Agent], Awaitable[None]]`` (``AgentHookFunc``).
- ``on_step_start`` fires before ``Agent.step()`` (i.e. before the LLM call
  for that step AND before any browser action is executed that step).
- ``on_step_end`` fires after each step completes (after actions, before the
  next step's LLM call).
- ``Agent.__init__`` also accepts ``register_new_step_callback`` which fires
  after the LLM produces an output but before the library executes it.
  Signature: ``Callable[[BrowserStateSummary, AgentOutput, int], None|Awaitable[None]]``.

**What hooks give us:** ``on_step_end`` receives the ``Agent`` object; we can
inspect ``agent.state.last_result`` and ``agent.browser_session`` to log the
URL after each step (useful anomaly signal). ``register_new_step_callback``
gives us the ``AgentOutput.action`` list (a list of ``ActionModel`` objects)
*after* the LLM decided them but *before* execution in a given step.

**What hooks do NOT give us:** an abort mechanism. ``on_step_start`` and
``on_step_end`` are fire-and-forget observers — raising inside them
propagates to browser-use's outer ``run()`` loop and would cancel the whole
task, but the library does not offer a structured "veto this step" API.
``register_new_step_callback`` similarly has no return path to abort.

**Decision:** We wire ``on_step_end`` into ``_run_agent`` to log the current
URL after every step at DEBUG level. This provides post-hoc anomaly
detection without forking the library. We do NOT attempt to veto individual
steps via a raised exception (that would produce an ``(web error)`` return
and is indistinguishable from a legitimate failure; it is also not the
behaviour ``confirm_callback`` promises — which gates the whole task, not
individual steps).

Wiring the existing ``confirm_callback`` into an action-level gate is not
feasible without a library fork: the hooks observe but cannot veto. The
SSRF filter (entry-point) + hard timeout + safe-mode prefix (when
``allow_destructive=False``) remain the layered defences for the
``allow_destructive=True`` case.

This finding and the residual risk (mid-task navigation to private IPs) are
documented above under "SSRF / private-network filter".

Wall-clock timeout
~~~~~~~~~~~~~~~~~~~
``agent.run(...)`` is wrapped in ``asyncio.wait_for(..., timeout=timeout_s)``
(constructor param, default 120 s). On cancellation the task is cleaned up
by asyncio and ``web_task`` returns a ``"(web error) task exceeded …"``
string. The scrape path (``_try_scrape``) also passes the timeout as a
``requests``-style cap via ``_try_scrape_timeout``.

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

import asyncio
import ipaddress
import logging
import os
import socket
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Telemetry opt-out — MUST happen before any browser_use import.
# browser-use reads ANONYMIZED_TELEMETRY at package import time.
# Setting it here (module top-level) guarantees it fires before the lazy
# imports inside web_task / _run_agent.
# Ref: https://docs.browser-use.com/development/telemetry
# ---------------------------------------------------------------------------
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

# ---------------------------------------------------------------------------
# Version-check opt-out — browser-use 0.13.1 calls check_latest_browser_use_version()
# (an async HTTP request to PyPI) on every agent.run().  This is uninvited
# network egress.  CONFIG.BROWSER_USE_VERSION_CHECK reads this env var as a
# property on each call so setting it at module load time is sufficient.
# Ref: browser_use/config.py BROWSER_USE_VERSION_CHECK property;
#      browser_use/agent/service.py line ~2037.
# ---------------------------------------------------------------------------
os.environ.setdefault("BROWSER_USE_VERSION_CHECK", "false")

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
# SSRF / private-network filter
# ---------------------------------------------------------------------------

# Schemes that are allowed through the filter.  Everything else (file:, ftp:,
# data:, javascript:, blob:, etc.) is rejected.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_BLOCKED_RESULT_PREFIX = "(web blocked) refused to access internal/loopback address"


def _check_url_allowed(url: str) -> str | None:
    """Return None if the URL is safe to fetch; a ``(web blocked) …`` string if not.

    Three-layer check:
    1. Scheme: must be http or https.
    2. Host as IP literal: checked directly with ``ipaddress``.
    3. DNS resolution: ``socket.getaddrinfo`` resolves hostnames; every
       returned address is checked.  If resolution fails → fail-closed.

    Only the host is inspected (never query strings or path segments) to
    keep logging metadata-safe and to avoid false positives.
    """
    if not url or not url.strip():
        return f"{_BLOCKED_RESULT_PREFIX}: empty URL"

    try:
        parsed = urlparse(url)
    except Exception:
        return f"{_BLOCKED_RESULT_PREFIX}: could not parse URL"

    # --- 1. Scheme check ---------------------------------------------------
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (
            f"(web blocked) scheme '{scheme}' is not allowed "
            "(only http and https are permitted)"
        )

    host = parsed.hostname or ""
    if not host:
        return f"{_BLOCKED_RESULT_PREFIX}: no host in URL"

    # Strip IPv6 brackets if present (urlparse.hostname already does this,
    # but guard defensively).
    host_clean = host.strip("[]")

    # --- 2. IP literal check -----------------------------------------------
    try:
        addr = ipaddress.ip_address(host_clean)
        if _is_private_addr(addr):
            logger.debug(
                "WebAgent SSRF block: IP literal %s is private/loopback/link-local",
                host_clean,
            )
            return f"{_BLOCKED_RESULT_PREFIX}: {host_clean}"
    except ValueError:
        # Not an IP literal — fall through to DNS resolution.
        pass

    # --- 3. DNS resolution check -------------------------------------------
    try:
        addrinfos = socket.getaddrinfo(host_clean, None)
    except socket.gaierror as exc:
        # Resolution failed — fail closed (reject).
        logger.debug(
            "WebAgent SSRF block: DNS resolution failed for host=%r: %s", host_clean, exc
        )
        return (
            f"(web blocked) could not resolve host '{host_clean}': "
            "DNS lookup failed (fail-closed)"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "WebAgent SSRF block: unexpected error resolving host=%r: %s",
            host_clean,
            type(exc).__name__,
        )
        return (
            f"(web blocked) could not resolve host '{host_clean}': "
            f"{type(exc).__name__}"
        )

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
            if _is_private_addr(addr):
                logger.debug(
                    "WebAgent SSRF block: host=%r resolved to private IP %s",
                    host_clean,
                    ip_str,
                )
                return f"{_BLOCKED_RESULT_PREFIX}: {host_clean} resolves to {ip_str}"
        except ValueError:
            continue  # Shouldn't happen — getaddrinfo always returns valid IPs

    return None  # All checks passed


def _is_private_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* is private, loopback, link-local, or otherwise reserved.

    Covers:
    - 127.0.0.0/8 (IPv4 loopback)
    - 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 (RFC-1918 private)
    - 169.254.0.0/16 (link-local — includes the cloud metadata IP 169.254.169.254)
    - ::1 (IPv6 loopback)
    - fc00::/7 (IPv6 unique-local)
    - fe80::/10 (IPv6 link-local)
    - Any other address flagged as private/loopback/link-local by ``ipaddress``.
    """
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
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
        This is the coarse-grained gate; fine-grained action interception is
        not achievable without a library fork — see module docstring.
    max_steps:
        Hard cap on the number of browser-use agent steps per task. Prevents
        runaway automation. Defaults to 20 (conservative; raise if tasks need
        more steps).
    timeout_s:
        Hard wall-clock timeout in seconds for the full ``agent.run()`` call.
        If the agent has not completed within this time it is cancelled and
        ``web_task`` returns an error string. Default: 120 s.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        allow_destructive: bool = False,
        confirm_callback: Callable[[str], bool] | None = None,
        max_steps: int = 20,
        timeout_s: float = 120.0,
    ) -> None:
        self._settings = settings
        self._allow_destructive = allow_destructive
        self._confirm_callback = confirm_callback
        self._max_steps = max_steps
        self._timeout_s = timeout_s

        logger.debug(
            "WebAgent initialised: model=%r allow_destructive=%r max_steps=%d "
            "timeout_s=%.1f ANONYMIZED_TELEMETRY=%r BROWSER_USE_VERSION_CHECK=%r",
            settings.gemini_model,
            allow_destructive,
            max_steps,
            timeout_s,
            os.environ.get("ANONYMIZED_TELEMETRY"),
            os.environ.get("BROWSER_USE_VERSION_CHECK"),
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
        # --- SSRF pre-flight check -----------------------------------------
        # Validate the entry-point URL before any network activity.
        # This covers the scrape path AND the browser agent entry point.
        if url:
            blocked = _check_url_allowed(url)
            if blocked is not None:
                logger.debug(
                    "WebAgent: SSRF filter blocked url host=%s", _safe_host(url)
                )
                return blocked

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
            "WebAgent: launching browser agent task_len=%d url=%r timeout_s=%.1f",
            len(effective_task),
            _safe_host(url) if url else "",
            self._timeout_s,
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

        # --- Agent construction + run with wall-clock timeout --------------
        # NOTE: max_steps is a parameter of Agent.run(), NOT Agent.__init__().
        # Passing it to the constructor would be silently swallowed by **kwargs
        # and the cap would never be applied — a live-only bug CI cannot catch.
        # Verified against browser-use 0.13.1 source: Agent.__init__ signature
        # has no max_steps param; Agent.run(max_steps: int = 500) does.
        #
        # on_step_end hook: logs the current browser URL after each step at
        # DEBUG level for post-hoc anomaly detection (mid-task navigation
        # monitoring). See module docstring "Action-level hook investigation"
        # for full rationale. The hook is observation-only; it does NOT abort
        # steps.
        #
        # asyncio.wait_for wraps the entire run() call with a hard wall-clock
        # cap (self._timeout_s). On timeout, asyncio.CancelledError propagates
        # out of wait_for; we catch it and return a structured error string.
        try:
            agent = Agent(
                task=effective_task,
                llm=llm,
            )

            async def _on_step_end(ag: object) -> None:
                """Log current URL after each step for anomaly detection."""
                try:
                    sess = getattr(ag, "browser_session", None)
                    if sess is not None:
                        current_url = getattr(sess, "current_url", None)
                        if current_url:
                            logger.debug(
                                "WebAgent step complete, current_host=%s",
                                _safe_host(str(current_url)),
                            )
                except Exception:  # noqa: BLE001
                    pass  # Never let the hook crash the step

            history = await asyncio.wait_for(
                agent.run(max_steps=self._max_steps, on_step_end=_on_step_end),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.debug(
                "WebAgent: agent.run() exceeded timeout_s=%.1f", self._timeout_s
            )
            return (
                f"(web error) task exceeded {self._timeout_s:.0f}s and was aborted"
            )
        except asyncio.CancelledError:
            logger.debug("WebAgent: agent.run() was cancelled")
            return f"(web error) task exceeded {self._timeout_s:.0f}s and was aborted"
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
