# Playbook: Give Tom his hands with Claude Code (~15 minutes)

Tom's coding lane uses Claude Code directly, logged in with the owner's Claude subscription. That is first-party use and allowed. JARVIS can also think with your Claude plan through the setup wizard, using Claude Sonnet 5 with the full staff toolset.

## 1. Install Claude Code

Use Anthropic's native installer:

```sh
curl -fsSL https://claude.ai/install.sh | bash
```

Or Homebrew:

```sh
brew install --cask claude-code
```

Then run:

```sh
claude
```

Follow the browser login with the owner's Claude Pro, Max, Team, Enterprise, or Console account.

## 2. Verify Claude Code

```sh
claude -p "say ok"
```

The reply should be `ok` or close enough. If `claude` is missing, open a new terminal and try again before changing anything else.

## 3. Install GitHub CLI

```sh
brew install gh
gh auth login --web --scopes repo
gh auth status
```

Use the GitHub account that is allowed to push branches and open pull requests for the product repo.

## 4. Set git identity

```sh
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Tom's guardrails live in `agents/tom/`: work in a worktree, open a pull request, never commit directly to `main`. Branch code is reversible; production and main are not his playground.

## 5. Confirm Tom's local tools

There is no API token to paste for Claude Code. Tom's coding lane needs this machine to have:

| Check | Command |
|---|---|
| Claude Code works | `claude -p "say ok"` |
| GitHub CLI is logged in | `gh auth status` |
| Git identity is set | `git config --global user.email` |

If any of those fail, fix them before asking Tom to code.

## 6. Register a repository

Clone the owner's repository into a local working folder:

```sh
gh repo clone OWNER/REPO
```

Add its name, absolute local path, and GitHub URL to `knowledge/tom/repos.md` in the JARVIS profile. Tom uses that file to find his repositories.

## Windows

In PowerShell, install Git and GitHub CLI, then open a new terminal:

```powershell
winget install Git.Git
winget install GitHub.cli
```

Run `gh auth login --web --scopes repo`, set your git identity as above, then follow the same clone and `knowledge/tom/repos.md` steps.

## Verify

For the sample app demo:

```sh
cd examples/sample-app
npm ci
npm run seed-bug
```

Then hand Tom `TICKET.md` in `#dev` and watch for the pull request.

## Rotation & housekeeping

If the owner's Claude account changes, run `claude logout` and `claude` to log in again. If GitHub access changes, run `gh auth logout` and `gh auth login --web --scopes repo`. Keep Tom on worktrees and PRs; if he ever touches `main`, stop and fix the guardrail before giving him more work.
