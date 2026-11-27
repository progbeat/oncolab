#!/usr/bin/env bash
set -euo pipefail

version=$1
tag=v$version
repo=$GITHUB_REPOSITORY
wheel="oncolab-$version-py3-none-any.whl"
sdist="oncolab-$version.tar.gz"

if [[ $(gh api "repos/$repo/git/ref/tags/$tag" --jq .object.sha) != "$GITHUB_SHA" ]]; then
    echo "$tag does not point at the tested commit" >&2
    exit 1
fi
if [[ $(gh release view "$tag" --repo "$repo" --json isDraft,isImmutable --jq '.isImmutable and (.isDraft | not)') != true ]]; then
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
if ! "${checksum[@]}" -- "$wheel" "$sdist" | cmp -s - sha256.sum; then
    echo "$tag distribution checksums do not match" >&2
    exit 1
fi
