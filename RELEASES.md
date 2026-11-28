# 0.3.0
<!-- Bump: minor -->
- In an interactive terminal, see elapsed time for VM creation, SSH connection, and active setup steps. OpenSSH prompts take precedence, and preparation indicators clear before the command starts.
- See the requested runtime, attempt number, and countdown when VM creation is retried. The retry reason is yellow in color terminals. The default is four attempts. Override it with `--attempts`, or set `--attempts 0` to retry transient failures until Ctrl+C. Colab's retry delay is never shortened; bounded runs report delays over two minutes and exit instead of waiting.
- Retry eligible SSH connection failures on the same VM with a separate attempt budget. Definitive refusals stop retries. If the command might already have started, it is not run again.
- Start eligible setup steps while the Drive configuration is still transferring. The command gets its own SSH channel and starts only after setup is ready. Parallel staging may reject rclone configs around 12 MiB or larger.
- If Colab credentials are missing, run `colab sessions` to sign in. Oncolab reports its own failures without dumping internal tracebacks; use `--debug` for more detail. The command's output is unchanged, and its exit status is preserved when cleanup succeeds.
- Cancelling allocation or setup attempts VM cleanup. If cleanup or release cannot be confirmed, check `colab sessions` for the VM endpoint and use Colab's Manage sessions view only once no writes are pending.
