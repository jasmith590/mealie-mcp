# PLAN.md: Mealie MCP on Proxmox (fork of amercat37/mealie-mcp-server)

> Hand this file to Claude Code at the root of the repo. Work through the phases in order.
> Anything marked **[HUMAN]** is a step Jamie does in a browser or the Proxmox UI. Stop, say exactly what's needed, and wait.

## Status

- [x] Phase 0: forked, baseline unit tests green (193/193)
- [x] Phase 1: import tools, prompt, config cleanup
- [x] Phase 2: Proxmox packaging (`deploy/`)
- [ ] Phase 3: infrastructure bring-up (needs Jamie)
- [ ] Phase 4: hardening and upkeep

## Goal

From the Claude phone app, Jamie shares a recipe link (TikTok, Facebook, Instagram, any website) or a screenshot/caption, and Claude creates a clean recipe in his self-hosted Mealie (`https://mealie.jamesorjamie.com`).

| Piece | Where it runs | Public hostname |
|---|---|---|
| Mealie (exists) | Proxmox LXC | `mealie.jamesorjamie.com` |
| Authentik (new) | Proxmox LXC via community-scripts | `auth.jamesorjamie.com` |
| Mealie MCP server (this repo) | Proxmox LXC, systemd, no Docker | `mealie-mcp.jamesorjamie.com` |
| Nginx Proxy Manager (exists) | already publishes Mealie via UniFi port forward | adds the two new hostnames |

Claude's servers (not the phone) call the MCP endpoint, so it **must** be public HTTPS with OAuth. Only `/mcp`, the OAuth discovery routes and `/healthz` get exposed. Mealie's API key never leaves the MCP LXC.

## What upstream already is (verified)

- Python 3.12, FastMCP (`mcp[cli]`), `httpx`, `pydantic`, `joserfc`. MIT (Robert Diao 2025, Allen Mercer 2026). **Keep the license and both copyright lines.**
- `src/server.py` serves streamable HTTP at `/mcp`. Auth is opt-in: `AUTHENTIK_ISSUER` + `AUTHENTIK_AUDIENCE` + `MCP_SERVER_URL`; RS256 JWT via JWKS with `aud` check; optional `AUTHENTIK_JWKS_URI` + `AUTHENTIK_HOST` for LAN key fetches.
- `create_recipe(RecipeCreate)` already takes `orgURL` and `notes` and resolves foods/units/tags/categories against the library. No edit/delete of existing recipes, by design.
- Needs Mealie **v3.30.0+**.
- Tests are standalone scripts (`python tests/<name>.py`), not pytest. Unit scripts need no infra; integration scripts need a live Mealie.

Decision: **stay in Python.** New code follows existing patterns (mixin in `src/mealie/`, tool in `src/tools/`, script-style tests with fakes).

---

## Phase 0: Fork and orient (done)

Cloned into `~/projects/mealie-mcp`, `upstream` remote, branch `jamie/main`, project renamed, `uv.lock` committed.

**[HUMAN]** Create an empty GitHub repo (e.g. `<you>/mealie-mcp`), then `git remote add origin <url> && git push -u origin jamie/main`.

## Phase 1: Recipe import features (done)

Mealie payloads verified against Mealie's source (Nov 2026):
`POST /api/recipes/create/url` `{url, includeTags, includeCategories}` and `POST /api/recipes/create/html-or-json` `{data, url?, includeTags, includeCategories}`; both return the new slug (201).

- `import_recipe_from_url(url, include_tags=False)`: Mealie scrapes from inside the house. Returns `{ok, slug, name, url, ingredientCount, instructionCount, hasImage}`. Errors or a recipe with no ingredients/instructions come back as `{ok: false, reason}` rather than raising, so the assistant falls back to building it by hand. Empty shells are reported, never deleted.
- `import_recipe_from_html(data, source_url=None)`: same, for page HTML or schema.org JSON-LD the assistant already has.
- `create_recipe`: new optional `image` (alias `imageUrl`). Image is best-effort; the result carries `imageStatus`. The `from-social` tag is created on first use (other unknown tags are still dropped, as upstream).
- `set_recipe_image(slug, image_url)`: images only. The one documented exception to "no edits to existing recipes".
- Prompt `import_recipe(source, caption)`: website → URL import first; social or failed import → build from caption/screenshot, normalize, dedupe-check, `from-social` tag, list assumptions in a note.
- Config: `MCP_HOST`, `MCP_PORT`, `LOG_FILE` (default: stdout only); unauthenticated `GET /healthz`.

Skipped on purpose: Mealie's `POST /api/recipes/create/ai` (needs Mealie's own OpenAI key; Claude already does that job).

## Phase 2: Proxmox-native packaging (done)

`deploy/`: `install.sh` (Debian 12/13 LXC, `uv`, `mealie-mcp` system user, `/opt/mealie-mcp/app`, env at `/etc/mealie-mcp/mealie-mcp.env` mode 600), `update.sh`, `mealie-mcp.service` (hardened), `mealie-mcp.env.example`, `cloudflared/config.yml.example`, `README.md`. Docker files kept untouched for upstream merges.

## Phase 3: Infrastructure bring-up [mostly HUMAN]

Claude Code prints each step, waits for confirmation, verifies over SSH only if given access. Never ask for or store passwords in the repo.

### 3a. Authentik LXC
1. **[HUMAN]** On the Proxmox host shell, run the installer from https://community-scripts.org/scripts/authentik (copy the command from that page; it pins Authentik `2026.8.5` today). At least 2 vCPU / 2 GB RAM.
2. Config lives in `/etc/authentik/config.yml`; per-service configs in `/etc/default/authentik-*`. HTTP on 9000.
3. **[HUMAN]** DHCP reservation in UniFi; record the IP.
4. **[HUMAN]** `http://<ip>:9000/if/flow/initial-setup/` to create `akadmin`; enable MFA.

### 3b. MCP LXC
1. **[HUMAN]** Debian 13 unprivileged LXC `mealie-mcp`, 1 vCPU / 512 MB / 4 GB, DHCP reservation.
2. Inside it: `bash deploy/install.sh` (see `deploy/README.md`).
3. **[HUMAN]** Mealie → Profile → API Tokens → `mcp-server`; paste into the env file.
4. Start with `AUTHENTIK_*` commented out. From the LAN: `curl http://<ip>:8000/healthz`, then MCP Inspector at `http://<ip>:8000/mcp`; confirm `get_recipes` and one `import_recipe_from_url`.

### 3c. Publish via Nginx Proxy Manager
Mealie is already public through UniFi port forwarding to Nginx Proxy Manager (confirmed from the response headers: `server: openresty`, `x-served-by`). Reuse it:
1. **[HUMAN]** DNS `A` records for `auth.` and `mealie-mcp.jamesorjamie.com` → same IP as `mealie.`.
2. **[HUMAN]** NPM proxy hosts with Let's Encrypt: `auth.` → `<authentik-ip>:9000` (websockets on); `mealie-mcp.` → `<mcp-ip>:8000` with the Advanced snippet from `deploy/README.md` (path allowlist plus `proxy_buffering off`).
3. No NPM access list and no Cloudflare Access on `mealie-mcp`; OAuth is the gate.

### 3d. Authentik provider
Per upstream README "OAuth / Authentik Setup": slug `mealie-mcp`, Confidential, strict redirect `https://claude.ai/api/mcp/auth_callback`, access token `hours=12`, refresh `days=30`, scopes `openid email profile offline_access`. Application `Mealie MCP`, bound to Jamie's user only. Client ID → `AUTHENTIK_AUDIENCE`. Uncomment `AUTHENTIK_*`, restart, then:
- `curl -i https://mealie-mcp.jamesorjamie.com/mcp` → `401` with `WWW-Authenticate`
- `curl https://mealie-mcp.jamesorjamie.com/.well-known/oauth-protected-resource` names the Authentik issuer

### 3e. Connect Claude
**[HUMAN]** claude.ai → Settings → Connectors → Add custom connector → `https://mealie-mcp.jamesorjamie.com/mcp`, Client ID/Secret under advanced. Log in via Authentik. Check it appears in the phone app.

**Done when:** from the phone, a blog URL imports, and a TikTok link + pasted caption creates a `from-social` recipe with assumptions noted.

## Phase 4: Hardening and upkeep
- Add both LXCs to the Proxmox backup job.
- Authentik updates: re-run the community script inside its LXC and pick update.
- `git fetch upstream && git merge upstream/main`; Jamie-only files are `deploy/`, `PLAN.md`, the import tools/prompt, `tests/test_import_tools.py`.
- Optional: rate limit `mealie-mcp` (nginx `limit_req` in the NPM host, or Cloudflare if you move to a tunnel).

## Guardrails
- Never commit env files, API keys, client secrets or LXC IPs.
- No tool that deletes or edits existing recipe content (images excepted).
- Don't remove upstream tools or prompts; add alongside.
- Small commits, one per sub-phase, with tests.

## Open questions for Jamie
1. ~~How is Mealie exposed?~~ UniFi port forward → Nginx Proxy Manager. Answered.
2. Mealie reports `"version": "develop"`, so the v3.30.0+ requirement can't be read from the API. The first LAN test in 3b (a URL import plus `get_recipe_detailed`) is the real check.
3. SSH access to the new LXCs for Claude Code, or scripts only?
4. Upstream's `create_recipe` instructions tell the assistant to tag `my-recipes` (the original author's library convention). Harmless (unknown tags are dropped) but worth replacing with your own default tag.
