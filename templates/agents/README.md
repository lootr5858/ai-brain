---
session:
  - core/*.md
subagent: []
---

# Agent store

This file's frontmatter is the manifest (stripped from injected copies). Each session gets this README, the `session` files, and the user store's README; each subagent gets the `subagent` files. The engine's `brain-context` hook injects them. If they are not in context, read them. Patterns are globs relative to this file.

Load skills and other files only when the current task matches them. Do not bulk-load the agent store.
