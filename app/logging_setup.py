"""Logging with secret redaction applied to the FINAL text (message + traceback)."""
from __future__ import annotations
import logging
import logging.handlers
import os
import re

SECRET_KEY_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE)", re.I)
JWT_PATTERN = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")


def secret_values(environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    vals = [v for k, v in environ.items() if SECRET_KEY_PATTERN.search(k) and v and len(v) >= 6]
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, "***REDACTED***")
    return JWT_PATTERN.sub("***JWT***", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets_provider=secret_values):
        super().__init__(fmt)
        self._secrets = secrets_provider

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets())


def setup_logging(log_dir: str, level=logging.INFO) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.TimedRotatingFileHandler(os.path.join(log_dir, "agent.log"),
                                                   when="midnight", backupCount=30, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)
    return root
