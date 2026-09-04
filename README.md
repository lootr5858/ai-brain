# AI Brain Engine

AI Brain Engine is the public mechanism for a local AI brain. It connects two user-owned stores to AI agents:

- `agents/` contains reusable agent behavior, rules, skills, and references.
- `user/` contains the user's preferences and selectively loaded context.
- `engine/` supplies configuration, bootstrap adapters, Model Context Protocol (MCP) access, a dashboard, skill fan-out, and optional backup.

An agent starts at the generated brain-root bootstrap. It loads the small required core from both stores, then follows explicit routes to additional context only when needed.

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
brain-root bootstrap
    ├── always: agent bootstrap + agent core
    ├── always: user bootstrap + preferences + context routing
    └── when routed: other rules, skills, references, and user context
```

Local MCP uses a read-only Standard Input/Output (STDIO) subprocess. Optional remote MCP exposes the same tools through OAuth-protected Streamable HTTP. The dashboard provides a local interface for browsing, search, permitted user-file editing, service status, remote-client management, and backup controls.

Python code and command `--help` output are authoritative for engine behavior. This README is the human setup and operating guide.

## 2. What is public and what stays private

| Item | Recommended location | Publish? |
| --- | --- | --- |
| Engine source and generic templates | `engine/` | Yes |
| Local path configuration | `brain.config.toml` | No |
| Agent rules, skills, and references | `agents/` | Separate decision |
| Preferences and personal context | `user/` | No by default |
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
git clone REPOSITORY_URL engine
cp engine/brain.config.toml.example brain.config.toml
cp -R engine/templates/agents agents
cp -R engine/templates/user user
cd engine
uv sync --locked
.venv/bin/python install/adapters.py --config ../brain.config.toml --apply
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml verify-stdio
```

Success ends with four `VERIFY ...: PASS` lines. The adapter command creates `BOOTSTRAP.md`, `AGENTS.md`, and `CLAUDE.md` in the parent brain directory.

## 4. Directory model

The setup above creates:

```text
brain/
├── brain.config.toml       local paths and optional settings
├── BOOTSTRAP.md            generated shared-memory router
├── AGENTS.md               generated Codex-style adapter
├── CLAUDE.md               generated Claude Code adapter
├── engine/                 public mechanism repository
├── agents/                 agent instructions and optional skills
└── user/                   preferences and user context
```

| Engine path | Purpose |
| --- | --- |
| `templates/` | Generic root, agent, and user starting files |
| `install/` | Adapters, skill fan-out, MCP, and dashboard commands |
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

[engine.fanout.targets]
claude-code = "~/.claude/skills"
agents-dir = "~/.agents/skills"

[engine.ops.dashboard]
host = "127.0.0.1"
port = 8765
```

| Key | Meaning |
| --- | --- |
| `paths.brain` | Brain container and generated root-adapter location |
| `paths.engine` | Engine checkout |
| `paths.agents` | Agent store |
| `paths.user` | User store |
| `engine.fanout.mode` | `symlink` or `copy` |
| `engine.fanout.targets.*` | Named skill destinations |
| `engine.ops.dashboard.host` | Dashboard bind host; only loopback names are accepted |
| `engine.ops.dashboard.port` | Dashboard port |
| `engine.ops.dashboard.public_origin` | Optional external HTTPS origin |
| `engine.ops.dashboard.embedding_url` | Optional loopback embedding endpoint |
| `engine.ops.dashboard.embedding_model` | Model sent to that endpoint |

Re-run the adapter installer after changing `paths.brain` or moving the setup.

## 6. Create your agent store

The starter store is deliberately small:

```text
agents/
├── BOOTSTRAP.md
└── core/
    └── behavior-rules.md
```

`BOOTSTRAP.md` defines what always loads and how additional content is routed. Markdown files under `core/` always load. Keep that directory small.

Add task-specific material only when needed:

- `rules/` for domain rules routed by the agent bootstrap.
- `skills/<name>/SKILL.md` for reusable task procedures.
- `reference/` for explicitly requested lookup material.

### Skill fan-out — optional

Fan-out requires one skill directory per entry in `agents/skills/skills.toml`. It defaults to dry-run and refuses unsafe skills or unmanaged destination collisions.

After adding skills, review and apply one sequence:

```bash
.venv/bin/python install/skills.py --config ../brain.config.toml --dry-run
.venv/bin/python install/skills.py --config ../brain.config.toml --apply
```

Run `install/skills.py --help` for target filtering, scanning, and uninstall options. Select copy mode in `brain.config.toml`.

## 7. Create your user store

The starter store contains only always-required routing data:

```text
user/
├── BOOTSTRAP.md
├── preferences.md
└── context-routing.md
```

- Put durable communication and workflow choices in `preferences.md`.
- Map clear task triggers to exact files in `context-routing.md`.
- Add profile, inventory, journal, knowledge, project, or other directories only when you have content for them.
- Do not route the entire store. If no route matches, the bootstrap tells the agent to ask instead of guessing.

Personal context belongs here, not in `engine/`. Keep credentials and service secrets out of both stores.

## 8. Connect an agent through MCP

Local MCP is optional but is the simplest portable connection. It starts one tokenless, read-only subprocess and writes no MCP state.

Print the exact connection and verify it:

```bash
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml stdio-command
.venv/bin/python install/brain_mcp.py --config ../brain.config.toml verify-stdio
```

Copy the returned `command`, `args`, and empty `env` into the local client's STDIO MCP configuration as separate fields. Regenerate them after moving the engine or configuration file.

The server exposes health and status, minimum core loading, routed discipline/skill/user/reference reads, search, and classified file listing/reading. Run `stdio-command` again instead of manually constructing paths.

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
