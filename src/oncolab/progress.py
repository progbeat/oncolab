import subprocess as sp
import sys

def progress(work, live, color, *, capture=False):
    import asyncio
    from contextlib import ExitStack
    from inspect import iscoroutinefunction

    with ExitStack() as stack:
        bar = None
        if live:
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output.vt100 import Vt100_Output
            from prompt_toolkit.shortcuts import ProgressBar
            from prompt_toolkit.shortcuts.progress_bar import Label, SpinningWheel, Text
            from prompt_toolkit.styles import Style
            from time import monotonic

            class Dots(SpinningWheel):
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

                def format(self, progress_bar, progress, width):
                    frame = self.frames[int(monotonic() / .3) % len(self.frames)]
                    return [("class:spinning-wheel", frame)]

            keys = stack.enter_context(create_pipe_input())
            output = Vt100_Output.from_pty(sys.stdout)
            output.enable_cpr = False
            bar = ProgressBar(
                formatters=[Dots(), Text(" "), Label()],
                style=Style.from_dict({"spinning-wheel": "ansigray", "label": "dim"} if color else {}),
                input=keys, output=output,
            )
            stack.enter_context(bar)
            bar.app.erase_when_done = True

        async def run(label, command):
            counter = bar(label=label, remove_when_done=True) if bar else None
            if counter is not None:
                bar.invalidate()
            try:
                if iscoroutinefunction(command):
                    return await command()
                if callable(command):
                    return await asyncio.to_thread(command)
                output = sp.PIPE if capture or live else None
                proc = await asyncio.create_subprocess_exec(*command, stdin=sp.DEVNULL, stdout=output, stderr=output)
                stdout, stderr = await proc.communicate()
                return sp.CompletedProcess(command, proc.returncode, stdout, stderr)
            finally:
                if counter is not None:
                    counter.done = True
                    bar.invalidate()

        return asyncio.run(work(run))


def run_step(label, command, live, color):
    return progress(lambda run: run(label, command), live, color)
