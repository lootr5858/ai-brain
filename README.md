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

Needs a domain on Cloudflare (active zone), a Cloudflare Zero Trust organization, and `cloudflared`. Run commands from `~/brain`.

| Placeholder | Replace with |
|---|---|
| `brain.example.com` | the hostname you publish the brain on |
| `you@example.com` | the email you log in with |
| `TEAM_NAME.cloudflareaccess.com` | your Zero Trust team domain (step 5) |
| `ZERO_TRUST_APP_AUD` | the AUD tag of the Brain app (step 5) |

The tunnel alone checks no identity: anyone with the URL reaches the server. Access does, and the engine needs it: the dashboard and `/authorize` refuse requests without an Access login. Create the Access apps before the tunnel route.

**1. Login method:** Zero Trust → Integrations → Identity providers. One-time PIN listed? Done. Else Add an identity provider → One-time PIN.

**2. Two policies:** Zero Trust → Access controls → Policies → Add a policy. Make each once; the apps reuse them.

| Policy name | Action | Include | Attach to |
|---|---|---|---|
| `owner` | Allow | Emails → `you@example.com` | the `brain` app only |
| `bypass` | [Bypass](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/) | Everyone | the 3 path apps only |

⚠️ Never attach `bypass` to the `brain` app: that makes the dashboard and `/authorize` public, so anyone could approve their own connection.

**3. Four Access apps.** For each row: Zero Trust → Access controls → Applications → Add an application → Self-hosted and private → Public DNS. Fill in the hostname, pick the policy under Add existing policy, set the name, then Create.

| Name | Subdomain + domain | Path | Policy |
|---|---|---|---|
| `brain` | `brain.example.com` | empty | `owner` |
| `brain-mcp` | `brain.example.com` | `mcp` | `bypass` |
| `brain-token` | `brain.example.com` | `token` | `bypass` |
| `brain-wellknown` | `brain.example.com` | `.well-known/*` | `bypass` |

- Each path gets its own app. Don't add paths to the `brain` app with Add public hostname: they would get its login.
- The Path box sits right of Domain; widen the window if you can't see it.
- `brain` app only: Authentication → One-time PIN only, Apply instant authentication on. Additional settings → App Launcher tile domain Default, or turn the tile off.
- Path apps: leave Authentication, session duration, and Additional settings as they are. Bypass ignores them.

Why the path apps: claude.ai and ChatGPT call these paths from their own servers, with no browser to type a PIN in. Access lets them through; the engine's OAuth guards `/mcp` instead, and a token only comes from `/authorize`, which stays behind your login.

**4. Tunnel:** Networking → Tunnels → Create a tunnel → install the connector it shows. Add a published application route: `brain.example.com` → `http://127.0.0.1:8787` ([guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)). Leave Protect with Access off: the engine checks the Access token itself, and the public paths carry none.

**5. Copy two values:**

| Flag | What | Where |
|---|---|---|
| `--cf-team` | team domain: Access login host, also serves the keys that sign login tokens | Zero Trust → Access controls → Access settings → App Launcher box; or the `Location` of `curl -sI https://brain.example.com/` |
| `--cf-aud` | Application Audience (AUD) tag of the Brain app: public ID, not a secret | Applications → Brain → Edit → Additional settings → AUD tag; also the `kid=` in that `Location` |

`--cf-team` takes no `https://`. Use the Brain app's AUD, not a public-path app's: Bypass requests carry no Access token. The engine accepts a login only when the token is signed by the team's keys, issued by `https://TEAM_NAME.cloudflareaccess.com`, and addressed to `ZERO_TRUST_APP_AUD`.

**6. Install and start:**

```bash
engine/.venv/bin/brain install --apply \
  --public-url https://brain.example.com \
  --cf-team TEAM_NAME.cloudflareaccess.com \
  --cf-aud ZERO_TRUST_APP_AUD
systemctl --user daemon-reload
systemctl --user enable --now brain.service
loginctl enable-linger "$USER"           # keep running after logout
```

`--public-url` takes `https://HOST` and must match the tunnel hostname: `brain install` and `brain serve` refuse anything else, and `/mcp` answers 421 to any other host. Default port 8787: change with `--port`. To run it by hand: `brain serve` with the same flags. Logs: `journalctl --user -u brain.service`.

**7. Check from outside:**

```bash
probe() { curl -s -o /dev/null -w '%{http_code}\n' "$@"; }
probe https://brain.example.com/.well-known/oauth-authorization-server    # 200
probe https://brain.example.com/.well-known/oauth-protected-resource/mcp  # 200
probe -X POST https://brain.example.com/mcp                               # 401
probe https://brain.example.com/                                          # 302 to the login
```

Changes take about a minute to reach Cloudflare. Probe again before changing anything.

| Got | Fix |
|---|---|
| 302 on a public path | that path's app is missing, mistyped, or not using `bypass`. The `kid=` in the redirect names the app that caught it: the `brain` app's AUD means no path app matched. Still 302 on a `.well-known` path? Add that exact path to the `brain-wellknown` app |
| 403 on `/` | Access isn't guarding it: the Brain app's Path isn't empty, or its policy isn't Allow |
| 502 | nothing on port 8787: `systemctl --user status brain.service`, then the logs |
| logged in, still refused | wrong `--cf-team` or `--cf-aud` |

**Logins:** only the dashboard and connecting an app go through Access, once per Brain app session duration. Connected web agents use the engine's OAuth: 1-hour access tokens and 30-day refresh tokens, renewed on each refresh.

## Connect web agents

| App | Where | Settings |
|---|---|---|
| [claude.ai](https://claude.com/docs/connectors/custom/remote-mcp) | Customize → Connectors → Add custom connector | URL `https://brain.example.com/mcp` · Authentication: Sign in now · OAuth client: Use Claude's published identity. Both show as Detected |
| [ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt) | chatgpt.com/plugins → + → Add custom MCP server | URL `https://brain.example.com/mcp` · Authentication: OAuth · Advanced OAuth settings → Registration method: Client Identifier Metadata Document (CIMD) · client ID and secret empty · token endpoint auth method: none · scope: `offline_access` |

A window opens: sign in with your email and the PIN Cloudflare sends, then press Allow on the brain's page. Only these two apps can connect. Each app's plan and workspace limits still apply.

"Couldn't discover OAuth settings" means the `.well-known` paths still redirect: run step 7.

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
