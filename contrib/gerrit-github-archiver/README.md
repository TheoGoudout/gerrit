# gerrit-github-archiver

Projects Gerrit changes onto GitHub pull requests so the review record
survives a migration away from Gerrit.

This is **layer C** of the archive design: the human-readable view. It is not
the durable record. Layers A and B — replicating `refs/heads/*` to the GitHub
mirror and `refs/changes/*` plus `All-Users` to a private archive repository —
are pure `replication` plugin configuration and are what actually guarantee
nothing is lost. They live in [`replication/`](replication/). Set those up
first; this tool is a convenience layer on top.

## Design

**The reconciler is the correctness mechanism, not the webhook.** Gerrit's
`webhooks` plugin retries a handful of times and then drops the event
permanently: no persistence, no acknowledgement, no replay. A webhook-only
design silently loses review history whenever this service is restarted. So
this tool re-derives the desired state from Gerrit on a timer and converges
the GitHub side. It is correct with webhooks disabled entirely; a webhook
receiver, when added, is only a latency optimisation that calls the same
projector.

**Every GitHub write is gated on a durable ledger** (SQLite), keyed by Gerrit
comment UUID and change-message id. Re-running is a no-op. To close the crash
window between a successful GitHub write and the corresponding ledger write,
every projected body carries a hidden provenance marker
(`<!-- gga:comment:<uuid> -->`); on startup the projector adopts pre-existing
GitHub items back into the ledger, so even a lost ledger does not duplicate
the archive.

**Message and comment state are tracked separately.** A review whose body
posted but whose comments did not is resumed, not skipped.

**Attribution is textual, not actorship.** Comments are posted by one bot
account with `**Alice Smith <a@x.com>** · 2026-03-14 09:22 UTC` in the body.
GitHub actorship degrades to `ghost` when an account is deleted and breaks on
rename; text survives both. It also means no per-user GitHub tokens, so a
compromise of this service cannot write to anyone's account.

**Patch sets force-push the archive head.** The final head SHA therefore
equals the commit that lands on the target branch, which is what makes GitHub
associate the merge commit with the pull request — and in turn what makes the
review reachable from `git blame`. The cost is that comments on earlier patch
sets are marked outdated by GitHub. Layer B preserves the true per-patch-set
anchoring regardless.

## Mapping and its losses

| Gerrit | GitHub | Note |
|---|---|---|
| Change message (a review action) | One review | Votes appear as body text |
| Inline comment in a diff hunk | Review comment | |
| Comment outside a hunk | Issue comment | GitHub rejects these inline with 422 |
| `/COMMIT_MSG`, `/PATCHSET_LEVEL` | Issue comment | No GitHub equivalent |
| File-level comment | Issue comment | |
| Character range | Line range | Character precision is lost |
| `side: PARENT` | `LEFT` | |
| `unresolved` | Body text | Thread resolution needs `contents: write` |
| Work in progress | Draft PR | |
| Abandoned | Closed PR | |
| Merged | Closed by GitHub itself | Once replication advances the base branch |

Unanchorable comments quote `context_lines` from the Gerrit API, so the source
the reviewer was looking at is preserved even though the anchor is not.

## Usage

```bash
pip install -r requirements.txt
export GERRIT_TOKEN=...   # Gerrit HTTP auth token (read-only use)
export GITHUB_TOKEN=...   # bot token: contents:write, pull_requests:write

# See what it would do, touching nothing.
python3 -m gerrit_github_archiver -c config.json --dry-run sweep

# One reconciliation pass.
python3 -m gerrit_github_archiver -c config.json sweep

# Continuous.
python3 -m gerrit_github_archiver -c config.json run

# Cutover: re-inspect every change, ignoring the lookback window.
python3 -m gerrit_github_archiver -c config.json sweep --full
```

Run `sweep --full` once when the trial ends so that changes still open at
cutover are archived too.

## Safety

- The Gerrit client is read-only; the archiver never writes to Gerrit.
- Private changes are excluded twice over — in the Gerrit query and again in
  the projector — so a visibility mistake needs two independent failures.
- Draft comments are never fetched: they are unpublished by definition.
- Credentials embedded in push URLs are redacted from logs.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The suite covers the mapping logic and drives the projector against in-memory
doubles. The properties worth keeping green: projecting twice leaves exactly
one pull request and one copy of every comment; a wiped ledger recovers via
markers rather than duplicating; and a review interrupted partway through
resumes.

## Not yet implemented

- Webhook receiver (`webhooks` plugin -> HTTP) for sub-minute latency.
- Deleting archive branches after a PR closes (`refs/pull/<n>/head` persists,
  so this is safe to add).
- Per-change locking; the sweep is single-threaded, which is sufficient at
  trial volume but would need revisiting alongside a webhook path.
