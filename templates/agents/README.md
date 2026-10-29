# Agent store

Always load every Markdown file in `core/`, relative to this file, unless already in context. Keep one import line per core file:

@core/behavior-rules.md

Load rules, skills, and references only when the current task matches their routing. Do not bulk-load the agent store.
