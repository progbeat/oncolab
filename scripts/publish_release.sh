#!/usr/bin/env bash
set -euo pipefail

version=$1
tag=v$version
repo=$GITHUB_REPOSITORY
sha=$GITHUB_SHA
assets=("dist/oncolab-$version-py3-none-any.whl" "dist/oncolab-$version.tar.gz" dist/sha256.sum)
for asset in "${assets[@]}"; do test -s "$asset"; done
notes=$(mktemp)
trap 'rm -f "$notes"' EXIT
tail -n +3 RELEASES.md > "$notes"

# Read the actual ref rather than accepting gh's default target branch.
ref="repos/$repo/git/ref/tags/$tag"
if ! existing=$(gh api "$ref" --jq .object.sha 2>/dev/null); then
    # If another run won the race, require the same commit below anyway.
    if ! gh api --method POST "repos/$repo/git/refs" -f "ref=refs/tags/$tag" -f "sha=$sha" --silent; then
        echo "Could not create $tag; checking for a same-commit retry" >&2
    fi
    existing=$(gh api "$ref" --jq .object.sha)
fi
if [[ "$existing" != "$sha" ]]; then
    echo "$tag already exists on a different commit; refusing to move it" >&2
    exit 1
fi

if ! gh release view "$tag" --repo "$repo" --json isDraft,isImmutable >/dev/null 2>&1; then
    gh release create "$tag" --repo "$repo" --verify-tag --draft --title "$tag" --notes-file "$notes"
fi
if [[ $(gh release view "$tag" --repo "$repo" --json isDraft --jq .isDraft) == true ]]; then
    gh release upload "$tag" "${assets[@]}" --repo "$repo" --clobber
    gh release edit "$tag" --repo "$repo" --draft=false
fi
test "$(gh release view "$tag" --repo "$repo" --json isImmutable --jq .isImmutable)" = true
expected=$(printf '%s\n' "${assets[@]##*/}" | sort)
actual=$(gh release view "$tag" --repo "$repo" --json assets --jq '.assets[].name' | sort)
test "$actual" = "$expected"
