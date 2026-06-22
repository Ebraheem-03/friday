Paste this as your first message in Claude Code (run from the `friday/` root):

---
Read CLAUDE.md in full, then docs/plan/PLAN.md and every file in docs/decisions/.

Then execute PLAN.md strictly in order from Step 0. Rules:
1. After each step, summarise files created/modified/deleted, then say "Ready for Step N+1 — proceed?" and wait for my confirmation.
2. Never skip or merge steps.
3. Read any file before modifying it.
4. On a conflict between the plan and reality, describe it and propose two options — do not guess.
5. Delete nothing unless PLAN.md says to.
6. Respect Pro economics: index with codebase-memory and prefer its graph tools over grep for structural questions; keep optional MCP servers off until their phase; do not start Agent Teams / parallel agents without my explicit go.
7. Delegate isolated work to the specialist subagents (forge/echo/vector/scout/mnemo/halo/sentinel) with the smallest brief; you carry cross-agent context.

Start with Step 0: index the repo and give me a findings summary of the skeleton, then stop at the review gate.
---
