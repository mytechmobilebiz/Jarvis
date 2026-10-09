# Ticket: Fresh demo accounts show no todos

> handoff
> A buyer reports that the sample app's `/todos` endpoint returns no onboarding tasks after the demo bug is seeded. Please inspect the sample app, restore the missing todos, add or keep a regression test, and open a PR with the smallest safe fix.

Tom, this came through support during the buyer walkthrough. It is not a production incident, but it is the sort of demo papercut that makes a tidy product look as though it was assembled in a corridor.

## Symptom

After seeding the bug, `GET /todos` returns an empty list. A fresh demo account should show two onboarding tasks:

- Wire Sarah to the support queue
- Ask Tom to inspect the demo workflow

## Repro

```bash
cd examples/sample-app
npm run seed-bug
npm test
```

Expected: both seeded todos are returned and tests pass.

Actual: the todos response is empty and the regression test fails.

## Notes

The likely area is `src/server.js`, but please verify rather than taking the hint too literally. The fix should make `npm test` green and leave unrelated files alone.
