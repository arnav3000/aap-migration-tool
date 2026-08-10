---
name: pr-new
description: Create and submit pull requests. Use when the user wants to open a PR with a clear summary and test plan.
---

# PR New

## Purpose

Push the current branch and open a GitHub PR with a structured summary and test plan.

## When to use

- User asks to create/open/submit a pull request
- Work is complete and ready for review

## Workflow

1. Inspect branch state:

   ```bash
   git status
   git diff
   git log main..HEAD --oneline
   git diff main...HEAD
   ```

2. Push if needed: `git push -u origin HEAD`
3. Create PR with `gh pr create`:

   ```bash
   gh pr create --title "..." --body "$(cat <<'EOF'
   ## Summary
   - ...

   ## Test plan
   - [ ] make check
   EOF
   )"
   ```

## Rules

- Summarize **all** commits on the branch, not only the latest.
- Do not update git config or force-push `main`.
- Return the PR URL when done.

## References

- [AGENTS.md](../../../AGENTS.md)
- [Makefile](../../../Makefile) — verification targets for test plan
