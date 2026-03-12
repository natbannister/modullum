import time

class Stopwatch:
    def __init__(self):
        self.total = 0.0
        self._start_time = None

    def start(self):
        if self._start_time is None:
            self._start_time = time.perf_counter()

    def stop(self):
        if self._start_time is not None:
            self.total += time.perf_counter() - self._start_time
            self._start_time = None

    def reset(self):
        self.total = 0.0
        self._start_time = None

    def elapsed(self):
        """Return total including current run if running."""
        if self._start_time is not None:
            return self.total + (time.perf_counter() - self._start_time)
        return self.total