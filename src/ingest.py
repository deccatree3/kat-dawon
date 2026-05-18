"""파싱 → 검수 → DB 저장을 한 번에 처리하는 오케스트레이터."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .btob_audit import (
    calculate_settlement_box_count,
    parse_btob_bundle_input,
    parse_btoc_bundle_orders,
    parse_extended_orders,
)
from .db import (
    get_box_capacity_map,
    get_conn,
    insert_items,
    insert_issues,
    insert_sheet_metrics,
    insert_storage_inventory,
    upsert_document,
)
from .parser import parse_billing_file, parse_storage_sheet, ParsedDocument
from .validator import validate, Issue


def ingest_file(
    path: str | Path,
    extended_orders_path: Optional[str | Path] = None,
    bundle_btob_path: Optional[str | Path] = None,
    bundle_btoc_path: Optional[str | Path] = None,
) -> tuple[ParsedDocument, list[Issue], int]:
    """청구마감 파일 적재. 추가 검증용 raw 파일이 있으면 함께 처리.

    - extended_orders_path: 확장주문검색(.xls) — BTOB 박스수 검증용
    - bundle_btob_path: 쿠팡 재고이동건(.xlsx) — BTOB 번들 raw 검증용
    - bundle_btoc_path: 확장주문검색 번들작업 검수용(.xls) — BTOC 번들 raw 검증용
    """
    path = Path(path)
    doc = parse_billing_file(path)
    issues = validate(doc)
    storage_rows = parse_storage_sheet(path)

    # 확장주문검색이 있으면 박스수 산출
    btob_metric: Optional[dict] = None
    if extended_orders_path is not None:
        try:
            with get_conn() as _c:
                cap = get_box_capacity_map(_c, doc.company)
            orders = parse_extended_orders(extended_orders_path)
            box_total, det = calculate_settlement_box_count(orders, cap, doc.year_month)
            btob_metric = {
                "settlement_boxes": float(box_total),
                "total_orders": float(det["total_orders"]),
                "milkrun_excluded": float(det["milkrun_excluded"]),
                "settled_orders": float(det["settled_orders"]),
            }
        except Exception as e:
            print(f"[WARN] 확장주문검색 처리 실패: {e}")

    # 번들 raw 합계 추출
    bundle_btob_raw: Optional[float] = None
    bundle_btoc_raw: Optional[float] = None
    if bundle_btob_path is not None:
        try:
            bundle_btob_raw, _ = parse_btob_bundle_input(bundle_btob_path)
        except Exception as e:
            print(f"[WARN] BTOB 번들 raw 처리 실패: {e}")
    if bundle_btoc_path is not None:
        try:
            bundle_btoc_raw = parse_btoc_bundle_orders(bundle_btoc_path)
        except Exception as e:
            print(f"[WARN] BTOC 번들 raw 처리 실패: {e}")

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
        if storage_rows:
            insert_storage_inventory(conn, doc_id, storage_rows)
        all_metrics = dict(doc.sheet_metrics)
        if btob_metric:
            all_metrics["__BTOB_정산"] = btob_metric
        bundle_raw_metric: dict[str, float] = {}
        if bundle_btob_raw is not None:
            bundle_raw_metric["btob_input_total"] = float(bundle_btob_raw)
        if bundle_btoc_raw is not None:
            bundle_raw_metric["btoc_order_total"] = float(bundle_btoc_raw)
        if bundle_raw_metric:
            all_metrics["__번들_raw"] = bundle_raw_metric
        if all_metrics:
            insert_sheet_metrics(conn, doc_id, all_metrics)

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
