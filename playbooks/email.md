# Playbook: Connect Sarah's inbox (IMAP app password, ~10 minutes)

Sarah needs a mailbox she can read and draft from. IMAP with an app password avoids OAuth app review entirely and keeps the setup understandable. Use a support mailbox or alias, not the owner's private inbox.

## 1. Create the mailbox app password

For Gmail:

1. Turn on 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security).
2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create an app password named `Jarvis Sarah`.
4. Copy the 16-character password and remove spaces when pasting it.

For other providers:

| Provider | App-password path | IMAP host | IMAP port | SMTP host | SMTP port |
|---|---|---|---|---|---|
| Gmail | Google Account → Security → App passwords | `imap.gmail.com` | `993` | `smtp.gmail.com` | `587` |
| iCloud Mail | [account.apple.com](https://account.apple.com/) → **Sign-In and Security → App-Specific Passwords** | `imap.mail.me.com` | `993` | `smtp.mail.me.com` | `587` |
| Fastmail | Fastmail → **Settings → Password & Security → App passwords** | `imap.fastmail.com` | `993` | `smtp.fastmail.com` | `465` |

Fastmail also supports SMTP `smtp.fastmail.com:587` with STARTTLS if your mail client requires it.

## 2. Add the mailbox values

Open `~/.hermes/profiles/jarvis/.env` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\.env`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/.env`) and add these exact variable names, replacing the sample values:

```dotenv
EMAIL_ADDRESS=support@example.com
EMAIL_PASSWORD=replace-with-app-password
EMAIL_IMAP_HOST=imap.example.com
EMAIL_IMAP_PORT=993
EMAIL_SMTP_HOST=smtp.example.com
EMAIL_SMTP_PORT=587
EMAIL_ALLOWED_USERS=owner@example.com
```

`EMAIL_ALLOWED_USERS` is recommended: list only the addresses allowed to command Jarvis by email. The same `EMAIL_PASSWORD` is used for SMTP unless your provider gives separate send and read passwords. Jarvis and Sarah read and send mail with these values.

## 3. Restart JARVIS

Restart JARVIS from the tray. Email switches on by itself once `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_HOST` and `EMAIL_SMTP_HOST` are all set. Email autopilot is fully on in this install: Sarah and Jarvis send email replies without asking for approval. Issuing refunds or other money movement still needs approval. To switch a category back to drafts-for-approval, set it to `false` in `agents/sarah/knowledge/autopilot.md`.

Sarah only sees unread mail in the Inbox and marks each message read as she picks it up (she never moves or deletes it), so a dedicated mailbox for her is the recommended setup.

## Verify

Send a test email to the support mailbox, then ask Jarvis in the chat: "Check the support inbox and report the newest test message in #support."

Sarah should reply to the test email directly and log it as auto-resolved.

## Rotation & housekeeping

Rotate by revoking the provider app password, creating a new one, replacing `EMAIL_PASSWORD` in the profile `.env`, and restarting JARVIS from the tray. If a teammate leaves, rotate immediately.
