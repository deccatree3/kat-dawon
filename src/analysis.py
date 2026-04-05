"""전월 대비 비교 및 추세 분석."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class ComparisonRow:
    item_name: str
    category: Optional[str]
    prev_amount: float
    curr_amount: float
    diff: float
    rate: Optional[float]       # 증감률 (%) — 전월 0일 때 None


def previous_year_month(ym: str) -> str:
    """'2026-02' → '2026-01'."""
    y, m = ym.split("-")
    y, m = int(y), int(m)
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def compare_documents(
    conn: sqlite3.Connection, company: str, year_month: str
) -> tuple[Optional[dict], Optional[dict], list[ComparisonRow]]:
    """현재월과 전월 문서를 비교. 둘 중 하나라도 없으면 빈 결과."""
    prev_ym = previous_year_month(year_month)

    curr = conn.execute(
        "SELECT * FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
    prev = conn.execute(
        "SELECT * FROM billing_document WHERE company=? AND year_month=?",
        (company, prev_ym),
    ).fetchone()

    if curr is None:
        return None, None, []

    def _items(doc_id: int) -> dict[str, tuple[float, Optional[str]]]:
        rows = conn.execute(
            "SELECT item_name, category, amount FROM billing_item WHERE document_id=?",
            (doc_id,),
        ).fetchall()
        # 같은 이름이 여러 번 있으면 합산
        acc: dict[str, tuple[float, Optional[str]]] = {}
        for r in rows:
            name = r["item_name"] or "(이름없음)"
            prev_val = acc.get(name, (0.0, r["category"]))
            acc[name] = (prev_val[0] + (r["amount"] or 0.0), r["category"])
        return acc

    curr_items = _items(curr["id"])
    prev_items = _items(prev["id"]) if prev else {}

    all_names = list(dict.fromkeys(list(prev_items.keys()) + list(curr_items.keys())))
    rows: list[ComparisonRow] = []
    for name in all_names:
        p_amt, p_cat = prev_items.get(name, (0.0, None))
        c_amt, c_cat = curr_items.get(name, (0.0, None))
        diff = c_amt - p_amt
        rate = (diff / p_amt * 100) if p_amt else None
        rows.append(
            ComparisonRow(
                item_name=name,
                category=c_cat or p_cat,
                prev_amount=p_amt,
                curr_amount=c_amt,
                diff=diff,
                rate=rate,
            )
        )
    return dict(curr), (dict(prev) if prev else None), rows


def comparison_dataframe(rows: list[ComparisonRow]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=["카테고리", "항목", "전월", "금월", "증감", "증감률(%)"]
        )
    df = pd.DataFrame(
        [
            {
                "카테고리": r.category or "",
                "항목": r.item_name,
                "전월": r.prev_amount,
                "금월": r.curr_amount,
                "증감": r.diff,
                "증감률(%)": r.rate,
            }
            for r in rows
        ]
    )
    return df.sort_values("증감", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def trend_dataframe(conn: sqlite3.Connection, company: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT year_month, supply_amount, vat, total_amount
        FROM billing_document
        WHERE company=?
        ORDER BY year_month
        """,
        (company,),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["year_month", "공급가", "VAT", "청구총액"])
    return pd.DataFrame(
        [
            {
                "year_month": r["year_month"],
                "공급가": r["supply_amount"],
                "VAT": r["vat"],
                "청구총액": r["total_amount"],
            }
            for r in rows
        ]
    )


def category_breakdown(conn: sqlite3.Connection, doc_id: int) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT COALESCE(category,'(미분류)') AS category, SUM(amount) AS amount
        FROM billing_item
        WHERE document_id=?
        GROUP BY category
        ORDER BY amount DESC
        """,
        (doc_id,),
    ).fetchall()
    return pd.DataFrame(
        [{"카테고리": r["category"], "금액": r["amount"]} for r in rows]
    )
