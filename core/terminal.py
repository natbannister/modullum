import logging
import sys
import time

class StreamingConsoleHandler(logging.StreamHandler):
    """
    A logging handler that streams output character-by-character to the terminal,
    like LLM token streaming. File logging is unaffected and logs instantly.
    """

    def __init__(self, stream=None, char_delay: float = 0.01, newline_delay: float = 0.05):
        """
        Args:
            stream: Output stream (defaults to sys.stdout)
            char_delay: Seconds between each character (default: 0.01s = 10ms)
            newline_delay: Extra pause after a newline (default: 0.05s = 50ms)
        """
        super().__init__(stream or sys.stdout)
        self.char_delay = char_delay
        self.newline_delay = newline_delay

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            for char in msg:
                self.stream.write(char)
                self.stream.flush()
                if char == '\n':
                    time.sleep(self.newline_delay)
                else:
                    time.sleep(self.char_delay)
            self.stream.write(self.terminator)
            self.stream.flush()
        except Exception:
            self.handleError(record)


def setup_logger(log_file: str, char_delay: float = 0.01, newline_delay: float = 0.05) -> logging.Logger:
    logger = logging.getLogger("run_logger")
    logger.setLevel(logging.DEBUG)

    # Terminal handler — streaming, no timestamps
    console_handler = StreamingConsoleHandler(char_delay=char_delay, newline_delay=newline_delay)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)

    # File handler — instant, with timestamps
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

    return logger