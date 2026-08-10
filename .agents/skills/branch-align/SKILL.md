---
name: branch-align
description: Align branch name after renaming. Use when a branch was renamed locally or on the remote and git refs need to match.
---

# Branch Align

## Purpose

Fix local/remote branch tracking after a branch rename so commits and PRs target the correct branch.

## When to use

- User renamed a branch and `git push` or PR base branch is wrong
- `git status` shows detached or stale upstream after rename
- CI or `gh pr` still references the old branch name

## Key commands

```bash
git branch -m <old-name> <new-name>
git push origin -u <new-name>
git push origin --delete <old-name>   # only when remote old branch should go away
gh pr edit <number> --head <new-name> # if PR head branch must be updated
```

## References

- [AGENTS.md](../../../AGENTS.md) — project invariants and agent roles
- [Makefile](../../../Makefile) — `make help` for local verification (`make check`)
