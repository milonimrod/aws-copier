"""Behaviour-proving tests for aws_copier.core.ignore_rules (IGNORE-01, IGNORE-02, IGNORE-03)."""

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aws_copier.core.ignore_rules import IGNORE_RULES, IGNORE_DIRS, GLOB_PATTERNS, SENSITIVE_DENY, IgnoreRules


class TestIgnoreRulesGlobPatterns:
    """IGNORE-01: fnmatch-based glob patterns correctly match filenames."""

    def test_bak_extension_matched(self):
        """report.bak must be ignored via *.bak glob."""
        assert IGNORE_RULES.should_ignore_file(Path("report.bak")) is True

    def test_pyc_extension_matched(self):
        """script.pyc must be ignored via *.pyc glob."""
        assert IGNORE_RULES.should_ignore_file(Path("script.pyc")) is True

    def test_tilde_suffix_matched(self):
        """notes~ must be ignored via *~ glob."""
        assert IGNORE_RULES.should_ignore_file(Path("notes~")) is True

    def test_tmp_extension_matched(self):
        """data.tmp must be ignored via *.tmp glob."""
        assert IGNORE_RULES.should_ignore_file(Path("data.tmp")) is True

    def test_exact_ds_store_matched(self):
        """.DS_Store matches both dot-file rule and explicit entry."""
        assert IGNORE_RULES.should_ignore_file(Path(".DS_Store")) is True

    def test_normal_file_not_ignored(self):
        """Ordinary files are NOT ignored — this is the regression guard."""
        assert IGNORE_RULES.should_ignore_file(Path("report.pdf")) is False
        assert IGNORE_RULES.should_ignore_file(Path("document.txt")) is False
        assert IGNORE_RULES.should_ignore_file(Path("image.jpg")) is False


class TestIgnoreRulesSensitiveDeny:
    """IGNORE-02: sensitive files (credentials, keys) are blocked."""

    def test_env_file_blocked(self):
        """.env file must be ignored (dot-file rule)."""
        assert IGNORE_RULES.should_ignore_file(Path(".env")) is True

    def test_env_variant_blocked(self):
        """.env.local must be ignored (dot-file rule)."""
        assert IGNORE_RULES.should_ignore_file(Path(".env.local")) is True

    def test_pem_file_blocked(self):
        """cert.pem must be ignored via *.pem sensitive_deny pattern."""
        assert IGNORE_RULES.should_ignore_file(Path("cert.pem")) is True

    def test_key_file_blocked(self):
        """private.key must be ignored via *.key sensitive_deny pattern."""
        assert IGNORE_RULES.should_ignore_file(Path("private.key")) is True

    def test_ssh_rsa_blocked(self):
        """id_rsa must be ignored via exact match in sensitive_deny."""
        assert IGNORE_RULES.should_ignore_file(Path("id_rsa")) is True

    def test_ssh_ed25519_blocked(self):
        """id_ed25519 must be ignored via exact match."""
        assert IGNORE_RULES.should_ignore_file(Path("id_ed25519")) is True

    def test_npmrc_blocked(self):
        """.npmrc must be ignored (dot-file rule)."""
        assert IGNORE_RULES.should_ignore_file(Path(".npmrc")) is True

    def test_netrc_blocked(self):
        """.netrc must be ignored (dot-file rule)."""
        assert IGNORE_RULES.should_ignore_file(Path(".netrc")) is True


class TestIgnoreRulesDirs:
    """Directory ignore logic — used by FileListener to prune scan recursion."""

    def test_git_dir_ignored(self):
        assert IGNORE_RULES.should_ignore_dir(Path(".git")) is True

    def test_pycache_dir_ignored(self):
        assert IGNORE_RULES.should_ignore_dir(Path("__pycache__")) is True

    def test_node_modules_dir_ignored(self):
        assert IGNORE_RULES.should_ignore_dir(Path("node_modules")) is True

    def test_venv_dir_ignored(self):
        assert IGNORE_RULES.should_ignore_dir(Path(".venv")) is True

    def test_hidden_dir_ignored(self):
        """Arbitrary hidden dir names are skipped."""
        assert IGNORE_RULES.should_ignore_dir(Path(".secret_dir")) is True

    def test_normal_dir_not_ignored(self):
        assert IGNORE_RULES.should_ignore_dir(Path("src")) is False
        assert IGNORE_RULES.should_ignore_dir(Path("Documents")) is False

    def test_symlink_dir_ignored(self, tmp_path):
        """Symlinks to directories are skipped to avoid cycles."""
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")
        assert IGNORE_RULES.should_ignore_dir(link) is True


class TestIgnoreRulesShouldIgnorePath:
    """WATCH-01: should_ignore_path also catches files inside an ignored ancestor directory —
    the gap should_ignore_file alone has, which real-time watchdog events hit (no batch-scan
    should_ignore_dir recursion guard applies to a raw filesystem event)."""

    def test_file_directly_in_ignored_dotdir_is_ignored(self, tmp_path):
        """A file inside a dot-prefixed directory anywhere under root is ignored."""
        path = tmp_path / ".sync" / "pictures_pictures" / "bisync.db"
        assert IGNORE_RULES.should_ignore_path(path, tmp_path) is True

    def test_file_in_named_ignored_ancestor_is_ignored(self):
        """A file under a named ignore_dirs ancestor (e.g. node_modules) is ignored, at any depth."""
        root = Path("/watch")
        path = root / "project" / "node_modules" / "pkg" / "index.js"
        assert IGNORE_RULES.should_ignore_path(path, root) is True

    def test_normal_file_in_normal_dir_is_not_ignored(self):
        """A plain file in a plain (non-ignored) directory tree is not ignored."""
        root = Path("/watch")
        path = root / "Pictures" / "2024" / "IMG_0001.JPG"
        assert IGNORE_RULES.should_ignore_path(path, root) is False

    def test_ignored_filename_itself_is_still_caught(self):
        """A dotfile directly under root (no ignored ancestor dir involved) is still caught."""
        root = Path("/watch")
        path = root / ".env"
        assert IGNORE_RULES.should_ignore_path(path, root) is True

    def test_watch_root_itself_is_never_treated_as_ignored_ancestor(self):
        """root is excluded from the ancestor check — a dot-prefixed watch root wouldn't self-ignore."""
        root = Path("/watch/.hidden_root")
        path = root / "photo.jpg"
        assert IGNORE_RULES.should_ignore_path(path, root) is False

    def test_path_outside_root_falls_back_to_filename_only_check(self):
        """A path not under root (ValueError from relative_to) still checks the filename itself."""
        root = Path("/watch")
        outside = Path("/other/place/.env")
        assert IGNORE_RULES.should_ignore_path(outside, root) is True  # filename itself ignored
        outside_normal = Path("/other/place/photo.jpg")
        assert IGNORE_RULES.should_ignore_path(outside_normal, root) is False


class TestIgnoreRulesImmutability:
    """D-05: IgnoreRules must be frozen — prevents accidental mutation of the singleton."""

    def test_instance_is_frozen(self):
        """Attempting to set an attribute raises FrozenInstanceError."""
        with pytest.raises(FrozenInstanceError):
            IGNORE_RULES.glob_patterns = frozenset()  # type: ignore[misc]


class TestIgnoreRulesSingleton:
    """D-06: IGNORE_RULES is the canonical instance — FileListener & FileChangeHandler both import this."""

    def test_singleton_importable(self):
        assert isinstance(IGNORE_RULES, IgnoreRules)

    def test_singleton_uses_module_constants(self):
        """Singleton's fields match the module-level constants."""
        assert IGNORE_RULES.glob_patterns == GLOB_PATTERNS
        assert IGNORE_RULES.sensitive_deny == SENSITIVE_DENY
        assert IGNORE_RULES.ignore_dirs == IGNORE_DIRS
