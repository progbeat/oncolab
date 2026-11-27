# oncolab

[![CI: passing](https://img.shields.io/github/actions/workflow/status/progbeat/oncolab/tests.yml?branch=master&event=push&label=CI)](https://github.com/progbeat/oncolab/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/progbeat/oncolab)](https://github.com/progbeat/oncolab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)

Run a terminal command on a fresh Google Colab VM, without writing a notebook.
`oncolab` creates the VM, runs your command, and normally releases the VM when it exits. A profile can
install tools and mount Google Drive before the command starts.

[Quick start](#quick-start) · [Run a command](#run-a-command) · [Configure the VM](#configure-the-vm) · [Troubleshooting](#troubleshooting)

![Live T4 run: oncolab prepares Git, FUSE, and rclone in parallel, then nvidia-smi reports a Tesla T4.](/../demo-media/demo.webp)

[Open the terminal demo](https://github.com/progbeat/oncolab/blob/demo-media/demo.webp)

## Quick start

On macOS or Linux, you need [uv](https://docs.astral.sh/uv/), OpenSSH, and a
Colab account.

1. If you don't already have the official Colab CLI, install it:
   `uv tool install google-colab-cli`.
2. If you're not signed in to Colab, run `colab sessions` and complete the Google
   sign-in. For the **Colab** connection you need a supported local SSH key, such
   as `~/.ssh/id_ed25519` with its matching `.pub`.
   [SSH setup and key selection](docs/configuration.md#ssh).
3. Install `oncolab` and run your first command:

   ```sh
   uv tool install oncolab
   oncolab echo hello
   ```

Expect `hello`. No config or Drive mount is needed.
To update later, run `uv tool upgrade oncolab`.
If you previously installed from a pinned Git URL, switch to PyPI once with
`uv tool uninstall oncolab` followed by `uv tool install oncolab`.

## Run a command

```sh
oncolab bash                              # interactive shell; exit when done
oncolab --gpu T4 nvidia-smi                # request a GPU, subject to availability
oncolab -- bash -c 'uname -m && pwd'       # evaluate the shell expression on Colab
oncolab python3 - < train.py              # send a standalone local script over stdin
```

Commands start in `/content`. Local files aren't copied automatically. Save
outputs to persistent storage before the VM is released.

Set `workdir` in your profile to change the VM
working directory, use `--copy` to stage files for the run, or use
`-e NAME=VALUE` to set variables for the command (`-e NAME` forwards an
exported local variable):

```sh
oncolab --gpu T4 --copy train.py --env MODE=train -- python3 train.py
```

Put `oncolab` flags before the
command. See `oncolab --help` for accelerator and memory options.
Use [`-p LOCAL_PORT:REMOTE_PORT`](docs/configuration.md#local-access-to-vm-ports)
to access a VM service from local loopback.

## Configure the VM

Pass `--config path/to/profile.toml`, or save your default profile as
`~/.config/oncolab/config.toml`. Start with the example that matches your task:

- [Install or prepare something before your command](examples/setup.toml): no Drive required.
- [Mount one Google Drive folder](examples/work.toml): create a
  [dedicated rclone config](docs/configuration.md#google-drive-mounts)
  and edit the source path.
- [Advanced profiles](docs/configuration.md#examples): multiple mounts with
  separate read/write settings, Git-on, or a wrapper that changes `HOME`.
- [Configuration showcase](examples/showcase.toml): task dependencies,
  file inputs, managed processes, and teardown. The `-v` / `--volume` comment
  describes [future local bind mounts](https://github.com/progbeat/oncolab/issues/4).

[Configuration reference](docs/configuration.md) ·
[Drive permissions and safety](docs/configuration.md#security-and-cleanup)

## Troubleshooting

- **Command not found:** run `uv tool update-shell` and reopen your terminal.
- **SSH fails:** check [key selection](docs/configuration.md#ssh); `oncolab`
  doesn't read `~/.ssh/config` for the Colab connection.
- **VM cleanup wasn't confirmed:** check `colab sessions`. If the VM is still
  there, use `colab stop -s <session>` only once you know no writes are pending.

[Development](docs/configuration.md#development) · Unofficial; not affiliated with Google.
