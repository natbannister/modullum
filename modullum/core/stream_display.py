import os
import sys
import time
import subprocess
import threading
from pathlib import Path


FIFO_PATH = "/tmp/modullum_stream"


class StreamDisplay:
    """
    Directs streaming node output to a tmux split pane via a named FIFO.
    Falls back to stdout if not running inside a tmux session.
    """

    def __init__(self, fifo_path: str = FIFO_PATH):
        self.fifo_path = fifo_path
        self._writer = None
        self._pane_id = None
        self._in_tmux = bool(os.environ.get("TMUX"))

    def open(self):
        """Creates the FIFO, spawns the pane, waits for a reader."""
        if not self._in_tmux:
            print("[StreamDisplay] Not in a tmux session — streaming to stdout.")
            self._writer = sys.stdout
            return

        # Create FIFO if absent
        if not os.path.exists(self.fifo_path):
            os.mkfifo(self.fifo_path)

        # Spawn pane running cat on the FIFO
        result = subprocess.run(
            ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}",
             f"cat {self.fifo_path}"],
            capture_output=True, text=True
        )
        self._pane_id = result.stdout.strip()

        # Open FIFO for writing — blocks until cat opens the read end.
        # We do this in a thread with a timeout so a failed pane spawn
        # doesn't hang the main process.
        self._writer = self._open_fifo_with_timeout(timeout=5.0)
        if self._writer is None:
            print("[StreamDisplay] Timed out waiting for pane reader — falling back to stdout.")
            self._writer = sys.stdout

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
        if self._writer:
            self._writer.write(text)
            if hasattr(self._writer, "flush"):
                self._writer.flush()

    def close(self):
        if self._writer and self._writer is not sys.stdout:
            self._writer.close()
            self._writer = None
        if self._pane_id and self._in_tmux:
            # Give cat a moment to flush before killing the pane
            time.sleep(0.5)
            subprocess.run(["tmux", "kill-pane", "-t", self._pane_id],
                           capture_output=True)
            self._pane_id = None

    def flush(self):
        if self._writer and hasattr(self._writer, "flush"):
            self._writer.flush()

    def __call__(self, text: str):
        self.write(text)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()