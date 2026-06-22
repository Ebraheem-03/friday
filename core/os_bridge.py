"""Linux OS bridge: AT-SPI semantic targeting + ydotool/xdotool input. Display-server auto-detect."""

# TODO(vector): detect X11 vs Wayland; AT-SPI find-by-role/name; input via ydotool/xdotool.
# SECURITY: every destructive action (delete/kill/overwrite/send) goes through confirm_or_dry_run().
