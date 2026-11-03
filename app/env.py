"""Minimal .env loader (no extra dependency). Never overrides variables already set."""
from __future__ import annotations
import os
import stat


def load_env_file(path: str, environ=None, override: bool = False) -> dict:
    environ = os.environ if environ is None else environ
    result = {"loaded": [], "warnings": []}
    if not os.path.exists(path):
        result["warnings"].append(f"{path} not found (defaults apply: PAPER mode)")
        return result
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        result["warnings"].append(f"{path} is readable by other users; run: chmod 600 {path}")
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if not k.isidentifier():
                continue
            if override or k not in environ:
                environ[k] = v
                result["loaded"].append(k)
    return result
