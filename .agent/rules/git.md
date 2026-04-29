## Git Identity

When committing and pushing to GitHub, use the ralphzilla bot identity:

```bash
env GIT_AUTHOR_NAME="ralphzilla[bot]" \
  GIT_AUTHOR_EMAIL="280339145+ralphzilla[bot]@users.noreply.github.com" \
  GIT_COMMITTER_NAME="ralphzilla[bot]" \
  GIT_COMMITTER_EMAIL="280339145+ralphzilla[bot]@users.noreply.github.com" \
  git commit --no-verify -m "message"
```

The bot identity ensures commits appear as authored by `ralphzilla[bot]` on GitHub, not by the human owner. This allows the human owner to approve PRs created by the bot under branch protection rules.
