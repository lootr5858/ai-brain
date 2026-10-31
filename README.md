# Brain engine

Serves your brain to AI agents: rules, skills, and personal knowledge, kept in two stores you own.

| Folder | Holds | Git |
|---|---|---|
| `engine/` | this code, no personal data | public repo |
| `agents/` | agent rules and skills | your repo |
| `user/` | personal knowledge | your repo, local is enough |

```text
LOCAL
Claude Code / Codex ── SessionStart · SubagentStart ──▶ brain hook ──▶ always-loaded context

REMOTE
claude.ai / ChatGPT ── HTTPS ──▶ Cloudflare Tunnel ──▶ brain serve (127.0.0.1:8787)
  /mcp · /token · /.well-known/*    public · /mcp needs an OAuth token
  /authorize · / (dashboard)        owner only · Cloudflare Access login
```

The code holds no store layout. It finds everything by rule:

| What | Found by |
|---|---|
| Always-loaded context | frontmatter of `agents/README.md` |
| Skills | any child of `agents/skills/` with a `SKILL.md` |
| Knowledge | every visible file in `user/`, routed by `user/README.md` |

## Layout

```text
brain/
├── engine/
│   ├── pyproject.toml    package `brain`: hook · serve · install
│   ├── brain/
│   │   ├── context.py    manifest → always-loaded text
│   │   ├── store.py      file access · 1 git commit per write
│   │   ├── mcp.py        MCP tools
│   │   ├── auth.py       OAuth · owner login · signed tokens
│   │   ├── app.py        one web app: /mcp + OAuth + dashboard
│   │   ├── install.py    entry blocks · hooks · systemd unit
│   │   └── web/          dashboard: API + static UI, no build step
│   └── tests/
├── agents/
└── user/
```

## Install a new brain

Needs Linux, Python 3.11+, git.

```bash
mkdir ~/brain && cd ~/brain
git clone https://github.com/lootr5858/ai-brain.git engine
# agents/ and user/: clone yours, or create them (see Stores)

python3 -m venv engine/.venv
engine/.venv/bin/pip install -e engine

engine/.venv/bin/brain install           # preview, writes nothing
engine/.venv/bin/brain install --apply

mkdir -p ~/.claude ~/.agents
ln -sT ~/brain/agents/skills ~/.claude/skills
ln -sT ~/brain/agents/skills ~/.agents/skills
```

Restart Claude Code and Codex. Codex asks you to trust the hooks once.

`brain install` writes:

| File | What | Rest of the file |
|---|---|---|
| `~/.claude/CLAUDE.md` · `~/.codex/AGENTS.md` | entry block between `brain-engine:bootstrap` markers | kept |
| `~/.claude/settings.json` · `~/.codex/hooks.json` | one `SessionStart` and one `SubagentStart` hook | kept |
| `~/.config/systemd/user/brain.service` | server unit, only with the web flags | replaced |

- Other locations: `--brain`, `--claude-home`, `--codex-home`, or `CLAUDE_CONFIG_DIR`, `CODEX_HOME`.
- Default `--brain`: the folder holding `engine/`.
- Moved the engine or its venv? Run `brain install --apply` again. Moving store files changes nothing.
- A nonempty `~/.codex/AGENTS.override.md` overrides `AGENTS.md`; the installer warns.
- Plugins: install them in each app. A plugin's skill reaches web agents when linked in `agents/skills/`.

## Stores

**`agents/`**

```text
agents/
├── README.md              frontmatter = manifest · body = how to use the store
├── core/*.md
└── skills/<name>/SKILL.md
```

```yaml
---
session:
  - core/*.md
subagent:
  - core/working-style.md
---
```

| Set | Loads | When |
|---|---|---|
| `session` | agents README · matching files · user README | session start |
| `subagent` | matching files | subagent start |

- A new file matching a glob loads with no other change.
- One hook output, 9,000 characters at most. Files that don't fit are named in a closing note, so the agent reads them directly.
- Preview: `engine/.venv/bin/brain hook session`

**`user/`**

Create `user/README.md`, then:

```bash
git init user
git -C user add -A
git -C user commit -m "Start"
```

- `README.md` always loads. It routes to the other files, which load only when needed.
- Writes need `user/` to be its own git repo, with a git `user.name` and `user.email`. Reads don't.
- Each write = 1 commit. Hand edits get their own commit before the next write.
- Text files only, 200 KB each. Hidden paths are refused.
- Connected web agents can read every file here. Keep secrets out.

## Run the server

Needs a domain on Cloudflare, Cloudflare Zero Trust, and `cloudflared`. Run commands from `~/brain`. Replace `brain.example.com`, `TEAM`, and `AUD`.

**1. Tunnel:** publish `brain.example.com` → `http://127.0.0.1:8787` ([guide](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)).

**2. Access apps:** both are needed.

| App | Covers | Policy | Why |
|---|---|---|---|
| Brain | whole host | Allow: your email | dashboard and `/authorize` |
| Brain public, 1 per path | `/mcp` · `/token` · `/.well-known/*` | [Bypass](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/): Everyone | web agents call these with no browser login; OAuth guards `/mcp` |

The engine checks the Access login too: everything outside the public paths is refused without it.

**3. Copy two values** from the Brain app ([where](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)):

- team domain: `TEAM.cloudflareaccess.com`
- AUD tag: Access controls → Applications → Configure → Additional settings

**4. Install and start:**

```bash
engine/.venv/bin/brain install --apply \
  --public-url https://brain.example.com \
  --cf-team TEAM.cloudflareaccess.com \
  --cf-aud AUD
systemctl --user daemon-reload
systemctl --user enable --now brain.service
loginctl enable-linger "$USER"           # keep running after logout
```

`--public-url` must match the tunnel hostname; `/mcp` answers 421 to any other host. Default port 8787: change with `--port`. To run it by hand: `brain serve` with the same flags.

**5. Check from outside:**

```bash
probe() { curl -s -o /dev/null -w '%{http_code}\n' "$@"; }
probe https://brain.example.com/.well-known/oauth-authorization-server    # 200
probe https://brain.example.com/.well-known/oauth-protected-resource/mcp  # 200
probe -X POST https://brain.example.com/mcp                               # 401
probe https://brain.example.com/                                          # 302 to the login
```

Got 302 on a `.well-known` line? Add that exact path to the Bypass apps.

## Connect web agents

| App | Where | Enter |
|---|---|---|
| [claude.ai](https://claude.com/docs/connectors/custom/remote-mcp) | Customize → Connectors → Add custom connector | `https://brain.example.com/mcp` · OAuth client: Claude's published identity |
| [ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt) | chatgpt.com/plugins → + → Add custom MCP server | `https://brain.example.com/mcp` · OAuth |

Then sign in through Cloudflare Access and press Allow. Only these two apps can connect. Each app's plan and workspace limits still apply.

| Tool | Does | Store |
|---|---|---|
| `get_core` | always-loaded set | agents, read |
| `list_skills` · `read_skill` | skill list · `SKILL.md` or one supporting file | agents, read |
| `list` · `read` · `search` | browse knowledge | user, read |
| `edit` | replace exact text that occurs once | user, write |
| `create` | new file in an existing folder | user, write |

No delete tool. Undo = git, or the dashboard's history.

**Revoke every connection:** delete `~/.local/state/brain/secret`, then restart the service. The next start makes a new one.

## Dashboard

`https://brain.example.com/`, served by the same app, behind the Brain Access app.

- Browse knowledge files and skills; Markdown and YAML render.
- Search both.
- Edit and create knowledge files with live preview. Skills are view-only.
- Per-file history, diff, and restore. A restore is a new commit.
- No delete, rename, or move.

Third-party libraries are vendored, so the page makes no outside requests: [`brain/web/static/vendor/README.md`](brain/web/static/vendor/README.md).

## Test

```bash
cd ~/brain/engine
.venv/bin/python -m unittest discover -s tests
```

Runs offline, on temp folders; one test starts a real server on a free loopback port. Real stores and agent settings stay untouched. Cloudflare Access and web-agent connections aren't covered: use step 5 and connect one.

## Update

```bash
cd ~/brain
git -C engine pull
engine/.venv/bin/pip install -e engine   # picks up new dependencies
systemctl --user restart brain.service
```

Store changes need no restart.

## Public repo

Never commit stores, secrets, or state here. Server state lives in `~/.local/state/brain/` (`XDG_STATE_HOME`), outside the repo. Licence: MIT.
