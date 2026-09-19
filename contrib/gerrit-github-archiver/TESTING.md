# Setting up Gerrit and exercising the archiver end to end

Written against `https://gerrit.goudout.com`, which reports Gerrit **3.3.0**,
NoteDb enabled, and two repositories (`All-Projects`, `All-Users`).

---

## 0. Two things to fix before the trial starts

### 0.1 The instance has no authentication

`GET /config/server/info` reports:

```json
"auth": { "auth_type": "DEVELOPMENT_BECOME_ANY_ACCOUNT" }
```

Gerrit's own documentation for that setting reads, in capitals:

> **DO NOT USE.** Only for use in a development environment. [...] a hyperlink
> titled `Become` appears [...] where they can enter the username of any
> existing user account, and immediately login as that account, **without any
> authentication taking place**.

The host answers on the public internet. Anyone who finds it can become any
account, including the administrator — create repositories, change access
rules, read everything. Nothing in this archiver design helps with that, and
every review the trial produces would be attributed to accounts anyone could
have assumed.

Fix this before anything else. Three options that work on 3.3:

**Reverse proxy does the authentication** — least moving parts:

```ini
[auth]
    type = HTTP
    httpHeader = X-Forwarded-User
[httpd]
    listenUrl = proxy-https://127.0.0.1:8080/
```

Then have nginx (or oauth2-proxy) authenticate and set `X-Forwarded-User`.
Gerrit must not be reachable except through the proxy, because anyone who can
reach it directly can set that header themselves.

**GitHub OAuth** — the natural fit here, since the team is already on GitHub,
and it makes Gerrit accounts line up with GitHub identities:

```ini
[auth]
    type = OAUTH
[plugin "gerrit-oauth-provider-github-oauth"]
    root-url = https://github.com
    client-id = <from your GitHub OAuth app>
    client-secret = <from your GitHub OAuth app>
```

Install the `gerrit-oauth-provider` plugin build matching 3.3.

**LDAP**, if you have a directory already.

Switching auth type rewrites how accounts are identified, so do it while the
instance is empty. Afterwards, restart Gerrit and confirm:

```bash
curl -s https://gerrit.goudout.com/config/server/info | tail -n +2 | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["auth"]["auth_type"])'
```

### 0.2 Gerrit 3.3.0 is from 2021 and is long out of support

Two consequences for a trial:

* The team would be evaluating a five-year-old Gerrit. Submit requirements,
  the attention set, the comment UI and the review flow have all moved on,
  so a negative verdict might be about 3.3 rather than about Gerrit.
* The `replication` and `webhooks` plugin configuration in this directory was
  written from current documentation. Check the 3.3 builds of both before
  relying on it — in particular whether `$site_path/etc/replication/` as a
  per-remote directory exists in that version, because the layout differs.

NoteDb is already enabled, so the upgrade path is the ordinary sequential one.
Upgrading before the trial is much cheaper than after.

---

## 1. Administrator account and archiver credentials

The first account to log in becomes an administrator. After switching auth
type, log in, then:

1. **Settings → HTTP Credentials → Generate Password.** This is the password
   the archiver and `git` use over HTTPS. (3.3 calls it an HTTP password;
   later versions replaced it with auth tokens.)
2. Note your username from **Settings → Profile**.

Sanity check — this must return your account, not 401:

```bash
curl -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
     https://gerrit.goudout.com/a/accounts/self | tail -n +2
```

Once real auth is on, drop `"anonymous": true` from the archiver config and
set `username` plus `GERRIT_TOKEN` instead.

---

## 2. Create the test repository

Via the UI: **BROWSE → Repositories → CREATE NEW**, tick *Create initial empty
commit*. Or over REST:

```bash
curl -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
     -X PUT -H 'Content-Type: application/json' \
     -d '{"description":"Archiver end-to-end test","create_empty_commit":true}' \
     https://gerrit.goudout.com/a/projects/archiver-test
```

The initial empty commit matters: without it there is no branch for changes to
target. On 3.3 the default branch is `master` unless `gerrit.defaultBranch`
says otherwise. Confirm, because every command below depends on it:

```bash
curl -s -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
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
curl -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
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
PARENT=$(curl -s -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
  "https://gerrit.goudout.com/a/changes/$CHANGE/comments" | tail -n +2 | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["src/calc.py"][0]["id"])')

curl -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
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
curl -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
     -X POST -H 'Content-Type: application/json' \
     "https://gerrit.goudout.com/a/changes/$CHANGE/revisions/current/review" \
     -d '{"message": "LGTM", "labels": {"Code-Review": 2}}'

curl -u "$GERRIT_USER:$GERRIT_HTTP_PASSWORD" \
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
