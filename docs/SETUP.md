# FRIDAY — Host setup (Linux, run once)

Do these before kicking off Claude Code. Items marked **[YOU]** need your input/secrets.

## 1. Toolchain
```bash
# Python + uv
curl -LsSf https://astral.sh/uv/install.sh | sh
# Node (for Playwright/context7/Electron MCPs and HUD)
# install via your distro or nvm; need Node 18+
# Build toolchain for codebase-memory (CGO/tree-sitter)
sudo apt update && sudo apt install -y build-essential   # Debian/Ubuntu
```

## 2. Build-time MCP servers
```bash
# codebase-memory (native Linux binary, NOT uvx). Auto-detects & configures Claude Code:
curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash
# verify:
codebase-memory-mcp --version || echo "add the install dir to PATH, or use an absolute path in .mcp.json"
```
Playwright and context7 need no install — they run via `npx` on first use (already in `.mcp.json`).

## 3. OS-control prerequisites (for the `vector` agent's build-time MCP)
```bash
sudo apt install -y ydotool at-spi2-core
systemctl --user enable --now ydotoold
sudo usermod -aG input "$USER"   # then log out/in
npm install -g @agent-sh/computer-use-linux
computer-use-linux doctor        # confirm readiness on your DE
```
**[YOU]** Confirm your display server (X11 / Wayland) and desktop environment (GNOME/KDE/Hyprland/i3/COSMIC) — `vector` needs this for window targeting.

## 4. Secrets — copy and fill `.env`
```bash
cp friday/.env.example friday/.env
```
**[YOU]** Fill: `GEMINI_API_KEY` (required). Optional: `TWENTY_FIRST_API_KEY` (21st.dev), and a GCP project for Stitch.

## 5. Optional MCPs
Only enable these inside the phase that needs them — see `docs/mcp/OPTIONAL-SERVERS.md`.

## 6. Kick off
Open `friday/` in Claude Code and paste `docs/CLAUDE_CODE_KICKOFF.md`.
