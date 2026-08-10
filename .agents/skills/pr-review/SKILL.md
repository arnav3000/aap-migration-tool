---
name: pr-review
description: Handle PR review feedback. Use when addressing review comments, resolving threads, or fixing CI after review.
---

# PR Review

## Purpose

Respond to reviewer feedback: fix code, reply on threads, re-run checks, push updates.

## When to use

- User shares review comments or asks to address PR feedback
- CI failed after review-driven changes

## Workflow

1. Fetch comments: `gh pr view <number> --comments` or review UI threads.
2. Apply focused fixes — minimal diff per comment.
3. Verify locally: `make check` or `make test-unit` for narrow fixes.
4. Push and reply on resolved threads.
5. Re-check CI: `gh pr checks <number>`.

## Principles

- Fix the root cause, not symptoms (see AGENTS.md Design Thinking).
- Do not expand scope beyond what reviewers asked.
- One commit per logical fix unless user requests squash.

## References

- [AGENTS.md](../../../AGENTS.md)
- [Makefile](../../../Makefile)
