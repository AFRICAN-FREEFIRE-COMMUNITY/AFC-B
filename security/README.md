# security/ (owner rules R55 to R61)

This folder is this project's answer to the seven security rules. The checker is global:

    node ~/.claude/skills/security-rules/scripts/check-security.mjs             # all seven, this repo
    node ~/.claude/skills/security-rules/scripts/check-security.mjs --ledger    # compare with debt.json (R32)
    node ~/.claude/skills/security-rules/scripts/check-security.mjs --idor http://localhost:3000
    node ~/.claude/skills/security-rules/scripts/check-security.mjs --live http://localhost:3000

Files here:

- `billing-caps.md`: R60 register, one row per paid service, cap, alert, URL, verified date. Re-verify every 90 days.
- `idor-cases.json`: R58 runtime cases, user A creates and user B is refused. Fill with real endpoints.
- `debt.json`: the ledger. Written by `--baseline` once, then by `--ledger` whenever a count falls. A HIGH count that rises fails the run and the commit hook.

The project config is `../security-rules.json`. The rule text is `~/.claude/OWNER-RULES.md`, section D.
