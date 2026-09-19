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
| `in_reply_to` | **not mapped yet** | Replies post as top-level comments |
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

# Continuous, sweep only.
python3 -m gerrit_github_archiver -c config.json run

# Continuous, sweep plus the webhook receiver (see webhooks.config.example).
export WEBHOOK_TOKEN=...
python3 -m gerrit_github_archiver -c config.json serve

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

## The webhook receiver

`serve` adds an HTTP listener for Gerrit's `webhooks` plugin on top of the
sweep. It is **latency only**: the plugin retries `maxTries` times and then
drops the event permanently, so the sweep still runs and still decides what is
correct. Never run the receiver alone.

Two things follow from how the plugin behaves:

*The handler must answer fast.* Anything slower than the retry budget turns a
delivery into a permanent loss. The handler authenticates, parses, enqueues and
returns `202` without touching Gerrit or GitHub.

*The body is a trigger, not a payload.* `CommentAddedEvent` carries the change
message and approvals but no inline comments, so the worker re-reads the change
from Gerrit rather than trusting what arrived.

Work is queued per change and **coalesced**: a burst of events for one change
collapses into one projection, and a change already in flight is never handed
to a second worker — if more events arrive meanwhile it is re-queued exactly
once. On overflow the queue sheds load and lets the sweep catch up.

### Authentication

The plugin cannot sign payloads and cannot send custom headers, so the only
credential available is one embedded in the configured URL. The receiver
accepts a shared secret as HTTP basic auth, an `X-Archiver-Token` header, or a
`?token=` query parameter, compared with `hmac.compare_digest`.

Note that the plugin's `sslVerify` defaults to **false**. Keep the receiver on
loopback or a private network, or set `sslVerify = true` on the Gerrit side —
otherwise the credential is exposed to anyone who can intercept the connection.

## Validated against real Gerrit data

The mapping has been run against a live change on
`gerrit-review.googlesource.com` (change 631582: 87 change messages, 45
comments across 6 paths). All 45 comments were covered with none lost, the 57
autogenerated messages were skipped, multi-line ranges mapped to
`start_line`/`start_side` correctly, and the two `/PATCHSET_LEVEL` comments
routed to the issue-comment fallback. Comment `path` is absent from the
payload because it is the map key, which is why it is injected during
grouping.

Not yet run against a live GitHub repository.

## Not yet implemented

- Comment threading. Real reviews use `in_reply_to` heavily (26 of 45
  comments on the sample change); replies currently post as top-level review
  comments, so thread structure is flattened.
- Deleting archive branches after a PR closes (`refs/pull/<n>/head` persists,
  so this is safe to add).
- Un-publishing a change that turns private after it was archived. The API
  cannot undo disclosure, so the archiver logs at ERROR and flags it for a
  human instead of failing quietly.
