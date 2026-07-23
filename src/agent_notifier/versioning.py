"""Runtime compatibility gates for externally versioned protocols."""

import re

SUPPORTED_VERSIONS = {
    "codex": {"0.145.0"},
    "cc-connect": {"1.3.2"},
}


class CompatibilityError(RuntimeError):
    pass


def parse_codex_version(output: str) -> str:
    match = re.search(r"\bcodex-cli\s+(\d+\.\d+\.\d+)\b", output)
    if not match:
        raise CompatibilityError("unable to parse Codex CLI version")
    return match.group(1)


def parse_cc_connect_version(output: str) -> str:
    match = re.search(r"\bcc-connect\s+v(\d+\.\d+\.\d+)\b", output)
    if not match:
        raise CompatibilityError("unable to parse cc-connect version")
    return match.group(1)


def require_supported_version(component: str, version: str) -> None:
    supported = SUPPORTED_VERSIONS.get(component)
    if supported is None:
        raise CompatibilityError(f"unknown component: {component}")
    if version not in supported:
        expected = ", ".join(sorted(supported))
        raise CompatibilityError(
            f"unsupported {component} version {version}; verified versions: {expected}"
        )
