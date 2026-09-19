#!/usr/bin/env bash
# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Layer B assumes GitHub accepts pushes into the refs/changes/* namespace.
# That is the one part of the design not confirmed by documentation, so test
# it before relying on it.
#
# Creates a few refs in a scratch repository you name, reports which
# namespaces were accepted, then deletes them again.
#
# Usage: ./check-github-ref-namespaces.sh <owner>/<scratch-repo>
#
# Use a repository you do not care about. The script only creates and removes
# the refs listed below, but do not point it at anything precious.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <owner>/<scratch-repo>" >&2
    exit 2
fi

REPO="$1"
: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a token with Contents: write on $REPO}"
REMOTE="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git"

# Namespaces to probe, in order of preference for layer B.
CANDIDATES=(
    "refs/changes/01/1/1"
    "refs/changes/01/1/meta"
    "refs/gerrit/changes/01/1/1"
    "refs/heads/gerrit-notedb/changes/01/1/1"
)

WORKDIR="$(mktemp -d)"
cleanup() {
    for ref in "${CREATED[@]:-}"; do
        [ -n "$ref" ] || continue
        git -C "$WORKDIR" push --quiet "$REMOTE" ":$ref" >/dev/null 2>&1 || true
    done
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

CREATED=()

git -C "$WORKDIR" init --quiet
git -C "$WORKDIR" -c user.email=probe@example.com -c user.name=probe \
    commit --quiet --allow-empty -m "ref namespace probe"
SHA="$(git -C "$WORKDIR" rev-parse HEAD)"

echo "probing ${REPO} with ${SHA:0:10}"
echo

ACCEPTED=()
for ref in "${CANDIDATES[@]}"; do
    printf '%-46s ' "$ref"
    if err="$(git -C "$WORKDIR" push --force "$REMOTE" "$SHA:$ref" 2>&1)"; then
        echo "ACCEPTED"
        ACCEPTED+=("$ref")
        CREATED+=("$ref")
    else
        echo "REJECTED"
        echo "$err" | sed 's/^/      /' | grep -iE 'remote|error|denied' | head -3 || true
    fi
done

echo
if printf '%s\n' "${ACCEPTED[@]:-}" | grep -q '^refs/changes/'; then
    echo "refs/changes/* works: use replication.config as shipped."
else
    echo "refs/changes/* was rejected. Remap the layer B refspec to a namespace"
    echo "that was accepted above, for example:"
    echo "    push = +refs/changes/*:refs/gerrit/changes/*"
    echo "The archive stays complete; only the ref names on GitHub differ."
fi
