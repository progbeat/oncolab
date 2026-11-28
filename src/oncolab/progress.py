import subprocess as sp
import sys

ALLOCATING = "Creating Colab VM…"
CHECKING_LOGIN = "Checking Colab sign-in…"
WAITING = "Waiting to retry…"
CONNECTING = "Connecting via SSH…"
COPYING = "Copying files…"
DRIVE_PREPARATION = ("Preparing FUSE…", "Preparing rclone…", "Starting Drive mounts…")


def preparation_labels(plan, has_copy=False):
    labels = [ALLOCATING, CHECKING_LOGIN, WAITING, CONNECTING]
    if has_copy:
        labels.append(COPYING)
    setup = plan.get("setup", {})
    labels.extend(step["description"] for step in setup.values())
    if mounts := plan.get("mounts"):
        labels.extend(DRIVE_PREPARATION)
        claimed = {step.get("mount") for step in setup.values()}
        labels.extend(f"Mounting {name.removeprefix('mounts.')}…" for name in mounts if name not in claimed)
    return labels


def progress(work, live, color, *, capture=False, labels=(), timed=True):
    import asyncio
    from contextlib import ExitStack
    from inspect import iscoroutinefunction
    from os import killpg
    from signal import SIGKILL

    with ExitStack() as stack:
        bar = None
        if live:
            from prompt_toolkit.layout import Dimension
            from prompt_toolkit.utils import get_cwidth
            from prompt_toolkit.formatted_text import to_formatted_text
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output.vt100 import Vt100_Output
            from prompt_toolkit.shortcuts import ProgressBar
            from prompt_toolkit.shortcuts.progress_bar import Formatter, ProgressBarCounter, SpinningWheel, Text
            from prompt_toolkit.styles import Style
            from time import monotonic

            class Dots(SpinningWheel):
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

                def format(self, progress_bar, progress, width):
                    frame = self.frames[int(monotonic() / .3) % len(self.frames)]
                    return [("class:spinning-wheel", frame)]

            planned_width = max((get_cwidth(label) for label in labels), default=1)

            def clipped(value, width):
                if get_cwidth(value) <= width:
                    return value
                cells = 1
                end = 0
                for char in value:
                    cells += get_cwidth(char)
                    if cells > width:
                        break
                    end += 1
                return value[:end] + "…" if width else ""

            class ActiveLabel(Formatter):
                def get_width(self, progress_bar):
                    remaining = output.get_size().columns - 3
                    if timed:
                        remaining -= timer.get_width(progress_bar).preferred + 2
                    # Reserve the allocation details space for the whole
                    # preparation plan, not just while that row is active.
                    if labels:
                        remaining -= min(30, max(0, (remaining + 1) // 2))
                    available = remaining
                    return Dimension.exact(max(1, min(planned_width, available)))

                def format(self, progress_bar, counter, width):
                    value = "".join(t for _, t, *_ in to_formatted_text(counter.label))
                    return [("class:label", clipped(value, width))]

            class Timer(Formatter):
                def __init__(self):
                    self.width = 6

                @staticmethod
                def value(counter):
                    return f"{monotonic() - counter.started:.1f}s"

                def get_width(self, progress_bar):
                    self.width = max([self.width, *(len(self.value(c)) for c in progress_bar.counters)])
                    return Dimension.exact(self.width)

                def format(self, progress_bar, counter, width):
                    return [("class:timer", self.value(counter).rjust(width))]

            class Details(Formatter):
                def get_width(self, progress_bar):
                    if any(hasattr(c.label, "detail") for c in progress_bar.counters):
                        return Dimension(min=0, weight=1)
                    return Dimension.exact(0)

                def format(self, progress_bar, counter, width):
                    detail = getattr(counter.label, "detail", None)
                    return [("class:label", clipped("  " + detail(), width))] if detail else []

            timer = Timer()
            keys = stack.enter_context(create_pipe_input())
            output = Vt100_Output.from_pty(sys.stdout)
            output.enable_cpr = False
            bar = ProgressBar(
                formatters=[Dots(), Text(" "), ActiveLabel(),
                            *([Text("  "), timer, Details()] if timed else [])],
                style=Style.from_dict({"spinning-wheel": "ansigray", "label": "dim", "timer": "ansicyan"} if color else {}),
                input=keys, output=output,
            )
            stack.enter_context(bar)
            bar.app.erase_when_done = True

        def reserve(label):
            if bar is not None:
                nonlocal planned_width
                width = get_cwidth(label if isinstance(label, str) else label())
                planned_width = max(planned_width, width)

        async def run(label, command):
            reserve(label)
            counter = None
            if bar is not None:
                # Publish the counter only after its monotonic timestamp is
                # initialized; the renderer runs in its own thread.
                counter = ProgressBarCounter(bar, None, label, True, None)
                counter.started = monotonic()
                bar.counters.append(counter)
                bar.invalidate()
            try:
                if iscoroutinefunction(command):
                    return await command()
                if callable(command):
                    return await asyncio.to_thread(command)
                output = sp.PIPE if capture or live else None
                proc = await asyncio.create_subprocess_exec(
                    *command, stdin=sp.DEVNULL, stdout=output, stderr=output, start_new_session=True,
                )
                try:
                    stdout, stderr = await proc.communicate()
                except asyncio.CancelledError:
                    try:
                        killpg(proc.pid, SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.communicate()
                    raise
                return sp.CompletedProcess(command, proc.returncode, stdout, stderr)
            finally:
                if counter is not None:
                    counter.done = True
                    bar.invalidate()

        run.reserve = reserve
        return asyncio.run(work(run))


def run_step(label, command, live, color, *, labels=(), timed=True):
    return progress(lambda run: run(label, command), live, color, labels=labels, timed=timed)
