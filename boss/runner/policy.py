"""Execution policy: permission profiles, command prefix rules, path/write boundaries, network policy."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PermissionProfile(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class CommandVerdict(StrEnum):
    ALLOWED = "allowed"
    PROMPT = "prompt"
    DENIED = "denied"


class NetworkPolicy(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    ALLOWLIST = "allowlist"


# Interpreter binaries that can execute arbitrary code inline via flags.
# When any of these appear with the corresponding flag in workspace_write
# mode the command is escalated to PROMPT because we cannot statically
# determine the write targets.
_INTERPRETER_INLINE_FLAGS: dict[str, tuple[str, ...]] = {
    "python": ("-c",),
    "python3": ("-c",),
    "node": ("-e", "--eval"),
    "sh": ("-c",),
    "bash": ("-c",),
    "zsh": ("-c",),
    "ruby": ("-e",),
    "perl": ("-e",),
}


_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    PermissionProfile.READ_ONLY: {
        "writable_roots": [],
        "network": NetworkPolicy.DISABLED,
        "allowed_prefixes": [],
        "prompt_prefixes": [],
        "denied_prefixes": [],
        "allow_shell": False,
    },
    PermissionProfile.WORKSPACE_WRITE: {
        "writable_roots": [],  # filled from workspace root at runtime
        "network": NetworkPolicy.DISABLED,
        "allowed_prefixes": [
            "git", "python", "python3", "pip", "npm", "npx", "node",
            "swift", "swiftc", "xcodebuild", "cargo", "rustc",
            "make", "cmake", "go", "pytest", "unittest",
            "cat", "head", "tail", "wc", "grep", "find", "ls", "echo",
            "diff", "patch", "sort", "uniq", "sed", "awk",
            "mkdir", "cp", "mv", "touch", "chmod",
        ],
        "prompt_prefixes": [
            "curl", "wget", "brew", "pip install", "npm install",
            "open", "osascript",
        ],
        "denied_prefixes": [
            "sudo", "rm -rf /", "mkfs", "dd if=", "diskutil erase",
            "launchctl", "defaults write", "shutdown", "reboot",
            "kill -9", "killall",
        ],
        "allow_shell": True,
    },
    PermissionProfile.FULL_ACCESS: {
        "writable_roots": [],
        "network": NetworkPolicy.ENABLED,
        "allowed_prefixes": [],
        "prompt_prefixes": [],
        "denied_prefixes": [
            "sudo", "rm -rf /", "mkfs", "dd if=", "diskutil erase",
            "shutdown", "reboot",
        ],
        "allow_shell": True,
    },
}

_MODE_PROFILE_DEFAULTS: dict[str, str] = {
    "ask": PermissionProfile.READ_ONLY,
    "plan": PermissionProfile.READ_ONLY,
    "review": PermissionProfile.READ_ONLY,
    "agent": PermissionProfile.WORKSPACE_WRITE,
    "deploy": PermissionProfile.FULL_ACCESS,
}


@dataclass(frozen=True)
class PathPolicy:
    writable_roots: tuple[Path, ...]
    workspace_root: Path | None = None

    def is_write_allowed(self, target: Path) -> bool:
        if not self.writable_roots:
            return True  # full_access: no restrictions
        resolved = target.resolve()
        for root in self.writable_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def explain_denial(self, target: Path) -> str:
        roots_str = ", ".join(str(root) for root in self.writable_roots)
        return f"Write to {target} denied: not within allowed roots [{roots_str}]"


@dataclass(frozen=True)
class ExecutionPolicy:
    profile: PermissionProfile
    path_policy: PathPolicy
    network: NetworkPolicy
    domain_allowlist: tuple[str, ...]
    allowed_prefixes: tuple[str, ...]
    prompt_prefixes: tuple[str, ...]
    denied_prefixes: tuple[str, ...]
    allow_shell: bool
    env_scrub_keys: tuple[str, ...]
    enforcement: str = "boss"  # "boss" or "os" — always "boss" for now

    def check_command(self, command: str | list[str]) -> CommandVerdict:
        """Check whether a command is allowed, needs prompting, or is denied."""
        normalized = _normalize_command(command)
        if not normalized:
            return CommandVerdict.DENIED

        # Denied prefixes checked first (most restrictive wins)
        for prefix in self.denied_prefixes:
            if normalized.startswith(prefix.lower()):
                return CommandVerdict.DENIED

        # In read_only mode, deny all shell execution
        if not self.allow_shell:
            return CommandVerdict.DENIED

        # Interpreter+inline-flag escalation: if the command runs an
        # interpreter with an inline code flag (e.g. python -c, sh -c),
        # we cannot determine what it will write so escalate to PROMPT.
        if self.profile != PermissionProfile.FULL_ACCESS:
            if _is_interpreter_inline(command):
                return CommandVerdict.PROMPT

        # In full_access with no allowed_prefixes, everything not denied is allowed
        if self.profile == PermissionProfile.FULL_ACCESS and not self.allowed_prefixes:
            # Check prompt_prefixes first
            for prefix in self.prompt_prefixes:
                if normalized.startswith(prefix.lower()):
                    return CommandVerdict.PROMPT
            return CommandVerdict.ALLOWED

        # Check prompt prefixes
        for prefix in self.prompt_prefixes:
            if normalized.startswith(prefix.lower()):
                return CommandVerdict.PROMPT

        # Check allowed prefixes
        for prefix in self.allowed_prefixes:
            if normalized.startswith(prefix.lower()):
                return CommandVerdict.ALLOWED

        # If allowed_prefixes are set and command doesn't match, it needs prompting
        if self.allowed_prefixes:
            return CommandVerdict.PROMPT

        return CommandVerdict.ALLOWED

    def check_write(self, target: Path) -> CommandVerdict:
        """Check whether a write to the given path is allowed."""
        if self.profile == PermissionProfile.READ_ONLY:
            return CommandVerdict.DENIED
        if self.profile == PermissionProfile.FULL_ACCESS and not self.path_policy.writable_roots:
            return CommandVerdict.ALLOWED
        if self.path_policy.is_write_allowed(target):
            return CommandVerdict.ALLOWED
        return CommandVerdict.PROMPT

    def check_network(self, domain: str | None = None) -> CommandVerdict:
        """Check whether network access is allowed, optionally for a specific domain."""
        if self.network == NetworkPolicy.DISABLED:
            return CommandVerdict.DENIED
        if self.network == NetworkPolicy.ENABLED:
            return CommandVerdict.ALLOWED
        # allowlist mode
        if domain and self.domain_allowlist:
            normalized_domain = domain.lower().strip()
            for allowed in self.domain_allowlist:
                if normalized_domain == allowed.lower() or normalized_domain.endswith("." + allowed.lower()):
                    return CommandVerdict.ALLOWED
            return CommandVerdict.DENIED
        if not domain:
            return CommandVerdict.PROMPT
        return CommandVerdict.DENIED

    def scrubbed_env(self) -> dict[str, str]:
        """Return a copy of os.environ with sensitive keys removed."""
        env = dict(os.environ)
        for key in self.env_scrub_keys:
            env.pop(key, None)
        # Always scrub common secrets regardless of config
        for key in list(env.keys()):
            key_upper = key.upper()
            if any(
                marker in key_upper
                for marker in ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "CREDENTIALS")
            ):
                # Keep OPENAI_API_KEY only in full_access mode
                if key_upper == "OPENAI_API_KEY" and self.profile == PermissionProfile.FULL_ACCESS:
                    continue
                env.pop(key, None)
        return env

    def to_dict(self) -> dict[str, Any]:
        """Serialize for diagnostics/status payloads."""
        return {
            "profile": self.profile.value,
            "enforcement": self.enforcement,
            "allow_shell": self.allow_shell,
            "network": self.network.value,
            "domain_allowlist": list(self.domain_allowlist),
            "writable_roots": [str(p) for p in self.path_policy.writable_roots],
            "workspace_root": str(self.path_policy.workspace_root) if self.path_policy.workspace_root else None,
            "allowed_prefixes": list(self.allowed_prefixes),
            "prompt_prefixes": list(self.prompt_prefixes),
            "denied_prefixes": list(self.denied_prefixes),
            "env_scrub_keys": list(self.env_scrub_keys),
        }


@dataclass(frozen=True)
class RunnerConfig:
    raw: dict[str, Any]
    default_profile: PermissionProfile
    mode_profiles: dict[str, PermissionProfile]
    network_enabled: bool
    domain_allowlist: tuple[str, ...]
    writable_roots: tuple[Path, ...]
    allowed_prefixes: tuple[str, ...]
    prompt_prefixes: tuple[str, ...]
    denied_prefixes: tuple[str, ...]
    env_scrub_keys: tuple[str, ...]


def load_runner_config(workspace_root: Path | str | None = None) -> RunnerConfig:
    """Load [runner] config from .boss/config.toml, merging with defaults."""
    from boss.control import load_boss_control

    control = load_boss_control(workspace_root)
    raw = control.config.raw.get("runner", {})
    if not isinstance(raw, dict):
        raw = {}

    default_profile = _parse_profile(raw.get("default_profile"), fallback=PermissionProfile.WORKSPACE_WRITE)

    mode_profiles: dict[str, PermissionProfile] = {}
    mode_profiles_raw = raw.get("mode_profiles", {})
    if isinstance(mode_profiles_raw, dict):
        for mode_name, profile_name in mode_profiles_raw.items():
            parsed = _parse_profile(profile_name)
            if parsed:
                mode_profiles[str(mode_name).lower()] = parsed

    network_enabled = _bool(raw.get("network_enabled"), default=False)
    domain_allowlist = _string_tuple(raw.get("domain_allowlist"))
    writable_roots = tuple(
        Path(p).expanduser() for p in _string_tuple(raw.get("writable_roots"))
    )
    env_scrub_keys = _string_tuple(raw.get("env_scrub_keys"))

    commands = raw.get("commands", {})
    if not isinstance(commands, dict):
        commands = {}

    allowed_prefixes = _string_tuple(commands.get("allowed_prefixes"))
    prompt_prefixes = _string_tuple(commands.get("prompt_prefixes"))
    denied_prefixes = _string_tuple(commands.get("denied_prefixes"))

    return RunnerConfig(
        raw=raw,
        default_profile=default_profile,
        mode_profiles=mode_profiles,
        network_enabled=network_enabled,
        domain_allowlist=domain_allowlist,
        writable_roots=writable_roots,
        allowed_prefixes=allowed_prefixes,
        prompt_prefixes=prompt_prefixes,
        denied_prefixes=denied_prefixes,
        env_scrub_keys=env_scrub_keys,
    )


def runner_config_for_mode(mode: str | None, workspace_root: Path | str | None = None) -> ExecutionPolicy:
    """Build an ExecutionPolicy from config for the given mode."""
    config = load_runner_config(workspace_root)
    resolved_mode = (mode or "agent").lower()

    # Resolve profile: mode_profiles > mode defaults > config default
    profile = config.mode_profiles.get(
        resolved_mode,
        _MODE_PROFILE_DEFAULTS.get(resolved_mode, config.default_profile),
    )

    defaults = _PROFILE_DEFAULTS[profile]
    ws_root = _resolve_workspace_root(workspace_root)

    # Build writable roots: config overrides + workspace root for workspace_write
    writable_roots = list(config.writable_roots)
    if profile == PermissionProfile.WORKSPACE_WRITE and ws_root:
        writable_roots.append(ws_root)
    # Add temp directory as always writable for workspace_write and full_access
    if profile != PermissionProfile.READ_ONLY:
        import tempfile
        writable_roots.append(Path(tempfile.gettempdir()))

    path_policy = PathPolicy(
        writable_roots=tuple(writable_roots),
        workspace_root=ws_root,
    )

    # Merge command prefixes: config overrides > profile defaults
    allowed_prefixes = config.allowed_prefixes or tuple(defaults.get("allowed_prefixes", []))
    prompt_prefixes = config.prompt_prefixes or tuple(defaults.get("prompt_prefixes", []))
    denied_prefixes = config.denied_prefixes or tuple(defaults.get("denied_prefixes", []))

    # Network policy
    if profile == PermissionProfile.READ_ONLY:
        network = NetworkPolicy.DISABLED
    elif config.network_enabled:
        network = NetworkPolicy.ALLOWLIST if config.domain_allowlist else NetworkPolicy.ENABLED
    else:
        network = NetworkPolicy(defaults.get("network", NetworkPolicy.DISABLED))

    return ExecutionPolicy(
        profile=profile,
        path_policy=path_policy,
        network=network,
        domain_allowlist=config.domain_allowlist,
        allowed_prefixes=allowed_prefixes,
        prompt_prefixes=prompt_prefixes,
        denied_prefixes=denied_prefixes,
        allow_shell=defaults.get("allow_shell", False),
        env_scrub_keys=config.env_scrub_keys,
        enforcement="boss",
    )


def _normalize_command(command: str | list[str]) -> str:
    if isinstance(command, list):
        text = " ".join(str(part) for part in command)
    else:
        text = str(command)
    return text.strip().lower()


def _is_interpreter_inline(command: str | list[str]) -> bool:
    """Return True if command invokes an interpreter with an inline-code flag."""
    if isinstance(command, str):
        parts = command.split()
    else:
        parts = [str(p) for p in command]
    if not parts:
        return False
    binary = parts[0].rsplit("/", 1)[-1].lower()
    flags = _INTERPRETER_INLINE_FLAGS.get(binary)
    if not flags:
        return False
    for part in parts[1:]:
        if part in flags:
            return True
    return False


def _parse_profile(value: Any, fallback: PermissionProfile | None = None) -> PermissionProfile | None:
    if value is None:
        return fallback
    text = str(value).strip().lower()
    try:
        return PermissionProfile(text)
    except ValueError:
        return fallback


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return (text,) if text else ()


def _resolve_workspace_root(workspace_root: Path | str | None) -> Path | None:
    if workspace_root is None:
        from boss.control import default_workspace_root
        return default_workspace_root()
    root = Path(workspace_root).expanduser()
    return root if root.is_dir() else root.parent
