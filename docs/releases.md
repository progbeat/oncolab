# Releases

A change to the distributed Python code, package configuration or locked
dependencies includes one version increment in the same PR. Documentation,
examples, tests and CI-only changes keep the existing version.

1. Choose the next patch, minor or major version from the current `main`.
2. Update `[project].version` in `pyproject.toml` and run `uv lock`.
3. Replace `RELEASES.md` with the notes for this release:

   ```markdown
   # 0.2.0
   <!-- Bump: minor -->
   - Describe the change for users.
   ```

PR checks validate the version change and build the package without publishing.
Merge one version bump at a time. Before merging another package PR, update it
against current `main` and wait for a fresh `release-policy` check. Require that
up-to-date check in branch protection if more than one maintainer merges releases.
After merge, the same workflow publishes only after both supported Python
versions pass CI and the built wheel passes an installation smoke test. It
creates the new version tag at the tested commit and attaches the wheel,
source distribution and SHA-256 checksums to a draft before publishing.
No manual tag, separate release PR or PyPI upload is needed.

Keep GitHub Immutable Releases enabled. Set the `IMMUTABLE_RELEASES_READ_TOKEN`
repository Actions secret to a fine-grained token scoped only to this repository
with **Administration: read**. The workflow uses that token only to verify
the setting immediately before publishing the draft. If verification fails,
the release stays a draft. The built-in Actions token still handles publication.

If a run fails, rerun it at the same
commit: a pre-existing tag is accepted only if it still points to that exact
commit. Never delete or move a published version tag. Published release notes
are preserved in GitHub Releases; `RELEASES.md` is just the current draft.
