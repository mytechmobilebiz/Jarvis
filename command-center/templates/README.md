# Owner templates

Files here are mounted read-only into the worker at `/app/templates`.

- `proposal-template.html`: the website design proposal the Sales agent fills
  in (`{{CLIENT_NAME}}`, `{{CLIENT_COMPANY}}`, `{{CLIENT_EMAIL}}`,
  `{{PROPOSAL_DATE}}`, `{{VALID_UNTIL}}`). Without it, a plain built-in
  placeholder is used. Copy your own here; it is not committed to git.
