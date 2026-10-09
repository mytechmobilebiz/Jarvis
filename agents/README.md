# Staffing a New Role

Copy `agents/_template/` to `agents/<name>/`, then make six decisions before writing a line of lore: name, register, Slack channel, skills, knowledge files, and schedule. A role should know its beat, its voice, where it reports, which procedures it owns, which owner-fillable facts it needs, and which cron jobs deserve the nuisance of existing.

In v1, roles are composed into the single Jarvis profile installed from `agents/jarvis/`. The setup wizard copies role skills into Jarvis, offers to register cron jobs, merges persona rules, and fills channel IDs in `knowledge/org.md`. Every cron entry ships with `"enabled": false`; the wizard or owner turns the key only after the role's knowledge, credentials, limits, and delivery channel have been personalised. The role folder keeps the Hermes profile-distribution shape so it can later stand alone with:

```bash
hermes profile install ./agents/<name>
```

Ava is the worked example: started from `_template`, given a sharp content-editor register, two skills, two knowledge templates, and one daily hook cron. Do likewise, only with fewer darlings left alive.
