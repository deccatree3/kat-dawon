"""SQLite 스키마 및 입출력 유틸리티."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "billing.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS billing_document (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company        TEXT    NOT NULL,
    year_month     TEXT    NOT NULL,
    period_from    TEXT,
    period_to      TEXT,
    supply_amount  REAL,
    vat            REAL,
    total_amount   REAL,
    source_file    TEXT,
    imported_at    TEXT    NOT NULL,
    UNIQUE(company, year_month)
);

CREATE TABLE IF NOT EXISTS billing_item (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES billing_document(id) ON DELETE CASCADE,
    row_index      INTEGER,
    category       TEXT,
    item_name      TEXT,
    quantity       REAL,
    unit_price     REAL,
    amount         REAL,
    formula_ref    TEXT,
    remarks        TEXT
);

CREATE TABLE IF NOT EXISTS validation_issue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES billing_document(id) ON DELETE CASCADE,
    severity       TEXT    NOT NULL,
    issue_type     TEXT    NOT NULL,
    item_name      TEXT,
    expected_value REAL,
    actual_value   REAL,
    diff           REAL,
    description    TEXT
);

CREATE INDEX IF NOT EXISTS idx_item_doc ON billing_item(document_id);
CREATE INDEX IF NOT EXISTS idx_issue_doc ON validation_issue(document_id);
CREATE INDEX IF NOT EXISTS idx_doc_company_ym ON billing_document(company, year_month);
"""


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    company: str,
    year_month: str,
    period_from: Optional[str],
    period_to: Optional[str],
    supply_amount: float,
    vat: float,
    total_amount: float,
    source_file: str,
) -> int:
    """동일 (company, year_month) 문서가 있으면 교체 후 새 id 반환."""
    existing = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM billing_document WHERE id=?", (existing["id"],))

    cursor = conn.execute(
        """
        INSERT INTO billing_document
            (company, year_month, period_from, period_to,
             supply_amount, vat, total_amount, source_file, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company,
            year_month,
            period_from,
            period_to,
            supply_amount,
            vat,
            total_amount,
            source_file,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return cursor.lastrowid


def insert_items(conn: sqlite3.Connection, document_id: int, items: Iterable[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO billing_item
            (document_id, row_index, category, item_name,
             quantity, unit_price, amount, formula_ref, remarks)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                it.get("row_index"),
                it.get("category"),
                it.get("item_name"),
                it.get("quantity"),
                it.get("unit_price"),
                it.get("amount"),
                it.get("formula_ref"),
                it.get("remarks"),
            )
            for it in items
        ],
    )


def insert_issues(conn: sqlite3.Connection, document_id: int, issues: Iterable[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO validation_issue
            (document_id, severity, issue_type, item_name,
             expected_value, actual_value, diff, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                it.get("severity"),
                it.get("issue_type"),
                it.get("item_name"),
                it.get("expected_value"),
                it.get("actual_value"),
                it.get("diff"),
                it.get("description"),
            )
            for it in issues
        ],
    )


def list_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*,
               (SELECT COUNT(*) FROM validation_issue v
                 WHERE v.document_id=d.id AND v.severity='critical') AS n_critical,
               (SELECT COUNT(*) FROM validation_issue v
                 WHERE v.document_id=d.id AND v.severity='warning')  AS n_warning
        FROM billing_document d
        ORDER BY company, year_month DESC
        """
    ).fetchall()


def get_document(conn: sqlite3.Connection, doc_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM billing_document WHERE id=?", (doc_id,)).fetchone()


def get_items(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM billing_item WHERE document_id=? ORDER BY row_index", (doc_id,)
    ).fetchall()


def get_issues(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM validation_issue WHERE document_id=? ORDER BY severity, id",
        (doc_id,),
    ).fetchall()


def find_document(conn: sqlite3.Connection, company: str, year_month: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
