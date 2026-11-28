import subprocess as sp
import sys

def progress(work, live, color, *, capture=False):
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
            from prompt_toolkit.shortcuts.progress_bar import Formatter, SpinningWheel, Text
            from prompt_toolkit.styles import Style
            from time import monotonic, time as wall_clock

            class Dots(SpinningWheel):
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

                def format(self, progress_bar, progress, width):
                    frame = self.frames[int(monotonic() / .3) % len(self.frames)]
                    return [("class:spinning-wheel", frame)]

            planned_width = 1

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
                    available = output.get_size().columns - 5 - timer.get_width(progress_bar).preferred
                    return Dimension.exact(max(1, min(planned_width, available)))

                def format(self, progress_bar, counter, width):
                    value = "".join(t for _, t, *_ in to_formatted_text(counter.label))
                    return [("class:label", clipped(value, width))]

            class Timer(Formatter):
                def __init__(self):
                    self.width = 6

                @staticmethod
                def value(counter):
                    return f"{wall_clock() - counter.start_time.timestamp():.1f}s"

                def get_width(self, progress_bar):
                    self.width = max([self.width, *(len(self.value(c)) for c in progress_bar.counters)])
                    return Dimension.exact(self.width)

                def format(self, progress_bar, counter, width):
                    return [("class:timer", self.value(counter).rjust(width))]

            timer = Timer()
            keys = stack.enter_context(create_pipe_input())
            output = Vt100_Output.from_pty(sys.stdout)
            output.enable_cpr = False
            bar = ProgressBar(
                formatters=[Dots(), Text(" "), ActiveLabel(), Text("  "), timer],
                style=Style.from_dict({"spinning-wheel": "ansigray", "label": "dim", "timer": "ansicyan"} if color else {}),
                input=keys, output=output,
            )
            stack.enter_context(bar)
            bar.app.erase_when_done = True

        def reserve(label):
            if bar is not None:
                nonlocal planned_width
                width = get_cwidth(label) if isinstance(label, str) else 72
                planned_width = max(planned_width, width)

        async def run(label, command):
            reserve(label)
            counter = bar(label=label, remove_when_done=True) if bar else None
            if counter is not None:
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


def run_step(label, command, live, color):
    return progress(lambda run: run(label, command), live, color)
