"""log_setup.py — Centralized logging for RuneSync.

Call init_logging(log_path) once at startup (before Tk is created).
Returns a queue.Queue that main.py's Debug Log tab should drain every 500ms.
"""
import io, logging, logging.handlers, queue, re, sys
from datetime import datetime

# Maps known [TAG] prefixes to a base log level
_TAG_LEVELS: dict = {
    "[ugg]":     logging.DEBUG,
    "[lcu]":     logging.DEBUG,
    "[monitor]": logging.INFO,
}

_ERROR_RE = re.compile(r"\b(error|failed|exception|traceback|✗)\b", re.IGNORECASE)
_WARN_RE  = re.compile(r"\b(warn|warning|timeout)\b", re.IGNORECASE)


class _StderrInterceptor(io.TextIOBase):
    """
    Drop-in replacement for sys.stderr.

    Buffers partial writes until a newline is seen, then:
      1. Forwards the line to the real stderr file (so runesync.log still works)
      2. Parses any leading [TAG] prefix
      3. Promotes lines containing error/warn keywords to the appropriate level
      4. Emits a structured LogRecord to the root logger
    """
    def __init__(self, real, logger: logging.Logger):
        self._real     = real
        self._log      = logger
        self._buf      = ""
        self._emitting = False  # re-entry guard

    def write(self, text: str) -> int:
        self._real.write(text)
        self._real.flush()
        if self._emitting:
            return len(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self._emitting = True
                try:
                    self._emit(line)
                finally:
                    self._emitting = False
        return len(text)

    def flush(self):
        self._real.flush()

    def _emit(self, line: str):
        tag       = "[unknown]"
        remainder = line
        for t in _TAG_LEVELS:
            if line.startswith(t):
                tag       = t
                remainder = line[len(t):].lstrip()
                break

        base = _TAG_LEVELS.get(tag, logging.DEBUG)
        if _ERROR_RE.search(remainder):
            level, sev = logging.ERROR,   "error"
        elif _WARN_RE.search(remainder):
            level, sev = logging.WARNING, "warn"
        elif base >= logging.INFO:
            level, sev = base,            "info"
        else:
            level, sev = logging.DEBUG,   "debug"

        self._log.log(
            level, remainder,
            extra={"rs_tag": tag, "rs_severity": sev},
            stacklevel=1,
        )


class _StructuredFormatter(logging.Formatter):
    """Formats log records for the runesync.log file."""
    def format(self, record: logging.LogRecord) -> str:
        ts  = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        tag = getattr(record, "rs_tag",      "[unknown]")
        sev = getattr(record, "rs_severity", record.levelname.lower())
        return f"{ts}  {tag:<12} {sev.upper():<7} {record.getMessage()}"


def init_logging(log_path: str) -> queue.Queue:
    """
    Set up the root logger with:
      - TimedRotatingFileHandler → runesync.log (rotates at midnight, old file deleted)
      - QueueHandler             → returned queue (consumed by the Debug Log tab)

    Also wraps sys.stderr in _StderrInterceptor so every existing
    print(..., file=sys.stderr) call across all modules is captured.

    Parameters
    ----------
    log_path : str
        Absolute path to runesync.log (already opened by the caller).

    Returns
    -------
    queue.Queue  consumed by main.py's _debug_drain() loop.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = _StructuredFormatter()

    # ── File handler: plain append, no rotation ───────────────────────────────
    # TimedRotatingFileHandler was crashing on Windows: at midnight it tries to
    # os.rename() the log file, but PyInstaller already has it open as sys.stderr,
    # causing a PermissionError on every subsequent write for the rest of the session.
    # Plain FileHandler avoids all of that. Log is cleared on each app restart because
    # PyInstaller opens sys.stderr in write mode.
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8", delay=False)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # ── Queue handler: feeds the in-app Debug Log tab ─────────────────────────
    log_queue: queue.Queue = queue.Queue(maxsize=5000)
    qh = logging.handlers.QueueHandler(log_queue)
    qh.setLevel(logging.DEBUG)
    root.addHandler(qh)

    # ── Intercept sys.stderr ──────────────────────────────────────────────────
    # sys.stderr is already the open file handle at this point.
    sys.stderr = _StderrInterceptor(sys.stderr, root)

    # ── Silence noisy third-party loggers ─────────────────────────────────────
    logging.getLogger("PIL").setLevel(logging.WARNING)

    return log_queue
