# Deploying Gerrit 3.14 on Coolify

Replaces the unauthenticated 3.3.0 instance. Read
[../GERRIT-SETUP.md](../GERRIT-SETUP.md) first for *why* this is a fresh
install rather than an upgrade.

## Which Gerrit is this?

Worth separating two things, because they are unrelated:

* **The source tree in this repository is Gerrit `master`** — unreleased
  development. `version.bzl` is not even tracked at HEAD; it is written at
  release time. Do **not** deploy a build of it.
* **Deploy `gerritcodereview/gerrit:3.14.3`**, the current stable release.

The archiver itself is version-agnostic: it is standalone Python that talks to
a deployed Gerrit over REST. This source tree was only ever used as reference
documentation, to check API behaviour against the implementation rather than
guessing from docs.

## Before you start

Register the GitHub OAuth App (GERRIT-SETUP.md §2). You need the client ID and
secret before the first boot, because Gerrit refuses to start with
`auth.type = OAUTH` and no provider configured.

Callback URL: `https://gerrit.goudout.com/oauth` — that exact path.

## 1. Fetch the plugin JAR

The official image does not ship the oauth plugin.

```bash
curl -L -o oauth.jar \
  'https://gerrit-ci.gerritforge.com/job/plugin-oauth-bazel-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/oauth/oauth.jar'

# Sanity check: must say 3.14.x
unzip -p oauth.jar META-INF/MANIFEST.MF | grep Gerrit-ApiVersion
```

Commit `oauth.jar` next to the `Dockerfile`. `lastSuccessfulBuild` is a moving
target, so fetching at build time makes two builds of the same Dockerfile
differ — the Dockerfile has that variant commented out if you prefer it.

The jar currently published reports `Gerrit-ApiVersion: 3.14.2-SNAPSHOT`
against a 3.14.3 image. That is fine: Gerrit reads `Gerrit-ApiVersion` for
reporting only (`ServerPlugin.java:166`, surfaced by `ListPlugins`) and does
not reject a mismatch.

## 2. Create the Coolify resource

New Resource → **Docker Compose**, pointed at the repository holding this
`deploy/` directory.

| Coolify setting | Value |
|---|---|
| Domain | `https://gerrit.goudout.com` |
| Port | `8080` |
| Build pack | Docker Compose |
| Base directory | the path to this `deploy/` folder |

Let Coolify manage the Traefik labels. Do not hand-write them.

**Do not point DNS at it yet** if you can avoid it — see §4 on why the first
login matters.

## 3. First boot

Deploy. The entrypoint initialises `/var/gerrit` on an empty site, creating
`All-Projects`, `All-Users` and a generated `serverId`. Watch the logs for
`Initialized /var/gerrit`; with cold volumes this takes a few minutes, which
is what the 300s `start_period` in the compose file is for.

At this point Gerrit is running with the image's default auth, **not** the
`DEVELOPMENT_BECOME_ANY_ACCOUNT` that the old instance had. Nobody can become
an arbitrary account. But nobody can usefully log in yet either.

## 4. Write the configuration

Open a terminal on the container from Coolify, then append the OAuth sections:

```bash
cat >> /var/gerrit/etc/gerrit.config <<'EOF'

[auth]
	type = OAUTH

[plugin "gerrit-oauth-provider-github-oauth"]
	root-url = https://github.com/
	client-id = YOUR_CLIENT_ID
EOF

cat > /var/gerrit/etc/secure.config <<'EOF'
[plugin "gerrit-oauth-provider-github-oauth"]
	client-secret = YOUR_CLIENT_SECRET
EOF

chmod 600 /var/gerrit/etc/secure.config
```

See `gerrit.config.template` and `secure.config.template` for the full
versions including the `sendemail` and `download` sections.

Leave everything `init` generated alone — `serverId` especially, since NoteDb
records reference it.

Restart the resource.

> **Then log in immediately.** The first account to authenticate is granted
> `ADMINISTRATE_SERVER` automatically (`AccountManager.java:410`). On a
> publicly resolvable host that is a race you want to win.

## 5. Verify

```bash
curl -s https://gerrit.goudout.com/config/server/info | tail -n +2 | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["auth"]["auth_type"])'
# expect: OAUTH

curl -s -o /dev/null -w '%{http_code}\n' https://gerrit.goudout.com/a/accounts/self
# expect: 401 -- but note this proves nothing on its own. It returns 401
# simply because no session cookie was sent, and it does so just as readily
# under DEVELOPMENT_BECOME_ANY_ACCOUNT, where anyone can get a session from
# the Become page and then this endpoint answers happily. Only the auth_type
# check above decides whether the instance is safe.

curl -s https://gerrit.goudout.com/config/server/version
# expect: "3.14.3"

# The oauth plugin must actually be loaded. A 404 here means the jar is not
# in /var/gerrit/plugins, and no amount of config will make OAUTH work.
curl -s -o /dev/null -w '%{http_code}\n' https://gerrit.goudout.com/oauth
# expect: anything but 404 (302 to GitHub once configured)
```

If `auth_type` still reads `DEVELOPMENT_BECOME_ANY_ACCOUNT`, the old instance
is still serving that hostname — check what the DNS record and the proxy point
at before assuming the deploy failed.

## Recovering a stock-image deployment

If you already deployed `gerritcodereview/gerrit` directly rather than building
the Dockerfile above, the oauth plugin is absent and `auth.type` is whatever
the site was initialised with. Fix the running container first, then make it
durable.

Gerrit's config files are git-config format and `git` is present in the image,
so edit them with `git config` rather than a heredoc. Appending a second
`[auth]` section would leave the original `type =` in place and Gerrit reads
the first one — a mistake that looks like the change silently doing nothing.

```bash
# What is actually persistent? Anything not listed here is lost on redeploy.
mount | grep /var/gerrit

ls -la /var/gerrit/plugins/
git config -f /var/gerrit/etc/gerrit.config --get auth.type

curl -fL -o /var/gerrit/plugins/oauth.jar \
  'https://gerrit-ci.gerritforge.com/job/plugin-oauth-bazel-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/oauth/oauth.jar'

P=plugin.gerrit-oauth-provider-github-oauth
git config -f /var/gerrit/etc/gerrit.config auth.type OAUTH
git config -f /var/gerrit/etc/gerrit.config $P.root-url 'https://github.com/'
git config -f /var/gerrit/etc/gerrit.config $P.client-id 'YOUR_CLIENT_ID'
git config -f /var/gerrit/etc/gerrit.config gerrit.canonicalWebUrl 'https://gerrit.goudout.com/'
git config -f /var/gerrit/etc/gerrit.config httpd.listenUrl 'proxy-http://*:8080/'
git config -f /var/gerrit/etc/secure.config $P.client-secret 'YOUR_CLIENT_SECRET'

chmod 600 /var/gerrit/etc/secure.config
chown gerrit:gerrit /var/gerrit/etc/secure.config /var/gerrit/plugins/oauth.jar
```

Restart from Coolify, verify per §5, and log in immediately.

Then check who else is already an administrator. While the instance was open
anyone could have created an account and joined that group; switching auth
does not remove accounts or their group memberships, it only stops them
logging in through the old route. **BROWSE → Groups → Administrators** —
remove anything you do not recognise.

Finally make it durable. Unless `mount` showed `/var/gerrit/plugins` and
`/var/gerrit/etc` as volumes, both edits live only in the container layer and
vanish on the next deploy. Switch to the Dockerfile and compose file in this
directory, which bake the jar into the image and mount `etc`.

## Things specific to this setup

**`proxy-http://`, not `http://`.** Coolify's Traefik terminates TLS, so
Gerrit must know it is behind a proxy. With plain `http://` it emits `http://`
redirects, the OAuth callback breaks, and clone URLs come out wrong. This is
the single most common way this deployment fails.

**SSH is not exposed.** Coolify's proxy handles HTTP. Port 29418 would need a
raw TCP mapping, and nothing here needs it: the archiver uses REST over HTTPS,
and `download.scheme = http` tells Gerrit to advertise clone URLs that
actually work. Add the mapping later if people want SSH push.

**All five volumes matter.** `git` and `db` hold the repositories and NoteDb —
the review history itself, and the thing layer B archives. `etc` holds
`serverId`. Losing `index` or `cache` only costs a reindex.

**Upgrades are one minor version at a time.** Pin the image tag. Moving from
`3.14.x` to `3.16.x` in one step will not work — Lucene does not support more
than one version jump — which is exactly how the old instance ended up
stranded on 3.3.

## Then

Continue with [../TESTING.md](../TESTING.md) §1 to create the archiver's
account and token, then §2 for the test repository.
