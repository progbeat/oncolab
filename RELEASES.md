# 0.2.0
<!-- Bump: minor -->
- Copy local files and directories to a fresh Colab VM with repeatable `--copy`. Uploads overlap with independent setup steps, and setup can explicitly wait for files it needs.
- Set the VM working directory with `workdir` and provide command environment defaults via `[env]`. Override or forward variables with Docker-style `-e` and `--env`.
