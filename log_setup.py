"""log_setup.py — Centralized logging for RuneSync.

Call init_logging(log_path) once at startup (before Tk is created).
Returns a queue.Queue that main.py's Debug Log tab should drain every 500ms.

Design (and the bugs this avoids):
  - A size-based RotatingFileHandler OWNS runesync.log (1 MB x 3 backups) and is
    the SOLE writer of the file. The old code added a plain FileHandler AND let
    the stderr interceptor write raw text to the same file (sys.stderr was the
    file), so every line was logged twice — once raw, once formatted. Now the
    handler is the only file writer, so a line is logged exactly once.
    Because nothing else holds the file open, rotation's rename can't hit the
    PermissionError that killed the earlier TimedRotatingFileHandler, and the
    log no longer grows unbounded across long autostart sessions.
  - A QueueHandler feeds the in-app Debug Log tab.
  - sys.stderr is replaced by _StderrInterceptor so every existing
    print(..., file=sys.stderr) across the codebase becomes a structured record.
    Raw text is echoed to the ORIGINAL stderr (a real console in dev; None under
    PyInstaller --windowed), never to the log file.
  - sys.excepthook / threading.excepthook (and Tk's report_callback_exception,
    wired in main.py) route uncaught exceptions to one ERROR record WITH the
    traceback, instead of a mangled multi-line stderr dump.
"""
import io, logging, logging.handlers, queue, re, sys, threading
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
      1. Parses any leading [TAG] prefix
      2. Promotes lines containing error/warn keywords to the right level
      3. Emits a structured LogRecord to the root logger
    Raw text is echoed to the original stderr when one exists (a real console in
    dev; None under --windowed). The log file is written only by the handler, so
    lines are never double-logged.
    """
    def __init__(self, real, logger: logging.Logger):
        self._real     = real
        self._log      = logger
        self._buf      = ""
        self._emitting = False  # re-entry guard

    def write(self, text: str) -> int:
        if self._real is not None:
            try:
                self._real.write(text)
                self._real.flush()
            except Exception:
                pass  # a dead/closed console must never break logging
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
        if self._real is not None:
            try:
                self._real.flush()
            except Exception:
                pass

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
        line = f"{ts}  {tag:<12} {sev.upper():<7} {record.getMessage()}"
        # This overrides Formatter.format() entirely, so the base class's
        # exception appending is bypassed — re-add it or tracebacks vanish.
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        elif record.exc_text:
            line += "\n" + record.exc_text
        return line


def _install_excepthooks(logger: logging.Logger) -> None:
    """Route uncaught exceptions (main thread + worker threads) to a single
    ERROR record with the traceback. Tk callback exceptions are wired separately
    in main.py via root.report_callback_exception."""
    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.error("Uncaught exception", exc_info=(exc_type, exc, tb),
                     extra={"rs_tag": "[crash]", "rs_severity": "error"})
    sys.excepthook = _excepthook

    def _threadhook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        logger.error(f"Uncaught exception in thread {name}",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                     extra={"rs_tag": "[crash]", "rs_severity": "error"})
    threading.excepthook = _threadhook


def init_logging(log_path: str) -> queue.Queue:
    """
    Set up the root logger and return the queue consumed by main.py's
    _debug_drain() loop.

    Parameters
    ----------
    log_path : str
        Absolute path to runesync.log.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Wrap stderr FIRST so that if a handler below fails to initialise, sys.stderr
    # is never left as None (PyInstaller --windowed) — a later print(file=stderr)
    # would otherwise raise AttributeError.
    sys.stderr = _StderrInterceptor(sys.stderr, root)

    fmt = _StructuredFormatter()

    # Size-based rotation. This handler is the ONLY opener of the file, so the
    # rename it performs on rollover can't collide with an external open handle.
    fh = logging.handlers.RotatingFileHandler(
        log_path, mode="a", maxBytes=1_000_000, backupCount=3,
        encoding="utf-8", delay=False,
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # Queue handler: feeds the in-app Debug Log tab.
    log_queue: queue.Queue = queue.Queue(maxsize=5000)
    qh = logging.handlers.QueueHandler(log_queue)
    qh.setLevel(logging.DEBUG)
    root.addHandler(qh)

    _install_excepthooks(root)

    # Silence noisy third-party loggers.
    logging.getLogger("PIL").setLevel(logging.WARNING)

    return log_queue
