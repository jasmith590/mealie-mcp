# Deploying on Proxmox (no Docker)

Three LXCs end up involved: the Mealie one you already have, **Authentik** (new), and **mealie-mcp** (new). The Nginx Proxy Manager that already publishes `mealie.jamesorjamie.com` (UniFi forwards 443 to it) publishes two more hostnames:

| Hostname | Goes to |
|---|---|
| `auth.jamesorjamie.com` | Authentik LXC, port 9000 |
| `mealie-mcp.jamesorjamie.com` | mealie-mcp LXC, port 8000 (only `/mcp`, `/.well-known/*`, `/healthz`) |

Do it in this order. The server works without auth on the LAN first, so you can test each piece before the next one depends on it.

## 1. Authentik LXC

1. Proxmox host shell: run the installer command from <https://community-scripts.org/scripts/authentik>. Give it at least **2 vCPU / 2 GB RAM** (Advanced settings) if the defaults are lower.
2. UniFi: add a DHCP reservation for the new container.
3. Browser: `http://<authentik-ip>:9000/if/flow/initial-setup/` and create the `akadmin` password. Then turn on MFA for that account (user settings, MFA devices).

Script notes: config is `/etc/authentik/config.yml`, per-service settings in `/etc/default/authentik-*`. To update later, run the same script again inside the container.

## 2. mealie-mcp LXC

1. Proxmox: create an **unprivileged Debian 13** container named `mealie-mcp`: 1 vCPU, 512 MB RAM, 4 GB disk. Keep **Nesting** on (Options, Features); the systemd hardening needs it. DHCP reservation in UniFi.
2. Get the code into it. Either clone from GitHub (private repo: add a read-only deploy key to `/root/.ssh` first) or copy your Mac's folder over:
   ```bash
   # from the Mac
   scp -r ~/projects/mealie-mcp root@<mcp-ip>:/root/mealie-mcp
   ```
3. Inside the container:
   ```bash
   bash /root/mealie-mcp/deploy/install.sh
   ```
   It installs `uv` and Python 3.12, creates the `mealie-mcp` user, puts the app in `/opt/mealie-mcp/app`, writes `/etc/mealie-mcp/mealie-mcp.env` (mode 600) and enables the service. Re-running it is safe.
4. Mealie: Profile → API Tokens → create `mcp-server`. Edit the env file:
   ```bash
   nano /etc/mealie-mcp/mealie-mcp.env   # MEALIE_BASE_URL (LAN IP) and MEALIE_API_KEY
   systemctl start mealie-mcp
   curl http://127.0.0.1:8000/healthz     # {"ok":true}
   journalctl -u mealie-mcp -f
   ```
5. LAN test from the Mac with MCP Inspector:
   ```bash
   npx @modelcontextprotocol/inspector
   ```
   Transport "Streamable HTTP", URL `http://<mcp-ip>:8000/mcp`. Run `get_recipes`, then `import_recipe_from_url` with a real blog recipe.

## 3. Publish the hostnames (Nginx Proxy Manager)

1. **DNS:** at your DNS provider, add `A` records for `auth.jamesorjamie.com` and `mealie-mcp.jamesorjamie.com` pointing to the same public IP as `mealie.jamesorjamie.com`.
2. **NPM, Proxy Host `auth.jamesorjamie.com`:**
   - Scheme `http`, forward to `<authentik-ip>`, port `9000`
   - Turn on **Websockets Support** and **Block Common Exploits**
   - SSL tab: request a new Let's Encrypt certificate, **Force SSL**, **HTTP/2**
3. **NPM, Proxy Host `mealie-mcp.jamesorjamie.com`:**
   - Scheme `http`, forward to `<mcp-ip>`, port `8000`
   - **Block Common Exploits** on; Websockets not needed
   - SSL tab: Let's Encrypt, **Force SSL**, **HTTP/2**
   - **Advanced** tab, paste this. It only exposes what Claude needs, and stops nginx from buffering the MCP event stream:
     ```nginx
     if ($uri !~ "^/(mcp/?$|healthz$|\.well-known/)") {
         return 404;
     }
     proxy_buffering off;
     proxy_cache off;
     proxy_read_timeout 300s;
     ```
4. From your phone on cellular (off Wi-Fi, so you're really coming from outside):
   - `https://auth.jamesorjamie.com` shows the Authentik login page
   - `https://mealie-mcp.jamesorjamie.com/healthz` shows `{"ok":true}`
   - `https://mealie-mcp.jamesorjamie.com/` is a 404, which is the point

Don't put an NPM Access List (basic auth or an IP allowlist) on `mealie-mcp`: Claude's servers make the calls from Anthropic's IPs and can't answer a password prompt. Authentik OAuth is the lock.

*Alternative:* a Cloudflare Tunnel also works if you'd rather not forward ports; see [`cloudflared/config.yml.example`](cloudflared/config.yml.example). Don't put Cloudflare Access in front of `mealie-mcp` either.

## 4. Authentik provider for the MCP server

Admin UI → Applications → Providers → Create → **OAuth2/OpenID Provider**:

| Field | Value |
|---|---|
| Name | `mealie-mcp` (the slug in the issuer URL comes from the application below) |
| Authorization flow | `default-provider-authorization-explicit-consent` |
| Client type | Confidential |
| Redirect URIs | Strict: `https://claude.ai/api/mcp/auth_callback` |
| Signing key | any certificate (e.g. the self-signed one Authentik ships) |
| Access token validity | `hours=12` |
| Refresh token validity | `days=30` |
| Scopes | `openid`, `email`, `profile`, `offline_access` |

Then Applications → Applications → Create: name `Mealie MCP`, slug **`mealie-mcp`**, provider = the one above. On the application's **Policy / Group / User Bindings** tab, bind only your user.

Copy the provider's **Client ID** and **Client Secret** (you need the secret in step 5).

## 5. Turn on auth

In `/etc/mealie-mcp/mealie-mcp.env`, uncomment and fill:

```bash
MCP_SERVER_URL=https://mealie-mcp.jamesorjamie.com
AUTHENTIK_ISSUER=https://auth.jamesorjamie.com/application/o/mealie-mcp/
AUTHENTIK_AUDIENCE=<client id>
AUTHENTIK_JWKS_URI=http://<authentik-ip>:9000/application/o/mealie-mcp/jwks/
AUTHENTIK_HOST=auth.jamesorjamie.com
```

```bash
systemctl restart mealie-mcp
curl -i https://mealie-mcp.jamesorjamie.com/mcp -X POST          # 401 + WWW-Authenticate
curl https://mealie-mcp.jamesorjamie.com/.well-known/oauth-protected-resource   # names the Authentik issuer
curl https://mealie-mcp.jamesorjamie.com/healthz                  # still {"ok":true}
```

## 6. Connect Claude

claude.ai (web) → Settings → Connectors → **Add custom connector**:
- URL: `https://mealie-mcp.jamesorjamie.com/mcp`
- Advanced settings: the Authentik Client ID and Client Secret

Log in through Authentik when asked. The connector then shows up in the Claude phone app. Try: "Save this recipe: <blog link>", then a TikTok link plus its caption.

## Day to day

| Task | Command (inside the mealie-mcp LXC) |
|---|---|
| Update after pushing to GitHub | `bash /opt/mealie-mcp/app/deploy/update.sh` |
| Logs | `journalctl -u mealie-mcp -f` |
| Restart | `systemctl restart mealie-mcp` |
| Settings | `/etc/mealie-mcp/mealie-mcp.env`, then restart |

If you installed by copying the folder (no GitHub remote), copy it again and re-run `install.sh` instead of `update.sh`.

Add both new LXCs to your Proxmox backup job.

## Troubleshooting

- **Service exits with `226/NAMESPACE`**: turn on Nesting for the LXC, or comment out the hardening block in `deploy/mealie-mcp.service` and re-run `install.sh`.
- **Server refuses to start, mentions AUTHENTIK_AUDIENCE**: you set `AUTHENTIK_ISSUER` without `AUTHENTIK_AUDIENCE`. Set both, or neither.
- **Claude says the connector failed to authorize**: check the redirect URI is exactly `https://claude.ai/api/mcp/auth_callback` and that your user is bound to the application.
- **Every token is rejected (401 after login)**: the issuer in the env file must match the provider's "OpenID Configuration Issuer" exactly, trailing slash included, and the audience must be that provider's Client ID.
- **Claude connects, then hangs or times out on tool calls**: NPM is buffering the stream. Check the `proxy_buffering off;` lines are in the mealie-mcp host's Advanced tab.
- **Authentik login loops or redirects to `http://`**: turn on Websockets Support for the auth host and keep Force SSL on; NPM sends `X-Forwarded-Proto`, which Authentik trusts by default.
- **Imports from a site fail**: some sites block scrapers. Paste the recipe text in the chat instead and Claude builds it with `create_recipe`.
