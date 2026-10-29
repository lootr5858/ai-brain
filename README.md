# AI Brain Engine

AI Brain Engine is the public mechanism for a local AI brain. It connects two user-owned stores to AI agents:

- `agents/` contains reusable agent behavior, skills, and plugins.
- `user/` contains the user's personal records.
- `engine/` supplies configuration, bootstrap adapters, Model Context Protocol (MCP) access, a dashboard, skill fan-out, and optional backup.

Each store's `README.md` is its only contract with the engine. The agents README frontmatter lists, as globs, what every session and subagent loads; the engine's `brain-context` hook injects that set, so moving or adding store files never changes runtime wiring.

The engine reproduces this mechanism. It does not reproduce anyone else's instructions, identity, history, credentials, or data.

## Contents

1. [What it is](#1-what-it-is)
2. [What is public and what stays private](#2-what-is-public-and-what-stays-private)
3. [Five-minute setup](#3-five-minute-setup)
4. [Directory model](#4-directory-model)
5. [Configuration](#5-configuration)
6. [Create your agent store](#6-create-your-agent-store)
7. [Create your user store](#7-create-your-user-store)
8. [Connect an agent through MCP](#8-connect-an-agent-through-mcp)
9. [Brain Dashboard](#9-brain-dashboard-optional)
10. [Remote access](#10-remote-access-optional)
11. [Backup](#11-backup-optional)
12. [Verify the installation](#12-verify-the-installation)
13. [Troubleshooting](#13-troubleshooting)
14. [Public-repository safety](#14-public-repository-safety)

## 1. What it is

The engine keeps mechanism separate from content:

```text
agent request
    ↓
brain-context hook (session or subagent start)
    ├── always: agents README + its manifest globs
    ├── always (session): user README
    └── on purpose: files those READMEs route to
```

Local MCP uses a read-only Standard Input/Output (STDIO) subprocess. Optional remote MCP exposes the same tools through OAuth-protected Streamable HTTP. The dashboard provides a local interface for browsing, search, permitted user-file editing, service status, remote-client management, and backup controls.

Python code and command `--help` output are authoritative for engine behavior. This README is the human setup and operating guide.

## 2. What is public and what stays private

| Item | Recommended location | Publish? |
| --- | --- | --- |
| Engine source and generic templates | `engine/` | Yes |
| Local path configuration | `brain.config.toml` | No |
| Agent behavior, skills, and plugins | `agents/` | Separate decision |
| Personal records | `user/` | No by default |
| OAuth state, caches, and service state | Standard user state directories | No |
| Passwords, access tokens, and client secrets | External credential storage | Never |

Keep local stores beside the engine, not inside its Git history. The included store templates contain only generic starting instructions.

## 3. Five-minute setup

Requirements: Git, Python 3.11 or newer, and `uv`.

The suggested location is `$HOME/brain`. Replace it with any writable directory or directory name. Brain store paths come from `brain.config.toml`; optional client state uses command flags or client defaults.

Replace `REPOSITORY_URL` with this repository's clone URL:

```bash
mkdir -p "$HOME/brain"
cd "$HOME/brain"
git clone https://github.com/lootr5858/ai-brain.git engine
cp engine/brain.config.toml.example brain.config.toml
cp -R engine/templates/agents agents
cp -R engine/templates/user user
cd engine
uv sync --locked
.venv/bin/python install/adapters.py --config ../brain.config.toml --apply
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml verify-stdio
```

Success ends with four `VERIFY ...: PASS` lines. The adapter command creates `BOOTSTRAP.md` in the brain directory and installs the entry instructions in `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md` plus `brain-context` hooks in `~/.codex/hooks.json` and `~/.claude/settings.json`, so the brain is available from any project.

Adapters read the configured `paths.agents/README.md` directly and include only `paths.user/README.md` when that file exists. `--agents-root` overrides the agent store; `--user-root` selects one user store and requires its `README.md`; `--no-user` omits user guidance. No user directories are scanned. `--brain` defaults these stores to its `agents/` and `user/` children unless overridden. Paths expand `$HOME`, `${HOME}`, and `~`. Both runtimes get the same entry block naming the two READMEs; the hooks do the loading. Hook commands are fixed — `bin/brain-context session --chunk N` and `bin/brain-context subagent --chunk N --json SubagentStart` — and each chunk stays under Claude Code's hook-output cap, which replaces about 10k characters or more with a short preview. Codex asks you to trust the hooks once; later store changes keep the same commands. Existing brain-root bootstraps are preserved; new ones use the two-store template. See [Claude memory](https://code.claude.com/docs/en/memory) and [Codex instructions](https://developers.openai.com/codex/guides/agents-md/).

The installer preserves existing global instructions and adds or updates its own block between `brain-engine:bootstrap` comment markers. In hook files it replaces only groups whose command runs `bin/brain-context` and keeps everything else. It defaults to dry-run; use `--apply` to write. Client destinations honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR`; `--codex-home` and `--claude-home` override them. A nonempty Codex `AGENTS.override.md` takes precedence over `AGENTS.md`, so the installer reports it for you to reconcile.

This setup loads brain guidance for every project. Keep the always-loaded core small and ensure the agent can read the brain directory. Existing brain-root `AGENTS.md` and `CLAUDE.md` from older installs are left untouched; remove redundant entries after reviewing any personal edits. Restart the agent after installing.

## 4. Directory model

The setup above creates:

```text
brain/
├── brain.config.toml       local paths and optional settings
├── BOOTSTRAP.md            generated shared-memory router
├── engine/                 public mechanism repository
├── agents/                 agent instructions, skills, and plugins
└── user/                   personal records

~/.codex/AGENTS.md          global entry block (Codex)
~/.claude/CLAUDE.md         global entry block (Claude Code)
~/.codex/hooks.json         brain-context hooks (Codex)
~/.claude/settings.json     brain-context hooks (Claude Code)
```

| Engine path | Purpose |
| --- | --- |
| `bin/brain-context` | Prints the always-loaded set from the store manifests, whole or per hook chunk |
| `templates/` | Generic root, agent, and user starting files |
| `install/` | Adapters, skill/plugin installation, MCP, and dashboard commands |
| `lib/` | Shared engine behavior |
| `servers/brain-mcp/` | Local STDIO and optional remote HTTP entrypoints |
| `dashboard/` | Local dashboard server and interface |
| `backup/brain/user/` | Optional encrypted backup runner and systemd units |
| `tests/` | Local checks and aggregate smoke runner |

The parent directory is only a container. Do not initialize it as one Git repository unless you intentionally want all stores in the same history.

## 5. Configuration

`brain.config.toml` may live anywhere. Relative paths resolve from that file's directory. Select it with `--config FILE` or the `BRAIN_CONFIG` environment variable.

The included example defines the complete basic configuration:

```toml
[paths]
brain = "."
engine = "engine"
agents = "agents"
user = "user"

[engine.fanout]
mode = "symlink"
state = ".brain-install/state.json"

[engine.fanout.targets]
claude-code = "~/.claude/skills"
codex-dir = "~/.codex/skills"

[engine.fanout.plugins.claude-code]
runtime = "claude-code"
exclude = ["mattpocock"]

[engine.fanout.plugins.codex-dir]
runtime = "codex"
marketplace_name = "brain-local"
marketplace = "~/.local/share/brain/codex-marketplace"
executable = "codex"

[engine.ops.dashboard]
host = "127.0.0.1"
port = 8765
```

| Key | Meaning |
| --- | --- |
| `paths.brain` | Brain container and generated `BOOTSTRAP.md` location |
| `paths.engine` | Engine checkout |
| `paths.agents` | Agent store |
| `paths.user` | User store |
| `engine.fanout.mode` | `symlink` or `copy` for standalone skills and Claude plugins |
| `engine.fanout.state` | Installer ownership and recovery record; default: `<brain>/.brain-install/state.json` |
| `engine.fanout.targets.*` | Named standalone skill destinations; Claude plugins use the corresponding destination |
| `engine.fanout.plugins.<target>.runtime` | `claude-code` or `codex`; target must also exist in `targets` |
| `engine.fanout.plugins.<target>.exclude` | Optional list of source folder names under `agents/plugins` to skip for this target; default: `[]` |
| `engine.fanout.plugins.<target>.marketplace_name` | Unique name for an installer-owned Codex marketplace |
| `engine.fanout.plugins.<target>.marketplace` | Dedicated local Codex marketplace directory |
| `engine.fanout.plugins.<target>.executable` | Codex executable name or path; default: `codex` |
| `engine.ops.dashboard.host` | Dashboard bind host; only loopback names are accepted |
| `engine.ops.dashboard.port` | Dashboard port |
| `engine.ops.dashboard.public_origin` | Optional external HTTPS origin |
| `engine.ops.dashboard.embedding_url` | Optional loopback embedding endpoint |
| `engine.ops.dashboard.embedding_model` | Model sent to that endpoint |

Configured paths expand environment variables and `~`; relative paths resolve from the configuration file's directory. Re-run the adapter installer after changing `paths.brain` or moving the setup.

## 6. Create your agent store

The starter store is deliberately small:

```text
agents/
├── README.md
└── core/
    └── behavior-rules.md
```

`README.md` is the entry point and the manifest. Its frontmatter lists glob patterns per context set:

```yaml
---
session:
  - core/*.md
subagent:
  - skills/<name>/SKILL.md
---
```

A new file under a listed pattern loads with no other change; renaming or moving a file means editing one manifest line. The body routes everything else. Keep the session set small: `bin/brain-context session` prints exactly what agents receive, and a note appears in the output when a pattern matches nothing or the set exceeds its chunks.

Add other material in any layout the README routes, for example `skills/<name>/SKILL.md` for reusable procedures and `plugins/<directory>/` for complete plugins with manifests, bundled skills, and hooks.

### Skill and plugin installation — optional

The installer discovers packages directly from the configured agent store. There is no `skills.toml` requirement; an old index is ignored.

```text
agents/
├── skills/
│   └── focus/
│       └── SKILL.md
└── plugins/
    └── scanline/
        ├── .claude-plugin/plugin.json
        ├── .codex-plugin/plugin.json
        ├── skills/
        │   └── scanline/SKILL.md
        └── hooks/hooks.json
```

Only immediate skill directories with `SKILL.md` are standalone skills, including symlinks to complete skill directories. Plugin roots belong under `plugins/`; their manifest names identify them even when the directory names differ. Each application uses its own manifest. A missing manifest for a selected application produces `SKIP`; malformed manifests, duplicate plugin names, missing resources, and unmanaged destination collisions produce refusals. Missing or empty source directories are allowed. Plugin-contained skills are not automatically discovered as standalone skills, but explicit links under `agents/skills` are.

Use a target's plugin `exclude` list to skip source folders before their manifests are read. Entries are folder names, not manifest names or paths. For example, `exclude = ["mattpocock"]` skips `agents/plugins/mattpocock` as a Claude plugin while its individual links under `agents/skills` remain installable. Other plugins and targets are unaffected. Exclusion prevents future installation; it does not remove an existing installation. `--uninstall` still uses the ownership record to remove Brain-managed plugins even after they are excluded.

Standalone skill validation accepts `disable-model-invocation` and `argument-hint` alongside the existing metadata fields. Installation preserves skill metadata and supporting files. The existing content scanner remains in place. The scanner skips `.git` directories and worktree pointer files; all other skill files retain the existing content and binary-file checks. Plugins use structural validation of manifests, skill metadata, hook declarations, explicit bundled command-resource paths, and internal symlinks; legitimate binary assets are allowed. This does not execute or audit the behavior of plugin scripts. Repository metadata such as `.git` is excluded from plugin snapshots and content fingerprints. External or broken plugin symlinks are refused so snapshots remain self-contained.

#### Application behavior

| Content | Claude Code | Codex |
| --- | --- | --- |
| Standalone skills | Link/copy to the configured destination, normally `~/.claude/skills` | Link/copy to the configured destination, `~/.codex/skills` in the supplied config |
| Complete plugins | Link/copy the whole plugin into the Claude skill destination; requires skills-directory plugin support | Copy a managed snapshot into the dedicated Brain marketplace, register it, then install through the native CLI |
| Source edits | Linked content remains live; reload plugin components when needed. Rerun for copied content | Rerun the installer: changed content triggers native removal and reinstallation to refresh the cache |

Claude's [skills-directory plugin documentation](https://code.claude.com/docs/en/plugins-reference#skills-directory-plugins) describes in-place discovery. Use a Claude Code version that supports it (the locally checked 2.1.269 help advertises this feature). Restart or use `/reload-plugins` after changing hooks or other plugin components. Actual discovery of linked plugins should be checked in your application after installation.

Codex requires an executable supporting the documented [plugin and marketplace commands](https://learn.chatgpt.com/docs/developer-commands#codex-plugin). Its native settings and cache location follow the CLI environment, including `CODEX_HOME`; this is separate from the standalone skill destination. Codex plugins are cached installations, not live links. The installer detects changes with content fingerprints and never edits source plugin manifests or versions. An unchanged, disabled plugin stays disabled; refreshing changed content reinstalls it. Native authentication/setup, if requested, must be completed in the application.

Hooks stay inside their plugin; the installer does not merge them into global hook settings or execute them. The preview lists hook events. Codex requires [review and trust of plugin hook definitions](https://learn.chatgpt.com/docs/hooks#plugin-bundled-hooks). Start a new task after installing or refreshing a Codex plugin and complete that review before expecting hooks to run.

Claude skills normally appear as `/skill-name`; plugin skills use `/plugin-name:skill-name`, unless hidden by their configuration. Codex skills use its skill selector and `$` mentions (`/skills` in CLI/IDE). Hooks are automatic event handlers, not individual slash commands. See [Claude skills](https://code.claude.com/docs/en/skills) and [Codex skills](https://learn.chatgpt.com/docs/build-skills).

#### Preview and apply manually

Run these from the engine directory, after reviewing your `brain.config.toml`:

```bash
.venv/bin/python install/skills.py --config ../brain.config.toml --dry-run
.venv/bin/python install/skills.py --config ../brain.config.toml --apply
```

No flags means preview. `--dry-run` overrides `--apply`. Preview performs no writes and launches no native CLI, so native marketplace/installed-plugin ownership checks are deferred until `--apply`, before any placements. Installation stops if those checks fail. Structural refusals block the whole plan.

Select just one kind or application:

```bash
.venv/bin/python install/skills.py --config ../brain.config.toml --kind skills --target codex-dir --dry-run
.venv/bin/python install/skills.py --config ../brain.config.toml --kind plugins --target claude-code --dry-run
.venv/bin/python install/skills.py --config ../brain.config.toml --kind plugins --target codex-dir --dry-run
```

Replace `--dry-run` with `--apply` only after reviewing the output. `--kind` accepts `skills`, `plugins`, or `all` (default); `--target` is repeatable and refers to your configured names. Old configurations without plugin runtime tables still install standalone skills. Names such as `agents-dir` remain usable when configured; update commands to match your chosen names.

When standalone skills are applied to `claude-code`, the installer preserves unrelated Claude settings and sets `skillListingBudgetFraction` to `0.025`. Its settings destination defaults to `~/.claude/settings.json`; use `--settings FILE` for another location. Plugin-only installation does not make this settings change. For temporary standalone skill destinations, `--root DIR --kind skills` also redirects state and default settings under that directory. `--root` cannot redirect a native plugin install and is rejected for `plugins` or `all`.

The existing scanner remains available separately:

```bash
.venv/bin/python install/skills.py --scan ../agents/skills/focus
```

#### Removal and recovery

```bash
.venv/bin/python install/skills.py --config ../brain.config.toml --kind plugins --uninstall --dry-run
.venv/bin/python install/skills.py --config ../brain.config.toml --kind plugins --uninstall --apply
```

Omit `--kind plugins` to remove both kinds, or add `--target` to narrow removal. Uninstall uses the ownership record, so it can remove installed packages whose sources have since been deleted. It also recognizes legacy standalone links pointing to current Brain sources and legacy marked copies. Source files, unrelated installations, and unrelated settings remain untouched; uninstall does not reset the Claude listing budget.

Keep `.brain-install/state.json` while installations exist. Do not edit managed copies, snapshots, or the generated marketplace catalog: changes there are refused instead of overwritten. Edit sources under `agents/`. Uninstall before changing an installed package's source or destination; then update configuration and install again. Changing a target path does not migrate older links automatically.

Native failures can leave earlier operations completed. The installer stops, reports failure, and records pending native operations. Inspect the output and ownership record, rerun preview, resolve the reported cause, and retry manually. Uninstall removes only tracked native plugins; the dedicated marketplace is removed only when no other installed plugins remain. Unknown files are preserved. Actual plugin activation and hook trust remain application-controlled.

#### Verification

The normal checks exercise helpers against temporary fixtures and mocked native JSON responses. They do not run `skills.py`, an agent CLI, or any hook:

```bash
.venv/bin/python tests/test_fanout.py
.venv/bin/python tests/test_plugins.py
.venv/bin/python tests/test_config.py
```

`tests/smoke-skill-fanout.sh` includes helper tests and repository hygiene checks. A separate, optional test launches the CLI only against temporary standalone skill fixtures; it is not part of the smoke suite:

```bash
.venv/bin/python tests/test_fanout_cli.py --run-cli-tests
```

After your real manual installation, verify that the intended skills appear, invoke one bundled skill, inspect the application's hook configuration/trust state, and trigger only the hook events you intend to test. Mocked tests cannot establish application discovery, authentication, or hook execution on your machine.

## 7. Create your user store

The starter store is a flat set of topic records behind one router:

```text
user/
├── README.md
├── profile.md
├── family.md
├── career.md
└── gear.yaml
```

- `README.md` is the entry point: read and update rules plus a table linking each record. It always loads; records load only when relevant.
- Keep one file per topic and register each new file in the `README.md` table.
- Add a topic record only when you have content for it.
- Do not load the entire store; agents read only the records a task needs.

Personal context belongs here, not in `engine/`. Keep credentials and service secrets out of both stores.

## 8. Connect an agent through MCP

Local MCP is optional but is the simplest portable connection. It starts one tokenless, read-only subprocess and writes no MCP state.

Print the exact connection and verify it:

```bash
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml stdio-command
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml verify-stdio
```

Copy the returned `command`, `args`, and empty `env` into the local client's STDIO MCP configuration as separate fields. Regenerate them after moving the engine or configuration file.

The server exposes health and status, the always-loaded set (`get_core`, from the same manifest as the hooks), skill and user-record reads, search, and classified file listing/reading. Run `stdio-command` again instead of manually constructing paths.

## 9. Brain Dashboard (optional)

The dashboard binds only to `127.0.0.1` or `localhost`. It provides content browsing, lexical search, optional semantic search, permitted user-file editing with undo, repository/service status, remote MCP client controls, and backup controls.

Editing requires the `user/` store to be a Git repository with a configured Git author. From the brain directory, initialize its history before starting the dashboard:

```bash
git -C user init
git -C user add .
git -C user commit -m "Initial user brain"
```

Configure `user.name` and `user.email` with Git before the commit if they are not already set.

Linux with systemd can install it as a user service:

```bash
.venv/bin/python install/dashboard.py --config ../brain.config.toml install-service
systemctl --user daemon-reload
systemctl --user enable --now brain-dashboard.service
.venv/bin/python install/dashboard.py --config ../brain.config.toml verify-local
```

The installer writes the unit but does not enable or start it. Reinstall the unit after moving the engine, then reload and restart it.

For semantic search, add both `embedding_url` and `embedding_model` under `[engine.ops.dashboard]`. The endpoint must use HTTP on loopback. Without both values, search remains lexical.

Public dashboard access requires a separately managed authenticated HTTPS proxy to the loopback service. The engine does not configure that boundary.

## 10. Remote access (optional)

Remote MCP requires Linux with systemd, a stable public HTTPS URL, and a separately managed reverse proxy to `127.0.0.1:8787`. The HTTP server remains on loopback and protects MCP with OAuth.

Initialize the URL, register a client, and install the service:

```bash
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml remote init
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml remote add-client
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml remote install-service
systemctl --user daemon-reload
systemctl --user enable --now brain-mcp-remote.service
```

Follow the interactive prompts exactly. A client secret is displayed once; save it outside the repository. Use `remote list-clients` and `remote remove-client` for later administration.

The service installer does not manage the proxy, enable the unit, or start it. Remote authorization state is stored outside the repository under the standard user state directory.

### Remove remote access

First revoke every registered client:

```bash
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml remote remove-client
```

Select `all` and enter the confirmation phrase shown by the command. Then remove the user service:

```bash
systemctl --user disable --now brain-mcp-remote.service
rm -- "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/brain-mcp-remote.service"
systemctl --user daemon-reload
```

Remove the separately managed HTTPS proxy route yourself. The engine does not own it.

To permanently discard the saved remote URL, clients, credentials, and OAuth state, delete the database after stopping the service:

```bash
rm -- "${XDG_STATE_HOME:-$HOME/.local/state}/brain-engine/brain-mcp/remote.sqlite3"
```

This last step is irreversible.

## 11. Backup (optional)

Backup requires Linux with systemd, `restic`, and an encrypted restic repository. It backs up only `BRAIN_ROOT/user` and `BRAIN_ROOT/brain.config.toml`; it does not follow a custom `paths.user` or back up the public engine checkout.

Install the units and private environment file from the engine root:

```bash
install -Dm0644 backup/brain/user/brain-user-backup.service "$HOME/.config/systemd/user/brain-user-backup.service"
install -Dm0644 backup/brain/user/brain-user-backup.timer "$HOME/.config/systemd/user/brain-user-backup.timer"
install -Dm0600 backup/brain/user/brain-user-backup.env.example "$HOME/.config/brain-os/brain-user-backup.env"
${EDITOR:-vi} "$HOME/.config/brain-os/brain-user-backup.env"
systemctl --user daemon-reload
systemctl --user enable --now brain-user-backup.timer
systemctl --user start brain-user-backup.service
```

| Environment variable | Value |
| --- | --- |
| `BRAIN_ROOT` | Absolute brain-container path |
| `BRAIN_ENGINE` | Absolute engine-checkout path |
| `RESTIC_REPOSITORY` | Restic repository specification |
| `RESTIC_PASSWORD_FILE` | Password-file path outside Git and the brain container |
| `BRAIN_USER_BACKUP_ACK` | Literal `encrypted-user-data` acknowledgement |

Successful runs keep 7 daily, 4 weekly, and 6 monthly snapshots, prune, run `restic check`, and update dashboard health markers. Restore is staged and verified before a separate confirmed replacement action.

## 12. Verify the installation

Run all repository checks from the engine root:

```bash
bash tests/smoke.sh
```

This verifies configuration, adapters, fan-out, operations, dashboard behavior, backup behavior, local MCP, remote protocol behavior, and public-file hygiene with synthetic data and temporary repositories. Protocol tests may open loopback sockets. Service tests use fakes and do not change systemd. The suite does not contact live MCP clients, public proxies, embedding providers, or backup repositories.

After configuring an optional service, also use its verification command or inspect it with `systemctl --user status UNIT`.

## 13. Troubleshooting

| Symptom | Action |
| --- | --- |
| `uv sync --locked` fails | Run `uv --verbose sync --locked` and fix the reported Python, lockfile, cache, or network error. |
| A configured path is missing | Check `brain.config.toml`; relative paths start at that file's directory. |
| MCP cannot import dependencies | Run `uv sync --locked`, then use the generated `.venv/bin/python` command. |
| MCP works directly but not in a client | Replace the client's command and arguments with fresh `stdio-command` output, then restart the client. |
| A service fails after moving files | Reinstall its unit, run `systemctl --user daemon-reload`, and restart it. |
| Dashboard rejects its bind address | Use `127.0.0.1` or `localhost`; place authentication and HTTPS in a separate proxy. |
| Fan-out reports `REFUSE` | Resolve the unsafe skill or unmanaged destination; the installer will not overwrite it. |
| Remote OAuth fails | Check the saved public URL, client callback, service status, and proxy forwarding before replacing credentials. |
| Backup fails | Check the private environment file, restic repository access, password file, and service journal. |

## 14. Public-repository safety

Publish only the engine repository. Before publishing, inspect exactly what Git will include:

```bash
git status --short
git ls-files
bash tests/smoke.sh
```

Do not add `brain.config.toml`, local stores, environment files, credentials, OAuth state, databases, caches, generated service files, or backup data. Keep examples generic. Review staged changes before every public commit.
