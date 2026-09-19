# Setting up Gerrit and exercising the archiver end to end

Assumes a Gerrit set up per [GERRIT-SETUP.md](GERRIT-SETUP.md) — 3.14 with
GitHub OAuth. Where 3.3 differs it is noted inline, since that is what
`gerrit.goudout.com` ran before the rebuild.

---

## 0. Prerequisite

This assumes a properly authenticated Gerrit. If you are still on the 3.3.0
instance with `auth_type: DEVELOPMENT_BECOME_ANY_ACCOUNT`, stop and work
through [GERRIT-SETUP.md](GERRIT-SETUP.md) first — on that instance anyone on
the internet can become any account, so nothing produced here would mean
anything.

---

## 1. Administrator account and archiver credentials

The first account to log in becomes an administrator (see GERRIT-SETUP.md
§5). Then:

1. **Settings → Auth Tokens** and create one. This is what the archiver and
   `git` use over HTTPS. On 3.11 and earlier this was instead
   **Settings → HTTP Credentials → Generate Password**.
2. Note your username from **Settings → Profile**.

Sanity check — this must return your account, not 401:

```bash
curl -u "$GERRIT_USER:$GERRIT_TOKEN" \
     https://gerrit.goudout.com/a/accounts/self | tail -n +2
```

Drop `"anonymous": true` from the archiver config and set `username` plus
`GERRIT_TOKEN` instead. Give the archiver its own account rather than reusing
yours; it only ever issues GETs.

---

## 2. Create the test repository

Via the UI: **BROWSE → Repositories → CREATE NEW**, tick *Create initial empty
commit*. Or over REST:

```bash
curl -u "$GERRIT_USER:$GERRIT_TOKEN" \
     -X PUT -H 'Content-Type: application/json' \
     -d '{"description":"Archiver end-to-end test","create_empty_commit":true}' \
     https://gerrit.goudout.com/a/projects/archiver-test
```

The initial empty commit matters: without it there is no branch for changes to
target. Confirm the branch name before going further, because every command below
depends on it — the default differs between Gerrit versions and is settable
with `gerrit.defaultBranch`:

```bash
curl -s -u "$GERRIT_USER:$GERRIT_TOKEN" \
     https://gerrit.goudout.com/a/projects/archiver-test/branches/ | tail -n +2
```

---

## 3. Grant yourself review rights

**Repositories → archiver-test → Access → Edit**, and on `refs/*` give
`Registered Users`:

| Permission | Range | Why |
|---|---|---|
| Push | — | to create changes at `refs/for/*` |
| Label Code-Review | -2 .. +2 | `+2` is needed to submit |
| Submit | — | to merge the change |

`+2` on your own change is fine in Gerrit — unlike GitHub, which refuses to
let you approve your own pull request. That asymmetry is why the archiver has
the bot open the pull requests.

---

## 4. Clone and push a change

```bash
git clone https://gerrit.goudout.com/a/archiver-test
cd archiver-test

# Without this hook every push is rejected for a missing Change-Id.
curl -Lo .git/hooks/commit-msg https://gerrit.goudout.com/tools/hooks/commit-msg
chmod +x .git/hooks/commit-msg
```

Create a file with enough lines to comment on — you need distinct lines for
single-line, multi-line and reply comments:

```bash
mkdir -p src
cat > src/calc.py <<'EOF'
"""Small module used to exercise the archiver."""


def add(a, b):
    return a + b


def divide(a, b):
    # No zero check here on purpose: something to comment on.
    result = a / b
    return result


def main():
    print(add(2, 3))
    print(divide(10, 2))
EOF

git add src/calc.py
git commit -m "Add a small calculator module

Exercises the Gerrit to GitHub archiver end to end."
git push origin HEAD:refs/for/master
```

The push prints the change URL. Keep the number:

```bash
CHANGE=<the number from the push output>
```

---

## 5. Post comments that exercise every path

This is the point of the exercise: each row below drives a different branch of
the projection.

| Comment | Exercises |
|---|---|
| single line in a changed file | GitHub review comment |
| multi-line range | `start_line` / `start_side` mapping |
| `/COMMIT_MSG` | issue-comment fallback |
| `/PATCHSET_LEVEL` | issue-comment fallback |
| a reply | `in_reply_to` (currently flattened — a known gap) |
| unresolved | the unresolved note in the body |

One request posts all of them:

```bash
curl -u "$GERRIT_USER:$GERRIT_TOKEN" \
     -X POST -H 'Content-Type: application/json' \
     "https://gerrit.goudout.com/a/changes/$CHANGE/revisions/current/review" \
     -d '{
  "message": "First pass. A few things.",
  "labels": {"Code-Review": -1},
  "comments": {
    "src/calc.py": [
      {"line": 9, "message": "divide by zero is unhandled here", "unresolved": true},
      {"range": {"start_line": 8, "start_character": 0,
                 "end_line": 11, "end_character": 0},
       "message": "this whole block could raise; wrap it"}
    ],
    "/COMMIT_MSG": [
      {"line": 7, "message": "mention why the module exists"}
    ],
    "/PATCHSET_LEVEL": [
      {"message": "overall this looks fine, one real issue"}
    ]
  }
}'
```

For the reply, fetch a comment UUID and answer it:

```bash
PARENT=$(curl -s -u "$GERRIT_USER:$GERRIT_TOKEN" \
  "https://gerrit.goudout.com/a/changes/$CHANGE/comments" | tail -n +2 | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["src/calc.py"][0]["id"])')

curl -u "$GERRIT_USER:$GERRIT_TOKEN" \
     -X POST -H 'Content-Type: application/json' \
     "https://gerrit.goudout.com/a/changes/$CHANGE/revisions/current/review" \
     -d "{\"message\": \"Replying inline.\",
          \"comments\": {\"src/calc.py\": [
            {\"line\": 9, \"in_reply_to\": \"$PARENT\",
             \"message\": \"good catch, fixing in the next patch set\"}]}}"
```

---

## 6. Second patch set, then submit

The second patch set is what exercises the force-push, and shows you what
GitHub does to comments on superseded patch sets — it marks them outdated and
folds them away. That is the deliberate trade discussed in the README.

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path("src/calc.py")
p.write_text(p.read_text().replace(
    "    # No zero check here on purpose: something to comment on.\n    result = a / b",
    "    if b == 0:\n        raise ValueError(\"division by zero\")\n    result = a / b"))
EOF

git commit -a --amend --no-edit    # keeps the Change-Id, so this is patch set 2
git push origin HEAD:refs/for/master
```

Then approve and submit:

```bash
curl -u "$GERRIT_USER:$GERRIT_TOKEN" \
     -X POST -H 'Content-Type: application/json' \
     "https://gerrit.goudout.com/a/changes/$CHANGE/revisions/current/review" \
     -d '{"message": "LGTM", "labels": {"Code-Review": 2}}'

curl -u "$GERRIT_USER:$GERRIT_TOKEN" \
     -X POST -H 'Content-Type: application/json' \
     "https://gerrit.goudout.com/a/changes/$CHANGE/submit"
```

---

## 7. Run the archiver

**Dry run first.** Nothing is written; this confirms the change is visible and
in scope:

```bash
python3 -m gerrit_github_archiver -c config.json --dry-run -v sweep
# expect: inspected=1 ... skipped=1   (dry-run counts as skipped)
```

**Then for real, against a repository you do not care about.** Create an empty
GitHub repo, say `TheoGoudout/archiver-scratch`, and point `projects[].github`
at it. The base branch must exist there, so push the Gerrit branch once:

```bash
git push https://github.com/TheoGoudout/archiver-scratch.git HEAD:master
python3 -m gerrit_github_archiver -c config.json -v sweep
```

Check, in the scratch repository:

- [ ] one pull request per Gerrit change
- [ ] each Gerrit review is one GitHub review, in order
- [ ] inline comments land on the right lines, attributed by name in the body
- [ ] the multi-line comment spans the right range
- [ ] `/COMMIT_MSG` and `/PATCHSET_LEVEL` arrive as issue comments quoting
      their source
- [ ] the reply appears (flattened, not threaded — the known gap)
- [ ] patch-set-1 comments are marked outdated after the force-push
- [ ] the pull request is closed, and reads **Merged** if the archiver got
      there before replication advanced the base branch

**Then run it a second time.** Nothing should change: no new pull request, no
duplicated comments. That is the property the whole ledger design exists for,
and it is the single most important thing to confirm by hand.

```bash
python3 -m gerrit_github_archiver -c config.json -v sweep
# expect: unchanged=1, and nothing new in the repository
```
