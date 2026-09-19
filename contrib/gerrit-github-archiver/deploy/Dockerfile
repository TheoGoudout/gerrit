# Gerrit 3.14 with the OAuth plugin baked in.
#
# The official image does not ship the oauth plugin, and Gerrit refuses to
# start when auth.type = OAUTH but no provider is installed, so it has to be
# present before the first boot.
#
# Pin the base image: "latest" would silently move you across minor versions,
# and Gerrit cannot skip versions on upgrade.
FROM gerritcodereview/gerrit:3.14.3

USER root

# Option A (reproducible, recommended): commit the jar next to this Dockerfile
# and copy it in. Fetch it once with:
#   curl -L -o oauth.jar \
#     'https://gerrit-ci.gerritforge.com/job/plugin-oauth-bazel-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/oauth/oauth.jar'
COPY --chown=gerrit:gerrit oauth.jar /var/gerrit/plugins/oauth.jar

# Option B (convenient, not reproducible): fetch at build time instead.
# lastSuccessfulBuild moves, so two builds of this Dockerfile can differ.
# ADD --chown=gerrit:gerrit \
#     https://gerrit-ci.gerritforge.com/job/plugin-oauth-bazel-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/oauth/oauth.jar \
#     /var/gerrit/plugins/oauth.jar

USER gerrit
