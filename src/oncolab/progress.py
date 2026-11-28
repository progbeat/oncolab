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
                    return Dimension.exact(max(1, min(planned_width, remaining)))

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

            timer = Timer()
            active_label = ActiveLabel()

            def detail_text(progress_bar):
                for counter in progress_bar.counters:
                    if detail := getattr(counter.label, "detail", None):
                        return detail()
                return ""

            def detail_inline(progress_bar, detail):
                remaining = (output.get_size().columns - 3 - 2
                             - active_label.get_width(progress_bar).preferred
                             - timer.get_width(progress_bar).preferred)
                return detail and get_cwidth(detail) + 2 <= remaining

            class Details(Formatter):
                def get_width(self, progress_bar):
                    if detail_inline(progress_bar, detail_text(progress_bar)):
                        return Dimension(min=0, weight=1)
                    return Dimension.exact(0)

                def format(self, progress_bar, counter, width):
                    detail = detail_text(progress_bar)
                    return [("class:label", clipped("  " + detail, width))] if width else []

            class PreparationBar(ProgressBar):
                @property
                def title(self):
                    # Wrap VM retry details onto their own line when they do
                    # not fit, without moving the elapsed-time column.
                    detail = detail_text(self) if timed else ""
                    if detail and not detail_inline(self, detail):
                        return [("class:label", clipped(detail, output.get_size().columns - 1))]
                    return None

                @title.setter
                def title(self, value):
                    pass  # ProgressBar initializes this field; the view derives it from the active row.

            keys = stack.enter_context(create_pipe_input())
            output = Vt100_Output.from_pty(sys.stdout)
            output.enable_cpr = False
            bar = PreparationBar(
                formatters=[Dots(), Text(" "), active_label,
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
