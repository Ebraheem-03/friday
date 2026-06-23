"""Gemini brain: streaming client + tool-call routing to runtime modules.

Latency budget
--------------
- First text chunk target: <800 ms from user_text received to first yielded delta.
- Tool-call round-trip: adds one extra model call; keep tool execution <200 ms for
  memory/recall. OS/web actions are inherently slower and are not latency-bounded here.

Runtime purity (CLAUDE.md §0)
------------------------------
No MCP anywhere in this file. Plain google-genai only. Tool Protocols are injected as
constructor arguments so the Brain is fully testable without any runtime module installed.

SDK attribute paths confirmed for google-genai==2.9.0
------------------------------------------------------
- `chunk.text`                                  — text delta on GenerateContentResponse
- `chunk.function_calls`                        — list[FunctionCall] | None (property)
- `chunk.candidates[0].content.parts[i].function_call` — underlying FunctionCall
- `FunctionCall.name`                           — str: the declared function name
- `FunctionCall.args`                           — dict[str, Any]: keyword args from model
- `types.Part.from_function_response(name=..., response={...})` — builds response Part
- `types.Content(role="user", parts=[part])`    — wraps response part for next turn
- `types.Tool(function_declarations=[...])`     — declares tools to the model
- `types.FunctionDeclaration(name=..., description=..., parameters=...)` — single tool
- `types.Schema(type=types.Type.OBJECT, properties={...}, required=[...])` — param schema
- `types.GenerateContentConfig(tools=[...], automatic_function_calling=...)`
- `types.AutomaticFunctionCallingConfig(disable=True)` — disables SDK auto-AFC so we
  do manual routing here (giving us full control over Protocol dispatch).

Verification: grep/inspect of installed package at
  ~/.pyenv/versions/3.12.6/lib/python3.12/site-packages/google/genai/models.py lines
  8719–8866; types.py via `python3 -c "from google.genai import types; ..."`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from google import genai
from google.genai import types

from core.config import Settings

logger = logging.getLogger(__name__)

# Maximum number of tool-call round-trips per user turn.  Gemini normally
# closes the loop in 1–2 rounds; 5 is a hard ceiling to prevent infinite
# chains from a misbehaving or prompt-injected model response.
_MAX_TOOL_ROUNDS: int = 5

# ---------------------------------------------------------------------------
# Runtime tool Protocols
# ---------------------------------------------------------------------------
# These define the interface the Brain expects from runtime modules (mnemo,
# vector, scout). Default no-op stubs satisfy the Protocols so the Brain runs
# standalone. Real implementations are injected by the Orchestrator in Steps 5–7.


@runtime_checkable
class MemoryTool(Protocol):
    """Interface to the mnemo memory module (Step 5)."""

    async def remember(self, key: str, value: str) -> str:
        """Store a key/value pair. Returns a confirmation message."""
        ...

    async def recall(self, query: str) -> str:
        """Retrieve memory entries relevant to query. Returns text summary."""
        ...


@runtime_checkable
class OSTool(Protocol):
    """Interface to the vector OS bridge (Step 6)."""

    async def os_action(self, action: str, params: dict[str, Any]) -> str:
        """Execute an OS-level action (click, type, open, etc.). Returns result summary."""
        ...


@runtime_checkable
class WebTool(Protocol):
    """Interface to the scout web agent (Step 7)."""

    async def web_task(self, task: str, url: str = "") -> str:
        """Execute a web task (search, scrape, fill form, etc.). Returns result summary."""
        ...


# ---------------------------------------------------------------------------
# Default no-op stubs (satisfy Protocols; used when no real impl injected)
# ---------------------------------------------------------------------------


class _NoopMemory:
    """Safe no-op memory stub: used until mnemo is wired in Step 5."""

    async def remember(self, key: str, value: str) -> str:
        logger.debug("NoopMemory.remember(%r, %r) — stub", key, value)
        return f"(memory stub) stored '{key}'"

    async def recall(self, query: str) -> str:
        logger.debug("NoopMemory.recall(%r) — stub", query)
        return "(memory stub) no memories yet"


class _NoopOS:
    """Safe no-op OS stub: used until vector is wired in Step 6."""

    async def os_action(self, action: str, params: dict[str, Any]) -> str:
        logger.debug("NoopOS.os_action(%r, %r) — stub", action, params)
        return f"(os stub) action '{action}' not yet implemented"


class _NoopWeb:
    """Safe no-op web stub: used until scout is wired in Step 7."""

    async def web_task(self, task: str, url: str = "") -> str:
        logger.debug("NoopWeb.web_task(%r, %r) — stub", task, url)
        return f"(web stub) task '{task}' not yet implemented"


# ---------------------------------------------------------------------------
# Tool declarations (registered with Gemini)
# ---------------------------------------------------------------------------
# Declarations are intentionally thin; full JSON schema detail arrives per step.

_TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="remember",
        description="Store a piece of information for later recall. Use this when the "
        "user asks you to remember something.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "key": types.Schema(
                    type=types.Type.STRING,
                    description="Short label for the memory (e.g. 'user_name').",
                ),
                "value": types.Schema(
                    type=types.Type.STRING,
                    description="The content to remember.",
                ),
            },
            required=["key", "value"],
        ),
    ),
    types.FunctionDeclaration(
        name="recall",
        description="Search stored memories for information relevant to a query.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Natural-language query describing what to look up.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="os_action",
        description="Perform a Linux OS-level action such as clicking, typing, opening "
        "an application, reading a file, or running a command.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "action": types.Schema(
                    type=types.Type.STRING,
                    description="Action name (e.g. 'click', 'type', 'open_app', 'run_command').",
                ),
                "params": types.Schema(
                    type=types.Type.OBJECT,
                    description="Action-specific parameters as a JSON object.",
                ),
            },
            required=["action", "params"],
        ),
    ),
    types.FunctionDeclaration(
        name="web_task",
        description="Execute a web automation task: search, scrape a URL, fill a form, "
        "or interact with a website.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task": types.Schema(
                    type=types.Type.STRING,
                    description="Plain-English description of the task to perform.",
                ),
                "url": types.Schema(
                    type=types.Type.STRING,
                    description="Optional starting URL. Leave empty for search tasks.",
                ),
            },
            required=["task"],
        ),
    ),
]

_GENERATE_CONFIG = types.GenerateContentConfig(
    tools=[types.Tool(function_declarations=_TOOL_DECLARATIONS)],
    # Disable SDK automatic function calling so we control routing to Protocols.
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)

# ---------------------------------------------------------------------------
# Conversation history type alias
# ---------------------------------------------------------------------------

ConversationHistory = list[types.Content]
"""Ordered list of Content objects representing the chat so far."""


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------


class Brain:
    """Gemini streaming brain with tool-call routing to runtime Protocols.

    Parameters
    ----------
    settings:
        Typed settings (api key + model read from here).
    memory:
        Implementation of MemoryTool. Defaults to no-op stub.
    os_tool:
        Implementation of OSTool. Defaults to no-op stub.
    web_tool:
        Implementation of WebTool. Defaults to no-op stub.

    Example
    -------
    >>> cfg = Settings.from_env()
    >>> brain = Brain(cfg)
    >>> async for delta in brain.stream("Hello", history=[]):
    ...     print(delta, end="", flush=True)
    """

    def __init__(
        self,
        settings: Settings,
        *,
        memory: MemoryTool | None = None,
        os_tool: OSTool | None = None,
        web_tool: WebTool | None = None,
    ) -> None:
        self._model = settings.gemini_model
        # Pass key explicitly — never rely on ambient env in prod code path.
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._memory: MemoryTool = memory or _NoopMemory()  # type: ignore[assignment]
        self._os_tool: OSTool = os_tool or _NoopOS()  # type: ignore[assignment]
        self._web_tool: WebTool = web_tool or _NoopWeb()  # type: ignore[assignment]
        logger.info("Brain initialised with model=%r", self._model)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def stream(
        self,
        user_text: str,
        history: ConversationHistory,
    ) -> AsyncIterator[str]:
        """Stream text deltas for *user_text* given *history*.

        Yields text chunks as they arrive from Gemini. If the model emits a
        function-call instead of text, the call is routed to the matching
        Protocol method, the result is fed back as a function-response, and
        streaming continues — yielding the model's follow-up text.

        The caller (Orchestrator / voice layer) must iterate the generator to
        drive the stream; the generator is lazy and holds no background tasks.

        Parameters
        ----------
        user_text:
            The latest user utterance (already ASR-decoded by echo in Step 4).
        history:
            The conversation history so far, as a list of Content objects.
            Caller retains ownership; this method does not mutate the list.

        Latency notes
        -------------
        - Text chunks arrive token-by-token; the voice layer (echo, Step 4)
          starts TTS as soon as it sees a sentence-ending chunk.
        - Each tool-call adds one extra model round-trip (~500–1500 ms depending
          on network). Keep tool execution sub-200 ms for memory/recall.
        """
        # Build the contents list: history + new user turn.
        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_text)],
        )
        contents: list[types.Content] = list(history) + [user_content]

        async for delta in self._run_stream(contents):
            yield delta

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_stream(
        self, contents: list[types.Content], _depth: int = 0
    ) -> AsyncIterator[str]:
        """Drive a single model call, handle any function-calls, and yield text.

        This is an async generator.  When the model emits function-calls we
        dispatch them, append the function-response, and recurse to get the
        model's follow-up text.  Recursion is bounded by ``_MAX_TOOL_ROUNDS``
        so a misbehaving or prompt-injected model cannot trigger an infinite
        chain of API calls.

        Parameters
        ----------
        contents:
            Accumulated conversation contents for this model call.
        _depth:
            Current recursion depth (callers must not pass this; it is managed
            internally).  When ``_depth`` reaches ``_MAX_TOOL_ROUNDS`` the
            recursion halts and a sentinel error string is returned to the
            model instead of executing further tool calls.
        """
        function_calls_seen: list[types.FunctionCall] = []

        async for chunk in await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=_GENERATE_CONFIG,
        ):
            # Text chunk — yield immediately so the voice layer can start TTS.
            if chunk.text:
                yield chunk.text

            # Function-call chunks — collect (model may spread across chunks).
            if chunk.function_calls:
                for fc in chunk.function_calls:
                    function_calls_seen.append(fc)

        # If the model issued tool-calls, execute them and continue the stream.
        if function_calls_seen:
            if _depth >= _MAX_TOOL_ROUNDS:
                # Hard stop: return an error to the model rather than looping.
                logger.warning(
                    "Tool-call depth limit (%d) reached; aborting further rounds.",
                    _MAX_TOOL_ROUNDS,
                )
                # Yield nothing further — the orchestrator's error handler will
                # log and reset state.  The turn ends here.
                return

            response_parts: list[types.Part] = []
            for fc in function_calls_seen:
                result = await self._dispatch_tool(fc)
                response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )

            # Append function-response turn and recurse to get the model reply.
            function_response_content = types.Content(
                role="user",
                parts=response_parts,
            )
            contents_with_response = list(contents) + [function_response_content]
            async for delta in self._run_stream(contents_with_response, _depth + 1):
                yield delta

    async def _dispatch_tool(self, fc: types.FunctionCall) -> str:
        """Route a FunctionCall to the matching Protocol method.

        Parameters
        ----------
        fc:
            The FunctionCall object from the model (has .name and .args).

        Returns
        -------
        str
            The string result from the Protocol method, to be sent back as the
            function-response.
        """
        name = fc.name or ""
        args: dict[str, Any] = dict(fc.args) if fc.args else {}

        logger.debug("Tool call: %s(%r)", name, args)

        if name == "remember":
            return await self._memory.remember(
                key=args.get("key", ""),
                value=args.get("value", ""),
            )
        elif name == "recall":
            return await self._memory.recall(query=args.get("query", ""))
        elif name == "os_action":
            return await self._os_tool.os_action(
                action=args.get("action", ""),
                params=args.get("params", {}),
            )
        elif name == "web_task":
            return await self._web_tool.web_task(
                task=args.get("task", ""),
                url=args.get("url", ""),
            )
        else:
            logger.warning("Unknown tool call: %r — returning error to model", name)
            return f"Error: unknown tool '{name}'"
