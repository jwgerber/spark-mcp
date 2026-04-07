"""Security regression tests for the hardening in commit 6a70ee8.

These tests verify that the path sandbox, attachment filename sanitizer,
and template name validator actually prevent the attacks documented in
the original security review. They run without Spark Desktop installed
and without the ``mcp`` package available (server.py is not imported).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from spark_mcp import config as config_mod
from spark_mcp.config import (
    UnsafePathError,
    resolve_safe_path,
    validate_template_name,
)
from spark_mcp.database import SparkDatabase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the sandbox allow-list at an isolated tmp dir for the test.

    We monkeypatch ``_allowed_roots`` so the test never depends on the
    developer's real ``~/Downloads`` and so every test is hermetic.
    """
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "nested").mkdir()

    def fake_roots():
        return [root.resolve()]

    monkeypatch.setattr(config_mod, "_allowed_roots", fake_roots)
    return root


# ---------------------------------------------------------------------------
# resolve_safe_path — positive cases
# ---------------------------------------------------------------------------


class TestResolveSafePathAllowed:
    def test_file_inside_root_is_allowed(self, sandbox):
        target = sandbox / "foo.pdf"
        resolved = resolve_safe_path(str(target), require_suffix=[".pdf"])
        assert resolved == target.resolve()

    def test_nested_subdirectory_is_allowed(self, sandbox):
        target = sandbox / "nested" / "deep" / "bar.pdf"
        resolved = resolve_safe_path(str(target), require_suffix=[".pdf"])
        assert resolved == target.resolve()

    def test_must_exist_true_with_existing_file(self, sandbox):
        target = sandbox / "exists.pdf"
        target.write_bytes(b"%PDF-1.4 test")
        resolved = resolve_safe_path(
            str(target), must_exist=True, require_suffix=[".pdf"]
        )
        assert resolved == target.resolve()

    def test_png_suffix_allowed_for_images(self, sandbox):
        target = sandbox / "sig.png"
        resolved = resolve_safe_path(
            str(target), require_suffix=[".png", ".jpg", ".jpeg"]
        )
        assert resolved.suffix == ".png"

    def test_tilde_expansion(self, sandbox, monkeypatch):
        # Re-point home so that ~ expands inside the sandbox.
        monkeypatch.setenv("HOME", str(sandbox))
        resolved = resolve_safe_path("~/foo.pdf", require_suffix=[".pdf"])
        assert resolved == (sandbox / "foo.pdf").resolve()


# ---------------------------------------------------------------------------
# resolve_safe_path — attack cases
# ---------------------------------------------------------------------------


class TestResolveSafePathBlocked:
    def test_absolute_system_path_rejected(self, sandbox):
        with pytest.raises(UnsafePathError):
            resolve_safe_path("/etc/passwd")

    def test_dot_dot_traversal_rejected(self, sandbox):
        escape = sandbox / ".." / ".." / "etc" / "passwd"
        with pytest.raises(UnsafePathError):
            resolve_safe_path(str(escape))

    def test_home_dotfile_rejected(self, sandbox, monkeypatch):
        # Real ~/.ssh/authorized_keys shape — must be rejected even when
        # HOME is outside the sandbox.
        monkeypatch.setenv("HOME", "/Users/nobody")
        with pytest.raises(UnsafePathError):
            resolve_safe_path("~/.ssh/authorized_keys")

    def test_launch_agents_plist_rejected(self, sandbox, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/nobody")
        with pytest.raises(UnsafePathError):
            resolve_safe_path("~/Library/LaunchAgents/com.evil.plist")

    def test_wrong_suffix_rejected(self, sandbox):
        with pytest.raises(UnsafePathError):
            resolve_safe_path(
                str(sandbox / "foo.txt"), require_suffix=[".pdf"]
            )

    def test_must_exist_rejects_missing(self, sandbox):
        with pytest.raises(UnsafePathError):
            resolve_safe_path(
                str(sandbox / "not_here.pdf"),
                must_exist=True,
                require_suffix=[".pdf"],
            )

    def test_empty_string_rejected(self, sandbox):
        with pytest.raises(UnsafePathError):
            resolve_safe_path("")

    def test_none_rejected(self, sandbox):
        with pytest.raises(UnsafePathError):
            resolve_safe_path(None)  # type: ignore[arg-type]

    def test_symlink_escape_rejected(self, sandbox, tmp_path):
        """A symlink inside the sandbox that points outside must be rejected.

        This is the most important adversarial case: the visible path looks
        safe, but ``resolve()`` follows the link and the containment check
        catches the escape.
        """
        outside = tmp_path / "outside_secret.pdf"
        outside.write_bytes(b"secret")
        link = sandbox / "innocent.pdf"
        os.symlink(outside, link)

        with pytest.raises(UnsafePathError):
            resolve_safe_path(
                str(link), must_exist=True, require_suffix=[".pdf"]
            )

    def test_suffix_check_is_case_insensitive(self, sandbox):
        # Allowed even if the caller uses uppercase extension.
        resolved = resolve_safe_path(
            str(sandbox / "FOO.PDF"), require_suffix=[".pdf"]
        )
        assert resolved.suffix.lower() == ".pdf"


# ---------------------------------------------------------------------------
# validate_template_name
# ---------------------------------------------------------------------------


class TestValidateTemplateName:
    @pytest.mark.parametrize(
        "name",
        [
            "good",
            "good_name",
            "good-name",
            "Mixed_Case-1",
            "a",
            "A" * 64,  # boundary
        ],
    )
    def test_valid_names(self, name):
        assert validate_template_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "a" * 65,  # over boundary
            "has.dot",
            "has/slash",
            "has\\backslash",
            "has space",
            "../etc/passwd",
            "..",
            ".",
            "foo\x00bar",
            "foo;rm -rf",
        ],
    )
    def test_invalid_names(self, name):
        with pytest.raises(UnsafePathError):
            validate_template_name(name)

    def test_non_string_rejected(self):
        with pytest.raises(UnsafePathError):
            validate_template_name(None)  # type: ignore[arg-type]
        with pytest.raises(UnsafePathError):
            validate_template_name(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SparkDatabase._get_attachment_path
# ---------------------------------------------------------------------------


class TestAttachmentPath:
    @pytest.fixture
    def db(self):
        # Lazy init: construction no longer touches the filesystem.
        return SparkDatabase()

    @pytest.mark.parametrize(
        "filename",
        [
            "../../../../etc/passwd",
            "../../Documents/secret.txt",
            "/etc/passwd",
            "foo\x00.txt",
            "..",
            ".",
            "",
            "foo/bar.txt",
            "foo\\bar.txt",
        ],
    )
    def test_traversal_attempts_rejected(self, db, filename):
        assert db._get_attachment_path(42, filename) is None

    def test_none_filename_rejected(self, db):
        assert db._get_attachment_path(42, None) is None

    def test_non_string_filename_rejected(self, db):
        assert db._get_attachment_path(42, 42) is None  # type: ignore[arg-type]

    def test_clean_filename_returns_containment(self, db):
        """A safe filename must return a path that lives inside the
        message's own cache directory."""
        path = db._get_attachment_path(42, "invoice.pdf")
        assert path is not None
        assert "messagesData" in str(path)
        assert "/42/invoice.pdf" in str(path)
        # Must be inside the cache tree, not elsewhere
        assert "Spark Desktop" in str(path)


# ---------------------------------------------------------------------------
# Lazy DB init
# ---------------------------------------------------------------------------


class TestLazyDbInit:
    def test_construction_does_not_raise_when_spark_missing(self):
        # The whole point: server.py imports SparkDatabase at startup.
        # Construction must not touch the filesystem anymore.
        SparkDatabase()

    def test_connect_messages_raises_when_missing(self, monkeypatch, tmp_path):
        db = SparkDatabase()
        # Point at a path that definitely doesn't exist.
        db.messages_db_path = tmp_path / "nope.sqlite"
        with pytest.raises(FileNotFoundError):
            db._connect_messages()


# ---------------------------------------------------------------------------
# _safe_signature_image (default path must go through the sandbox)
# ---------------------------------------------------------------------------


class TestSafeSignatureImage:
    def test_default_signature_outside_sandbox_rejected(
        self, sandbox, monkeypatch
    ):
        """Regression for the fix after the reviewer caught the trusted-
        default bypass: a configured default that points outside the
        allow-list must be rejected, not silently used."""
        from spark_mcp import pdf_operations

        # Pretend config has a default sig pointing outside the sandbox.
        monkeypatch.setattr(
            pdf_operations,
            "get_signature_path",
            lambda: "/tmp/evil/sig.png",
        )
        with pytest.raises(UnsafePathError):
            pdf_operations._safe_signature_image(None)

    def test_default_signature_inside_sandbox_accepted(
        self, sandbox, monkeypatch
    ):
        from spark_mcp import pdf_operations

        sig = sandbox / "sig.png"
        sig.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header bytes
        monkeypatch.setattr(
            pdf_operations, "get_signature_path", lambda: str(sig)
        )
        resolved = pdf_operations._safe_signature_image(None)
        assert resolved == sig.resolve()

    def test_no_default_and_no_arg_raises(self, sandbox, monkeypatch):
        from spark_mcp import pdf_operations

        monkeypatch.setattr(
            pdf_operations, "get_signature_path", lambda: None
        )
        with pytest.raises(FileNotFoundError):
            pdf_operations._safe_signature_image(None)

    def test_caller_supplied_outside_sandbox_rejected(self, sandbox):
        from spark_mcp import pdf_operations

        with pytest.raises(UnsafePathError):
            pdf_operations._safe_signature_image("/etc/shadow")
