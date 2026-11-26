# On Colab

[![Tests](https://github.com/progbeat/oncolab/actions/workflows/tests.yml/badge.svg)](https://github.com/progbeat/oncolab/actions/workflows/tests.yml)
[![Release](https://img.shields.io/badge/release-v0.1.0-blue)](https://github.com/progbeat/oncolab/releases/tag/v0.1.0)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://github.com/progbeat/oncolab/blob/main/pyproject.toml)

Run a terminal command on a fresh Google Colab VM. On Colab provisions the VM, sets up your environment, and releases the VM when the command exits. It runs ordinary commands, scripts already on the VM, or an interactive shell. You do not need to write a notebook or wrap your command in Python.

[Installation](#installation) · [Usage](#usage) · [Configuration](#configuration) · [Security](#security-and-cleanup)

## Installation

You need [uv](https://docs.astral.sh/uv/), OpenSSH, a Colab account, and read access to this private repository through GitHub SSH. Install the official Colab CLI alongside On Colab:

```sh
uv tool install google-colab-cli
uv tool install 'git+ssh://git@github.com/progbeat/oncolab.git@v0.1.0'
oncolab --version
```

If the tools aren't on your PATH, run `uv tool update-shell`, then reopen your terminal. On the first run, follow the Colab CLI’s sign-in prompt if it appears. Install [rclone](https://rclone.org/install/) locally only if you want Drive mounts.

## Usage

```sh
oncolab echo hello                      # smoke test on a fresh CPU VM
oncolab bash                            # interactive shell; exit to release the VM
oncolab --gpu T4 python3                 # interactive Python on a T4, if available
oncolab -- bash -c 'uname -m && pwd'      # a shell expression, evaluated on the VM
printf 'print(123)\n' | oncolab python3   # piped stdin; stdout is command output
```

Commands start in `/content`. A terminal is allocated when both stdin and stdout are terminals. Put On Colab options before your command; everything after the command name is passed to it. Use `--` to mark the boundary explicitly. `-A` opts in to SSH agent forwarding; use it only when you trust the VM and its setup. Run `oncolab --help` for accelerator and memory options.

## Configuration

No config is required. On Colab reads `$XDG_CONFIG_HOME/oncolab/config.toml` (normally `~/.config/oncolab/config.toml`) or the exact path passed to `--config`. It never discovers setup from the current project. Configs are trusted local executable configuration, not files to accept from strangers.

```toml
[rclone]
config = "~/.config/rclone/rclone.conf"  # optional; default is rclone's path
allow_full_drive = false

[mounts.work]
source = "gdrive:work"
target = "/content/work"
read_only = false

[mounts.reference]
source = "reference:archive"
target = "/mnt/reference"
read_only = true

[setup.packages]
description = "Installing Polars…"
run = ["uv", "pip", "install", "--system", "polars"]

[setup.inputs]
description = "Checking inputs…"
run = 'test -d "$ONCOLAB_MOUNT_WORK/datasets"'
depends = ["mounts.work"]
```

Each mount can override `config`. Use distinct remote names if two config files define different remotes. Only direct Google Drive OAuth remotes are supported in v0.1. Targets must be empty directories under `/content`, `/mnt`, `/home`, or `/root` and cannot overlap. Each mount exports `ONCOLAB_MOUNT_<NAME>` (uppercase, hyphens converted to underscores); the mount named `work` also exports `COLAB_DRIVE`. The VFS cache stays at the rclone default.

Use an argv array for an exact command, a string for Bash with `-e -o pipefail`, or `script = "./setup.sh"` to run the contents of a local file with those Bash options. Relative local paths resolve beside the TOML. `depends` lists setup names or `mounts.<name>`. Independent steps run in parallel. Unknown dependencies and cycles fail **before** allocation. Setup exports do not reach other steps or the final command.

Try an example without changing your default config (run these from a checkout):

```sh
oncolab --config examples/setup.toml -- cat /tmp/oncolab-sample/value
oncolab --config examples/home.toml -- printenv HOME
```

For Drive, first create your own direct OAuth remotes with `rclone config`. Copy [the two-mount example](examples/drive.toml), edit the remote names and paths, and select it with `--config`. See [the Git-on profile](examples/git-on.toml) if you want Colab to prepare Git and Git-on. Leave those setup steps out if you don't need them.

### Entrypoint

Like [Docker’s exec-form ENTRYPOINT](https://docs.docker.com/reference/dockerfile/#entrypoint), the optional top-level `entrypoint` is an argv prefix. Put it before the first TOML table header. With `oncolab -- python3 -i`, this config runs `env HOME=/content/my-home python3 -i`:

```toml
entrypoint = ["env", "HOME=/content/my-home"]

[setup.home]
run = ["mkdir", "-p", "/content/my-home"]
```

For a wrapper script installed by setup, use `entrypoint = ["/path/to/runner"]` and finish the wrapper with `exec "$@"`. To inline a shell wrapper, use `entrypoint = ["sh", "-c", 'export MY_VAR=value; exec "$@"', "oncolab"]`; the last element is the shell's `$0`, not part of the user command. A wrapper can use `runuser` to change UID, but root-owned FUSE mounts remain owner-only by default. Use HOME-only changes if that process needs those mounts; On Colab does not enable cross-user mount access.

## Security and cleanup

- Only selected rclone sections travel over SSH stdin. They are staged in a private RAM-backed directory; the daemon switches to in-memory configuration and clears the staging file. Encrypted configs and FIFO loading are not part of v0.1. Never embed credentials in setup, CLI arguments, or the TOML.
- The rclone management interface is a Unix socket inside that private directory, with no TCP listener. Its pathname is removed after the mount phase, **before the entrypoint or user command**. That blocks new connections by pathname, not connections already open. Setup and the VM itself must be trusted. The daemon stays alive to serve FUSE.
- `read_only` controls access through that mount, not the OAuth grant. Use a separately authorized least-privilege remote if the credential itself must be read-only. The configured scope is checked, but that value is not proof of what an existing OAuth token was granted.
- SSH normally trusts the authenticated Colab bridge, as the official CLI does. For independently verified host keys, set `[ssh]` with `known_hosts = "/path/to/known_hosts"` containing the trusted key for `colab-runtime`. Do not trust a key copied from an unverified connection.
- If unmount fails, the VM remains running to avoid interrupting writes. An ambiguous disconnect after the remote command is attempted does not force-stop the VM. Use `colab sessions` to verify its status.
- This is an unofficial project and is not affiliated with Google.

## Development

```sh
uv --no-config sync --locked
uv --no-config run --locked python -m unittest discover -s tests -v
uv build
```
