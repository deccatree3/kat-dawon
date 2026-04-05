"""파싱 → 검수 → DB 저장을 한 번에 처리하는 오케스트레이터."""
from __future__ import annotations

from pathlib import Path

from .db import get_conn, insert_items, insert_issues, upsert_document
from .parser import parse_billing_file, ParsedDocument
from .validator import validate, Issue


def ingest_file(path: str | Path) -> tuple[ParsedDocument, list[Issue], int]:
    doc = parse_billing_file(path)
    issues = validate(doc)

    with get_conn() as conn:
        doc_id = upsert_document(
            conn,
            company=doc.company,
            year_month=doc.year_month,
            period_from=doc.period_from,
            period_to=doc.period_to,
            supply_amount=doc.supply_amount,
            vat=doc.vat,
            total_amount=doc.total_amount,
            source_file=doc.source_file,
        )
        insert_items(
            conn,
            doc_id,
            [
                {
                    "row_index": it.row_index,
                    "category": it.category,
                    "item_name": it.item_name,
                    "quantity": it.quantity,
                    "unit_price": it.unit_price,
                    "amount": it.amount,
                    "formula_ref": it.formula_amount,
                    "remarks": it.remarks,
                }
                for it in doc.items
            ],
        )
        insert_issues(conn, doc_id, [i.as_dict() for i in issues])

    return doc, issues, doc_id


def ingest_directory(directory: str | Path) -> list[tuple[str, int, int]]:
    """디렉토리 내 모든 xlsx 파일을 처리. (파일명, doc_id, 이슈수) 리스트 반환."""
    directory = Path(directory)
    results: list[tuple[str, int, int]] = []
    for f in sorted(directory.glob("*.xlsx")):
        if f.name.startswith("~$"):
            continue
        try:
            _, issues, doc_id = ingest_file(f)
            results.append((f.name, doc_id, len(issues)))
        except Exception as e:
            results.append((f.name, -1, -1))
            print(f"[ERROR] {f.name}: {e}")
    return results
