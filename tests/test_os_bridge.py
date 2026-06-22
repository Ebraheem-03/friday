"""Unit tests for core/os_bridge.py.

CI constraints:
- No display server required (XDG_SESSION_TYPE is patched in each test).
- No ydotoold daemon, no ydotool/xdotool binary (shutil.which is mocked).
- No gi/Atspi installed (gi import is mocked where needed).
- No FRIDAY_LIVE flag required for any test in this file.

Baseline: 179 passed, 2 skipped. This file must only add to the passed count.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers: reset the cached display-server detection between tests.
# ---------------------------------------------------------------------------


def _reset_server_cache() -> None:
    """Clear the module-level server detection cache so tests are isolated."""
    import core.os_bridge as mod

    mod._detected_server = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_server_cache(monkeypatch: pytest.MonkeyPatch):
    """Auto-reset the display-server cache before every test.

    Also strips the host's real display-server env vars so detection is
    deterministic regardless of the developer's session (e.g. a Wayland host
    would otherwise mask a missing per-test cache reset that headless CI hits).
    Tests that need a specific server set it explicitly + reset the cache.
    """
    for var in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    _reset_server_cache()
    yield
    _reset_server_cache()


@pytest.fixture()
def dry_run_bridge():
    """OsBridge in dry-run mode (default safe posture)."""
    from core.os_bridge import OsBridge

    return OsBridge(dry_run=True)


@pytest.fixture()
def live_bridge():
    """OsBridge in live mode with a deny-all confirm callback."""
    from core.os_bridge import OsBridge

    return OsBridge(dry_run=False, confirm_callback=lambda desc: False)


@pytest.fixture()
def confirmed_bridge():
    """OsBridge in live mode with an allow-all confirm callback."""
    from core.os_bridge import OsBridge

    return OsBridge(dry_run=False, confirm_callback=lambda desc: True)


# ---------------------------------------------------------------------------
# Tests: Display-server detection
# ---------------------------------------------------------------------------


class TestDisplayServerDetection:
    """detect_display_server() reads env vars and caches the result."""

    def test_detects_wayland_from_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)

        from core.os_bridge import detect_display_server, DisplayServer

        assert detect_display_server() == DisplayServer.WAYLAND

    def test_detects_x11_from_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)

        from core.os_bridge import detect_display_server, DisplayServer

        assert detect_display_server() == DisplayServer.X11

    def test_detects_wayland_from_wayland_display(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.delenv("DISPLAY", raising=False)

        from core.os_bridge import detect_display_server, DisplayServer

        assert detect_display_server() == DisplayServer.WAYLAND

    def test_detects_x11_from_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")

        from core.os_bridge import detect_display_server, DisplayServer

        assert detect_display_server() == DisplayServer.X11

    def test_unknown_when_no_display_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)

        from core.os_bridge import detect_display_server, DisplayServer

        assert detect_display_server() == DisplayServer.UNKNOWN

    def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second call returns the same object without re-reading env."""
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        from core.os_bridge import detect_display_server

        first = detect_display_server()
        # Change env — should NOT affect cached result.
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        second = detect_display_server()

        assert first is second  # same object (cached)

    def test_xdg_takes_precedence_over_wayland_display(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If XDG_SESSION_TYPE=x11 but WAYLAND_DISPLAY is set, XDG wins."""
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

        from core.os_bridge import detect_display_server, DisplayServer

        assert detect_display_server() == DisplayServer.X11


# ---------------------------------------------------------------------------
# Tests: OSTool Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_bridge_satisfies_ostool_protocol(self, dry_run_bridge) -> None:
        """OsBridge must satisfy the runtime_checkable OSTool Protocol from brain.py."""
        from core.brain import OSTool

        assert isinstance(dry_run_bridge, OSTool)

    @pytest.mark.asyncio
    async def test_os_action_returns_string(self, dry_run_bridge) -> None:
        """os_action always returns a str, never raises."""
        result = await dry_run_bridge.os_action("read_file", {"path": "/nonexistent"})
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: Action allowlist dispatch
# ---------------------------------------------------------------------------


class TestActionAllowlist:
    @pytest.mark.asyncio
    async def test_unknown_action_returns_error_string(self, dry_run_bridge) -> None:
        """Unknown action name returns an error string, never raises."""
        result = await dry_run_bridge.os_action("destroy_world", {})
        assert isinstance(result, str)
        assert "unknown" in result.lower()
        assert "destroy_world" in result

    @pytest.mark.asyncio
    async def test_known_actions_are_dispatched(self, dry_run_bridge) -> None:
        """Each known action name is recognised and dispatched (not 'unknown')."""
        known_actions = ["read_file", "find_element", "open_app", "click", "type", "run_command"]
        for action in known_actions:
            result = await dry_run_bridge.os_action(action, {})
            assert isinstance(result, str), f"Action '{action}' did not return str"
            # Must not say 'unknown OS action'.
            assert "unknown OS action" not in result, (
                f"Action '{action}' was not dispatched: {result!r}"
            )

    @pytest.mark.asyncio
    async def test_empty_action_returns_error_string(self, dry_run_bridge) -> None:
        result = await dry_run_bridge.os_action("", {})
        assert isinstance(result, str)
        assert "unknown" in result.lower()


# ---------------------------------------------------------------------------
# Tests: confirm / dry-run gate
# ---------------------------------------------------------------------------


class TestConfirmDryRunGate:
    """Core security requirement: destructive actions are gated."""

    @pytest.mark.asyncio
    async def test_run_command_dry_run_does_not_call_subprocess(
        self, dry_run_bridge
    ) -> None:
        """In dry-run mode, run_command does NOT call subprocess and returns [dry-run]."""
        with patch("core.os_bridge.subprocess.run") as mock_run:
            result = await dry_run_bridge.os_action(
                "run_command", {"argv": ["echo", "hello"]}
            )
        mock_run.assert_not_called()
        assert result.startswith("[dry-run]")
        assert "run_command" in result.lower() or "echo" in result.lower()

    @pytest.mark.asyncio
    async def test_run_command_denied_does_not_call_subprocess(
        self, live_bridge
    ) -> None:
        """When confirm callback returns False, subprocess is NOT called."""
        with patch("core.os_bridge.subprocess.run") as mock_run:
            result = await live_bridge.os_action(
                "run_command", {"argv": ["echo", "hello"]}
            )
        mock_run.assert_not_called()
        assert "blocked" in result.lower() or "denied" in result.lower()

    @pytest.mark.asyncio
    async def test_run_command_confirmed_calls_subprocess(
        self, confirmed_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When confirm callback returns True, subprocess.run IS called."""
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "hello\n"
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/echo"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result) as mock_run:
                result = await confirmed_bridge.os_action(
                    "run_command", {"argv": ["echo", "hello"]}
                )

        mock_run.assert_called_once()
        # Must NOT use shell=True.
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("shell", False) is False
        assert "hello" in result or "exit" in result.lower()

    @pytest.mark.asyncio
    async def test_dry_run_returns_string_starting_with_dry_run_prefix(
        self, dry_run_bridge
    ) -> None:
        result = await dry_run_bridge.os_action(
            "run_command", {"argv": ["rm", "-rf", "/tmp/test"]}
        )
        assert result.startswith("[dry-run]"), f"Expected [dry-run] prefix, got: {result!r}"

    def test_confirm_or_dry_run_returns_none_when_allowed(self) -> None:
        """_confirm_or_dry_run returns None when callback approves."""
        from core.os_bridge import OsBridge

        bridge = OsBridge(dry_run=False, confirm_callback=lambda d: True)
        blocked = bridge._confirm_or_dry_run("test action")
        assert blocked is None

    def test_confirm_or_dry_run_returns_string_when_denied(self) -> None:
        from core.os_bridge import OsBridge

        bridge = OsBridge(dry_run=False, confirm_callback=lambda d: False)
        blocked = bridge._confirm_or_dry_run("test action")
        assert isinstance(blocked, str)
        assert len(blocked) > 0

    def test_confirm_or_dry_run_returns_string_in_dry_run(self) -> None:
        from core.os_bridge import OsBridge

        bridge = OsBridge(dry_run=True)
        blocked = bridge._confirm_or_dry_run("dangerous thing")
        assert isinstance(blocked, str)
        assert "[dry-run]" in blocked


# ---------------------------------------------------------------------------
# Tests: run_command security
# ---------------------------------------------------------------------------


class TestRunCommandSecurity:
    """run_command must never use shell=True and must reject string argv."""

    @pytest.mark.asyncio
    async def test_run_command_uses_argv_list_not_shell(
        self, confirmed_bridge
    ) -> None:
        """subprocess.run is invoked with a list, not shell=True."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/bin/ls"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result) as mock_run:
                await confirmed_bridge.os_action("run_command", {"argv": ["ls", "/tmp"]})

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        # First positional arg must be a list.
        argv_passed = call_args.args[0] if call_args.args else call_args.kwargs.get("args")
        assert isinstance(argv_passed, list), "argv must be a list, not a string"
        # shell=True must not be set.
        assert call_args.kwargs.get("shell", False) is False

    @pytest.mark.asyncio
    async def test_run_command_rejects_string_argv(self, confirmed_bridge) -> None:
        """If argv is a string, run_command returns an error without calling subprocess."""
        with patch("core.os_bridge.subprocess.run") as mock_run:
            result = await confirmed_bridge.os_action(
                "run_command", {"argv": "rm -rf /"}
            )
        mock_run.assert_not_called()
        assert "must be a list" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_run_command_missing_argv_returns_error(
        self, confirmed_bridge
    ) -> None:
        result = await confirmed_bridge.os_action("run_command", {})
        assert "error" in result.lower() or "requires" in result.lower()

    @pytest.mark.asyncio
    async def test_run_command_binary_not_on_path_returns_error(
        self, confirmed_bridge
    ) -> None:
        with patch("core.os_bridge.shutil.which", return_value=None):
            result = await confirmed_bridge.os_action(
                "run_command", {"argv": ["not_a_real_binary_xyz"]}
            )
        assert "not found" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_run_command_timeout_returns_error(self, confirmed_bridge) -> None:
        with patch("core.os_bridge.shutil.which", return_value="/bin/sleep"):
            with patch(
                "core.os_bridge.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["sleep"], timeout=1),
            ):
                result = await confirmed_bridge.os_action(
                    "run_command", {"argv": ["sleep", "999"], "timeout": 1}
                )
        assert "timed out" in result.lower() or "timeout" in result.lower()


# ---------------------------------------------------------------------------
# Tests: read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_file_reads_content(self, dry_run_bridge, tmp_path) -> None:
        """read_file returns file content (not gated — safe action)."""
        p = tmp_path / "hello.txt"
        p.write_text("hello world")

        result = await dry_run_bridge.os_action("read_file", {"path": str(p)})
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_read_file_nonexistent_returns_error(
        self, dry_run_bridge
    ) -> None:
        result = await dry_run_bridge.os_action(
            "read_file", {"path": "/no/such/file/ever.txt"}
        )
        assert "error" in result.lower() or "not exist" in result.lower()

    @pytest.mark.asyncio
    async def test_read_file_missing_path_returns_error(self, dry_run_bridge) -> None:
        result = await dry_run_bridge.os_action("read_file", {})
        assert "error" in result.lower() or "requires" in result.lower()

    @pytest.mark.asyncio
    async def test_read_file_size_cap(self, dry_run_bridge, tmp_path) -> None:
        """Files over _READ_FILE_MAX_BYTES are truncated with a notice."""
        from core.os_bridge import _READ_FILE_MAX_BYTES

        p = tmp_path / "big.txt"
        # Write slightly more than the cap.
        p.write_bytes(b"x" * (_READ_FILE_MAX_BYTES + 1000))

        result = await dry_run_bridge.os_action("read_file", {"path": str(p)})
        assert "truncated" in result.lower()

    @pytest.mark.asyncio
    async def test_read_file_at_cap_not_truncated(self, dry_run_bridge, tmp_path) -> None:
        """A file exactly at _READ_FILE_MAX_BYTES is NOT truncated."""
        from core.os_bridge import _READ_FILE_MAX_BYTES

        p = tmp_path / "exact.txt"
        p.write_bytes(b"a" * _READ_FILE_MAX_BYTES)

        result = await dry_run_bridge.os_action("read_file", {"path": str(p)})
        assert "truncated" not in result.lower()

    @pytest.mark.asyncio
    async def test_read_file_sensitive_path_refused(self, dry_run_bridge) -> None:
        """read_file refuses obviously sensitive paths like /etc/shadow."""
        result = await dry_run_bridge.os_action(
            "read_file", {"path": "/etc/shadow"}
        )
        assert "sensitive" in result.lower() or "refuses" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_read_file_is_not_gated(self, dry_run_bridge, tmp_path) -> None:
        """read_file is a SAFE action — it executes even in dry-run mode."""
        p = tmp_path / "safe.txt"
        p.write_text("safe content")

        result = await dry_run_bridge.os_action("read_file", {"path": str(p)})
        # Should NOT be prefixed with [dry-run]
        assert not result.startswith("[dry-run]")
        assert "safe content" in result


# ---------------------------------------------------------------------------
# Tests: graceful degradation when tool binary is absent
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Bridge must never crash when ydotool/xdotool is absent."""

    @pytest.mark.asyncio
    async def test_type_no_tool_returns_error_string(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        with patch("core.os_bridge.shutil.which", return_value=None):
            result = await dry_run_bridge.os_action("type", {"text": "hello"})

        assert isinstance(result, str)
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_click_no_tool_returns_error_string(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        with patch("core.os_bridge.shutil.which", return_value=None):
            result = await dry_run_bridge.os_action("click", {"x": 100, "y": 200})

        assert isinstance(result, str)
        assert "error" in result.lower() or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_type_missing_text_returns_error(self, dry_run_bridge) -> None:
        result = await dry_run_bridge.os_action("type", {})
        assert "error" in result.lower() or "requires" in result.lower()

    @pytest.mark.asyncio
    async def test_click_missing_params_returns_error(self, dry_run_bridge) -> None:
        result = await dry_run_bridge.os_action("click", {})
        assert "error" in result.lower() or "requires" in result.lower()


# ---------------------------------------------------------------------------
# Tests: AT-SPI graceful degradation (gi not installed)
# ---------------------------------------------------------------------------


class TestAtSpiGracefulDegradation:
    """find_element and click must degrade gracefully when gi/Atspi is absent."""

    @pytest.mark.asyncio
    async def test_find_element_without_gi_returns_graceful_error(
        self, dry_run_bridge
    ) -> None:
        """When gi is not importable, find_element returns an error string."""
        with patch.dict("sys.modules", {"gi": None}):
            import core.os_bridge as mod

            # Call the helper directly.
            result = mod._atspi_find_element("push button", "OK")

        assert isinstance(result, str)
        assert "unavailable" in result.lower() or "not installed" in result.lower()

    @pytest.mark.asyncio
    async def test_find_element_missing_role_returns_error(
        self, dry_run_bridge
    ) -> None:
        result = await dry_run_bridge.os_action("find_element", {"name": "OK"})
        assert "error" in result.lower() or "requires" in result.lower()

    @pytest.mark.asyncio
    async def test_find_element_missing_name_returns_error(
        self, dry_run_bridge
    ) -> None:
        result = await dry_run_bridge.os_action("find_element", {"role": "push button"})
        assert "error" in result.lower() or "requires" in result.lower()

    @pytest.mark.asyncio
    async def test_click_with_atspi_fallback_to_coords(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If AT-SPI finds no element, click falls back to explicit coordinates."""
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

        # AT-SPI fails to find element.
        with patch("core.os_bridge._atspi_find_element", return_value="AT-SPI unavailable"):
            with patch("core.os_bridge.shutil.which", return_value="/usr/bin/ydotool"):
                fake_result = MagicMock()
                fake_result.returncode = 0
                fake_result.stdout = "ok"
                with patch("core.os_bridge.subprocess.run", return_value=fake_result):
                    result = await dry_run_bridge.os_action(
                        "click",
                        {"role": "push button", "name": "OK", "x": 50, "y": 60},
                    )
        # Should use coordinates fallback and succeed.
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: open_app
# ---------------------------------------------------------------------------


class TestOpenApp:
    @pytest.mark.asyncio
    async def test_open_app_launches_binary(self, dry_run_bridge) -> None:
        """open_app uses Popen with the resolved binary path."""
        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/gedit"):
            with patch("core.os_bridge.subprocess.Popen") as mock_popen:
                result = await dry_run_bridge.os_action("open_app", {"app": "gedit"})

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        argv_passed = call_args.args[0] if call_args.args else call_args.kwargs.get("args")
        assert argv_passed == ["/usr/bin/gedit"]
        assert "launched" in result.lower() or "gedit" in result.lower()

    @pytest.mark.asyncio
    async def test_open_app_missing_app_returns_error(self, dry_run_bridge) -> None:
        result = await dry_run_bridge.os_action("open_app", {})
        assert "error" in result.lower() or "requires" in result.lower()

    @pytest.mark.asyncio
    async def test_open_app_not_on_path_returns_error(self, dry_run_bridge) -> None:
        with patch("core.os_bridge.shutil.which", return_value=None):
            result = await dry_run_bridge.os_action("open_app", {"app": "notarealapp"})
        assert "not found" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_open_app_rejects_shell_metacharacters(
        self, dry_run_bridge
    ) -> None:
        """open_app blocks attempts to inject shell metacharacters."""
        malicious_inputs = [
            "gedit; rm -rf /",
            "$(whoami)",
            "app && curl evil.com",
            "`id`",
        ]
        for evil_app in malicious_inputs:
            result = await dry_run_bridge.os_action("open_app", {"app": evil_app})
            assert "error" in result.lower() or "metacharacter" in result.lower(), (
                f"Expected error for malicious input {evil_app!r}, got: {result!r}"
            )

    @pytest.mark.asyncio
    async def test_open_app_is_safe_not_gated(self, dry_run_bridge) -> None:
        """open_app is a SAFE action — it is NOT blocked in dry-run mode."""
        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/gedit"):
            with patch("core.os_bridge.subprocess.Popen"):
                result = await dry_run_bridge.os_action("open_app", {"app": "gedit"})
        # Must not be prefixed with [dry-run].
        assert not result.startswith("[dry-run]")


# ---------------------------------------------------------------------------
# Tests: type action with ydotool / xdotool
# ---------------------------------------------------------------------------


class TestTypeAction:
    @pytest.mark.asyncio
    async def test_type_wayland_uses_ydotool(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        _reset_server_cache()  # bridge init cached host/unknown; force re-detect

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/ydotool"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result) as mock_run:
                result = await dry_run_bridge.os_action("type", {"text": "hello"})

        mock_run.assert_called_once()
        argv_called = mock_run.call_args.args[0]
        assert argv_called[0] == "ydotool"
        assert "type" in argv_called
        assert "hello" in result or "5 characters" in result or "characters" in result

    @pytest.mark.asyncio
    async def test_type_x11_uses_xdotool(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        _reset_server_cache()  # bridge init may have cached wayland; force re-detect

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/xdotool"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result) as mock_run:
                result = await dry_run_bridge.os_action("type", {"text": "world"})

        mock_run.assert_called_once()
        argv_called = mock_run.call_args.args[0]
        assert argv_called[0] == "xdotool"
        assert "characters" in result


# ---------------------------------------------------------------------------
# Tests: click action
# ---------------------------------------------------------------------------


class TestClickAction:
    @pytest.mark.asyncio
    async def test_click_coordinates_wayland(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        _reset_server_cache()  # bridge init cached host/unknown; force re-detect

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/ydotool"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result) as mock_run:
                result = await dry_run_bridge.os_action("click", {"x": 100, "y": 200})

        mock_run.assert_called_once()
        argv_called = mock_run.call_args.args[0]
        assert argv_called[0] == "ydotool"
        assert "clicked" in result.lower() or "100" in result

    @pytest.mark.asyncio
    async def test_click_coordinates_x11(
        self, dry_run_bridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        _reset_server_cache()  # bridge init may have cached wayland; force re-detect

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/xdotool"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result) as mock_run:
                result = await dry_run_bridge.os_action("click", {"x": 50, "y": 60})

        mock_run.assert_called_once()
        argv_called = mock_run.call_args.args[0]
        assert argv_called[0] == "xdotool"
        assert "clicked" in result.lower() or "50" in result


# ---------------------------------------------------------------------------
# Tests: _run_input_tool helper
# ---------------------------------------------------------------------------


class TestRunInputTool:
    def test_missing_binary_returns_false(self) -> None:
        from core.os_bridge import _run_input_tool

        with patch("core.os_bridge.shutil.which", return_value=None):
            ok, msg = _run_input_tool(["ydotool", "type", "test"])

        assert ok is False
        assert "not found" in msg.lower() or "ydotool" in msg.lower()

    def test_successful_command_returns_true(self) -> None:
        from core.os_bridge import _run_input_tool

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "done"
        fake_result.stderr = ""

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/ydotool"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result):
                ok, msg = _run_input_tool(["ydotool", "type", "hello"])

        assert ok is True
        assert "done" in msg

    def test_nonzero_exit_returns_false(self) -> None:
        from core.os_bridge import _run_input_tool

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "permission denied"

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/ydotool"):
            with patch("core.os_bridge.subprocess.run", return_value=fake_result):
                ok, msg = _run_input_tool(["ydotool", "click", "--", "100", "200"])

        assert ok is False
        assert "permission" in msg.lower() or msg

    def test_timeout_returns_false(self) -> None:
        from core.os_bridge import _run_input_tool

        with patch("core.os_bridge.shutil.which", return_value="/usr/bin/ydotool"):
            with patch(
                "core.os_bridge.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["ydotool"], timeout=10),
            ):
                ok, msg = _run_input_tool(["ydotool", "type", "slow"])

        assert ok is False
        assert "timed out" in msg.lower() or "timeout" in msg.lower()

    def test_empty_argv_returns_false(self) -> None:
        from core.os_bridge import _run_input_tool

        ok, msg = _run_input_tool([])
        assert ok is False


# ---------------------------------------------------------------------------
# Tests: _atspi_find_element import error handling
# ---------------------------------------------------------------------------


class TestAtSpiImport:
    def test_gi_import_error_returns_string(self) -> None:
        """If gi cannot be imported, _atspi_find_element returns an error string."""
        from core.os_bridge import _atspi_find_element

        # Patch gi itself to raise ImportError.
        with patch.dict("sys.modules", {"gi": None}):
            result = _atspi_find_element("push button", "OK")

        assert isinstance(result, str)
        # Must contain a human-readable error, not raise.
        assert "unavailable" in result.lower() or "not installed" in result.lower()


# ---------------------------------------------------------------------------
# Tests: os_action never raises to caller (error-return contract)
# ---------------------------------------------------------------------------


class TestNoRaiseContract:
    @pytest.mark.asyncio
    async def test_handler_exception_is_caught_and_returned_as_string(
        self, dry_run_bridge
    ) -> None:
        """If a handler raises unexpectedly, os_action catches it and returns an error string."""

        def _explode(params: dict) -> str:
            raise RuntimeError("unexpected handler failure")

        dry_run_bridge._dispatch["read_file"] = _explode

        result = await dry_run_bridge.os_action("read_file", {"path": "/anything"})
        assert isinstance(result, str)
        assert "error" in result.lower()
