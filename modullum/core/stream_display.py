import os
import sys
import time
import subprocess
import threading
from pathlib import Path
from typing import Callable


FIFO_PATH = "/tmp/modullum_stream"


class StreamDisplay:
    """
    Routes streaming output to a tmux split pane if available.
    Falls back to a callable (e.g. logger.info, console.log) or stdout.
    """

    def __init__(
        self,
        fifo_path: str = FIFO_PATH,
        autoclose: bool = True,
        fallback: Callable[[str], None] | None = None,
    ):
        self.fifo_path = fifo_path
        self.autoclose = autoclose
        self._fallback = fallback
        self._writer = None
        self._pane_id = None
        self._in_tmux = bool(os.environ.get("TMUX"))

    def open(self):
        if not self._in_tmux:
            self._writer = None  # will use fallback path in write()
            return

        if not os.path.exists(self.fifo_path):
            os.mkfifo(self.fifo_path)

        result = subprocess.run(
            ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}",
             f"cat {self.fifo_path}"],
            capture_output=True, text=True
        )
        self._pane_id = result.stdout.strip()
        self._writer = self._open_fifo_with_timeout(timeout=5.0)

        if self._writer is None:
            # Pane spawned but FIFO never got a reader — treat as no tmux
            self._in_tmux = False

    def _open_fifo_with_timeout(self, timeout: float):
        result = [None]

        def _open():
            try:
                result[0] = open(self.fifo_path, "w", buffering=1)
            except OSError:
                pass

        t = threading.Thread(target=_open, daemon=True)
        t.start()
        t.join(timeout)
        return result[0]

    def write(self, text: str):
        if self._writer is not None:
            self._writer.write(text)
            self._writer.flush()
        elif self._fallback is not None:
            self._fallback(text)
        else:
            sys.stdout.write(text)
            sys.stdout.flush()

    def flush(self):
        if self._writer is not None:
            self._writer.flush()

    def close(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self.autoclose and self._pane_id and self._in_tmux:
            subprocess.run(
                ["tmux", "kill-pane", "-t", self._pane_id],
                capture_output=True,
            )
            self._pane_id = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()