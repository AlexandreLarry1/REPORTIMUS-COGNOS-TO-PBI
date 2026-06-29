"""Langfuse v4 tracing wrapper. All calls are no-ops when LANGFUSE_ENABLED != true."""
import os
from contextlib import contextmanager

ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"

_lf = None


class _Noop:
    def generation(self, *a, **kw): return self
    def span(self, *a, **kw): return self
    def update(self, **kw): return self
    def end(self, **kw): return self


class _Handle:
    """Wraps a Langfuse v4 observation and creates typed children."""
    def __init__(self, obs=None):
        self._obs = obs

    def generation(self, name: str, **kw):
        if not _lf:
            return _Noop()
        return _Handle(_lf.start_observation(name=name, as_type="generation", **kw))

    def span(self, name: str, **kw):
        if not _lf:
            return _Noop()
        return _Handle(_lf.start_observation(name=name, as_type="span", **kw))

    def update(self, **kw):
        if self._obs:
            self._obs.update(**kw)
        return self

    def end(self, **kw):
        if self._obs:
            if kw:
                self._obs.update(**kw)
            self._obs.end()


if ENABLED:
    os.environ.setdefault(
        "LANGFUSE_HOST",
        os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
    )
    from langfuse import Langfuse
    _lf = Langfuse()


def log_errors(errors: list, *, kind: str = "errors") -> None:
    """Attach a list of errors to the current trace as a dedicated child observation.

    Pass structured dicts or strings. No-op when Langfuse is disabled or the list
    is empty. Must be called within an active ``with obs.trace(...)`` block so the
    observation nests under the current trace.
    """
    if not ENABLED or not _lf or not errors:
        return
    try:
        _lf.start_observation(
            name=kind,
            as_type="span",
            metadata={"errors": errors, "count": len(errors)},
        )
    except Exception:
        pass


def log_event(name: str, **metadata) -> None:
    """Attach a named event (e.g. warning, info) to the current trace.

    No-op when Langfuse is disabled. Must be called within an active
    ``with obs.trace(...)`` block.
    """
    if not ENABLED or not _lf:
        return
    try:
        _lf.start_observation(name=name, as_type="span", metadata=metadata)
    except Exception:
        pass


@contextmanager
def trace(name: str, **metadata):
    """Top-level trace. Children (span/generation) auto-nest via OTel context."""
    if not ENABLED or not _lf:
        yield _Noop()
        return
    with _lf.start_as_current_observation(name=name, as_type="span", metadata=metadata):
        yield _Handle()
    _lf.flush()