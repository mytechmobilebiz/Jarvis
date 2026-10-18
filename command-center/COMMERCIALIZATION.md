# Commercialization plan: Infinite AI Command Center

Status: planning document, 2026-10-18. The personal development version keeps
running as it is; everything here prepares a future commercial edition sold
by Infinite Marketing. Nothing in this document is legal advice: every item
marked **legal review** needs a qualified lawyer before distribution.

## 1. Where each requirement stands

| # | Requirement | Built now | Still to do |
|---|---|---|---|
| 1 | Proprietary code separate from third-party components | All proprietary code is in `command-center/`. No third-party code is vendored: Python and npm packages and container images are fetched from their publishers at build/install time. | Move `command-center/` to its own repository (see blocker B3). |
| 2 | Replaceable workflow engine | `app/workflow.py`: one `WorkflowEngine` interface with three engines: `n8n`, `webhook` (HMAC-signed POSTs any engine can receive) and `none`. Engines call back through engine-neutral endpoints (`/api/integrations/workflow/ack`). Agents never talk to n8n. | Commercial default engine decision (blocker B1). |
| 3 | No redistribution without rights | The installer downloads n8n, PostgreSQL, nginx and base images from their publishers; Docker Desktop is a customer-installed prerequisite; no AI model is shipped (API only, customer's own key). | Keep it that way in the commercial installer; licensed alternatives where needed (B1, B2). |
| 4 | Software bill of materials | `SBOM.md` and `sbom.cdx.json` (CycloneDX 1.5), generated from the shipped image and lockfile by `scripts/generate-sbom.py`: 42 Python packages, 8 npm packages in the browser bundle (all MIT), 226 build-only npm packages, 7 images. No unknown licenses. | Regenerate on every dependency change; add Debian/Alpine OS packages of the images (e.g. with Syft) before distribution. |
| 5 | Windows installation with checks, validation, database initialization, uninstall | `install.ps1` (prerequisite check, `.env` generation, configuration validation inside the image, start with database initialization and migrations, smoke test), `uninstall.ps1` (keeps data unless `-DeleteData`, which takes a final backup), `backup.ps1`, `status.ps1`. | A signed MSI or Inno Setup wrapper with an Apps & Features uninstall entry; code-signing certificate (B6). |
| 6 | Customer API keys, configurable AI providers | Settings page: the customer enters their own Anthropic key, stored encrypted (Fernet, `CC_SECRETS_KEY`), never returned (only `…last4`). Provider, model and daily cap are customer settings; the worker resolves them per task. `LLMProvider` interface for more providers. | Further providers (e.g. a local model server) behind the same interface, only where their terms allow. |
| 7 | Developer settings separate from customer settings | Deployment/developer settings: environment only (`app/config.py`, `.env`), never shown in the UI. Customer settings: a whitelist in `app/customer_settings.py`, stored in the database, admin-only. | Per-customer branding settings (white-label). |
| 8 | Activation, updates, backups, license management | Offline license files signed with Ed25519 (`app/licensing.py`); the private key never ships. Expired or invalid licenses fall back to the configured edition and never delete or lock data. Backups via `backup.ps1`. | Activation service (optional online check-in, seat transfer), signed update packages and an update channel, encrypted and scheduled backups with tested restore. |
| 9 | Personal, Professional, Agency editions | `EDITIONS` with per-edition agents, features, site and user limits, enforced on task creation, planning, site creation and user creation. The owner's installation runs the internal, unsellable `developer` edition. | Final edition contents and prices (business decision). |
| 10 | Credit Repair data isolated from marketing data | Design below (section 4). | Build with the Credit Repair Agent; legal review (B5). |

### Editions as configured today

| | Personal | Professional | Agency |
|---|---|---|---|
| Websites | 5 | 50 | unlimited |
| Users | 1 | 5 | unlimited |
| Agents | supervisor, WordPress monitor, content, SEO | + sales, support, ads | all, including Credit Repair |
| AI with the customer's own key | yes | yes | yes |
| Multi-user roles, service tokens, client reports | | yes | yes |
| White-label, alternative workflow engine, Credit Repair | | | yes |

These are placeholders for a business decision; they live in one table in
`app/licensing.py`.

## 2. Blockers before distribution

| ID | Blocker | Why it matters | Options |
|---|---|---|---|
| **B1** | **n8n license** | n8n Community Edition is under the Sustainable Use License: use "for your own internal business purposes", and distribution only free of charge for non-commercial purposes. Shipping or provisioning n8n as part of a paid product, or letting customers build workflows through our product, needs a commercial agreement with n8n (e.g. n8n Embed). The owner's own internal use is fine. | (a) license n8n Embed; (b) commercial editions default to `WORKFLOW_ENGINE=webhook` or `none`, with n8n an optional customer-installed integration under the customer's own agreement (**legal review** of whether even that is acceptable); (c) a built-in workflow runner. |
| **B2** | **Docker Desktop** | Docker Desktop is free only for personal use, education, non-commercial open source and small businesses (under 250 employees *and* under $10M revenue); larger customers need a paid subscription, and it cannot be redistributed. | State it as a prerequisite in the EULA and sales material; support Rancher Desktop or Podman (Apache-2.0) as alternatives; longer term, a native Windows-service installer with no container runtime. |
| **B3** | **Repository and code provenance** | `command-center/` lives inside the repository of the purchased Jarvis product, whose license may not permit redistribution of its contents. The Command Center imports no Jarvis code; one list of error-page phrases was adapted from `agents/jarvis/scripts/check_websites.py`, a script written for the owner after the Jarvis import. | Move `command-center/` to a new repository; rewrite or confirm ownership of the adapted phrase list; keep a provenance record. |
| **B4** | **AI provider terms** | Customers use their own Anthropic accounts (BYOK), so each has their own agreement and usage policy, and their data goes to Anthropic under it. Infinite Marketing must not resell or share a key. | Disclose in the EULA/privacy policy that AI features send content to the customer's chosen provider; keep AI optional (rule-based mode works without it). |
| **B5** | **Credit repair regulation** | Credit repair is heavily regulated: the federal Credit Repair Organizations Act (no fees before services are fully performed, mandatory written contract and disclosures, 3-business-day cancellation), the FCRA dispute process, the FTC Telemarketing Sales Rule (no advance fees for credit repair sold by phone), and state laws that add registration, bonding or outright restrictions. Georgia (the 678 area code suggests the business is based there) has its own credit-services and debt-adjusting statutes. Client data (SSNs, dates of birth, credit reports) also falls under data-security and breach-notification laws, possibly the FTC Safeguards Rule. | **Legal review** before the module touches real client data or is sold; the software should support compliance (contracts, waiting periods, disclosures) and never make or assist misleading claims. |
| **B6** | **Distribution basics** | Windows SmartScreen warns on unsigned installers; customers need terms they accept. | Code-signing certificate; EULA, privacy policy, data-processing terms, support policy; trademark search for "Infinite AI". |
| **B7** | **Export classification** | The product uses standard encryption (TLS, Fernet, Ed25519). | Confirm the mass-market classification before selling outside the US (**legal review**). |

## 3. Commercial architecture (target)

```
 Infinite Marketing (vendor)                     Customer installation (on their PC or server)
 ─────────────────────────────                   ──────────────────────────────────────────────
 build pipeline ──► signed release package ──►   installer (prerequisite checks, .env, validation)
 license signer (private Ed25519 key) ──────►   license file  ──► entitlements (edition, limits)
 update channel (signed manifests) ─────────►   updater (backup → migrate → smoke test)
                                                 Command Center (proprietary)
                                                   ├─ workflow engine: webhook / none / licensed n8n
                                                   ├─ AI provider: customer's own key, encrypted
                                                   ├─ marketing database
                                                   └─ credit repair database (isolated, encrypted)
```

- **Single-tenant, customer-hosted.** Each customer runs their own copy; there
  is no shared vendor cloud holding their data. An optional hosted edition is a
  separate decision with its own security and compliance work.
- **Licensing never holds data hostage.** Expiry limits features, not access
  to data, exports or backups.
- **Telemetry off by default.** Any update or license check sends only the
  license id and version, opt-in, documented.
- **Updates are forward-only migrations**, preceded by an automatic backup and
  followed by the smoke test; a failed update restores the backup.

## 4. Credit Repair data isolation (design)

The Credit Repair Agent's data never shares a database, role, key or code path
with marketing data:

| Control | Design |
|---|---|
| Storage | A separate PostgreSQL database `credit_repair` with its own login role; the main application role has no grant on it. |
| Encryption | Sensitive fields (SSN, date of birth, account numbers, report contents) encrypted in the application with a separate key, `CREDIT_REPAIR_DATA_KEY`, that the rest of the platform never loads. |
| Access | A per-user permission (`sensitive_data`), off by default even for admins; every read and write recorded in a separate access log. |
| Leakage | Events and summaries about credit-repair work carry ids only: nothing personal goes to the workflow engine, the activity feed, logs or the AI model. Sending personal data to an AI model requires an explicit, per-installation opt-in, with redaction by default. |
| Edition | Agency only, and off until an admin enables it after accepting the compliance notice. |
| Backups | Separate, encrypted backup set; retention and deletion rules configurable per customer. |
| Outbound actions | Dispute letters and client messages are drafts that require approval; nothing is sent to bureaus or clients automatically. |

## 5. Roadmap to a first commercial release

1. Owner decisions: confirm the seven agents, edition contents and prices.
2. Resolve B1 (n8n) and B3 (own repository); legal review of B5 before building
   credit-repair features that store real data.
3. Credit Repair Agent with the isolation above; remaining agents with mock
   integrations, then real ones as each is approved.
4. Activation service and update channel; scheduled encrypted backups with a
   tested restore.
5. Signed installer, EULA and privacy documents, support process; pilot with
   one friendly customer before general sale.
