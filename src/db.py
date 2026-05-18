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

CREATE TABLE IF NOT EXISTS storage_inventory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES billing_document(id) ON DELETE CASCADE,
    product_code TEXT,
    product_name TEXT,
    damage_flag  TEXT,
    loc_group    TEXT,
    location     TEXT,
    quantity     REAL DEFAULT 0,
    pallet_count REAL DEFAULT 0,
    plt_group    TEXT  -- 보관비 시트 PLT 컬럼 merged cell 그룹 식별자 (예: "G40-G49")
);

-- 시트별 보조 메트릭 (예: '반품택배' total_AG = 8000, '출고택배(cj)' row_count = 1435)
-- 청구 항목과 시트 합계 검증에 사용
CREATE TABLE IF NOT EXISTS sheet_metric (
    document_id  INTEGER NOT NULL REFERENCES billing_document(id) ON DELETE CASCADE,
    sheet_name   TEXT    NOT NULL,
    metric_key   TEXT    NOT NULL,
    metric_value REAL,
    PRIMARY KEY (document_id, sheet_name, metric_key)
);

-- 상품별 박스 입수량 마스터 (BTOB 박스수 산출용)
-- 매월 회사·년월 기준이 아닌, 회사·상품명 기준으로 보존 (한 번 등록 후 재사용)
CREATE TABLE IF NOT EXISTS box_capacity (
    company       TEXT NOT NULL,
    product_name  TEXT NOT NULL,
    units_per_box REAL NOT NULL,
    updated_at    TEXT,
    PRIMARY KEY (company, product_name)
);

CREATE INDEX IF NOT EXISTS idx_item_doc ON billing_item(document_id);
CREATE INDEX IF NOT EXISTS idx_issue_doc ON validation_issue(document_id);
CREATE INDEX IF NOT EXISTS idx_doc_company_ym ON billing_document(company, year_month);
CREATE INDEX IF NOT EXISTS idx_storage_doc ON storage_inventory(document_id);
"""


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # 기존 DB 마이그레이션: storage_inventory에 plt_group 컬럼이 없으면 추가
        cols = {row[1] for row in conn.execute("PRAGMA table_info(storage_inventory)").fetchall()}
        if "plt_group" not in cols:
            conn.execute("ALTER TABLE storage_inventory ADD COLUMN plt_group TEXT")
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


# ---------------------------------------------------------------------------
# sheet_metric — 시트별 보조 메트릭 (예: 반품택배 AG합계)
# ---------------------------------------------------------------------------
def insert_sheet_metrics(
    conn: sqlite3.Connection,
    document_id: int,
    metrics: dict[str, dict[str, float]],
) -> None:
    """metrics: {sheet_name: {metric_key: value, ...}, ...}."""
    rows = [
        (document_id, sheet, key, value)
        for sheet, kv in metrics.items()
        for key, value in kv.items()
    ]
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO sheet_metric (document_id, sheet_name, metric_key, metric_value) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def get_sheet_metric(
    conn: sqlite3.Connection, doc_id: int, sheet_pattern: str, metric_key: str
) -> Optional[float]:
    row = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name LIKE ? AND metric_key=?""",
        (doc_id, sheet_pattern, metric_key),
    ).fetchone()
    return row["metric_value"] if row else None


# ---------------------------------------------------------------------------
# box_capacity — 상품별 박스 입수량 마스터
# ---------------------------------------------------------------------------
def upsert_box_capacities(
    conn: sqlite3.Connection, company: str, items: Iterable[dict]
) -> int:
    """items: [{'product_name', 'units_per_box'}]. 입력된 행수 반환."""
    rows = [
        (company, it["product_name"], float(it["units_per_box"]),
         datetime.now().isoformat(timespec="seconds"))
        for it in items
        if it.get("product_name") and it.get("units_per_box") is not None
    ]
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO box_capacity (company, product_name, units_per_box, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(company, product_name) DO UPDATE SET
             units_per_box = excluded.units_per_box,
             updated_at = excluded.updated_at""",
        rows,
    )
    return len(rows)


def get_box_capacity_map(conn: sqlite3.Connection, company: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT product_name, units_per_box FROM box_capacity WHERE company=?",
        (company,),
    ).fetchall()
    return {r["product_name"]: r["units_per_box"] for r in rows}


def count_box_capacities(conn: sqlite3.Connection, company: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM box_capacity WHERE company=?", (company,)
    ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# storage_inventory
# ---------------------------------------------------------------------------
def insert_storage_inventory(conn: sqlite3.Connection, document_id: int, rows: Iterable[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO storage_inventory
            (document_id, product_code, product_name, damage_flag,
             loc_group, location, quantity, pallet_count, plt_group)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                r.get("product_code"),
                r.get("product_name"),
                r.get("damage_flag"),
                r.get("loc_group"),
                r.get("location"),
                r.get("quantity", 0),
                r.get("pallet_count", 0),
                r.get("plt_group"),
            )
            for r in rows
        ],
    )


def get_storage_summary(conn: sqlite3.Connection, doc_id: int) -> Optional[dict]:
    """doc_id 기준 SKU수, 총재고, 총PLT 집계."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT product_code) AS sku_count,
               SUM(quantity) AS total_qty,
               SUM(pallet_count) AS total_plt
        FROM storage_inventory WHERE document_id=?
        """,
        (doc_id,),
    ).fetchone()
    if row is None or row["sku_count"] == 0:
        return None
    return dict(row)


def get_plt_change_verdict(
    conn: sqlite3.Connection, curr_doc_id: int, prev_doc_id: int, product_code: str
) -> dict:
    """SKU의 PLT 증가가 실제 점유 증가인지 LOC 내 재분배인지 판정.

    각 LOC별로 (LOC 합 PLT 4월 vs 3월) 비교. 해석:
      - LOC 합이 증가 → 실제 신규 점유
      - LOC 합 동일 → 같은 LOC 내 재분배 (다른 SKU가 빠지고 이 SKU 비중 늘어남)
      - LOC 합 감소 → 일부 다른 SKU 출고. 이 SKU의 PLT 증가는 재분배일 가능성

    Returns: {
      'real_increase_locs': [...],
      'reallocation_locs': [...],
      'mixed_locs': [...],
      'summary': '전부 실제 증가' / '일부 재분배 의심' / '주로 재분배 의심',
    }
    """
    curr_locs = [r["location"] for r in conn.execute(
        """SELECT DISTINCT location FROM storage_inventory
           WHERE document_id=? AND product_code=? AND location IS NOT NULL""",
        (curr_doc_id, product_code),
    ).fetchall()]
    real, realloc, mixed = [], [], []
    for loc in curr_locs:
        c_p = conn.execute(
            "SELECT COALESCE(SUM(pallet_count),0) AS p FROM storage_inventory WHERE document_id=? AND location=?",
            (curr_doc_id, loc),
        ).fetchone()["p"]
        p_p = conn.execute(
            "SELECT COALESCE(SUM(pallet_count),0) AS p FROM storage_inventory WHERE document_id=? AND location=?",
            (prev_doc_id, loc),
        ).fetchone()["p"]
        diff = c_p - p_p
        if diff > 0.5:
            real.append((loc, p_p, c_p))
        elif abs(diff) < 0.5:
            realloc.append((loc, p_p, c_p))
        else:
            mixed.append((loc, p_p, c_p))
    if real and not realloc and not mixed:
        summary = "실제 증가"
    elif realloc and not real:
        summary = "재분배만"
    elif real and (realloc or mixed):
        summary = "혼합 (일부 실제 / 일부 재분배)"
    elif mixed and not real:
        summary = "주로 감소·재분배"
    else:
        summary = "판단 어려움"
    return {
        "real_increase_locs": real,
        "reallocation_locs": realloc,
        "mixed_locs": mixed,
        "summary": summary,
    }


def get_loc_sharing_info(
    conn: sqlite3.Connection, doc_id: int, product_code: str
) -> dict:
    """특정 상품이 속한 PLT 그룹 단위로 함께 적재된 SKU 수·총수량·PLT 집계.

    PLT 그룹은 보관비 시트의 PLT 컬럼 merged cell로 정의됨 (예: 'G40-G49').
    같은 그룹 = 실제 같은 팔레트군에 적재된 SKU들.

    Returns: {
      'groups': [(plt_group_id, sku_count, total_qty, total_plt), ...],
      'is_solo': True if 모든 그룹에 이 상품만 존재 else False,
      'shared_sku_count': 이 상품의 그룹들에 함께 있는 다른 unique SKU 수,
      'total_qty_at_group': 공유 그룹들의 총 수량 (이 상품 + 다른 SKU 모두),
      'total_plt_at_group': 공유 그룹들의 총 PLT,
    }
    """
    # 이 상품이 속한 plt_group 목록
    own_groups = conn.execute(
        """SELECT DISTINCT plt_group FROM storage_inventory
           WHERE document_id=? AND product_code=? AND plt_group IS NOT NULL""",
        (doc_id, product_code),
    ).fetchall()
    if not own_groups:
        return {"groups": [], "is_solo": True, "shared_sku_count": 0,
                "total_qty_at_group": 0, "total_plt_at_group": 0}

    groups_info = []
    other_skus = set()
    total_qty_shared = 0.0
    total_plt_shared = 0.0
    for r in own_groups:
        gid = r["plt_group"]
        agg = conn.execute(
            """SELECT COUNT(DISTINCT product_code) AS sku_cnt,
                      SUM(quantity) AS total_qty,
                      SUM(pallet_count) AS total_plt
               FROM storage_inventory
               WHERE document_id=? AND plt_group=?""",
            (doc_id, gid),
        ).fetchone()
        sku_cnt = agg["sku_cnt"] or 0
        total_qty = float(agg["total_qty"] or 0)
        total_plt = float(agg["total_plt"] or 0)
        groups_info.append((gid, sku_cnt, total_qty, total_plt))
        if sku_cnt > 1:
            others = conn.execute(
                """SELECT DISTINCT product_code FROM storage_inventory
                   WHERE document_id=? AND plt_group=? AND product_code != ?""",
                (doc_id, gid, product_code),
            ).fetchall()
            for o in others:
                other_skus.add(o["product_code"])
            total_qty_shared += total_qty
            total_plt_shared += total_plt
    is_solo = all(sku_cnt <= 1 for _, sku_cnt, _, _ in groups_info)
    return {
        "groups": groups_info,
        "is_solo": is_solo,
        "shared_sku_count": len(other_skus),
        "total_qty_at_group": total_qty_shared,
        "total_plt_at_group": total_plt_shared,
    }


def get_storage_by_loc(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    """LOC그룹별 집계."""
    return conn.execute(
        """
        SELECT loc_group,
               COUNT(DISTINCT product_code) AS sku_count,
               SUM(quantity) AS total_qty,
               SUM(pallet_count) AS total_plt
        FROM storage_inventory WHERE document_id=?
        GROUP BY loc_group ORDER BY total_plt DESC
        """,
        (doc_id,),
    ).fetchall()


def get_storage_products(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    """상품별 재고/PLT (product_code 기준 SUM)."""
    return conn.execute(
        """
        SELECT product_code, product_name,
               SUM(quantity) AS qty, SUM(pallet_count) AS plt
        FROM storage_inventory WHERE document_id=?
        GROUP BY product_code
        ORDER BY plt DESC, qty DESC
        """,
        (doc_id,),
    ).fetchall()
