import logging
import sys
import time

from prompt_toolkit import prompt
from prompt_toolkit.styles import Style

# ── rich ──────────────────────────────────────────────────────────────────────

from rich.console import Console

console = Console()

def status_spinner(message: str):
    return console.status(f"[bold]{message}", spinner="star2")

# ── Prompt toolkit style ──────────────────────────────────────────────────────

_style = Style.from_dict({"placeholder": "#666666"})

def get_input(placeholder: str = "Send a message") -> str:
    """Request user input with a stylised prompter and wait time recording."""
    t0 = time.monotonic()

    user_input = prompt(">>> ", placeholder=placeholder, style=_style)

    user_wait_s = round(time.monotonic() - t0, 3)

    return user_input, user_wait_s

# ── Logging ───────────────────────────────────────────────────────────────────

class StreamingConsoleHandler(logging.StreamHandler):
    """
    A logging handler that optionally streams output character-by-character to the terminal,
    like LLM token streaming. File logging is unaffected and logs instantly.
    """

    def __init__(
        self,
        stream=None,
        streaming: bool = True,
        char_delay: float = 0.01,
        newline_delay: float = 0.05,
    ):
        """
        Args:
            stream: Output stream (defaults to sys.stdout)
            streaming: Whether to stream character-by-character (default: True)
            char_delay: Seconds between each character (default: 0.01s = 10ms)
            newline_delay: Extra pause after a newline (default: 0.05s = 50ms)
        """
        super().__init__(stream or sys.stdout)
        self.streaming = streaming
        self.char_delay = char_delay
        self.newline_delay = newline_delay

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            if self.streaming:
                for char in msg:
                    self.stream.write(char)
                    self.stream.flush()
                    time.sleep(self.newline_delay if char == '\n' else self.char_delay)
            else:
                self.stream.write(msg)
            self.stream.write(self.terminator)
            self.stream.flush()
        except Exception:
            self.handleError(record)


def setup_logger(
    log_file: str,
    streaming: bool = False,
    char_delay: float = 0.01,
    newline_delay: float = 0.05,
) -> logging.Logger:
    logger = logging.getLogger("run_logger")
    logger.setLevel(logging.DEBUG)

    # Terminal handler — optionally streaming, no timestamps
    console_handler = StreamingConsoleHandler(
        streaming=streaming,
        char_delay=char_delay,
        newline_delay=newline_delay,
    )
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)

    # File handler — instant, with timestamps
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

    return logger