# 0.2.0
<!-- Bump: minor -->
- Copy local files and directories to Colab with repeatable `--copy`. In a profile, set `copy` on a named `[setup.<name>]` task. Independent tasks can run in parallel, and `needs` gates the entire task, including its copy.
- Set the VM working directory with `workdir` and share profile environment defaults via `[env]`. Override or forward variables for the command with Docker-style `-e` and `--env`.
- Compose setup tasks with streamed stdin inputs, managed processes with readiness probes, and dependency-ordered teardown.
- Mount Google Drive with `--mount source=gdrive:work,target=/mnt/work` or a named setup task. Use an explicit, dedicated rclone config; the entire file is sent to the VM. Use `-p` / `--publish` for local loopback access to a VM service.
