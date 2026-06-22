"""Linux OS bridge: AT-SPI semantic targeting + ydotool/xdotool input.

Display-server auto-detection (XDG_SESSION_TYPE + corroborating env vars).
Security contract: every destructive action is gated through confirm_or_dry_run().

Runtime purity (CLAUDE.md §0)
------------------------------
No MCP in this module. Native Python only: gi/Atspi (PyGObject), shutil, subprocess.
gi is imported lazily so this module loads cleanly in CI where PyGObject is absent.

Host requirements (for live use — do NOT configure these here, list for human):
- ydotool binary: `sudo apt install ydotool`
- ydotoold daemon: `sudo systemctl enable --now ydotoold`  or  `ydotoold &`
- uinput access: user must be in the `input` group — `sudo usermod -aG input $USER`
- AT-SPI accessibility: enabled in GNOME Settings → Accessibility → Enable (or via
  `gsettings set org.gnome.desktop.interface toolkit-accessibility true`)
- PyGObject runtime: `sudo apt install python3-gi gir1.2-atspi-2.0`  (or `pip install '.[os]'`
  with system dev libs: `sudo apt install libgirepository-2.0-dev gobject-introspection`)

Action taxonomy
---------------
SAFE (no confirm/dry-run required — read-only or non-destructive launch):
  - read_file       Read a local file (size-capped; no sensitive-path traversal).
  - find_element    Find a UI element by role/name via AT-SPI (read-only).
  - open_app        Launch a desktop application (additive; does not destroy state).
  - click           Synthetic click on a UI element or coordinate.
  - type            Synthetic keyboard text input.

DESTRUCTIVE (always gated by confirm_or_dry_run):
  - run_command     Execute an arbitrary subprocess (can change any system state).

Note: `click` and `type` are not gated because they target a specific UI element or
coordinate and cannot irreversibly destroy data on their own (the application they
target would need to handle that separately). `open_app` only launches, never kills.
`run_command` is the sole fully destructive action in the current allowlist; future
additions (kill_process, delete_file, send_email) must also be gated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Display-server detection
# ---------------------------------------------------------------------------

# File size cap for read_file: 256 KiB. Large files are truncated with a note.
_READ_FILE_MAX_BYTES: int = 256 * 1024

# Maximum output chars logged from command results (never log full output by default).
_CMD_RESULT_LOG_CHARS: int = 120


class DisplayServer(str, Enum):
    """Known display server types."""

    WAYLAND = "wayland"
    X11 = "x11"
    UNKNOWN = "unknown"


_detected_server: DisplayServer | None = None


def detect_display_server() -> DisplayServer:
    """Return the active display server, detected once and cached.

    Detection order:
    1. XDG_SESSION_TYPE (canonical, set by the display manager).
    2. WAYLAND_DISPLAY (present on Wayland even without XDG_SESSION_TYPE).
    3. DISPLAY (X11 fallback).
    4. Unknown if none of the above.

    Logs the result at INFO on the first call only.
    """
    global _detected_server
    if _detected_server is not None:
        return _detected_server

    xdg = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "").strip()
    x11_display = os.environ.get("DISPLAY", "").strip()

    if xdg == "wayland" or (not xdg and wayland_display):
        server = DisplayServer.WAYLAND
    elif xdg == "x11" or (not xdg and x11_display):
        server = DisplayServer.X11
    else:
        server = DisplayServer.UNKNOWN

    logger.info(
        "Display server detected: %s (XDG_SESSION_TYPE=%r, WAYLAND_DISPLAY=%r, DISPLAY=%r)",
        server.value,
        xdg or "(unset)",
        wayland_display or "(unset)",
        x11_display or "(unset)",
    )
    _detected_server = server
    return server


# ---------------------------------------------------------------------------
# Subprocess helper (input dispatch)
# ---------------------------------------------------------------------------

def _run_input_tool(argv: list[str]) -> tuple[bool, str]:
    """Run a display-input tool (ydotool / xdotool) as a subprocess.

    Returns (success: bool, message: str).
    Never raises; all errors are returned as message strings.
    """
    if not argv:
        return False, "Empty argv passed to _run_input_tool"

    binary = argv[0]
    binary_path = shutil.which(binary)
    if binary_path is None:
        return (
            False,
            f"Input tool '{binary}' not found on PATH. "
            f"Install it and (for ydotool) start ydotoold daemon. "
            f"See host requirements in os_bridge.py module docstring.",
        )

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, f"Binary not found when executing: {binary}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out running: {binary} {argv[1:]}"
    except OSError as exc:
        return False, f"OS error running {binary}: {exc}"

    if result.returncode == 0:
        return True, result.stdout.strip() or "ok"
    return False, (result.stderr.strip() or f"exit code {result.returncode}")


def _choose_input_tool() -> str | None:
    """Return the preferred input binary name for the current display server.

    Returns None if neither tool is available, or the server is unknown.
    """
    server = detect_display_server()
    if server == DisplayServer.WAYLAND:
        if shutil.which("ydotool"):
            return "ydotool"
        # ydotool is required on Wayland; xdotool is X11-only — do not fall back.
        return None
    elif server == DisplayServer.X11:
        if shutil.which("xdotool"):
            return "xdotool"
        return None
    return None


# ---------------------------------------------------------------------------
# AT-SPI UI element lookup (lazy gi import)
# ---------------------------------------------------------------------------

def _atspi_find_element(role: str, name: str) -> Any:
    """Find a UI element by accessibility role and name using AT-SPI.

    Imports gi/Atspi lazily so this module loads cleanly in CI without PyGObject.
    Returns the Atspi.Accessible object, or a string error message if unavailable.

    Parameters
    ----------
    role:
        AT-SPI role name string (e.g. 'push button', 'text', 'menu item').
    name:
        Accessible name of the element (case-insensitive substring match).
    """
    try:
        import gi  # noqa: PLC0415

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: PLC0415
    except (ImportError, ValueError) as exc:
        return f"AT-SPI unavailable (gi/Atspi not installed): {exc}"

    try:
        desktop = Atspi.get_desktop(0)
    except Exception as exc:
        return f"AT-SPI desktop not accessible: {exc}"

    role_lower = role.lower().strip()
    name_lower = name.lower().strip()

    def _walk(node: Any, depth: int = 0) -> Any:
        if depth > 16:
            return None
        try:
            node_role = node.get_role_name().lower()
            node_name = (node.get_name() or "").lower()
        except Exception:
            return None

        if role_lower in node_role and name_lower in node_name:
            return node

        try:
            child_count = node.get_child_count()
        except Exception:
            return None

        for i in range(child_count):
            try:
                child = node.get_child_at_index(i)
            except Exception:
                continue
            found = _walk(child, depth + 1)
            if found is not None:
                return found

        return None

    result = _walk(desktop)
    if result is None:
        return f"AT-SPI: no element found with role={role!r} name={name!r}"
    return result


# ---------------------------------------------------------------------------
# confirm / dry-run gate
# ---------------------------------------------------------------------------

# Synchronous confirm callback type: receives a description string, returns bool.
ConfirmCallback = Callable[[str], bool]

# Async confirm callback type.
AsyncConfirmCallback = Callable[[str], Awaitable[bool]]


def _default_confirm(description: str) -> bool:
    """Default confirm callback: always deny (safe / dry-run default posture).

    In production, replace with a callback that prompts the user.
    """
    logger.warning(
        "Destructive action blocked by default deny confirm callback: %s",
        description,
    )
    return False


# ---------------------------------------------------------------------------
# OsBridge — main class
# ---------------------------------------------------------------------------


class OsBridge:
    """Linux OS bridge satisfying the OSTool Protocol from core.brain.

    Parameters
    ----------
    dry_run:
        When True (default), destructive actions are described but NOT executed.
        The bridge returns a '[dry-run] would ...' string instead.
    confirm_callback:
        A sync callable(description: str) -> bool called for destructive actions
        when dry_run=False. Return True to allow execution, False to abort.
        Defaults to a deny-all callback that logs a warning.

    Security notes
    --------------
    - Every destructive action (run_command) passes through confirm_or_dry_run().
    - run_command passes argv lists directly to subprocess.run — no shell=True,
      no string interpolation of user input. If the model sends a string command
      it is rejected with an error message; the caller must pass a list.
    - read_file caps output at _READ_FILE_MAX_BYTES and never logs file content.
    - AT-SPI imports are lazy and fail gracefully.

    Usage
    -----
    Inject at Brain construction time::

        bridge = OsBridge(dry_run=False, confirm_callback=my_prompt)
        brain = Brain(settings, os_tool=bridge)

    # TODO(atlas): inject OsBridge at app boot
    """

    def __init__(
        self,
        *,
        dry_run: bool = True,
        confirm_callback: ConfirmCallback | None = None,
    ) -> None:
        self._dry_run = dry_run
        self._confirm_callback: ConfirmCallback = confirm_callback or _default_confirm

        # Build the action allowlist: maps action name -> handler coroutine.
        self._dispatch: dict[str, Callable[..., Any]] = {
            "read_file": self._act_read_file,
            "find_element": self._act_find_element,
            "open_app": self._act_open_app,
            "click": self._act_click,
            "type": self._act_type,
            "run_command": self._act_run_command,
        }

        logger.info(
            "OsBridge initialised (dry_run=%s, display_server=%s)",
            self._dry_run,
            detect_display_server().value,
        )

    # ------------------------------------------------------------------
    # OSTool Protocol method
    # ------------------------------------------------------------------

    async def os_action(self, action: str, params: dict[str, Any]) -> str:
        """Dispatch an OS action by name from the explicit allowlist.

        Returns a human-readable result string.  Never raises to the caller;
        unknown or failed actions return an error string (mirrors _NoopOS and
        the Brain tool router).

        Parameters
        ----------
        action:
            Action name. Must be in the allowlist.
        params:
            Action-specific parameters dict.
        """
        handler = self._dispatch.get(action)
        if handler is None:
            known = ", ".join(sorted(self._dispatch))
            logger.warning("os_action: unknown action %r (known: %s)", action, known)
            return f"Error: unknown OS action '{action}'. Known actions: {known}"

        try:
            result = handler(params)
            # Handlers may be sync or async.
            if asyncio.iscoroutine(result):
                result = await result
            return str(result)
        except Exception as exc:
            logger.exception("os_action %r raised unexpectedly", action)
            return f"Error executing OS action '{action}': {exc}"

    # ------------------------------------------------------------------
    # confirm / dry-run gate
    # ------------------------------------------------------------------

    def _confirm_or_dry_run(self, description: str) -> str | None:
        """Gate a destructive action.

        Returns None when the action is allowed (caller should proceed).
        Returns a non-empty string when the action is blocked (dry-run or
        denied); the caller should return this string immediately.
        """
        if self._dry_run:
            return f"[dry-run] would {description}"

        allowed = self._confirm_callback(description)
        if not allowed:
            return f"[blocked] destructive action denied by confirm callback: {description}"

        return None  # proceed

    # ------------------------------------------------------------------
    # SAFE actions
    # ------------------------------------------------------------------

    def _act_read_file(self, params: dict[str, Any]) -> str:
        """Read a file from disk, size-capped at _READ_FILE_MAX_BYTES.

        Safe action: read-only, no mutation.

        params keys:
          path (str, required): Absolute or relative path to read.
        """
        raw_path = params.get("path", "")
        if not raw_path:
            return "Error: read_file requires 'path' parameter"

        path = Path(raw_path).expanduser().resolve()

        # Basic sensitive-path hint (non-exhaustive; belt-and-suspenders).
        sensitive_prefixes = ("/etc/shadow", "/etc/passwd", "/proc/", "/sys/")
        for prefix in sensitive_prefixes:
            if str(path).startswith(prefix):
                logger.warning(
                    "read_file: refusing to read potentially sensitive path (logged path only): %s",
                    path,
                )
                return (
                    f"Error: read_file refuses to read potentially sensitive path "
                    f"'{path}'. Request a different path."
                )

        if not path.exists():
            return f"Error: path does not exist: {path}"
        if not path.is_file():
            return f"Error: path is not a file: {path}"

        try:
            raw = path.read_bytes()
        except PermissionError:
            return f"Error: permission denied reading: {path}"
        except OSError as exc:
            return f"Error reading file: {exc}"

        truncated = False
        if len(raw) > _READ_FILE_MAX_BYTES:
            raw = raw[:_READ_FILE_MAX_BYTES]
            truncated = True

        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            return f"Error decoding file as UTF-8: {exc}"

        # Never log file content.
        logger.debug("read_file: read %d bytes from path (content not logged)", len(raw))

        if truncated:
            return (
                f"{text}\n\n[truncated: file exceeded {_READ_FILE_MAX_BYTES} bytes; "
                f"only the first {_READ_FILE_MAX_BYTES} bytes are shown]"
            )
        return text

    def _act_find_element(self, params: dict[str, Any]) -> str:
        """Find a UI element by AT-SPI role and name.

        Safe action: read-only accessibility query.

        params keys:
          role (str, required): AT-SPI role name (e.g. 'push button').
          name (str, required): Accessible name (substring match).
        """
        role = params.get("role", "")
        name = params.get("name", "")
        if not role:
            return "Error: find_element requires 'role' parameter"
        if not name:
            return "Error: find_element requires 'name' parameter"

        result = _atspi_find_element(role, name)
        if isinstance(result, str):
            # Error string from the helper.
            return result

        # result is an Atspi.Accessible; return a description.
        try:
            actual_name = result.get_name() or "(unnamed)"
            actual_role = result.get_role_name() or "(unknown role)"
        except Exception as exc:
            return f"Element found but could not describe it: {exc}"

        return f"Found element: role='{actual_role}' name='{actual_name}'"

    def _act_open_app(self, params: dict[str, Any]) -> str:
        """Launch a desktop application.

        Safe action: spawns a new process; does not destroy existing state.
        Uses subprocess with shell=False and an explicit argv list.

        params keys:
          app (str, required): Application executable name or command.
                               Must be a single binary name (no shell metacharacters).
                               Example: 'gedit', 'gnome-terminal'.
        """
        app = str(params.get("app", "")).strip()
        if not app:
            return "Error: open_app requires 'app' parameter"

        # Reject anything that looks like shell composition, path traversal, or an
        # absolute/relative path.  Slashes are forbidden so a model cannot supply
        # '/usr/bin/rm' (or '../evil') and bypass the "plain name only" intent;
        # shutil.which resolves the full path after this check.
        forbidden_chars = set(";&|`$(){}[]<>\\/")
        if any(c in app for c in forbidden_chars):
            return (
                "Error: open_app 'app' value contains shell metacharacters or a path "
                "separator. Provide a plain application name only (e.g. 'gedit')."
            )

        binary = shutil.which(app)
        if binary is None:
            return f"Error: application '{app}' not found on PATH"

        try:
            subprocess.Popen(  # noqa: S603 (explicit argv, no shell)
                [binary],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return f"Error launching '{app}': {exc}"

        logger.info("open_app: launched '%s'", app)
        return f"Launched application: '{app}'"

    def _act_click(self, params: dict[str, Any]) -> str:
        """Perform a synthetic mouse click.

        Safe action: clicks a specific target; does not irreversibly destroy data.

        Targeting priority:
        1. AT-SPI semantic (role + name) — preferred on Wayland/GNOME.
        2. Absolute screen coordinates (x, y) — fallback.

        params keys (at least one targeting method required):
          role (str, optional): AT-SPI role for semantic targeting.
          name (str, optional): AT-SPI name for semantic targeting.
          x    (int, optional): Screen X coordinate (fallback).
          y    (int, optional): Screen Y coordinate (fallback).
          button (int, optional): Mouse button (1=left, 2=middle, 3=right). Default 1.
        """
        role = str(params.get("role", "")).strip()
        name = str(params.get("name", "")).strip()
        x = params.get("x")
        y = params.get("y")
        button = int(params.get("button", 1))

        # Semantic targeting first.
        click_x: int | None = None
        click_y: int | None = None

        if role and name:
            element = _atspi_find_element(role, name)
            if isinstance(element, str):
                # Error or not found — fall through to coordinates if provided.
                logger.debug("AT-SPI click: %s", element)
                if x is None or y is None:
                    return element  # No coordinate fallback available.
            else:
                # Got an element — extract coordinates from its bounding box.
                try:
                    coords = element.get_position(0)  # 0 = ATSPI_COORD_TYPE_SCREEN
                    size = element.get_size()
                    click_x = int(coords.x + size.width // 2)
                    click_y = int(coords.y + size.height // 2)
                except Exception as exc:
                    logger.debug("AT-SPI bounding box failed: %s", exc)
                    if x is None or y is None:
                        return f"AT-SPI element found but coordinates unavailable: {exc}"

        if click_x is None or click_y is None:
            # Fall back to explicit coordinates.
            if x is None or y is None:
                return "Error: click requires either (role+name) or (x, y) parameters"
            click_x = int(x)
            click_y = int(y)

        server = detect_display_server()
        tool = _choose_input_tool()
        if tool is None:
            return (
                f"Error: no input tool available for display server '{server.value}'. "
                f"Install ydotool (Wayland) or xdotool (X11) on the host."
            )

        if tool == "ydotool":
            argv = ["ydotool", "click", "--button", str(button), "--", str(click_x), str(click_y)]
        else:
            argv = ["xdotool", "mousemove", str(click_x), str(click_y), "click", str(button)]

        ok, msg = _run_input_tool(argv)
        if ok:
            return f"Clicked at ({click_x}, {click_y}) with button {button}"
        return f"Error: click failed: {msg}"

    def _act_type(self, params: dict[str, Any]) -> str:
        """Type text into the focused UI element using the input tool.

        Safe action: sends keystrokes; does not irreversibly destroy data on its own.

        params keys:
          text (str, required): Text to type.
        """
        text = params.get("text", "")
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return "Error: type requires non-empty 'text' parameter"

        server = detect_display_server()
        tool = _choose_input_tool()
        if tool is None:
            return (
                f"Error: no input tool available for display server '{server.value}'. "
                f"Install ydotool (Wayland) or xdotool (X11) on the host."
            )

        if tool == "ydotool":
            argv = ["ydotool", "type", "--", text]
        else:
            argv = ["xdotool", "type", "--", text]

        ok, msg = _run_input_tool(argv)
        if ok:
            # Never log the actual text content (may contain sensitive input).
            logger.debug("type: sent %d chars via %s", len(text), tool)
            return f"Typed {len(text)} characters successfully"
        return f"Error: type failed: {msg}"

    # ------------------------------------------------------------------
    # DESTRUCTIVE actions (gated by confirm_or_dry_run)
    # ------------------------------------------------------------------

    def _act_run_command(self, params: dict[str, Any]) -> str:
        """Execute an arbitrary subprocess command.

        DESTRUCTIVE: can alter any system state. Gated by confirm_or_dry_run().

        Security:
        - argv must be a list of strings — no shell=True, no shell string interpolation.
        - If the model supplies a bare string (not a list), the action is rejected.
          This prevents prompt-injection from smuggling shell metacharacters.
        - Command and its arguments are logged at DEBUG level ONLY as the first
          element (the binary name), never the full argv, to avoid leaking arguments
          that may contain secrets.

        params keys:
          argv   (list[str], required): Command and arguments as a list.
          timeout (int, optional): Timeout in seconds. Default 30.

        Example: {'argv': ['ls', '-la', '/tmp'], 'timeout': 10}
        """
        argv = params.get("argv")
        if not argv:
            return "Error: run_command requires 'argv' parameter (list of strings)"
        if isinstance(argv, str):
            return (
                "Error: run_command 'argv' must be a list, not a string. "
                "Pass ['command', 'arg1', 'arg2', ...] to prevent shell injection."
            )
        if not isinstance(argv, (list, tuple)):
            return "Error: run_command 'argv' must be a list"

        argv = [str(a) for a in argv]
        if not argv or not argv[0]:
            return "Error: run_command 'argv' list must not be empty"

        timeout_secs = int(params.get("timeout", 30))

        # Build the human-readable description for the confirm gate.
        description = f"run_command argv={argv!r} timeout={timeout_secs}s"

        blocked = self._confirm_or_dry_run(description)
        if blocked is not None:
            return blocked

        # Confirmed — proceed.
        binary = argv[0]
        binary_path = shutil.which(binary)
        if binary_path is None:
            return f"Error: command not found on PATH: '{binary}'"

        logger.debug("run_command: executing '%s' (remaining args not logged)", binary)

        try:
            result = subprocess.run(
                argv,  # explicit list — no shell=True
                capture_output=True,
                text=True,
                timeout=timeout_secs,
            )
        except FileNotFoundError:
            return f"Error: binary not found: '{binary}'"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout_secs}s: '{binary}'"
        except OSError as exc:
            return f"Error: OS error running '{binary}': {exc}"

        # Log metadata only — never log stdout/stderr content.
        logger.info(
            "run_command: '%s' exited with code %d, stdout_len=%d stderr_len=%d",
            binary,
            result.returncode,
            len(result.stdout),
            len(result.stderr),
        )

        output_parts: list[str] = []
        if result.stdout:
            stdout_preview = result.stdout[:_CMD_RESULT_LOG_CHARS]
            if len(result.stdout) > _CMD_RESULT_LOG_CHARS:
                stdout_preview += f"\n[... {len(result.stdout) - _CMD_RESULT_LOG_CHARS} more chars truncated]"
            output_parts.append(f"stdout:\n{stdout_preview}")
        if result.stderr:
            stderr_preview = result.stderr[:_CMD_RESULT_LOG_CHARS]
            if len(result.stderr) > _CMD_RESULT_LOG_CHARS:
                stderr_preview += f"\n[... {len(result.stderr) - _CMD_RESULT_LOG_CHARS} more chars truncated]"
            output_parts.append(f"stderr:\n{stderr_preview}")

        suffix = "\n\n".join(output_parts) if output_parts else "(no output)"
        return f"Command '{binary}' exited with code {result.returncode}.\n{suffix}"
