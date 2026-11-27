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
expected=$(printf '%s\n' "${assets[@]##*/}" | LC_ALL=C sort)
verify_assets() {
    local actual
    actual=$(gh release view "$tag" --repo "$repo" --json assets --jq '.assets[].name' | LC_ALL=C sort)
    if [[ "$actual" != "$expected" ]]; then
        echo "$tag does not contain exactly the expected assets" >&2
        return 1
    fi
}

if [[ $(gh release view "$tag" --repo "$repo" --json isDraft --jq .isDraft) == true ]]; then
    gh release upload "$tag" "${assets[@]}" --repo "$repo" --clobber
    verify_assets
    gh release edit "$tag" --repo "$repo" --draft=false
fi
if [[ "$(gh release view "$tag" --repo "$repo" --json isImmutable --jq .isImmutable)" != true ]]; then
    echo "GitHub did not confirm $tag is immutable after publication; check the repository’s Immutable Releases setting" >&2
    exit 1
fi
verify_assets
