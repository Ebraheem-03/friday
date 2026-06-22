# FRIDAY — STATUS

_Single source of cross-agent truth. Atlas keeps this current._

## Now
- Step 1 complete (forge) — `pyproject.toml` pinned; CI workflow created. Pending sentinel provenance review, then atlas commits.

## Next
- Step 2: core config + state machine (forge).

## [REVIEW] — needs the human
- [ ] **OS:** confirm X11 vs Wayland + desktop environment (blocks `vector` window targeting).
- [ ] **Secrets:** `GEMINI_API_KEY` in `friday/.env` (blocks `forge`/`brain`).
- [ ] **codebase-memory:** run install script; confirm `codebase-memory-mcp` on PATH.
- [ ] **OS host setup:** `ydotoold` + `at-spi2-core` installed; `computer-use-linux doctor` green.
- [ ] **(optional) 21st.dev** API key; **(optional) Stitch** GCP project + billing.
- [ ] Create the git repo and empty `dev` + `main` branches.

## Log
- 2026-06-22 — atlas — Step 0 Orient: indexed repo (166 nodes/165 edges), read all ADRs; confirmed stubs consistent with ADRs. Noted git already initialised (dev/main exist) — Step 1 git-init sub-task is a no-op. Delegated Step 1 to forge.
- 2026-06-22 — forge — Step 1: pinned all 8 packages in pyproject.toml (==exact), added [build-system] + [tool.pytest.ini_options]; created .github/workflows/ci.yml (lint+pytest, PyGObject apt deps included). Versions resolved from PyPI JSON API — all confirmed stable.
- 2026-06-22 — sentinel — Step 1 security + correctness review. All 8 package pins verified live against PyPI JSON: canonical names, no typosquats, all pinned versions exist as stable releases. Two bugs found and fixed: (1) invalid build-backend `setuptools.backends.legacy:build` → corrected to `setuptools.build_meta`; (2) missing `tests/` dir would have caused pytest exit-5 (CI red) → added `tests/__init__.py` + `tests/test_smoke.py` placeholder. browser-use telemetry noted (MEDIUM, non-blocking). VERDICT: GO.
- _(append: date — agent — what changed)_
