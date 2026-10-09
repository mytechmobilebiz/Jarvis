# Cron Jobs

`jobs.json` must remain valid JSON. Packs ship every job with `"enabled": false`. The setup wizard or owner enables a schedule only after its knowledge, credentials, limits, and delivery channel have been personalised.

Commented example shape:

```jsonc
{
  "jobs": [
    {
      "id": "<role>-daily-example",
      "name": "<skill-name>",
      "prompt": "Run <skill-name> per its skill. Budget: keep this run under <N> tokens; stop and report when the cap is hit.",
      "skills": ["<skill-name>"],
      "schedule": {
        "kind": "cron",
        "expr": "0 9 * * *",
        "display": "daily at 09:00"
      },
      "schedule_display": "daily at 09:00",
      "enabled": false,
      "deliver": "slack"
    }
  ]
}
```
