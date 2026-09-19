# Layers A and B: replication configuration

These are configuration, not code. Together they are what actually guarantees
no review history is lost — the archiver in the parent directory (layer C) is
only a readable view on top.

Set these up **first**. Once they are running the trial is safe regardless of
whether layer C ever ships.

| | Destination | Carries | Purpose |
|---|---|---|---|
| **A** `github-mirror` | public repo | `refs/heads/*`, `refs/tags/*` | the read-only GitHub instance |
| **B** `github-archive` | **private** repo | `refs/changes/*` + branches/tags | lossless NoteDb review record |
| **B** `github-archive-allusers` | **private** repo | `refs/users/*`, groups | identity for the above |

## Install

```bash
cp replication.config   $site_path/etc/replication.config
cp secure.config.example $site_path/etc/secure.config   # then edit
chmod 600 $site_path/etc/secure.config
ssh -p 29418 gerrit-host gerrit plugin reload replication
```

Edit `myorg`, `myorg-archive` and `myproject` throughout. Create the three
GitHub repositories up front — `createMissingRepositories` is off because
GitHub cannot create a repository by being pushed to.

Watch `$site_path/logs/replication_log` for the first pushes.

## Why layer B exists

Gerrit's review history is already a git repository. `refs/changes/XX/YYYY/meta`
is a commit chain whose tree is a NoteMap of JSON: every comment, character
range, vote, reviewer change and status transition. `refs/changes/XX/YYYY/N`
is each patch set. Replicating those is a lossless archive for one `git push`
— no API calls, no rate limits, no tokens, and none of the fidelity losses
layer C accepts when it squeezes Gerrit's model into GitHub's.

`All-Users` is not optional. NoteDb deliberately pseudonymises comment authors
as `Gerrit User 1000042` (`ChangeNoteUtil.GERRIT_USER_TEMPLATE`); real names
and emails live in `refs/users/*`. Replicate the project repositories alone
and you get a complete archive in which nobody has a name.

## Four things that will bite you

**`mirror = true` would delete the archive pull requests.** It removes remote
refs absent locally, and `+refs/heads/*:refs/heads/*` covers everything under
`refs/heads/`. Layer C pushes its PR heads to `refs/heads/gerrit-archive/<n>`,
which do not exist in Gerrit — so enabling mirror on the `github-mirror`
remote deletes them on the next pass and breaks every archived diff. It
defaults to false. Leave it there.

**`replicatePermissions` defaults to *true*.** That replicates
`refs/meta/config` — your project ACLs — to the remote. Explicitly false on
the public mirror.

**Never replicate `refs/draft-comments/*`.** Those are unpublished comments.
Archiving them would disclose private notes their authors chose not to
publish. The All-Users refspecs are deliberately enumerated rather than
wildcarded for this reason. Be equally careful with
`refs/meta/external-ids`, which is left commented out.

**`$site_path/etc/replication/` as a directory silently disables this file.**
If that directory exists, every `remote` section in `replication.config` is
ignored. Use one form or the other.

## The one unverified assumption

GitHub documents that `refs/pull/*` is rejected as a hidden ref, and it stores
but no longer displays `refs/notes/*`. Whether it accepts `refs/changes/*` is
not documented either way. Test it before trusting layer B:

```bash
GITHUB_TOKEN=... ./check-github-ref-namespaces.sh myorg/scratch-repo
```

The script probes each candidate namespace against a scratch repository and
cleans up after itself. If `refs/changes/*` is rejected, remap the destination
side of the refspec — the archive stays complete, only the names change:

```
push = +refs/changes/*:refs/gerrit/changes/*
```

## Interaction with layer C

`replicationDelay` on `github-mirror` is set to 120s, longer than the
archiver's 60s poll. The archiver wants its pull request to exist *before* the
merge commit reaches the mirror's default branch: GitHub then sees the head
become reachable from the base and marks the PR **Merged** by itself. If
replication wins that race the change is still archived, but the PR reads
**Closed** with a synthetic base pinned at the parent commit.

The cost is a mirror that lags by up to two minutes, which is fine for a
read-only mirror. If you would rather the mirror be prompt, lower the delay
and accept "Closed" badges.

## Restoring from the archive

The archive repositories are ordinary git. To inspect a change offline:

```bash
git clone https://github.com/myorg-archive/myproject.git
git fetch origin '+refs/changes/*:refs/changes/*'
git log refs/changes/42/1042/meta          # the review history
git cat-file -p refs/changes/42/1042/meta  # footers: votes, reviewers, status
```

The comment bodies are JSON in the meta ref's tree. Account ids resolve
against the All-Users archive:

```bash
git show refs/users/42/1000042:account.config
```
