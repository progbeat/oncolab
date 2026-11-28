# 0.4.0
<!-- Bump: minor -->
- Interactive setup shows a `Setting up…` timer with active jobs indented underneath. SSH progress finishes before the remote setup handoff. By default, completed rows disappear and preparation leaves no output before the command starts.
- Use `--verbose` to keep the elapsed times for successful VM creation, SSH connection, and setup on stderr. During setup, finished jobs stay visible only while they can still be on the dependency chain that determines when setup ends. After setup, only the jobs on that chain remain, with check marks and their own elapsed times. `--verbose` does not enable debug logging or change the command's output.
