# Local git in the shell

Prefer the **GitHub MCP tools** (`create_or_update_file`, `push_files`,
`create_branch`, `create_pull_request`, `merge_pull_request`, …) for repo
changes — they commit/branch/PR through the GitHub API, attributed to the
connected account, with no local clone. Use the shell's `git` only when you
genuinely need a local working tree (run a build/formatter/tests then commit,
resolve conflicts, complex history ops).

When you do use local `git`, it's already wired up for you:

- **Push/clone are pre-authenticated.** The platform signs `git` and `gh` in
  with your connected GitHub account, so `git clone`/`fetch`/`push` against
  `github.com` and `gh` commands just work — no `gh auth login` or
  `gh auth setup-git` needed.
- **Set the commit identity once** before your first local commit (the sandbox
  has no default), derived from your account:
  ```bash
  login=$(gh api user --jq .login)
  git config --global user.name  "$login"
  git config --global user.email "$login@users.noreply.github.com"
  ```
  (PowerShell: `$login = gh api user --jq .login; git config --global user.name $login; git config --global user.email "$login@users.noreply.github.com"`)
  The `@users.noreply.github.com` email keeps your real address private while
  still linking the commit to your GitHub account.
- **An installation identity** (an agent whose GitHub identity is a GitHub App
  installation): `gh api user` answers 403, since the token is the app's, not
  a person's. Set the identity to the app's bot user instead:
  ```bash
  slug=$(gh api app --jq .slug 2>/dev/null || echo otodock)
  git config --global user.name  "${slug}[bot]"
  git config --global user.email "${slug}[bot]@users.noreply.github.com"
  ```
  `gh api installation/repositories` lists what the token reaches.
- **It's overridable.** If the user asks to commit under a different name/email,
  just set those with `git config` (global, or `--local` inside a repo) — it's
  plain git config, nothing is locked.
