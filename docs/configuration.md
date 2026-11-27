# oncolab reference

## Command execution

Commands start in the profile's `workdir` (`/content` by default). A remote terminal is allocated when both stdin and
stdout are terminals. Put `oncolab` options before the command; `--` explicitly
marks the boundary. The remaining arguments are passed to the command. Use
`bash -c '…'` when you want shell syntax evaluated on the VM.

## SSH

The public installer uses HTTPS; no GitHub SSH key is needed. The connection
to Colab does not read `~/.ssh/config`. `oncolab`
looks for `~/.ssh/id_ed25519.pub` first, then `~/.ssh/id_ecdsa.pub`, and uses the
matching identity if found. Otherwise it falls back to the official CLI's
selection. For a new key, see the [Colab CLI SSH guide](https://github.com/googlecolab/google-colab-cli/blob/main/docs/06_ssh_access.md).

`-A` forwards your SSH agent; leave it off unless the command needs it and you
trust the VM and the software running there. The official Colab CLI must be
available as `colab` on PATH. Version 0.8.0 was used to test this release; the
dependency is not pinned.

## Profiles

No profile is loaded from the current project automatically. The default is
`$XDG_CONFIG_HOME/oncolab/config.toml` (usually
`~/.config/oncolab/config.toml`); `--config` selects a file explicitly.
Profiles can execute code. Use only profiles and scripts you trust.

### Working directory and environment

```toml
workdir = "/content/project"

[env]
MODE = "train"
```

Put `workdir` before any table header. It must be an absolute path on the VM
filesystem. It is created if needed and is the starting directory for setup
and the command, before any `cd` in the entrypoint. The `[env]` table sets
literal string values for setup, teardown, the entrypoint, and the command. Use
`-e NAME=VALUE` (or `--env NAME=VALUE`) to set an explicit value. With
`-e NAME`, `oncolab` takes the value from the exported local environment;
if it is absent locally, the variable is unset for the command even if the
profile or VM supplied it. CLI overrides apply to the entrypoint and command,
not to setup. `-e NAME=` sets an empty value. Repeat the option
as needed; the last occurrence wins. Forward only values you intend to
expose to the remote command. Avoid putting secrets in command-line values
or plaintext profiles.

### Copy local files

Use `[[copy]]` for files or directories needed on every run of a profile:

```toml
workdir = "/content/project"

[[copy]]
local = "train.py"

[[copy]]
local = "data.csv"
remote = "inputs/"
```

Profile sources are relative to the TOML file. Omit `remote` to copy into
`workdir` under the source basename. CLI copies add to profile copies: they
do not replace them, and overlapping destinations are rejected.

For one-off copies, repeat `--copy` as needed:

```sh
oncolab --copy train.py -- python3 train.py
oncolab --copy ./project:code -- ls code
oncolab --copy data.csv:/tmp/input.csv -- wc -l /tmp/input.csv
```

Relative CLI sources resolve from the current local directory. Without a
destination, copy under the source basename into the VM working directory.
A relative destination is resolved against the same VM directory. A trailing
slash sets a containing directory; otherwise the destination is the exact new
file or directory path. If a local name contains a colon, pass separate literal
paths, for example `--copy-to 'train:today.py' ./` to copy it into the working
directory. Destinations must not exist or overlap a Drive mount.
Directories include hidden files; symlinks and other special entries are
rejected rather than followed. Copy only trusted local paths and keep the
source tree unchanged during upload. Changes on the VM are not copied back.

Copy streams over the same SSH connection while independent setup runs.
If a setup step uses these globally copied files, add `needs = ["copy"]` to wait for
all uploads. If a copy supplies the working directory or one of its parents,
setup commands also wait for it so they start in the configured working
directory. The final command starts only after copy and setup succeed.

### Setup tasks

Name each task under `[setup.<name>]`. The optional `description` is displayed
during setup. `needs` lists task names. Dependencies gate the whole task,
including any file copy; independent tasks run concurrently and declaration
order is irrelevant. Unknown dependencies and cycles are rejected before
allocation.

Use `run` for a command that must exit successfully, or `start` for a process
managed through the end of the session. An optional `ready` probe on a
started process is retried until it succeeds or times out. Dependencies and
the main command wait for readiness; the probe is not a continuous health
check. Use a foreground process with `start`.

For `run`, `start`, `ready`, and `teardown`, a string runs in Bash with
`-e -o pipefail`; an array passes exact executable arguments without a
shell. Shell exports in one command do not carry to another.

```toml
[setup.install]
copy = "install.sh:-"
run = ["bash", "-e", "-o", "pipefail", "-s"]

[setup.input]
needs = ["install"]
copy = { source = "input.txt", target = "data/input.txt", chmod = 0o644 }
```

Task-local `copy` accepts the same short path form as `--copy`, a table with
`source`, optional `target` and optional `chmod`, or a list of these. Sources
are relative to the profile; relative VM targets use `workdir`. A task can
only copy, or copy before its `run` or `start`. The special target `-`
feeds one file into that task's stdin instead of copying to its destination
filesystem. It does not consume the main command's input.

An optional `teardown` runs for a task that began execution, even if its
setup stopped partway through. It runs before releasing direct or transitive
dependencies. If teardown fails, its dependencies and the VM stay running.
Legacy setup commands can still use `depends` and `script`.

### Google Drive mounts

1. Create a dedicated config, for example with
   `rclone --config ./rclone-colab.conf config`, and add the Google Drive
   remotes you want to use on the VM. Choose OAuth scopes that grant only the
   access you need. `drive.file` is limited to files the app can access; use
   `drive.readonly` for read-only access to existing files.
2. Copy [the one-folder profile](../examples/work.toml) to `work.toml`, substitute
   your remote and folder, and run `oncolab --config work.toml -- ls /mnt/work`.
3. To write through a mount, use `readonly = false` (the default) **and** a remote
   whose OAuth grant permits writing to the intended files.

Declare `mount` as a named setup task so other tasks can wait for it with
`needs`. For a one-off mount, keep `[rclone].config` in your profile and use:

```sh
oncolab --mount source=gdrive:work,target=/content/work -- ls /content/work
```

`source` uses rclone's `remote:path` notation. The part before the colon
names a section in the selected rclone config. One session can mount paths
from several named remotes in that file. If a source or target contains a
literal comma, declare the mount in TOML instead of using `--mount`.
Targets must be empty,
non-overlapping directories below `/content`, `/mnt`, `/home`, or `/root`.
The tool exports `ONCOLAB_MOUNT_<NAME>` to setup and the command (uppercased, with
hyphens replaced by underscores). A mount named `work` also sets `COLAB_DRIVE`.

`[rclone].config` explicitly selects the file to send. Relative paths are
resolved beside the TOML profile. All mounts in the session share a single
config. There is no default rclone config lookup or section filtering.
VFS cache settings use rclone defaults.

If you're upgrading a profile with `[mounts.<name>]`, move each mount to
`[setup.<name>].mount`, use `readonly` instead of `read_only`, and refer to
its task by name in `needs`. Review the dedicated config before selecting
it: the previous version sent only the selected sections.

### Local access to VM ports

Use repeatable `-p LOCAL_PORT:REMOTE_PORT` (or `--publish`) or set a
top-level `ports = ["8080:8765"]` before any table headers. The mapping in
that example makes a service listening on the VM's `127.0.0.1:8765`
available at `127.0.0.1:8080` on the local machine. Ports are TCP, bind to
local loopback only, and are released when the session ends. This is not a
public URL.

### Entrypoint

An optional top-level `entrypoint` array prefixes the command arguments, like
[Docker's exec form](https://docs.docker.com/reference/dockerfile/#entrypoint).
Put it before the first TOML table. For example:

```toml
entrypoint = ["sh", "-c", 'export MY_VAR=value; exec "$@"', "oncolab"]
```

The last item becomes the shell's `$0`. A wrapper script can instead be
installed by setup and invoked as `entrypoint = ["/path/to/runner"]`; finish
the wrapper with `exec "$@"`. A wrapper can switch users, but default root-owned
FUSE mounts are not accessible to the new UID. Changing only `HOME` does not
change the UID.

## Examples

| Profile | What it does |
| --- | --- |
| [setup.toml](../examples/setup.toml) | Runs two ordered steps without Drive. Try `oncolab --config examples/setup.toml -- cat /tmp/oncolab-sample/value` from the checkout. |
| [work.toml](../examples/work.toml) | Mounts one Drive folder read-only. |
| [drive.toml](../examples/drive.toml) | Mounts two folders with distinct read/write settings. |
| [git-on.toml](../examples/git-on.toml) | Prepares Git and Git-on alongside a Drive mount, as in the demo. Adapt the mount and review the install commands before use. |
| [home.toml](../examples/home.toml) | Creates a directory and sets `HOME` for your command. |
| [showcase.toml](../examples/showcase.toml) | Demonstrates the configuration format, including per-task copies, a managed process, readiness, and teardown. The input files are illustrative and not bundled. |

## Security and cleanup

- A read-only mount limits filesystem operations, not the credential.
  Use a separately authorized read-only remote if the credential must be
  read-only. Use a trusted VM and trusted setup; avoid credentials in TOML,
  command arguments, or setup commands.
- The entire selected config travels byte for byte over SSH stdin and is
  staged in a private RAM-backed directory. Use a dedicated config and do
  not include credentials that should not be sent to the VM. After mounting,
  rclone's config save path is
  set to `/dev/null`, and the staging directory is removed before the
  entrypoint or command. The daemon stays alive to serve FUSE. Removing the
  Unix socket blocks new pathname connections, not already open ones.
- SSH normally trusts the authenticated Colab bridge. If you have independently
  verified host keys, set `[ssh].known_hosts` to a local file that pins
  `colab-runtime`. Do not use a key copied from an unverified connection.
- On a failed unmount or ambiguous SSH disconnect after command start,
  `oncolab` does not force-stop the VM. Check `colab sessions` rather than
  assuming it stopped.

## Development

```sh
uv --no-config sync --locked
uv --no-config run --locked python -m unittest discover -s tests -v
uv build
```
