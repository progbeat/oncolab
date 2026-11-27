#!/usr/bin/env bash
set -euo pipefail

version=$1
expected_digest=${2:-}
tag=v$version
repo=$GITHUB_REPOSITORY
wheel="oncolab-$version-py3-none-any.whl"
sdist="oncolab-$version.tar.gz"

if [[ $expected_digest == --recover ]]; then
    # Only an absent published release (or a draft) calls for a fresh build.
    if ! state=$(gh api "repos/$repo/releases/tags/$tag" \
        --jq 'if .draft then "draft" elif .immutable then "immutable" else "mutable" end' 2>&1); then
        if [[ $state == *'(HTTP 404)' ]]; then
            exit 3
        fi
        printf '%s\n' "$state" >&2
        exit 1
    fi
    if [[ $state == draft ]]; then
        exit 3
    fi
    if [[ $state != immutable ]]; then
        echo "$tag is not a published immutable release" >&2
        exit 1
    fi
elif [[ ! $expected_digest =~ ^[a-f0-9]{64}$ ]]; then
    echo "Missing or invalid checksum manifest digest from the release job" >&2
    exit 1
fi
if [[ $(gh api "repos/$repo/git/ref/tags/$tag" --jq .object.sha) != "$GITHUB_SHA" ]]; then
    echo "$tag does not point at the tested commit" >&2
    exit 1
fi
if [[ $expected_digest != --recover ]] &&
    [[ $(gh release view "$tag" --repo "$repo" --json isDraft,isImmutable --jq '.isImmutable and (.isDraft | not)') != true ]]; then
    echo "$tag is not a published immutable release" >&2
    exit 1
fi

mkdir dist
gh release download "$tag" --repo "$repo" --dir dist \
    --pattern "$wheel" --pattern "$sdist" --pattern sha256.sum
cd dist
if command -v sha256sum >/dev/null; then
    checksum=(sha256sum)
else
    checksum=(shasum -a 256)
fi
if [[ $expected_digest != --recover ]]; then
    manifest=$("${checksum[@]}" -- sha256.sum)
    if [[ ${manifest%% *} != "$expected_digest" ]]; then
        echo "$tag checksum manifest does not match the smoke-tested distributions" >&2
        exit 1
    fi
fi
if ! "${checksum[@]}" -- "$wheel" "$sdist" | cmp -s - sha256.sum; then
    echo "$tag distribution checksums do not match" >&2
    exit 1
fi
