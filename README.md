# oncolab

[![CI: passing](https://img.shields.io/github/actions/workflow/status/progbeat/oncolab/tests.yml?branch=master&event=push&label=CI)](https://github.com/progbeat/oncolab/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/progbeat/oncolab)](https://github.com/progbeat/oncolab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://github.com/progbeat/oncolab/blob/master/pyproject.toml)

Run a terminal command on a fresh Google Colab VM, without writing a notebook.
`oncolab` creates the VM, runs your command, and normally releases the VM when it exits. A profile can
install tools and mount Google Drive before the command starts.

[Quick start](https://github.com/progbeat/oncolab/blob/master/README.md#quick-start) · [Run a command](https://github.com/progbeat/oncolab/blob/master/README.md#run-a-command) · [Configure the VM](https://github.com/progbeat/oncolab/blob/master/README.md#configure-the-vm) · [Troubleshooting](https://github.com/progbeat/oncolab/blob/master/README.md#troubleshooting)

![Live T4 run: oncolab prepares Git, FUSE, and rclone in parallel, then nvidia-smi reports a Tesla T4.](https://raw.githubusercontent.com/progbeat/oncolab/demo-media/demo.webp)

[Open the terminal demo](https://github.com/progbeat/oncolab/blob/demo-media/demo.webp)

## Quick start

On macOS or Linux, you need [uv](https://docs.astral.sh/uv/), OpenSSH, and a
Colab account.

1. If you don't already have the official Colab CLI, install it:
   `uv tool install google-colab-cli`.
2. If you're not signed in to Colab, run `colab sessions` and complete the Google
   sign-in. For the **Colab** connection you need a supported local SSH key, such
   as `~/.ssh/id_ed25519` with its matching `.pub`.
   [SSH setup and key selection](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#ssh).
3. Install `oncolab` and run your first command:

   ```sh
   uv tool install oncolab
   oncolab echo hello
   ```

Expect `hello`. No config or Drive mount is needed.
To update later, run `uv tool upgrade oncolab`.

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
Use [`-p LOCAL_PORT:REMOTE_PORT`](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#local-access-to-vm-ports)
to access a VM service from local loopback.

## Configure the VM

Pass `--config path/to/profile.toml`, or save your default profile as
`~/.config/oncolab/config.toml`. Start with the example that matches your task:

- [Install or prepare something before your command](https://github.com/progbeat/oncolab/blob/master/examples/setup.toml): no Drive required.
- [Mount one Google Drive folder](https://github.com/progbeat/oncolab/blob/master/examples/work.toml): use your existing
  rclone config, or [install rclone and run `rclone config`](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#google-drive-mounts)
  once to create one. oncolab installs rclone and FUSE on the VM if needed;
  it does not require rclone locally once you have a config. Use an unencrypted
  file unless rclone can unlock it noninteractively on the VM. Local password
  helpers and `-e RCLONE_CONFIG_PASS` do not configure the mount.
- [Advanced profiles](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#examples): multiple mounts with
  separate read/write settings, Git-on, or a wrapper that changes `HOME`.
- [Configuration showcase](https://github.com/progbeat/oncolab/blob/master/examples/showcase.toml): task dependencies,
  file inputs, managed processes, and teardown. The `-v` / `--volume` comment
  describes [future local bind mounts](https://github.com/progbeat/oncolab/issues/4).

[Configuration reference](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md) ·
[Drive permissions and safety](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#security-and-cleanup)

## Troubleshooting

- If Colab needs credentials, run `colab sessions` in a terminal and complete sign-in. `oncolab`
  does not hold your command's input open for a hidden login prompt.
- Creating a VM can take several attempts. The progress view shows the current attempt and, on a
  transient failure, keeps the attempt number alongside the retry countdown and reason. Timers use
  one decimal and switch from seconds to minutes, hours, and days as they grow. Use
  `--attempts N` to set the total (default 4), `--attempts 1` to disable retries, or
  `--attempts 0` to keep retrying transient failures until Ctrl+C. A rejected accelerator, session limit, or rate limit ends that run with a
  distinct error. Temporary connection errors can be retried during either the lookup or the
  allocation request. On a retryable response, a bounded run reports server holdoffs longer than two
  minutes and exits rather than retrying early. With `--attempts 0`, it shows the countdown and
  waits for that holdoff to finish or for Ctrl+C. The existing `COLAB_CLI_REQUEST_TIMEOUT`,
  `COLAB_CLI_RETRY_BACKOFF`, and `COLAB_CLI_ASSIGN_RETRIES` environment settings still apply;
  `--attempts` overrides the last one.
- Interactive preparation is transient and disappears before the command starts. If OpenSSH may
  need a key passphrase or password, it owns the terminal during authentication. With redirected
  stdout there is no live display. `oncolab` explains its own failures separately, without
  adding a completion message for the remote command. Use `--debug` for internal diagnostic
  locations without raw credential data. The main command's output is unchanged. Configured setup
  commands report captured output only on failure, service and readiness output stays quiet, and
  teardown output goes to stderr.
- **Command not found:** run `uv tool update-shell` and reopen your terminal.
- **SSH fails:** check [key selection](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#ssh); `oncolab`
  doesn't read `~/.ssh/config` for the Colab connection. If OpenSSH may need to
  ask for a passphrase, its prompt takes precedence over the live SSH timer.
- **VM cleanup wasn't confirmed:** check the VM endpoint in `colab sessions`.
  The temporary SSH session name isn't saved in the default Colab CLI config.
  Use Colab's Manage sessions view to stop that VM only once you know no writes are pending.

[Development](https://github.com/progbeat/oncolab/blob/master/docs/configuration.md#development) · Unofficial; not affiliated with Google.


### Experimental parallel setup

Set `ONCOLAB_PARALLEL_SETUP=1` to start setup on the first SSH channel, upload the Drive
configuration concurrently with eligible setup tasks, and open the command's own SSH
channel without taking over its stdin. The command waits for setup; the first runner keeps
the services alive and owns cleanup. Without the opt-in, SSH startup is unchanged.
