# AI Brain bootstrap

Start here when an agent enters the brain directory.

## Always Load

1. Read `agents/BOOTSTRAP.md` and follow its always-load instructions.
2. Read `user/BOOTSTRAP.md` and follow its always-load instructions.

## Triggered Loading

- Use `agents/` for shared agent rules, skills, and references.
- Use `user/` for user preferences and user context.
- Use `engine/BOOTSTRAP.md` only for brain mechanisms: install, servers, dashboard, backup, deploy, and verification tooling.

Load other context only when an explicit route, skill description, rule, or user request matches the task.

Do not bulk-load all repositories. Do not treat this container as a git repository.
