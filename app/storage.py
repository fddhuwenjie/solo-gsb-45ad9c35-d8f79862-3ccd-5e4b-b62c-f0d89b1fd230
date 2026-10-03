"""SQLite persistence for ingested manifests and release channels."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCode, OCIError
from .reclaim import compute_reachable

DEFAULT_DB_PATH = os.environ.get(
    "OCI_RESOLVER_DB", os.path.join(os.getcwd(), "data", "resolver.db")
)

# Give concurrent writers (e.g. two racing promotes, or a promotion racing a
# storage reclamation) a grace period instead of failing immediately with
# SQLITE_BUSY; compare-and-swap then serializes them deterministically.
_BUSY_TIMEOUT_SECONDS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS manifests (
    digest     TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    doc        TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS channels (
    name          TEXT PRIMARY KEY,
    current_digest TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Append-only promotion ledger. Rows are never updated or deleted; each
-- channel owns a gap-free revision sequence starting at 1.
CREATE TABLE IF NOT EXISTS channel_promotions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    root_digest  TEXT NOT NULL,
    old_digest   TEXT,
    new_digest   TEXT NOT NULL,
    platforms    TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (channel, revision),
    FOREIGN KEY (channel) REFERENCES channels(name)
);
CREATE INDEX IF NOT EXISTS idx_channel_promotions_lookup
    ON channel_promotions (channel, revision);
"""


@dataclass(frozen=True)
class StoredDocument:
    digest: str
    media_type: str
    doc: dict[str, Any]
    size: int
    created_at: str


@dataclass(frozen=True)
class Channel:
    name: str
    current_digest: str
    revision: int
    updated_at: str


@dataclass(frozen=True)
class PromotionEvent:
    channel: str
    revision: int
    root_digest: str
    old_digest: str | None
    new_digest: str
    platforms: list[dict[str, Any]]
    created_at: str


@dataclass(frozen=True)
class ReclaimableDocument:
    digest: str
    size: int


@dataclass(frozen=True)
class ReclaimReport:
    dry_run: bool
    deleted: list[ReclaimableDocument]
    deleted_bytes: int
    remaining: int
    reachable: int
    roots: int
    dangling: list[str]


class ChannelConflict(Exception):
    """Raised when a compare-and-swap promotion loses the race.

    The transaction is rolled back and no promotion row is written.
    """

    def __init__(self, channel: str, expected: str, actual: str | None):
        super().__init__(
            f"expectedDigest {expected} is stale for channel {channel!r}; "
            f"current value is {actual}"
        )
        self.channel = channel
        self.expected = expected
        self.actual = actual


class Repository:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, timeout=_BUSY_TIMEOUT_SECONDS
        )
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(
        self, digest: str, media_type: str, doc: dict[str, Any], size: int
    ) -> bool:
        """Store a document. Returns True when a new row was inserted."""
        with self._connect() as conn:
            existed = conn.execute(
                "SELECT 1 FROM manifests WHERE digest = ?", (digest,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO manifests (digest, media_type, doc, bytes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(digest) DO UPDATE SET
                    media_type = excluded.media_type,
                    doc = excluded.doc,
                    bytes = excluded.bytes
                """,
                (digest, media_type, json.dumps(doc, ensure_ascii=False), size),
            )
            return existed is None

    def get(self, digest: str) -> StoredDocument | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT digest, media_type, doc, bytes, created_at "
                "FROM manifests WHERE digest = ?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return StoredDocument(
            digest=row["digest"],
            media_type=row["media_type"],
            doc=json.loads(row["doc"]),
            size=row["bytes"],
            created_at=row["created_at"],
        )

    def list_all(self) -> list[StoredDocument]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT digest, media_type, doc, bytes, created_at "
                "FROM manifests ORDER BY created_at, digest"
            ).fetchall()
        return [
            StoredDocument(
                digest=row["digest"],
                media_type=row["media_type"],
                doc=json.loads(row["doc"]),
                size=row["bytes"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM manifests").fetchone()[0]

    # ------------------------------------------------------------------
    # Release channels
    # ------------------------------------------------------------------

    def promote_channel(
        self,
        name: str,
        root_digest: str,
        new_digest: str,
        platforms: list[dict[str, Any]],
        expected_digest: str | None = None,
    ) -> PromotionEvent:
        """Atomically move a channel pointer and append a ledger row.

        Resolution for every requested platform must already have succeeded;
        this method only performs the compare-and-swap write.  When
        ``expected_digest`` is given it must equal the channel's current
        pointer (or the channel must not exist yet with expected set to
        None); otherwise a :class:`ChannelConflict` is raised and nothing is
        written.
        """
        conn = self._connect()
        try:
            # Acquire the write lock up front so concurrent promotions
            # serialize before either one reads the current pointer.
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT current_digest FROM channels WHERE name = ?",
                (name,),
            ).fetchone()
            old_digest = row["current_digest"] if row is not None else None

            if expected_digest is not None and expected_digest != old_digest:
                conn.rollback()
                raise ChannelConflict(name, expected_digest, old_digest)

            next_revision = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM channel_promotions WHERE channel = ?",
                (name,),
            ).fetchone()["revision"]

            created_row = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS ts"
            ).fetchone()
            created_at = created_row["ts"]

            conn.execute(
                """
                INSERT INTO channels (name, current_digest, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    current_digest = excluded.current_digest,
                    updated_at = excluded.updated_at
                """,
                (name, new_digest, created_at),
            )
            conn.execute(
                """
                INSERT INTO channel_promotions
                    (channel, revision, root_digest, old_digest, new_digest,
                     platforms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    next_revision,
                    root_digest,
                    old_digest,
                    new_digest,
                    json.dumps(platforms, ensure_ascii=False),
                    created_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return PromotionEvent(
            channel=name,
            revision=next_revision,
            root_digest=root_digest,
            old_digest=old_digest,
            new_digest=new_digest,
            platforms=platforms,
            created_at=created_at,
        )

    def get_channel(self, name: str) -> Channel | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT c.name, c.current_digest, c.updated_at,
                       COALESCE(MAX(p.revision), 0) AS revision
                FROM channels AS c
                LEFT JOIN channel_promotions AS p ON p.channel = c.name
                WHERE c.name = ?
                GROUP BY c.name
                """,
                (name,),
            ).fetchone()
        if row is None:
            return None
        return Channel(
            name=row["name"],
            current_digest=row["current_digest"],
            revision=row["revision"],
            updated_at=row["updated_at"],
        )

    def list_channels(self) -> list[Channel]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.name, c.current_digest, c.updated_at,
                       COALESCE(MAX(p.revision), 0) AS revision
                FROM channels AS c
                LEFT JOIN channel_promotions AS p ON p.channel = c.name
                GROUP BY c.name
                ORDER BY c.name
                """
            ).fetchall()
        return [
            Channel(
                name=row["name"],
                current_digest=row["current_digest"],
                revision=row["revision"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def list_events(
        self,
        name: str,
        *,
        before_revision: int | None = None,
        limit: int = 50,
    ) -> list[PromotionEvent]:
        """Return promotion ledger rows newest revision first.

        When ``before_revision`` is given only rows with a strictly smaller
        revision are returned (opaque cursor pagination).
        """
        sql = (
            "SELECT channel, revision, root_digest, old_digest, new_digest, "
            "platforms, created_at FROM channel_promotions WHERE channel = ?"
        )
        params: list[Any] = [name]
        if before_revision is not None:
            sql += " AND revision < ?"
            params.append(before_revision)
        sql += " ORDER BY revision DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            PromotionEvent(
                channel=row["channel"],
                revision=row["revision"],
                root_digest=row["root_digest"],
                old_digest=row["old_digest"],
                new_digest=row["new_digest"],
                platforms=json.loads(row["platforms"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def has_older_events(self, name: str, revision: int) -> bool:
        """Whether the ledger holds a row older than ``revision``."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM channel_promotions "
                "WHERE channel = ? AND revision < ? LIMIT 1",
                (name, revision),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Storage reclamation (garbage collection)
    # ------------------------------------------------------------------

    def reclaim_manifests(
        self,
        *,
        dry_run: bool = False,
        limit: int | None = None,
    ) -> ReclaimReport:
        """Delete stored documents not reachable from any channel.

        Roots are the union of every channel's current digest and every
        promotion ledger ``root_digest``.  From each root the manifests
        descriptors of stored Index documents are followed recursively;
        anything stored outside that reachable set is a deletion candidate.

        The whole operation runs in a single ``BEGIN IMMEDIATE`` transaction,
        which is the same write lock taken by :meth:`promote_channel`, so no
        promotion can move a pointer while reachability is computed or rows
        are deleted.  Any resolution/parsing failure rolls the transaction
        back and the database is left untouched.

        When ``dry_run`` is true the candidates are only reported; the
        transaction is rolled back.  When ``limit`` is given at most that many
        candidates are deleted, chosen by smallest digest in lexicographic
        order; ``remaining`` reports the candidates left for a later run.
        """
        conn = self._connect()
        try:
            # Take the write lock before reading anything: concurrent
            # promotions (and ingests) now block until the sweep finishes, so
            # the reachable set is computed over one stable snapshot.
            conn.execute("BEGIN IMMEDIATE")

            root_rows = conn.execute(
                "SELECT current_digest AS digest FROM channels "
                "UNION "
                "SELECT root_digest AS digest FROM channel_promotions "
                "WHERE root_digest IS NOT NULL"
            ).fetchall()
            roots = list(dict.fromkeys(row["digest"] for row in root_rows))

            reachable, dangling = compute_reachable(conn, roots)

            all_rows = conn.execute(
                "SELECT digest, bytes FROM manifests ORDER BY digest"
            ).fetchall()
            candidates = [
                ReclaimableDocument(digest=row["digest"], size=row["bytes"])
                for row in all_rows
                if row["digest"] not in reachable
            ]

            to_delete = candidates if limit is None else candidates[:limit]
            remaining = len(candidates) - len(to_delete)
            deleted_bytes = sum(item.size for item in to_delete)

            if not dry_run and to_delete:
                deleted_count = 0
                for item in to_delete:
                    cursor = conn.execute(
                        "DELETE FROM manifests WHERE digest = ?",
                        (item.digest,),
                    )
                    deleted_count += cursor.rowcount
                if deleted_count != len(to_delete):
                    raise OCIError(
                        ErrorCode.RECLAIM_FAILED,
                        "Reclamation deleted an unexpected number of rows; "
                        "the transaction was rolled back.",
                        {
                            "expected": len(to_delete),
                            "actual": deleted_count,
                        },
                    )

            report = ReclaimReport(
                dry_run=dry_run,
                deleted=list(to_delete),
                deleted_bytes=deleted_bytes,
                remaining=remaining,
                reachable=len(reachable),
                roots=len(roots),
                dangling=dangling,
            )

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return report
