# Remote access plan: https://command.infinitemarketingai.com

**Status: proposal only.** Nothing described here has been done. No DNS record,
nameserver, CWP setting, firewall rule, router port or Cloudflare setting has
been changed, and none will be without the owner's explicit approval at each
gate below. The Command Center keeps running at http://localhost:8090 exactly as
now.

Goal: reach the dashboard from anywhere at **https://command.infinitemarketingai.com**,
protected by **Cloudflare Access** in front of the **existing application
sign-in**, with the live website (Contabo VPS, CWP) and its email untouched, the
n8n editor and PostgreSQL private, and no router ports opened.

## 1. Current DNS findings

**Not yet known.** The cloud environment where this plan was written cannot make
DNS queries (its network policy blocks them), so the authoritative nameservers
and records of infinitemarketingai.com could not be read from here.

What we know from the owner: the domain and its website are hosted on a Contabo
VPS running CWP. The DNS zone is therefore most likely served by CWP's own BIND
nameservers (often `ns1`/`ns2.infinitemarketingai.com`) or by the registrar or
Contabo.

**Owner step 0 (read-only):** on the Windows PC, in PowerShell:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/mytechmobilebiz/Jarvis/ccr-eb27a2af-8u7i39/command-center/scripts/dns-inventory.ps1 -OutFile dns-inventory.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\dns-inventory.ps1
```

It asks a public resolver and then the domain's own nameserver for the apex,
`www`, `mail`, `webmail`, `ftp`, `autodiscover`, `autoconfig`, `ns1`, `ns2`,
MX, SPF, DMARC, DKIM (`default` selector), DS (DNSSEC) and the four planned
hostnames, and saves a text report (public DNS data only, no secrets). Also open
the zone in CWP (DNS Functions > Edit DNS Zone) and note every record, **without
saving anything**, because DNS cannot list a whole zone and a scan can miss
records. Send both; they decide the path below.

| The report shows | Meaning |
|---|---|
| NS ending in `ns.cloudflare.com` | Already on Cloudflare: only new records are added (gate C onwards). The tunnel must live in that same Cloudflare account. |
| NS at CWP, the registrar or Contabo | Option A, B, C or D below. |
| A DS record | DNSSEC is on: it must be switched off at the registrar before any nameserver change, and back on afterwards with Cloudflare's DS record. |
| `ns1`/`ns2.infinitemarketingai.com` answer with the VPS address | Other domains may use them as their nameservers: their A records must be copied exactly (and the registrar's glue records left alone). |

## 2. Can Cloudflare Tunnel work without disrupting the website or email?

Cloudflare publishes a tunnel hostname only for a domain whose DNS zone is
active **in the same Cloudflare account** (a CNAME to `<id>.cfargotunnel.com`
from outside DNS does not work). On the Free plan the zone must use Cloudflare's
nameservers. So:

| Option | What changes | Website and email | Cost | Verdict |
|---|---|---|---|---|
| **A. Move the domain's nameservers to Cloudflare** ("full setup"), every existing record copied exactly and left **DNS-only** (grey cloud) | The registrar's nameserver setting; DNS is then edited at Cloudflare, no longer in CWP | Unchanged if every record is copied: visitors and mail still go straight to the Contabo VPS; CWP's certificates keep working | Free | **Recommended**, done carefully with the gates below |
| B. Keep DNS in CWP, Cloudflare "CNAME setup" | One verification TXT and one CNAME added in CWP | Unchanged | Business plan (paid; check the current price) | Possible if no nameserver change is acceptable |
| C. A separate domain only for remote access (e.g. a new, cheap domain fully on Cloudflare) | Nothing on infinitemarketingai.com | Untouched | Domain fee | Lowest risk, but not `command.infinitemarketingai.com` |
| D. Private network instead of a public hostname (e.g. Tailscale on the PC and your devices) | Nothing in DNS | Untouched | Free tier | Most private; no public address at all |

Subdomain delegation (only `command.` on Cloudflare) needs the Enterprise plan.

**Answer:** yes, without interruption, if option A is done record by record with
all existing records DNS-only, DNSSEC handled first, and the old CWP zone kept as
the rollback. The risk is in the nameserver move, not in the tunnel.

## 3. Proposed architecture

```
 phone / laptop (anywhere)
        │ https://command.infinitemarketingai.com
        ▼
 Cloudflare edge ── Cloudflare Access (who may enter: your email, one-time PIN or Google, optional MFA)
        │ outbound-only tunnel (cloudflared dials out on port 7844; no router port, no public IP)
        ▼
 Windows PC · Docker Desktop
   cloudflared container ──(private "tunnel" network 172.29.37.0/28)──► dashboard nginx :8081 (tunnel.conf)
     · checks the Access token (access.required + AUD)                      · Access token present, else 403
     · can reach only the dashboard                                          · visitor IP only from cloudflared
                                                                             · Secure cookie, HSTS (this host only)
                                                                             · unknown hostnames: connection closed
                                                                                     │ /api → api:8000 (app sign-in,
                                                                                     ▼  roles, approvals as today)
   unchanged:  http://localhost:8090 → dashboard nginx :8080 (local use)
   never routed: PostgreSQL, the n8n editor (5680), the API port, the worker, XAMPP, the other n8n on 5678
```

Hostnames:

| Hostname | Now | Later |
|---|---|---|
| `command.infinitemarketingai.com` | The dashboard, behind Access + app sign-in | |
| `hooks.infinitemarketingai.com` | **No DNS record, no route** (refused if it ever arrives) | Messaging gateway (0.6.0, section 8) |
| `api.infinitemarketingai.com` | **No DNS record, no route** | Authenticated external API with its own per-client credentials, Access service tokens and a path allow-list; never the dashboard API or `CC_SERVICE_TOKEN` |
| `clients.infinitemarketingai.com` | **No DNS record, no route** | Customer portal; consider a separate registrable domain (section 7, cookie tossing) |
| apex, `www`, mail and all other existing names | **Unchanged, DNS-only** | |

The configuration is in the repository, inactive until enabled:
`docker-compose.tunnel.yml` (the cloudflared container, the private network,
the extra nginx listener) and `remote-access/tunnel.conf` (the listener).
Enabling it is one line in `.env`; disabling it is removing that line.

## 4. Security controls

Two independent sign-ins: **Cloudflare Access** decides who may reach the page
at all; the **Command Center sign-in** (bcrypt passwords, 12-hour server-side
sessions, roles, approvals) works exactly as today.

| Control | Where | Setting |
|---|---|---|
| Who may enter | Cloudflare Access application for `command.infinitemarketingai.com` | Allow policy: the owner's email address(es) only; login method one-time PIN or Google; optionally require an authenticator (independent MFA); session 12-24 h. Deny everyone else (Access is deny by default). |
| Token checked before the PC | cloudflared public hostname settings | Protect with Access: required, team name, the application's AUD tag |
| Token checked again | `tunnel.conf` | Requests without `Cf-Access-Jwt-Assertion` get 403 |
| HTTPS | Cloudflare edge (Universal SSL covers `command.`) | A Single Redirect rule http→https for `command.` only; minimum TLS 1.2 |
| Secure session cookie | `tunnel.conf` | `proxy_cookie_flags cc_session secure` (local http://localhost:8090 keeps working) |
| HSTS | `tunnel.conf` | `max-age=86400`, this hostname only. **Never** zone-wide HSTS, `includeSubDomains` or preload (they would bind the live website and make rollback dangerous) |
| Trusted proxy headers | `tunnel.conf` | Visitor IP from `CF-Connecting-IP` only when the peer is cloudflared's fixed address; `X-Forwarded-For`, `X-Real-IP` overwritten; `X-Forwarded-Proto: https` |
| Hostname allow-list | `tunnel.conf` | Only `command.`; anything else: connection closed |
| Cross-site writes | app (unchanged) | Custom header on every change request, no CORS; never add CORS for `*.infinitemarketingai.com` |
| Login attempts | app throttle (now per real visitor) + one Cloudflare rate-limiting rule on `/api/auth/login` (Free plan: path only, 10 s window) | |
| Other Cloudflare settings | zone | Proxy **only** the tunnel hostnames; **Bot Fight Mode off** (it cannot be skipped later for webhooks); WAF custom rules are kept for `hooks.` (section 8), since Access already blocks everything on `command.` |
| Private services | Docker | cloudflared is not on the networks of the API, PostgreSQL, worker or n8n; 8090 and 5680 stay on 127.0.0.1; no `tcp://` routes |
| Router and firewall | | Nothing opened: cloudflared only dials out |

## 5. Verified (in the cloud test environment, 2026-10-19)

The tunnel listener was run from the real dashboard image with
`remote-access/tunnel.conf`, and probed from the cloudflared address and from an
untrusted address on the same network:

| Check | Result |
|---|---|
| Request without an Access token | 403 (from any peer) |
| With a token: dashboard and `/api/health` | 200 |
| HSTS on page and API responses | `max-age=86400`, no includeSubDomains |
| Security headers (CSP, frame, nosniff, referrer, permissions) | kept |
| Unknown hostname, `localhost`, `hooks.` on the tunnel port | connection closed |
| Sign-in through the tunnel listener | cookie `HttpOnly; SameSite=strict; Secure` |
| Sign-out | the delete-cookie is Secure too |
| Visitor IP stated by cloudflared, with forged `X-Forwarded-For` / `X-Real-IP` | audit log shows the real visitor IP; forgeries ignored |
| Visitor IP claimed by any other peer | ignored; the peer's own address is logged |
| Local sign-in on 127.0.0.1:8090 | unchanged |
| Merged Compose configuration | valid; cloudflared only on the tunnel network, read-only, no capabilities |

Code review findings that shaped the design (no change to the running
installation was needed for them):

- The session cookie was Secure only with `COOKIE_SECURE=true` (default false):
  solved per hostname in `tunnel.conf`.
- Through the existing listener every remote visitor would share one address,
  so anyone past Access could lock the owner's sign-in for 15 minutes: solved by
  the trusted-proxy settings above.
- nginx answered any hostname: the tunnel listener names its hostname and
  closes others.
- The API trusts `X-Forwarded-For` from any peer (uvicorn `*`): harmless here
  because the tunnel listener overwrites it; to be narrowed later.
- n8n can reach the API directly and could state any client IP; cloudflared
  must therefore never join the internal or egress networks (it does not).

Not testable here and checked at go-live (section 6, step 7): the Cloudflare
side itself (certificate, Access login, the tunnel connection, real headers).

## 6. Setup steps (each gate needs the owner's explicit approval)

### Step 0: inventory (read-only, now)
Run `dns-inventory.ps1` (section 1); note the CWP zone; note DNSSEC; note the
registrar. Install the Command Center **0.5.0** first (it carries the inactive
tunnel files and the version check).

### Gate A: Cloudflare account and the zone in *pending* state (no live effect)
1. Create a Cloudflare account with the owner's email; turn on two-factor
   authentication for it.
2. Add site `infinitemarketingai.com`, Free plan. When asked, **untick "proxy
   imported records"**.
3. Import the zone: export it from CWP as a BIND zone file if CWP offers it,
   otherwise add records by hand. Every record **DNS-only** (grey cloud).
4. Compare every record with the inventory and the CWP zone: apex and `www`
   A/AAAA, `mail`, `webmail`, `ftp`, `autodiscover`/`autoconfig`, MX, SPF,
   DKIM (every selector), DMARC, verification TXT records, SRV, CAA, and
   `ns1`/`ns2` A records. Cloudflare shows the two nameservers it assigned.
5. Check the copy at those nameservers before anything changes (read-only):
   `.\dns-inventory.ps1 -Resolver <one of the assigned nameservers>` produces a
   second report from Cloudflare's copy; it must match the first report line for
   line (TTLs may differ). Check any record the script does not cover with
   `Resolve-DnsName <name> -Type <type> -Server <assigned nameserver>`.
A pending zone changes nothing for visitors; a Free pending zone is deleted
after 28 days if not activated.

### Gate B: the nameserver change (the only step that touches live DNS)
1. Choose a quiet time. If DNSSEC is on: switch it off at the registrar, then
   wait for the DS record's TTL (often 1-2 days) before continuing.
2. At the registrar, replace the nameservers with Cloudflare's two. Keep a
   screenshot of the old values. **Do not delete or edit the zone in CWP**: it is
   the rollback.
3. Watch for up to 24 h: website (`https://infinitemarketingai.com`, `www`),
   sending and receiving email, webmail, CWP's certificate renewal (it keeps
   working because the records point at the VPS unproxied).
4. From now on, DNS changes are made at Cloudflare. CWP's own zone editor no
   longer affects the live domain (for example, when CWP creates a new DKIM key
   or a subdomain, copy that record to Cloudflare).
5. If DNSSEC was on: enable it in Cloudflare and add Cloudflare's DS record at
   the registrar.

### Gate C: Cloudflare Access (Zero Trust Free plan, under 50 users)
1. Zero Trust dashboard → choose a team name (it becomes
   `<team>.cloudflareaccess.com`).
2. Identity: keep the Cloudflare default login or add One-time PIN; optionally
   Google. Optionally require an authenticator app (independent MFA).
3. Access → Applications → Self-hosted → public hostname
   `command.infinitemarketingai.com` → policy **Allow**, Include **Emails** =
   the owner's address(es) → session duration 12-24 h → save. Copy the
   application's **AUD tag**.
   Creating the Access application **before** the tunnel route means the
   hostname is never public without it.

### Gate D: the tunnel (the PC side)
1. Zero Trust → Networks → Tunnels (menu names change from time to time) → Create → Cloudflared → name
   `command-center-pc` → environment **Docker** → copy only the token (the long
   value after `--token`).
2. On the PC, in the Command Center folder (PowerShell):
   ```powershell
   New-Item -ItemType Directory -Force .\secrets | Out-Null
   Set-Content -Path .\secrets\cloudflared-token.txt -Value '<paste the token>' -NoNewline -Encoding ASCII
   icacls .\secrets /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
   Add-Content -Path .\.env -Value 'COMPOSE_FILE=docker-compose.yml;docker-compose.tunnel.yml'
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
   ```
   `start.ps1` recreates the dashboard container (a few seconds) and starts
   cloudflared. The tunnel shows **Healthy** in the Zero Trust dashboard.
3. Tunnel → Public hostname → Add: subdomain `command`, domain
   `infinitemarketingai.com`, service **HTTP** `dashboard:8081`. Under
   Additional settings → Access: **Protect with Access** on, team name, AUD tag.
   Cloudflare creates one new proxied CNAME `command` → `<id>.cfargotunnel.com`;
   no existing record is touched.
4. Do not add any other public hostname. The tunnel's catch-all stays
   "http_status:404".

### Gate E: zone settings
- Rules → Redirect rules → Single Redirect: hostname equals
  `command.infinitemarketingai.com` and scheme http → https (301). (Leave the
  zone-wide "Always Use HTTPS" off unless only tunnel hostnames are proxied.)
- SSL/TLS → Edge certificates: minimum TLS 1.2. **HSTS off** at zone level.
- Security → WAF → Rate limiting rule (Free: 1 rule): path equals
  `/api/auth/login`, 10 requests per 10 s per IP → block.
- Security → Bots: **Bot Fight Mode off**.

### Step 7: check from outside (phone on mobile data)
1. `https://command.infinitemarketingai.com` → Cloudflare Access page → sign in
   → the Command Center sign-in → dashboard.
2. In a private window without the Access sign-in: only the Access page appears.
3. Browser developer tools → Application → Cookies: `cc_session` is *Secure*,
   *HttpOnly*, *SameSite Strict*; the response has `Strict-Transport-Security:
   max-age=86400`.
4. Automation Logs / Activity log shows your phone's public IP for the sign-in.
5. `http://command.infinitemarketingai.com` redirects to https.
6. `https://hooks.infinitemarketingai.com`, `api.`, `clients.`: no DNS record.
7. On the PC, `http://localhost:8090` and the n8n editor at
   `http://localhost:5680` work as before; neither is reachable from outside.
8. Website and email still work.

### Step 8: keep the PC available
- Power: `powercfg /change standby-timeout-ac 0` and
  `powercfg /change hibernate-timeout-ac 0` (no sleep on mains power).
- Docker Desktop → Settings → General → *Start Docker Desktop when you sign in*:
  on. Decide whether the PC signs in automatically after a restart (Docker
  Desktop starts only after sign-in); otherwise remote access is down after a
  Windows Update restart until someone signs in.
- Windows Update: set active hours; check remote access after updates.
- cloudflared does not update itself: when a new release is announced, change
  the pinned version in `docker-compose.tunnel.yml` (through a Command Center
  update) and run `start.ps1`.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A record missed in the zone copy | Part of the website or email stops resolving | Inventory + CWP zone + record-by-record check at Cloudflare's nameservers before gate B; rollback = old nameservers |
| DNSSEC left on during the move | Whole domain unreachable for validating resolvers | Switch off and wait out the DS TTL first |
| `ns1`/`ns2` serve other domains | Those domains break if the A records are missing | Copy them exactly; leave the registrar's glue records alone |
| CWP keeps editing its own zone | Changes made in CWP stop taking effect | Make DNS changes at Cloudflare from gate B on |
| Accidentally proxying the website or mail records | Website behaviour changes; mail breaks (mail cannot go through the proxy) | Everything existing stays DNS-only; only `command.` is proxied |
| Zone-wide HSTS or "Always Use HTTPS" | Locks the website to HTTPS through Cloudflare; makes rollback unsafe | Not used; HSTS only on `command.`, max-age 1 day |
| Access policy too broad (e.g. "Everyone" or any one-time-PIN user) | Anyone could reach the app sign-in | Allow only named emails; the app sign-in remains a second barrier |
| The PC asleep, off or signed out | Remote access down (Cloudflare shows an error; nothing is exposed) | Step 8; an external uptime check |
| Access session expires while the dashboard is open | Background requests fail until the page is reloaded | Reload the page; a small app change (section 9) makes this clean |
| Cookie tossing from a compromised sibling site (`www`, a future portal) | Could sign the owner out or into another app session | Keep sibling sites trustworthy; Secure cookie on `command.`; app change in section 9; customer portal on its own domain |
| Tunnel token leaked | Someone could run a connector for this tunnel (still behind Access) | Token only in `secrets\` (owner and SYSTEM), never in `.env`, backups or chat; rotate in Zero Trust and restart |
| Free-plan limits | One rate-limit rule, path only, 10 s windows | The app's own throttle remains the main control |
| Wrong ingress rule (e.g. to localhost:5678 or XAMPP) | Exposes an unrelated service | Only `dashboard:8081`; cloudflared is in a container that cannot see the PC's localhost or the other networks |

## 8. Telegram and WhatsApp webhooks (future architecture, 0.6.0)

Messaging services call a public HTTPS address and cannot pass Cloudflare
Access, so they get their **own hostname and their own small service**; they
never reach the dashboard, the API's normal routes or the n8n editor.

```
 Telegram / Meta ──https──► hooks.infinitemarketingai.com (no Access app; WAF: POST to exact paths only,
                                                           GET only for the WhatsApp verification path)
        │ same tunnel, separate public hostname → http://gateway:8082
        ▼
 messaging gateway container (tunnel network + internal network only)
   · /telegram/<bot>: X-Telegram-Bot-Api-Secret-Token compared in constant time with a random secret_token (set via setWebhook)
   · /whatsapp: GET hub.verify_token handshake; POST X-Hub-Signature-256 = HMAC-SHA256(app secret, raw body), constant time
   · body size limit, duplicate drop by update_id / message id, per-sender rate limit, strict JSON schema
   · stores the message and hands it to the Command Center through an internal endpoint with its own scoped token
   · message text is data: it can never trigger an action outside the approved list; approvals stay in the dashboard
```

- **Telegram owner bot** can avoid webhooks entirely: long polling
  (`getUpdates`) needs only outbound connections, so no `hooks.` hostname is
  needed for it.
- **WhatsApp Cloud API** has no polling: it needs `hooks.` with a valid
  certificate (Cloudflare provides it). Meta retries failed deliveries for up to
  7 days, so duplicates must be dropped.
- n8n's built-in Telegram and WhatsApp triggers are possible, but they would
  require publishing n8n's webhook paths; the gateway keeps n8n fully private.
  If n8n triggers are ever used, only exact `/webhook/<id>/webhook` paths may be
  forwarded, never `/rest`, `/webhook-test` or the editor.
- Rollback: Telegram `deleteWebhook`; remove the WhatsApp callback URL in the
  Meta app; delete the `hooks.` public hostname.

## 9. Small app changes recommended before go-live (next release, with tests)

1. When the Access session expires, background requests should report it
   cleanly and the page reload (today they fail as network errors until a
   reload).
2. Refuse requests that carry more than one `cc_session` cookie (cookie tossing).
3. Run password checks off the event loop and bound the login-throttle memory.
4. Hide the "Open n8n" link for remote sessions (it points to the PC's localhost).
5. Optionally: limit the local 8090 listener to `localhost`/`127.0.0.1` host
   names (protection against DNS rebinding), and narrow uvicorn's trusted proxy
   to the dashboard's fixed address.

## 10. Rollback (fastest first; each step is independent)

| What | How | Effect |
|---|---|---|
| Stop remote access immediately | Zero Trust → Tunnels → `command-center-pc` → delete the `command` public hostname, or on the PC: `docker compose -f docker-compose.yml -f docker-compose.tunnel.yml stop cloudflared` | Seconds; local use unaffected |
| Remove the tunnel from the PC | Remove the `COMPOSE_FILE=` line from `.env`; `docker compose -f docker-compose.yml -f docker-compose.tunnel.yml rm -sf cloudflared`; run `start.ps1`; delete `secrets\cloudflared-token.txt` | Back to the exact current setup |
| Remove it at Cloudflare | Delete the tunnel, the `command` DNS record and the Access application | Nothing left of remote access |
| Undo the nameserver move | At the registrar, put back the old nameservers (from the screenshot); the CWP zone was never changed. If DNSSEC was re-enabled, remove Cloudflare's DS first | DNS back at CWP after propagation (up to 24-48 h) |
| HSTS | Only `command.`, 1 day; browsers forget it after a day | |

Data, Docker volumes, `.env` and the encryption keys are never touched by any
step: the tunnel adds one container and one nginx listener and removes nothing.

## 11. Decisions needed from the owner

1. Option A, B, C or D (section 2), after seeing the DNS inventory.
2. Approval of each gate (A to E) when it is reached.
3. Who may sign in through Access (email addresses) and how (one-time PIN,
   Google, an authenticator app).
4. Whether the PC signs in automatically after restarts (availability vs.
   physical security).
5. For 0.6.0: Telegram by long polling (no public webhook) or by webhook; when
   to build the WhatsApp gateway and `hooks.`.
