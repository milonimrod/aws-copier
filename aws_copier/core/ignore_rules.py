"""Centralized ignore rules for file and directory filtering."""

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet

logger = logging.getLogger(__name__)


# Glob patterns applied to filenames via fnmatch.fnmatch (IGNORE-01)
GLOB_PATTERNS: FrozenSet[str] = frozenset(
    {
        # System files (cross-platform)
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        # Windows system files
        "hiberfil.sys",
        "pagefile.sys",
        "swapfile.sys",
        # Temporary / editor swap files
        "*.tmp",
        "*.temp",
        "*.swp",
        "*.swo",
        # Build artifacts
        "*.pyc",
        "*.pyo",
        # Backup / editor backup files
        "*.bak",
        "*.backup",
        "*~",
        # Coverage artifacts
        ".coverage",
        # Our own tracking file
        ".milo_backup.info",
    }
)

# Sensitive / credential file deny list (IGNORE-02). Blocked via fnmatch.fnmatch.
SENSITIVE_DENY: FrozenSet[str] = frozenset(
    {
        # dotenv
        ".env",
        ".env.*",
        # TLS / private keys
        "*.pem",
        "*.key",
        # SSH private keys
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        # PKCS certs
        "*.p12",
        "*.pfx",
        # Credential config files
        ".npmrc",
        ".pypirc",
        ".netrc",
        # Generic secret pattern
        "*.secret",
    }
)

# Directory names to skip entirely (name-match, not glob)
IGNORE_DIRS: FrozenSet[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".venv",
        "venv",
        ".aws-copier",
        "$RECYCLE.BIN",
        "System Volume Information",
        ".Trashes",
        ".Spotlight-V100",
        ".fseventsd",
        ".vscode",
        ".idea",
    }
)


@dataclass(frozen=True)
class IgnoreRules:
    """Centralized, immutable ignore-rule set consumed by FileListener and FileChangeHandler."""

    glob_patterns: FrozenSet[str] = field(default_factory=lambda: GLOB_PATTERNS)
    sensitive_deny: FrozenSet[str] = field(default_factory=lambda: SENSITIVE_DENY)
    ignore_dirs: FrozenSet[str] = field(default_factory=lambda: IGNORE_DIRS)

    def should_ignore_file(self, path: Path) -> bool:
        """Return True if the file must not be uploaded to S3.

        Args:
            path: Path to the file being checked. Only `path.name` is used.

        Returns:
            True when the file matches any dot-prefix rule, sensitive deny pattern,
            or glob pattern; False otherwise.
        """
        name = path.name
        # Dot-prefix files always ignored (IGNORE-02 — includes .env, .npmrc, etc.)
        if name.startswith("."):
            return True
        # Sensitive file deny list (IGNORE-02)
        for pattern in self.sensitive_deny:
            if fnmatch.fnmatch(name, pattern):
                return True
        # Standard glob patterns (IGNORE-01)
        for pattern in self.glob_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def should_ignore_dir(self, path: Path) -> bool:
        """Return True if the directory should be skipped entirely during scan.

        Args:
            path: Path to the directory being checked. Only `path.name` is used.

        Returns:
            True when the directory matches a name in `ignore_dirs`, starts with
            a dot, or is a symlink; False otherwise.
        """
        name = path.name
        if name in self.ignore_dirs:
            return True
        if name.startswith("."):
            return True
        try:
            if path.is_symlink():
                return True
        except Exception:
            # If we cannot determine symlink status, err on the side of skipping.
            return True
        return False


# Module-level singleton — import this, do not instantiate IgnoreRules directly.
IGNORE_RULES: IgnoreRules = IgnoreRules()
