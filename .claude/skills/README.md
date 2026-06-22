# Skills in this project

Personal skills in `~/.claude/skills/` load into the **main thread** automatically. Two project notes:

## 1. Confirm what you actually have
Before any agent relies on a skill, run:
```bash
ls ~/.claude/skills/
```
`halo` references `frontend-design` (ships with Claude Code) and *optionally* `stitch`, `21st-dev`/`@21st-dev/magic`, and an `impeccable`-style audit skill. If those aren't present, either install them or have `halo` proceed without them — don't assume.

## 2. Vendor them for a self-contained repo (optional)
```bash
# adjust folder names to match `ls ~/.claude/skills/`
cp -r ~/.claude/skills/frontend-design .claude/skills/frontend-design
 cp -r ~/.claude/skills/stitch          .claude/skills/stitch
 cp -r ~/.claude/skills/21st-dev         .claude/skills/21st-dev
 cp -r ~/.claude/skills/impeccable       .claude/skills/impeccable
git add .claude/skills && git commit -m "chore: vendor project skills"
```

## 3. Subagents don't inherit skills automatically
Each agent that needs a skill is told to use it in its system prompt and has `Read`/`Bash` to load it. Skill name must equal its folder name; on collision, personal overrides project.
