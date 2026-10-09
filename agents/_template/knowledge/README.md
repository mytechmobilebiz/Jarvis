# Role Knowledge

<!--
Knowledge files hold durable facts the owner may edit: voice rules, account IDs,
thresholds, products, policies, and standing preferences.

Daily state does not belong here. Put work logs, generated reports, pending approvals,
and run output in workspace/reports/<role>-YYYY-MM-DD.md.
-->

Create one small markdown file per subject. Prefer tables and short bullets over essays.

Useful starter files:

- `style.md` for voice, banned phrases, examples, and formatting preferences.
- `cadence.md` for schedule, frequency, and recurring operating windows.
- `accounts.md` for account names and non-secret identifiers. Never store keys here.

Owner notes:

- **Role owner:** <!-- Who approves this role's public actions? -->
- **Slack channel:** <!-- Filled by the setup wizard in knowledge/org.md for the composed Jarvis profile. -->
- **Never do:** <!-- Hard prohibitions specific to this role. -->
