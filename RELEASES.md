# 0.2.0
<!-- Bump: minor -->
- Copy local files and directories to Colab with repeatable `--copy`. In a profile, set `copy` on a named `[setup.<name>]` task. Independent tasks can run in parallel, and `needs` gates the entire task, including its copy.
- Set the VM working directory with `workdir` and share profile environment defaults via `[env]`. Override or forward variables for the command with Docker-style `-e` and `--env`.
- Compose setup tasks with streamed stdin inputs, managed processes with readiness probes, and dependency-ordered teardown.
- **Mount profiles need migration.** Use `mount` in a named setup task instead of `[mounts.*]` and explicitly select a dedicated rclone config. The entire file is sent to the VM without scope filtering. See [Google Drive mounts](https://github.com/progbeat/oncolab/blob/v0.2.0/docs/configuration.md#google-drive-mounts) for migration steps.
- Use `--mount source=gdrive:work,target=/mnt/work` for one-off mounts and `-p` / `--publish` for local loopback access to a VM service.
