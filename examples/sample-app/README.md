# Sample App

This Express todos app is Tom's repair demo. It starts healthy, can be given a small realistic bug, and can be restored to its pristine state.

## Buyer Demo

1. Start from the pristine app:

   ```bash
   npm ci
   npm test
   ```

2. Seed the bug:

   ```bash
   npm run seed-bug
   ```

3. Confirm the customer symptom:

   ```bash
   npm test
   ```

   The tests should fail because `/todos` returns only completed todos, which means the two open onboarding tasks disappear.

4. Tell Tom in `#dev` to fix it by pasting `TICKET.md`.

5. Tom's expected workflow is: post a short plan, work on a `tom/<taskid>` branch, run the repo tests, and open a PR whose proof is that the tests are green.

To reset the app after the demo:

```bash
npm run heal
```
