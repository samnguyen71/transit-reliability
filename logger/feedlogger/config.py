"""Load and check config.toml.

Write ${NAME} in a URL, header value or healthcheck URL to read it from the
environment variable NAME. Keep those values (API keys, Healthchecks ping
URLs) in .env, which is never committed.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "transit-reliability-logger/0.1"
MIN_INTERVAL_SECONDS = 10

# Realtime feeds are small and change every few seconds. A static GTFS zip
# is large and changes a few times a year.
KIND_DEFAULTS = {
    "realtime": {"interval_seconds": 30, "timeout_seconds": 20, "max_mb": 50},
    "static": {"interval_seconds": 6 * 3600, "timeout_seconds": 300, "max_mb": 500},
}

_TOP_KEYS = {"data_dir", "user_agent", "min_free_gb", "feeds"}
_FEED_KEYS = {
    "name",
    "url",
    "kind",
    "interval_seconds",
    "timeout_seconds",
    "max_mb",
    "headers",
    "healthcheck_url",
}
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Query parameters that usually carry credentials, e.g. ?api_key=... or &token=...
_SECRET_PARAM_RE = re.compile(
    r"(?i)([?&][\w.-]*(?:key|token|secret|auth|pass|sig)[\w.-]*=)[^&#\s'\"]+"
)


class ConfigError(Exception):
    """A problem in config.toml or .env that has to be fixed first."""


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    kind: str  # "realtime" or "static"
    interval: float  # seconds between fetches
    timeout: float  # seconds allowed for one whole download
    max_bytes: int
    headers: Mapping[str, str]
    healthcheck_url: str | None = None


@dataclass(frozen=True)
class Config:
    data_dir: Path
    user_agent: str
    min_free_gb: float
    feeds: tuple[Feed, ...]
    secrets: frozenset[str] = frozenset()  # values to hide in logs

    def get_feed(self, name: str) -> Feed | None:
        return next((f for f in self.feeds if f.name == name), None)

    def redact(self, text: str) -> str:
        return redact(text, self.secrets)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Hide secret values and credential-looking URL parameters in `text`."""
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return _SECRET_PARAM_RE.sub(r"\1***", text)


def display_url(url: str) -> str:
    """The URL without credentials or query string, safe to print."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    query = "?..." if parts.query else ""
    return f"{parts.scheme}://{host}{parts.path}{query}"


def load_config(
    path: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    strict: bool = True,
) -> Config:
    """Read and check config.toml.

    With strict=False (used by `report`), missing ${NAME} values and
    placeholder URLs are allowed, since reporting needs neither.
    """
    env = os.environ if env is None else env
    path = Path(path)
    if path.is_dir():
        raise ConfigError(
            f"{path} is a folder, not a file. Docker creates a folder when the file it "
            "should mount doesn't exist yet. Delete the folder, copy "
            "config.example.toml to config.toml, and start again."
        )
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"{path} not found. Copy config.example.toml to config.toml and edit it."
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} isn't valid TOML: {e}") from None

    problems: list[str] = []
    secrets: set[str] = set()

    def expand(value: str, where: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            found = env.get(name, "")
            if not found and strict:
                problems.append(f"${{{name}}} in {where} is empty or not set: add it to .env")
            if len(found) >= 4:
                secrets.add(found)
            return found

        return _ENV_RE.sub(substitute, value)

    for key in sorted(set(raw) - _TOP_KEYS):
        problems.append(f"unknown setting '{key}' (expected: {', '.join(sorted(_TOP_KEYS))})")

    data_dir = raw.get("data_dir", "/data/raw")
    if not isinstance(data_dir, str) or not data_dir:
        problems.append("data_dir must be a folder path")
        data_dir = "/data/raw"
    user_agent = raw.get("user_agent", DEFAULT_USER_AGENT)
    if not isinstance(user_agent, str) or not user_agent.strip():
        problems.append("user_agent must be text")
        user_agent = DEFAULT_USER_AGENT
    min_free_gb = _number(raw.get("min_free_gb", 2), "min_free_gb", 0, problems)

    feeds_raw = raw.get("feeds")
    if not isinstance(feeds_raw, list) or not feeds_raw:
        problems.append("no feeds: add at least one [[feeds]] section")
        feeds_raw = []

    feeds: list[Feed] = []
    seen: set[str] = set()
    for index, item in enumerate(feeds_raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"feed #{index} must be a [[feeds]] section")
            continue
        name = item.get("name")
        label = f"feed '{name}'" if isinstance(name, str) and name else f"feed #{index}"
        for key in sorted(set(item) - _FEED_KEYS):
            problems.append(
                f"{label}: unknown setting '{key}' (expected: {', '.join(sorted(_FEED_KEYS))})"
            )
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            problems.append(
                f"{label}: name must use lowercase letters, digits, '-' or '_' "
                "(it becomes a folder name)"
            )
            name = f"feed{index}"
        elif name in seen:
            problems.append(f"{label}: two feeds have this name")
        seen.add(name)

        kind = item.get("kind", "realtime")
        if kind not in KIND_DEFAULTS:
            problems.append(f"{label}: kind must be 'realtime' or 'static'")
            kind = "realtime"
        defaults = KIND_DEFAULTS[kind]

        url = item.get("url")
        if not isinstance(url, str) or not url:
            problems.append(f"{label}: url is required")
            url = ""
        url = expand(url, f"{label} url")
        if url and urlsplit(url).scheme not in ("http", "https"):
            problems.append(f"{label}: url must start with https:// or http://")
        elif strict and (urlsplit(url).hostname or "").endswith(".example"):
            problems.append(f"{label}: replace the placeholder URL with your agency's feed URL")

        interval = _number(
            item.get("interval_seconds", defaults["interval_seconds"]),
            f"{label}: interval_seconds",
            MIN_INTERVAL_SECONDS,
            problems,
        )
        timeout = _number(
            item.get("timeout_seconds", defaults["timeout_seconds"]),
            f"{label}: timeout_seconds",
            1,
            problems,
        )
        max_mb = _number(item.get("max_mb", defaults["max_mb"]), f"{label}: max_mb", 1, problems)

        headers_raw = item.get("headers", {})
        headers: dict[str, str] = {}
        if not isinstance(headers_raw, dict) or not all(
            isinstance(v, str) for v in headers_raw.values()
        ):
            problems.append(
                f"{label}: headers must look like "
                'headers = { "x-api-key" = "${TRANSIT_API_KEY}" }'
            )
        else:
            headers = {k: expand(v, f"{label} header '{k}'") for k, v in headers_raw.items()}

        healthcheck_url = item.get("healthcheck_url")
        if healthcheck_url is not None:
            if not isinstance(healthcheck_url, str):
                problems.append(f"{label}: healthcheck_url must be text")
                healthcheck_url = None
            else:
                healthcheck_url = expand(healthcheck_url, f"{label} healthcheck_url") or None
                if healthcheck_url:
                    if urlsplit(healthcheck_url).scheme not in ("http", "https"):
                        problems.append(f"{label}: healthcheck_url must start with https://")
                    # The ping URL is a credential of sorts: anyone with it can ping.
                    secrets.add(healthcheck_url)
                    if len(urlsplit(healthcheck_url).path) >= 8:
                        secrets.add(urlsplit(healthcheck_url).path)

        feeds.append(
            Feed(
                name=name,
                url=url,
                kind=kind,
                interval=interval or defaults["interval_seconds"],
                timeout=timeout or defaults["timeout_seconds"],
                max_bytes=int((max_mb or defaults["max_mb"]) * 1024 * 1024),
                headers=headers,
                healthcheck_url=healthcheck_url,
            )
        )

    if problems:
        raise ConfigError(f"fix these in {path}:\n  - " + "\n  - ".join(problems))
    return Config(
        data_dir=Path(data_dir),
        user_agent=user_agent.strip(),
        min_free_gb=min_free_gb or 0.0,
        feeds=tuple(feeds),
        secrets=frozenset(secrets),
    )


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a .env file the way Docker Compose does for simple cases.

    Lines look like KEY=value. Blank lines and lines starting with # are
    skipped, and an unquoted value ends at " #".
    """
    path = Path(path)
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            raise ConfigError(f"{path}, line {number}: expected KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _number(value: object, where: str, minimum: float, problems: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{where} must be a number")
        return None
    if value < minimum:
        problems.append(f"{where} must be at least {minimum:g}")
        return None
    return float(value)
