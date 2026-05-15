"""Test fixtures for spark-mcp.

Builds a minimal synthetic Spark DB layout in a temp directory so tests can
exercise SparkDatabase without depending on the user's real Spark store.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import List, Tuple

import pytest

from spark_mcp.database import SparkDatabase


# (messageFrom, messageFromMailbox, messageFromDomain, subject, body)
FIXTURE_MESSAGES: List[Tuple[str, str, str, str, str]] = [
    # A: no display name in header; signed in body
    (
        "ctaylo@uchicago.edu",
        "ctaylo@uchicago.edu",
        "uchicago.edu",
        "Schedule update",
        "Hi Nick,\n\nUpdating you on the schedule.\n\nBest,\nChristine Taylo\nExecutive Assistant\nUniversity of Chicago",
    ),
    # B: display name in header at taylorwessing.com (the regression case)
    (
        '"Marie Keup" <m.keup@taylorwessing.com>',
        "m.keup@taylorwessing.com",
        "taylorwessing.com",
        "Legal review",
        "Dear Nick,\n\nPlease find attached the draft.\n\nKind regards,\nMarie Keup",
    ),
    # C: another Christine, different domain
    (
        "Christine Smith <csmith@example.com>",
        "csmith@example.com",
        "example.com",
        "Project sync",
        "Hi all, quick update on the project.\n\nThanks,\nChristine Smith",
    ),
    # D: localpart contains 'taylor' but it's not a name match
    (
        "taylor.swift@example.com",
        "taylor.swift@example.com",
        "example.com",
        "Newsletter",
        "Reading time: 5 minutes. Thanks for subscribing.",
    ),
]


def _create_messages_db(path: Path) -> None:
    """Create a minimal messages.sqlite that mirrors the columns we query.

    We deliberately use a simplified schema (only the columns the code reads)
    rather than reproducing Spark's full schema with all NOT NULL constraints.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE accounts (
                pk INTEGER PRIMARY KEY,
                accountTitle TEXT NOT NULL,
                ownerFullName TEXT
            );

            CREATE TABLE folders (
                pk INTEGER PRIMARY KEY,
                accountPk INTEGER NOT NULL,
                folderName TEXT NOT NULL
            );

            CREATE TABLE messages (
                pk INTEGER PRIMARY KEY AUTOINCREMENT,
                accountPk INTEGER NOT NULL DEFAULT 1,
                messageFrom TEXT,
                messageFromMailbox TEXT,
                messageFromDomain TEXT,
                messageTo TEXT,
                messageCc TEXT,
                messageBcc TEXT,
                subject TEXT,
                receivedDate INTEGER NOT NULL,
                inInbox INTEGER NOT NULL DEFAULT 1,
                inSent INTEGER NOT NULL DEFAULT 0,
                inDrafts INTEGER NOT NULL DEFAULT 0,
                unseen INTEGER NOT NULL DEFAULT 0,
                starred INTEGER NOT NULL DEFAULT 0,
                conversationPk INTEGER,
                numberOfFileAttachments INTEGER DEFAULT 0,
                meta TEXT,
                inReplyTo TEXT,
                messageReferences TEXT,
                messageId TEXT
            );

            INSERT INTO accounts (pk, accountTitle, ownerFullName)
            VALUES (1, 'Test Account', 'Test User');

            INSERT INTO folders (pk, accountPk, folderName) VALUES
                (1, 1, 'Inbox'),
                (2, 1, 'Sent'),
                (3, 1, 'Archive');
            """
        )

        base_ts = int(time.time()) - 3600  # 1 hour ago
        for i, (msgfrom, mailbox, domain, subject, _body) in enumerate(FIXTURE_MESSAGES):
            # A (i=0) is archived, not in inbox — exercises the auto-broaden default.
            in_inbox = 0 if i == 0 else 1
            conn.execute(
                """
                INSERT INTO messages
                    (messageFrom, messageFromMailbox, messageFromDomain,
                     subject, messageTo, receivedDate, inInbox, conversationPk)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (msgfrom, mailbox, domain, subject, "nick@example.com",
                 base_ts + i, in_inbox, 100 + i),
            )
        conn.commit()
    finally:
        conn.close()


def _create_search_db(path: Path) -> None:
    """Create a minimal search_fts5.sqlite with the messagesfts FTS5 table."""
    conn = sqlite3.connect(str(path))
    try:
        # Mirror the columns Spark indexes; we only use messagePk + searchBody.
        conn.executescript(
            """
            CREATE VIRTUAL TABLE messagesfts USING fts5(
                messagePk UNINDEXED,
                fromField,
                toField,
                subject,
                searchBody
            );
            """
        )
        for i, (msgfrom, _mailbox, _domain, subject, body) in enumerate(FIXTURE_MESSAGES):
            conn.execute(
                "INSERT INTO messagesfts (messagePk, fromField, toField, subject, searchBody) "
                "VALUES (?, ?, ?, ?, ?)",
                (i + 1, msgfrom, "nick@example.com", subject, body),
            )
        conn.commit()
    finally:
        conn.close()


def _create_calendar_db(path: Path) -> None:
    """Create a stub calendarsapi.sqlite — SparkDatabase init requires it."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE RDCALAPIEvent (
                pk INTEGER PRIMARY KEY,
                summary TEXT,
                descriptionProperty TEXT,
                dstart INTEGER,
                dend INTEGER,
                location TEXT,
                locationTitle TEXT,
                allDay INTEGER,
                status INTEGER,
                conferenceInfo TEXT,
                url TEXT
            );
            CREATE TABLE RDCALAPIAttendee (
                pk INTEGER PRIMARY KEY,
                refEventPK INTEGER,
                name TEXT,
                email TEXT,
                partStat INTEGER,
                role INTEGER
            );
            CREATE TABLE RDCALAPIOrganizer (
                pk INTEGER PRIMARY KEY,
                refEventPK INTEGER,
                name TEXT,
                email TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def spark_db(tmp_path: Path) -> SparkDatabase:
    """A SparkDatabase pointed at a fresh synthetic store."""
    base = tmp_path / "core-data"
    base.mkdir()
    _create_messages_db(base / "messages.sqlite")
    _create_search_db(base / "search_fts5.sqlite")
    _create_calendar_db(base / "calendarsapi.sqlite")
    return SparkDatabase(base_dir=base, cache_dir=tmp_path / "cache")


@pytest.fixture
def empty_spark_db(tmp_path: Path) -> SparkDatabase:
    """A SparkDatabase with no messages — for index_status tests."""
    base = tmp_path / "core-data-empty"
    base.mkdir()
    conn = sqlite3.connect(str(base / "messages.sqlite"))
    try:
        conn.executescript(
            """
            CREATE TABLE accounts (pk INTEGER PRIMARY KEY, accountTitle TEXT NOT NULL, ownerFullName TEXT);
            CREATE TABLE folders (pk INTEGER PRIMARY KEY, accountPk INTEGER NOT NULL, folderName TEXT NOT NULL);
            CREATE TABLE messages (
                pk INTEGER PRIMARY KEY AUTOINCREMENT, accountPk INTEGER NOT NULL DEFAULT 1,
                messageFrom TEXT, messageFromMailbox TEXT, messageFromDomain TEXT,
                receivedDate INTEGER NOT NULL, inInbox INTEGER NOT NULL DEFAULT 1,
                inSent INTEGER NOT NULL DEFAULT 0, inDrafts INTEGER NOT NULL DEFAULT 0,
                unseen INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0,
                subject TEXT, messageTo TEXT, conversationPk INTEGER, meta TEXT,
                numberOfFileAttachments INTEGER DEFAULT 0
            );
            INSERT INTO accounts (pk, accountTitle) VALUES (1, 'Empty');
            """
        )
        conn.commit()
    finally:
        conn.close()
    _create_search_db(base / "search_fts5.sqlite")
    # Clear out the rows the helper inserted; we want a truly empty FTS table here.
    sc = sqlite3.connect(str(base / "search_fts5.sqlite"))
    try:
        sc.execute("DELETE FROM messagesfts")
        sc.commit()
    finally:
        sc.close()
    _create_calendar_db(base / "calendarsapi.sqlite")
    return SparkDatabase(base_dir=base, cache_dir=tmp_path / "cache-empty")
