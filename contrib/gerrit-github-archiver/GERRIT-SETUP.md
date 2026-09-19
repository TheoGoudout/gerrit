# Standing up Gerrit properly for the trial

Target: **Gerrit 3.14** (current stable) with **GitHub OAuth**, replacing the
3.3.0 instance at `gerrit.goudout.com` that currently runs with no
authentication.

---

## Do not upgrade. Rebuild.

You chose "upgrade first", which is the right instinct — but for this instance
an upgrade is the wrong mechanism, and it is worth knowing why before you
spend a weekend on it.

**Gerrit cannot jump versions.** From the 3.10 release notes:

> the upgrade is supported only from Gerrit v3.9, as Lucene libraries do not
> support more than one version upgrade at a time

That constraint repeats at each step, so 3.3 → 3.14 is roughly eleven
sequential upgrades — 3.3 → 3.4 → 3.5 → … → 3.14 — several of them requiring
an **offline** reindex of every index. It is a multi-day job.

**And you have nothing to migrate.** The instance holds:

* two repositories, both of which Gerrit creates for itself
  (`All-Projects`, `All-Users`)
* zero changes, in any state
* whatever accounts exist, all of which anyone on the internet could have
  created or assumed anyway, given the auth type

So the eleven-hop upgrade would carefully preserve nothing. **Install 3.14
fresh instead.** Minutes rather than days, and the result is identical.

The one thing worth checking before you discard the old site: if you set up
project ACLs, groups or account records you care about, they live in
`All-Projects` and `All-Users`. Clone both first and keep them somewhere —
they are ordinary git repositories.

```bash
git clone https://gerrit.goudout.com/All-Projects.git old-all-projects.git
git clone https://gerrit.goudout.com/All-Users.git   old-all-users.git
```

---

## How the instance ended up open

Almost certainly `init --dev`. In Gerrit's own source, `InitAuth.java:84`:

```java
flags.dev ? AuthType.DEVELOPMENT_BECOME_ANY_ACCOUNT : AuthType.OPENID
```

`--dev` is documented as "Setup site with default options suitable for
developers", and one of those defaults is authentication that lets anyone log
in as anyone. It is fine on a laptop and catastrophic on a public host.

**Never pass `--dev` to a site that will be reachable.** That is the single
change that prevents a repeat.

---

> Deploying with Docker or Coolify instead of a tarball? Use
> [deploy/COOLIFY.md](deploy/COOLIFY.md), which covers the same ground
> with the container specifics, and skip to §2 below for the OAuth App.

## 1. Install Gerrit 3.14

Java: 3.13 dropped Java 17, so plan on **Java 21 or later**; confirm against
the [3.14 release page](https://www.gerritcodereview.com/3.14.html).

```bash
java -version                       # expect 21+
wget https://gerrit-releases.storage.googleapis.com/gerrit-3.14.0.war

# Note: no --dev. That is the whole point.
java -jar gerrit-3.14.0.war init -d /srv/gerrit-new --batch --no-auto-start
```

`--batch` accepts defaults without prompting and leaves `auth.type` at the
safe default rather than the development one. `--no-auto-start` keeps it down
until the configuration below is in place.

---

## 2. Register the GitHub OAuth App

On GitHub: **Settings → Developer settings → OAuth Apps → New OAuth App**.

| Field | Value |
|---|---|
| Application name | anything, e.g. `Gerrit (goudout.com)` |
| Homepage URL | `https://gerrit.goudout.com` |
| **Authorization callback URL** | `https://gerrit.goudout.com/oauth` |

The callback path is `/oauth` exactly. Getting it wrong produces a redirect
error at login and nothing more informative.

Keep the client ID and generate a client secret.

---

## 3. Install the oauth plugin

The plugin is an official Gerrit plugin
([`plugins/oauth`](https://gerrit.googlesource.com/plugins/oauth)). Take the
JAR built for **3.14** — a JAR for the wrong Gerrit version will fail to load.

```bash
cp oauth.jar /srv/gerrit-new/plugins/
```

---

## 4. Configure

`/srv/gerrit-new/etc/gerrit.config`:

```ini
[gerrit]
    canonicalWebUrl = https://gerrit.goudout.com/

[auth]
    type = OAUTH

[plugin "gerrit-oauth-provider-github-oauth"]
    root-url = https://github.com/
    client-id = <your client id>

[httpd]
    ; If Gerrit sits behind nginx or similar. Drop the proxy- prefix and use
    ; https://*:8443/ if Gerrit terminates TLS itself.
    listenUrl = proxy-https://127.0.0.1:8080/

[sendemail]
    ; Gerrit sends review mail; set this up or turn it off deliberately.
    enable = false
```

The secret goes in `/srv/gerrit-new/etc/secure.config`, not in
`gerrit.config`:

```ini
[plugin "gerrit-oauth-provider-github-oauth"]
    client-secret = <your client secret>
```

```bash
chmod 600 /srv/gerrit-new/etc/secure.config
```

Note the section name is `gerrit-oauth-provider-github-oauth`, which reads
oddly but is correct — it is inherited from the plugin's origins. If the
section name is wrong the plugin will not find its configuration and Gerrit
refuses to start, which at least fails loudly.

Then start it:

```bash
/srv/gerrit-new/bin/gerrit.sh start
```

---

## 5. Claim the administrator account

**The first account to log in becomes the administrator.** From
`AccountManager.java:410`:

```java
if (isFirstAccount) {
  // This is the first user account on our site. Assume this user
  // is going to be the site's administrator [...]
```

So log in yourself, immediately, before anyone else reaches it. Then confirm
you are in `Administrators` under **BROWSE → Groups**.

---

## 6. Verify before going further

The auth type must no longer be the development one:

```bash
curl -s https://gerrit.goudout.com/config/server/info | tail -n +2 | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["auth"]["auth_type"])'
# expect: OAUTH
```

And the unauthenticated API must now refuse you:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://gerrit.goudout.com/a/accounts/self'
# expect: 401
```

If either still shows the old behaviour, the old instance is still serving —
check what the proxy points at.

---

## 7. Create the archiver's credential

On 3.12 and later Gerrit replaced HTTP passwords with **authentication
tokens**. So on 3.14 this is **Settings → Auth Tokens**, not the "HTTP
Credentials → Generate Password" flow that 3.3 had.

Or over REST, once logged in:

```bash
curl -u "$USER:$EXISTING_TOKEN" -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"id":"archiver","lifetime":"365d"}' \
  https://gerrit.goudout.com/a/accounts/self/tokens/archiver
```

Give the archiver its **own account**, not yours. It only ever issues GETs, so
it needs no more than read access, and a separate account keeps its activity
distinguishable in the logs.

Then drop `"anonymous": true` from the archiver config and use:

```json
"gerrit": {
  "url": "https://gerrit.goudout.com",
  "username": "archiver",
  "git_url": "https://gerrit.goudout.com/a"
}
```

with `GERRIT_TOKEN` in the environment.

---

## 8. Re-check the plugin configs

With 3.14 rather than 3.3, the `replication/` and `webhooks.config.example`
files in this directory now match the documentation they were written from.
The 3.3 caveat in `TESTING.md` no longer applies.

---

Once this is done, continue with [TESTING.md](TESTING.md) §2 to create the
test repository and the change.
