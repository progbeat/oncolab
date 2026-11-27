# oncolab reference

## Command execution

Commands start in `/content`. A remote terminal is allocated when both stdin and
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

### Setup steps

Under `[setup.<name>]`, set exactly one of:

- `run = ["command", "arg"]` to pass exact arguments.
- `run = 'command | other_command'` to run Bash with `-e -o pipefail`.
- `script = "./setup.sh"` to run the contents of a local file with those Bash
  options. Relative paths are resolved beside the profile.

`description` is optional and appears during setup. `depends` lists setup names
or `mounts.<name>` prerequisites. Independent steps run in parallel; unknown
dependencies and cycles fail before a VM is allocated. Shell exports in setup
do not carry into the final command; use `entrypoint` for that.

### Google Drive mounts

1. Install [rclone](https://rclone.org/install/) locally and create a direct
   Google Drive OAuth remote with `rclone config`. For reading existing files,
   select the `drive.readonly` scope. `drive.file` is limited to files the app
   can access; it isn't a way to list any existing folder. Full Drive scope is
   rejected by default. To use an existing full-scope remote deliberately,
   set `allow_full_drive = true` under `[rclone]`.
2. Copy [the one-folder profile](../examples/work.toml) to `work.toml`, substitute
   your remote and folder, and run `oncolab --config work.toml -- ls /mnt/work`.
3. To write through a mount, use `read_only = false` (the default) **and** a remote
   whose OAuth grant permits writing to the intended files.

Each `[mounts.<name>]` has `source` and `target`. Targets must be empty,
non-overlapping directories below `/content`, `/mnt`, `/home`, or `/root`.
The tool exports `ONCOLAB_MOUNT_<NAME>` to setup and the command (uppercased, with
hyphens replaced by underscores). A mount named `work` also sets `COLAB_DRIVE`.

`[rclone].config` overrides the local rclone config path, and a mount can override
it with its own `config`. Use distinct remote names for different configs.
The tool supports plaintext configs and direct Google Drive OAuth remotes;
it does not forward service-account credentials. VFS cache settings use
rclone defaults.

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

## Security and cleanup

- A read-only mount limits filesystem operations, not the OAuth token.
  Use a separately authorized read-only remote if the credential itself must
  be read-only. A config's scope string is not proof of a token's actual grant.
  Use a trusted VM and trusted setup; avoid credentials in TOML, arguments or
  setup commands.
- Only selected rclone sections travel over SSH stdin. They are staged in a
  private RAM-backed directory. After mounting, rclone's config save path is
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
