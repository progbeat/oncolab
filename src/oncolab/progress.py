import subprocess as sp
import sys

from .duration import format_duration

ALLOCATING = "Creating Colab VM…"
CHECKING_LOGIN = "Checking Colab sign-in…"
WAITING = "Waiting to retry…"
CONNECTING = "Connecting via SSH…"
STARTING_RUNNER = "Preparing remote command…"
SETTING_UP = "Setting up…"
COPYING = "Copying files…"
DRIVE_PREPARATION = ("Preparing FUSE…", "Preparing rclone…", "Starting Drive mounts…")


def preparation_labels(plan, has_copy=False):
    labels = [ALLOCATING, CHECKING_LOGIN, WAITING, CONNECTING, STARTING_RUNNER, SETTING_UP]
    if has_copy:
        labels.append("  " + COPYING)
    setup = plan.get("setup", {})
    labels.extend("  " + step["description"] for step in setup.values())
    if mounts := plan.get("mounts"):
        labels.extend("  " + label for label in (*DRIVE_PREPARATION, "Receiving Drive config…"))
        claimed = {step.get("mount") for step in setup.values()}
        labels.extend(f"  Mounting {name.removeprefix('mounts.')}…" for name in mounts if name not in claimed)
    return labels


class SetupTimeline:
    """Track readiness through the actual graph, not job duration estimates."""

    def __init__(self):
        self.started = self.finished = None
        self.status = None
        self.jobs = {}
        self.starts = {}
        self.ends = {}

    def event(self, kind, *fields):
        if kind == "setup-start":
            self.started, = fields
        elif kind == "setup-job":
            key, label, dependencies = fields
            self.jobs[key] = (label, dependencies)
        elif kind == "setup-active":
            key, started = fields
            self.starts[key] = started
        elif kind == "setup-done":
            key, ended, status = fields
            self.ends[key] = (ended, status)
        elif kind == "setup-finish":
            self.finished, self.status = fields
        else:
            raise ValueError("Invalid setup progress event")

    def candidates(self):
        def blockers(keys):
            # An unfinished prerequisite necessarily becomes ready no earlier
            # than the observed completed ones. At a resolved join, the last
            # successful prerequisite determines readiness.
            outstanding = [key for key in keys if key not in self.ends]
            if outstanding:
                return outstanding
            success = [key for key in keys if self.ends[key][1] == 0]
            return [max(success, key=lambda key: self.ends[key][0])] if success else []

        keep = set()
        todo = blockers(self.jobs)
        visited = set()
        while todo:
            key = todo.pop()
            if key in visited:
                continue
            visited.add(key)
            if key in self.ends and self.ends[key][1] == 0:
                keep.add(key)
            todo.extend(blockers(self.jobs[key][1]))
        return keep

    def rows(self, verbose):
        if self.started is None:
            return []
        if self.finished is not None and (not verbose or self.status != 0):
            return []
        rows = [(0, "Setting up…", self.started, self.finished)]
        keep = self.candidates() if verbose else set()
        jobs = [key for key in self.starts if key in keep or key not in self.ends]
        for key in jobs:
            label, _ = self.jobs[key]
            if label:
                rows.append((2, label, self.starts[key], self.ends[key][0] if key in self.ends else None))
        return rows


def completed_steps(rows, labels, color):
    """Write the retained transcript apart from both live UI and stdout."""
    import os
    from contextlib import suppress

    try:
        from prompt_toolkit.utils import get_cwidth
    except ImportError:  # A noninteractive VM does not need prompt_toolkit.
        from unicodedata import combining, east_asian_width

        def get_cwidth(value):
            return sum((2 if east_asian_width(char) in "WF" else 1)
                       for char in value if not combining(char))

    stream = sys.stderr
    tty = stream.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    color = color and tty
    width = max((get_cwidth(label) for label in labels), default=1)
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    with suppress(OSError, ValueError):
        for indent, label, started, ended in rows:
            duration = format_duration(ended - started)
            padding = width - indent - get_cwidth(label)
            if tty:
                columns = os.get_terminal_size(stream.fileno()).columns
                available = max(1, columns - indent - 3 - 2 - max(6, len(duration)))
                if get_cwidth(label) > available:
                    prefix = ""
                    for char in label:
                        if get_cwidth(prefix) + get_cwidth(char) >= available:
                            break
                        prefix += char
                    label = prefix + "…"
                padding = min(padding, available - get_cwidth(label))
            mark = "\x1b[32m✓\x1b[0m" if color else "✓"
            timer = f"{duration:>6}"
            if color:
                label = f"\x1b[2m{label}\x1b[0m"
                timer = f"\x1b[36m{timer}\x1b[0m"
            line = f"{' ' * indent}{mark} {label}{' ' * max(0, padding)}  {timer}\n"
            if fd is None:
                stream.write(line)
                stream.flush()
                continue
            # A failed buffered write would be retried by Python at shutdown
            # and could replace a legitimate command exit code with 120.
            pending = memoryview(line.encode("utf-8"))
            while pending:
                written = os.write(fd, pending)
                if not written:
                    return
                pending = pending[written:]


def progress(work, live, color, *, capture=False, labels=(), timed=True, idle=None, takeover=None,
             setup=False, verbose=False):
    import asyncio
    from contextlib import ExitStack
    from inspect import iscoroutinefunction
    from os import killpg
    from signal import SIGKILL
    from time import monotonic

    # The remote runner receives this function and the duration formatter without their modules.
    refresh_interval = 0.1
    timeline = SetupTimeline() if setup else None
    with ExitStack() as stack:
        bar = None
        if live:
            from prompt_toolkit.filters import Condition
            from prompt_toolkit.layout import ConditionalContainer, Dimension, FormattedTextControl, HSplit, Window
            from prompt_toolkit.utils import get_cwidth
            from prompt_toolkit.formatted_text import to_formatted_text
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output.vt100 import Vt100_Output
            from prompt_toolkit.shortcuts import ProgressBar
            from prompt_toolkit.shortcuts.progress_bar import Formatter, ProgressBarCounter, SpinningWheel, Text
            from prompt_toolkit.styles import Style

            class Dots(SpinningWheel):
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

                def format(self, progress_bar, progress, width):
                    frame = self.frames[int(monotonic() / refresh_interval) % len(self.frames)]
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
                    ended = getattr(counter, "ended", None)
                    return format_duration((monotonic() if ended is None else ended) - counter.started)

                def get_width(self, progress_bar):
                    self.width = max([self.width, *(len(self.value(c)) for c in progress_bar.counters)])
                    return Dimension.exact(self.width)

                def format(self, progress_bar, counter, width):
                    return [("class:timer", self.value(counter).rjust(width))]

            class SetupLabel(ActiveLabel):
                def get_width(self, progress_bar):
                    return Dimension.exact(super().get_width(progress_bar).preferred + 2)

                def format(self, progress_bar, counter, width):
                    indent = getattr(counter, "indent", 0)
                    ended = getattr(counter, "ended", None)
                    icon = [("class:check", "✓")] if ended is not None else Dots().format(progress_bar, counter, 1)
                    return [("", " " * indent), *icon, ("", " "),
                            *super().format(progress_bar, counter, max(0, width - 2 - indent))]

            timer = Timer()
            active_label = ActiveLabel()

            def detail_parts(progress_bar):
                for counter in progress_bar.counters:
                    if parts := getattr(counter.label, "detail_parts", None):
                        return parts()
                    if detail := getattr(counter.label, "detail", None):
                        return [("class:label", detail())]
                return []

            def detail_inline(progress_bar, detail):
                remaining = (output.get_size().columns - 3 - 2
                             - active_label.get_width(progress_bar).preferred
                             - timer.get_width(progress_bar).preferred)
                cells = sum(get_cwidth(value) for _, value in detail)
                return cells and cells + 2 <= remaining

            class Details(Formatter):
                def get_width(self, progress_bar):
                    if detail_inline(progress_bar, detail_parts(progress_bar)):
                        return Dimension(min=0, weight=1)
                    return Dimension.exact(0)

                def format(self, progress_bar, counter, width):
                    parts = detail_parts(progress_bar)
                    return [("class:label", "  "), *parts] if width else []

            class PreparationBar(ProgressBar):
                @property
                def app(self):
                    return self._preparation_app

                @app.setter
                def app(self, application):
                    # ProgressBar starts the renderer immediately after assigning
                    # its app; set the interval before it starts the refresh task.
                    application.refresh_interval = refresh_interval
                    application.erase_when_done = True
                    self._preparation_app = application
                    if timed:
                        # ProgressBar's built-in title has a fixed one-row height.
                        # Add a separate wrapping window without touching the
                        # column layout or the height of the progress rows.
                        details = ConditionalContainer(
                            Window(FormattedTextControl(self.wrapped_detail),
                                   dont_extend_height=True, wrap_lines=True),
                            filter=Condition(lambda: bool(self.wrapped_detail())),
                        )
                        application.layout.container = HSplit([details, application.layout.container])

                def wrapped_detail(self):
                    detail = detail_parts(self)
                    if not detail or detail_inline(self, detail):
                        return []
                    width = max(1, output.get_size().columns - 1)
                    spans = []
                    cells = 0
                    space = False
                    for style, value in detail:
                        for word in value.split(" "):
                            if not word:
                                space = True
                                continue
                            length = get_cwidth(word)
                            if cells and space:
                                if cells + 1 + length > width:
                                    spans.append(("class:label", "\n"))
                                    cells = 0
                                else:
                                    spans.append(("class:label", " "))
                                    cells += 1
                            spans.append((style, word))
                            cells += length
                            space = True
                    # Window can wrap a single word longer than the terminal.
                    return spans

            keys = stack.enter_context(create_pipe_input())
            output = Vt100_Output.from_pty(sys.stdout)
            output.enable_cpr = False
            bar = PreparationBar(
                formatters=[*([SetupLabel()] if setup else [Dots(), Text(" "), active_label]),
                            *([Text("  "), timer, Details()] if timed else [])],
                style=Style.from_dict({"spinning-wheel": "ansigray", "label": "dim", "timer": "ansicyan",
                                       "retry-reason": "ansiyellow", "check": "ansigreen"} if color else {}),
                input=keys, output=output,
            )
            if idle:
                # Avoid a blank first frame while the remote event loop
                # schedules tasks, and a blank gap between task groups.
                waiting = ProgressBarCounter(bar, None, idle, False, None)
                waiting.started = monotonic()
                bar.counters.append(waiting)
            if takeover:
                takeover()
            stack.enter_context(bar)

        def reserve(label):
            if bar is not None:
                nonlocal planned_width
                width = get_cwidth(label if isinstance(label, str) else label())
                planned_width = max(planned_width, width)

        async def run(label, command, *, started_at=None):
            reserve(label)
            counter = None
            if bar is not None and not setup:
                # Publish the counter only after its monotonic timestamp is
                # initialized; the renderer runs in its own thread.
                counter = ProgressBarCounter(bar, None, label, not idle, None)
                counter.started = monotonic() if started_at is None else started_at
                if idle and bar.counters == [waiting]:
                    bar.counters[:] = [counter]
                else:
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
                    if idle:
                        bar.counters[:] = [c for c in bar.counters if c is not counter] or [waiting]
                    bar.invalidate()

        def setup_event(kind, *fields):
            timeline.event(kind, *fields)
            if bar is not None:
                if kind == "setup-job" and fields[1]:
                    reserve("  " + fields[1])
                counters = []
                for indent, label, started, ended in timeline.rows(verbose):
                    counter = ProgressBarCounter(bar, None, label, False, None)
                    counter.started, counter.ended, counter.indent = started, ended, indent
                    counters.append(counter)
                bar.counters[:] = counters
                bar.invalidate()

        run.reserve = reserve
        if setup:
            run.setup_event = setup_event
        result = asyncio.run(work(run))
    if verbose and timeline is not None and timeline.status == 0:
        completed_steps(timeline.rows(True), labels, color)
    return result


def run_step(label, command, live, color, *, labels=(), timed=True):
    return progress(lambda run: run(label, command), live, color, labels=labels, timed=timed)
