# Releases

A change to the distributed Python code, package configuration or locked
dependencies includes one version increment in the same PR. Documentation,
examples, tests and CI-only changes keep the existing version.

1. Choose the next patch, minor or major version from the current `master`.
2. Update `[project].version` in `pyproject.toml` and run `uv lock`.
3. Replace `RELEASES.md` with the notes for this release:

   ```markdown
   # 0.2.0
   <!-- Bump: minor -->
   - Describe the change for users.
   ```

PR checks validate the version change and build the package without publishing.
Merge one version bump at a time. Before merging another package PR, update it
against current `master` and wait for a fresh `release-policy` check. Require that
up-to-date check in branch protection if more than one maintainer merges releases.
After merge, the same workflow publishes only after both supported Python
versions pass CI and both release distributions pass fresh installation
smoke tests. It
creates the new version tag at the tested commit and attaches the wheel,
source distribution and SHA-256 checksums to a draft before publishing.
After verifying that release is immutable, a separate job downloads and
verifies the published files, then uploads the same wheel and source
distribution to PyPI. No manual tag, manual upload, or stored PyPI token is
needed.

Keep GitHub Immutable Releases enabled and protect version tags from changes.
GitHub applies immutability at publication. The workflow verifies the result
after publishing; if the setting is off, this check alerts you but does not
prevent publication. If a run fails, inspect the release before rerunning it
at the same commit: a pre-existing tag is accepted only if it still points to that exact
commit. Never delete or move a published version tag. Published release notes
are preserved in GitHub Releases; `RELEASES.md` is just the current draft.

## One-time PyPI setup

Before merging the first PyPI-enabled release, open the repository's GitHub
**Settings → Environments** and create the `pypi` environment. Allow
deployments only from the `master` branch; do not allow tags. Configure
required reviewers if you want an approval gate.

On PyPI, add a [pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
with the following values. If the project already exists under your account,
add the publisher under that project's **Publishing** settings instead.

| Field | Value |
| --- | --- |
| PyPI project name | `oncolab` |
| GitHub owner | `progbeat` |
| Repository name | `oncolab` |
| Workflow filename | `tests.yml` |
| Environment name | `pypi` |

The first successful upload creates the project; a pending publisher does not
reserve the name. If PyPI upload fails after GitHub publication, fix the cause
and rerun the failed job on the original workflow run. It uses the already
published GitHub assets and skips identical files already present on PyPI.
If an existing file has a different digest, investigate rather than trying to
replace it. After the first upload, use `uv tool run --no-cache --from
"oncolab==<version>" oncolab --version` to check a fresh installation from PyPI.
