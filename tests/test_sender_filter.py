"""Tests for the sender filter rewrite (spec Part 2 + Part 4 matrix)."""

from __future__ import annotations

import pytest

from spark_mcp.database import _localpart_candidates, _parse_display_name


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------

class TestLocalpartCandidates:
    def test_christine_taylo_generates_ctaylo(self):
        cands = _localpart_candidates("Christine Taylo")
        assert "ctaylo" in cands
        assert "christine.taylo" in cands
        assert "christine" in cands
        assert "taylo" in cands

    def test_email_is_not_a_name(self):
        assert _localpart_candidates("foo@bar.com") == []

    def test_domain_is_not_a_name(self):
        assert _localpart_candidates("example.com") == []

    def test_empty_returns_empty(self):
        assert _localpart_candidates("") == []
        assert _localpart_candidates("   ") == []


class TestParseDisplayName:
    def test_quoted_name(self):
        assert _parse_display_name('"Marie Keup" <m.keup@taylorwessing.com>') == "Marie Keup"

    def test_unquoted_name(self):
        assert _parse_display_name('Christine Smith <csmith@example.com>') == "Christine Smith"

    def test_no_display_name(self):
        assert _parse_display_name("ctaylo@uchicago.edu") is None


# ---------------------------------------------------------------------------
# Spec Part 4 — fixture-backed test matrix
# ---------------------------------------------------------------------------

def _pks(result):
    return sorted(e["messagePk"] for e in result["emails"])


class TestSenderFilterMatrix:
    """Required passing cases from spec Part 4.

    Fixture (see tests/conftest.py):
      pk=1 A: ctaylo@uchicago.edu (no display name); body signed "Christine Taylo"
      pk=2 B: "Marie Keup" <m.keup@taylorwessing.com>
      pk=3 C: Christine Smith <csmith@example.com>
      pk=4 D: taylor.swift@example.com (no display name)
    """

    def test_sender_name_christine_returns_A_and_C(self, spark_db):
        result = spark_db.list_emails(sender_name="Christine", verbose=True)
        assert _pks(result) == [1, 3]

    def test_sender_name_taylo_returns_A_via_signature(self, spark_db):
        result = spark_db.list_emails(sender_name="Taylo", verbose=True)
        pks = _pks(result)
        # A must appear (via signature body fallback or fuzzy localpart).
        assert 1 in pks
        # D's localpart contains 'taylor' but it's not a signature name; spec
        # says D should be excluded. Our fuzzy heuristic only fires on
        # multi-word names, so single-word "Taylo" can't conjure D from
        # localpart search — D should not appear.
        assert 4 not in pks
        # B (taylorwessing.com) must not be matched via domain false-positive.
        assert 2 not in pks

    def test_sender_email_ctaylo_substring(self, spark_db):
        result = spark_db.list_emails(sender_email="ctaylo")
        assert _pks(result) == [1]

    def test_sender_email_exact(self, spark_db):
        result = spark_db.list_emails(sender_email="ctaylo@uchicago.edu")
        assert _pks(result) == [1]

    def test_sender_domain_exact(self, spark_db):
        result = spark_db.list_emails(sender_domain="taylorwessing.com")
        assert _pks(result) == [2]

    def test_sender_domain_substring_does_not_match(self, spark_db):
        # The regression fix: "taylo" must NOT match "taylorwessing.com".
        result = spark_db.list_emails(sender_domain="taylo")
        assert result["total"] == 0

    def test_legacy_sender_full_name(self, spark_db):
        # Legacy `sender` with a full person name should find Christine Taylo
        # via the name path (header substring or signature fallback).
        result = spark_db.list_emails(sender="Christine Taylo", verbose=True)
        pks = _pks(result)
        assert 1 in pks
        assert result["diagnostics"]["matched_on"] in {
            "sender_name", "sender_signature_name", "sender_email"
        }

    def test_legacy_sender_taylo_regression(self, spark_db):
        # The headline bug: sender="Taylo" used to match m.keup@taylorwessing.com
        # because of substring matching on the concatenated From header.
        result = spark_db.list_emails(sender="Taylo", verbose=True)
        pks = _pks(result)
        assert 2 not in pks, "B (taylorwessing.com) must not appear for sender='Taylo'"
        # A should still be findable (via signature body fallback).
        assert 1 in pks


class TestVerboseDiagnostics:
    def test_diagnostics_block_present_when_verbose(self, spark_db):
        result = spark_db.list_emails(sender_name="Christine", verbose=True)
        assert "diagnostics" in result
        assert result["diagnostics"]["matched_on"] == "sender_name"

    def test_diagnostics_absent_when_not_verbose(self, spark_db):
        result = spark_db.list_emails(sender_name="Christine")
        assert "diagnostics" not in result

    def test_fuzzy_matches_used_for_multi_word_name(self, spark_db):
        result = spark_db.list_emails(sender_name="Christine Taylo", verbose=True)
        used = result["diagnostics"]["fuzzy_matches_used"]
        assert "ctaylo" in used or "christine.taylo" in used


class TestSearchEmailsSenderFilter:
    def test_search_emails_with_sender_domain(self, spark_db):
        # FTS body matches both B and the "Christine" rows; sender_domain
        # narrows to only the taylorwessing.com sender (B).
        result = spark_db.search_emails(
            query="draft", sender_domain="taylorwessing.com"
        )
        pks = sorted(r["messagePk"] for r in result["results"])
        assert pks == [2]

    def test_search_emails_sender_domain_no_substring_match(self, spark_db):
        # Regression: "taylo" must NOT match "taylorwessing.com" as a domain.
        result = spark_db.search_emails(query="draft", sender_domain="taylo")
        assert result["total"] == 0


class TestFolderAutoBroaden:
    """When a sender filter is set, default folder should broaden to 'all'."""

    def test_inbox_default_excludes_archived(self, spark_db):
        # A (pk=1) is archived in the fixture; no sender filter → inbox only.
        result = spark_db.list_emails()
        pks = _pks(result)
        assert 1 not in pks
        # Other rows are in inbox.
        assert set(pks).issubset({2, 3, 4})

    def test_sender_filter_broadens_to_all(self, spark_db):
        # With a sender filter, archived A must be findable.
        result = spark_db.list_emails(sender_email="ctaylo@uchicago.edu")
        assert _pks(result) == [1]

    def test_explicit_folder_inbox_still_filters(self, spark_db):
        # Explicit folder='inbox' must NOT auto-broaden even with sender filter.
        result = spark_db.list_emails(folder="inbox", sender_email="ctaylo@uchicago.edu")
        assert result["total"] == 0


class TestIndexStatus:
    def test_reports_account_totals(self, spark_db):
        status = spark_db.index_status()
        assert len(status["accounts"]) == 1
        acc = status["accounts"][0]
        assert acc["totalMessages"] == 4
        assert acc["newestMessageAt"] is not None

    def test_empty_db_reports_zero_and_stale(self, empty_spark_db):
        status = empty_spark_db.index_status()
        assert status["accounts"][0]["totalMessages"] == 0
        assert status["anyStale"] is True

    def test_owned_by_note_is_present(self, spark_db):
        status = spark_db.index_status()
        assert "Spark Desktop owns sync" in status["owned_by"]
