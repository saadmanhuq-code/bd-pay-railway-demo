"""Observability: PII-safe logging setup + a simple metrics registry.

Logging rules (spec/00 §4, arch §(c) audit notes, cut-last #11):

- A :class:`~bdpay.platform.pii.PIIRedactionFilter` is attached to EVERY
  handler that :func:`configure_logging` touches — no log line reaches a sink
  unredacted, and format args are consumed before redaction so parameters
  cannot smuggle identifiers past the filter.
- The JSON formatter emits a fixed field set (timestamp, level, logger,
  service, message, optional exception type/message). Raw payloads, request
  bodies, and connector responses are NEVER log fields — those travel as
  hashes (``raw_response_hash``) with the raw archived to the object store.

Metrics: an in-process counter/gauge registry exportable as Prometheus-style
text. Values are integers only — deterministic, comparison-safe, and
consistent with the no-floats discipline everywhere else in the platform.

Sentry integration (optional — disabled when ``SENTRY_DSN`` is unset):

- :func:`init_sentry` initialises the SDK against the DSN from env.
- A :class:`BDPaySentryFilter` ``before_send`` hook runs every event and its
  breadcrumbs through the existing :func:`~bdpay.platform.pii.redact` function
  before the payload leaves the process — raw PII NEVER reaches the Sentry
  transport.
- ``environment`` and ``release`` are tagged from ``SENTRY_ENVIRONMENT`` and
  ``SENTRY_RELEASE`` env vars (or the supplied ``environment``/``release``
  keyword args).
- FastAPI: call :func:`instrument_fastapi` after creating the ``FastAPI``
  instance; it is a no-op when Sentry was not initialised.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from bdpay.platform.pii import PIIRedactionFilter, redact

__all__ = [
    "BDPaySentryFilter",
    "JsonLogFormatter",
    "MetricsRegistry",
    "attach_pii_filters",
    "configure_logging",
    "init_sentry",
    "instrument_fastapi",
]

_HANDLER_TAG = "_bdpay_observability_handler"
_METRIC_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_LABEL_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# ---------------------------------------------------------------------------
# Sentry integration
# ---------------------------------------------------------------------------

_SENTRY_DSN_ENV = "SENTRY_DSN"
_SENTRY_ENVIRONMENT_ENV = "SENTRY_ENVIRONMENT"
_SENTRY_RELEASE_ENV = "SENTRY_RELEASE"

_sentry_initialised = False


def _redact_sentry_string(value: Any) -> Any:
    """Apply PII redaction to any string value found in a Sentry payload."""
    if isinstance(value, str):
        return redact(value)
    return value


def _redact_sentry_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact all string leaves in a Sentry event dict."""
    result: dict[str, Any] = {}
    for k, v in mapping.items():
        if isinstance(v, dict):
            result[k] = _redact_sentry_mapping(v)
        elif isinstance(v, list):
            result[k] = _redact_sentry_list(v)
        else:
            result[k] = _redact_sentry_string(v)
    return result


def _redact_sentry_list(lst: list[Any]) -> list[Any]:
    result: list[Any] = []
    for item in lst:
        if isinstance(item, dict):
            result.append(_redact_sentry_mapping(item))
        elif isinstance(item, list):
            result.append(_redact_sentry_list(item))
        else:
            result.append(_redact_sentry_string(item))
    return result


class BDPaySentryFilter:
    """Sentry ``before_send`` hook: redacts PII from events before transport.

    Breadcrumbs and exception values in the event are passed through
    :func:`~bdpay.platform.pii.redact` so that NID, mobile, PAN, email, and
    account-number patterns never leave the process in a Sentry payload.

    Usage::

        sentry_sdk.init(dsn=..., before_send=BDPaySentryFilter())
    """

    def __call__(
        self,
        event: dict[str, Any],
        hint: dict[str, Any],  # noqa: ARG002 — required by Sentry protocol
    ) -> dict[str, Any] | None:
        return _redact_sentry_mapping(event)


def init_sentry(
    *,
    dsn: str | None = None,
    environment: str | None = None,
    release: str | None = None,
) -> bool:
    """Initialise the Sentry SDK when a DSN is available.

    The DSN is resolved from (in order):

    1. The explicit ``dsn`` keyword argument.
    2. The ``SENTRY_DSN`` environment variable.

    When no DSN is found the function is a no-op and returns ``False``.
    ``environment`` and ``release`` fall back to ``SENTRY_ENVIRONMENT`` and
    ``SENTRY_RELEASE`` env vars when not supplied explicitly.

    The :class:`BDPaySentryFilter` ``before_send`` hook is always installed —
    raw PII is NEVER sent regardless of how the SDK is otherwise configured.

    Returns ``True`` if Sentry was initialised, ``False`` if disabled.
    """
    global _sentry_initialised

    resolved_dsn = dsn or os.environ.get(_SENTRY_DSN_ENV) or ""
    if not resolved_dsn:
        return False

    try:
        import sentry_sdk  # deferred — optional dependency
    except ImportError:
        logging.getLogger(__name__).warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; Sentry disabled"
        )
        return False

    resolved_env = environment or os.environ.get(_SENTRY_ENVIRONMENT_ENV) or "production"
    resolved_release = release or os.environ.get(_SENTRY_RELEASE_ENV) or None

    sentry_sdk.init(
        dsn=resolved_dsn,
        environment=resolved_env,
        release=resolved_release,
        before_send=BDPaySentryFilter(),
        # Disable automatic PII collection at SDK level — belt-and-suspenders.
        send_default_pii=False,
    )
    _sentry_initialised = True
    return True


def instrument_fastapi(app: Any) -> None:  # noqa: ANN401 — FastAPI not a hard dep here
    """Attach Sentry FastAPI instrumentation to ``app`` if Sentry is active.

    Must be called AFTER :func:`init_sentry`. Safe to call when Sentry was not
    initialised — it becomes a no-op.
    """
    if not _sentry_initialised:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        # Re-configure to add framework integrations if not already present.
        # sentry_sdk.init is idempotent on a running client; adding integrations
        # post-init requires a new client — use the low-level Hub API.
        client = sentry_sdk.get_client()
        if client.options and not any(
            isinstance(i, FastApiIntegration)
            for i in (client.options.get("integrations") or [])
        ):
            sentry_sdk.init(
                dsn=client.options.get("dsn", ""),
                environment=client.options.get("environment", "production"),
                release=client.options.get("release"),
                before_send=BDPaySentryFilter(),
                send_default_pii=False,
                integrations=[
                    StarletteIntegration(),
                    FastApiIntegration(),
                ],
            )
    except ImportError:
        pass


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line; fixed fields; no raw payloads.

    Fields: ``ts`` (RFC3339 UTC), ``level``, ``logger``, ``service``,
    ``message``, and — only when an exception is attached — ``exc_type`` and
    ``exc_message`` (redacted). ``record.args`` were already consumed by the
    PII filter, so ``getMessage()`` returns the final redacted text.
    """

    def __init__(self, *, service: str = "bdpay") -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC)
        doc: dict[str, object] = {
            "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc_type"] = record.exc_info[0].__name__
            doc["exc_message"] = redact(str(record.exc_info[1]))
        return json.dumps(doc, ensure_ascii=False, sort_keys=True)


def attach_pii_filters(logger: logging.Logger, *, email_salt: str = "") -> int:
    """Ensure every handler on ``logger`` carries a PIIRedactionFilter.

    Returns the number of handlers that received a new filter. Idempotent —
    a handler that already has one is left alone.
    """
    attached = 0
    for handler in logger.handlers:
        if not any(isinstance(f, PIIRedactionFilter) for f in handler.filters):
            handler.addFilter(PIIRedactionFilter(email_salt=email_salt))
            attached += 1
    return attached


def configure_logging(
    *,
    level: int = logging.INFO,
    service: str = "bdpay",
    email_salt: str = "",
    stream: object | None = None,
    logger: logging.Logger | None = None,
) -> logging.Logger:
    """Configure JSON logging with PII redaction on every handler.

    Installs one tagged StreamHandler (JSON formatter + PII filter) on the
    target logger (the root logger by default) and adds the PII filter to any
    pre-existing handlers. Re-running replaces the tagged handler instead of
    stacking duplicates.
    """
    target = logger if logger is not None else logging.getLogger()
    target.setLevel(level)

    for handler in list(target.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            target.removeHandler(handler)

    handler = logging.StreamHandler(stream)  # type: ignore[arg-type]
    handler.setFormatter(JsonLogFormatter(service=service))
    handler.addFilter(PIIRedactionFilter(email_salt=email_salt))
    setattr(handler, _HANDLER_TAG, True)
    target.addHandler(handler)

    attach_pii_filters(target, email_salt=email_salt)
    return target


def _check_labels(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if labels is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for key in sorted(labels):
        if not _LABEL_NAME_RE.match(key):
            raise ValueError(f"invalid metric label name {key!r}")
        value = labels[key]
        if not isinstance(value, str):
            raise ValueError(f"label {key!r} value must be str, got {type(value).__name__}")
        pairs.append((key, value))
    return tuple(pairs)


class MetricsRegistry:
    """Thread-safe integer counter/gauge registry with text export.

    Counters only go up (negative increments are refused); gauges are set to
    an integer value. ``export_text()`` renders deterministic Prometheus-style
    exposition lines sorted by metric name and label set.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._kinds: dict[str, str] = {}

    def _register(self, name: str, kind: str) -> None:
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(f"invalid metric name {name!r} (snake_case required)")
        existing = self._kinds.get(name)
        if existing is None:
            self._kinds[name] = kind
        elif existing != kind:
            raise ValueError(
                f"metric {name!r} is already registered as a {existing}, not a {kind}"
            )

    @staticmethod
    def _check_value(value: int, what: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{what} must be int (no floats), got {type(value).__name__}")
        return value

    def increment(
        self, name: str, *, value: int = 1, labels: Mapping[str, str] | None = None
    ) -> int:
        """Add ``value`` (>= 0) to a counter; returns the new total."""
        amount = self._check_value(value, "counter increment")
        if amount < 0:
            raise ValueError("counters only go up; negative increments are refused")
        key = (name, _check_labels(labels))
        with self._lock:
            self._register(name, "counter")
            total = self._counters.get(key, 0) + amount
            self._counters[key] = total
        return total

    def set_gauge(
        self, name: str, value: int, *, labels: Mapping[str, str] | None = None
    ) -> None:
        """Set a gauge to an integer value."""
        amount = self._check_value(value, "gauge value")
        key = (name, _check_labels(labels))
        with self._lock:
            self._register(name, "gauge")
            self._gauges[key] = amount

    def get(self, name: str, *, labels: Mapping[str, str] | None = None) -> int | None:
        key = (name, _check_labels(labels))
        with self._lock:
            if name in self._kinds and self._kinds[name] == "counter":
                return self._counters.get(key)
            return self._gauges.get(key)

    @staticmethod
    def _render_labels(pairs: tuple[tuple[str, str], ...]) -> str:
        if not pairs:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in pairs)
        return "{" + inner + "}"

    def export_text(self) -> str:
        """Prometheus-style exposition text, deterministically ordered."""
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._kinds):
                kind = self._kinds[name]
                lines.append(f"# TYPE {name} {kind}")
                series = self._counters if kind == "counter" else self._gauges
                for (series_name, pairs), value in sorted(series.items()):
                    if series_name == name:
                        lines.append(f"{name}{self._render_labels(pairs)} {value}")
            return "\n".join(lines) + ("\n" if lines else "")
