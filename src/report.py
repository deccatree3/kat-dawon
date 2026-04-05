"""검수·분석 결과를 엑셀 리포트로 내보낸다."""
from __future__ import annotations

import io
import sqlite3

import pandas as pd

from .analysis import compare_documents, comparison_dataframe, previous_year_month
from .db import get_document, get_issues, get_items


def build_report_bytes(conn: sqlite3.Connection, doc_id: int) -> bytes:
    """단일 문서의 요약·세부항목·검수이슈·전월대비를 4시트 엑셀로 반환."""
    doc = get_document(conn, doc_id)
    if doc is None:
        raise ValueError(f"document {doc_id} not found")

    items = [dict(r) for r in get_items(conn, doc_id)]
    issues = [dict(r) for r in get_issues(conn, doc_id)]

    summary_df = pd.DataFrame(
        [
            {"항목": "업체", "값": doc["company"]},
            {"항목": "년월", "값": doc["year_month"]},
            {"항목": "기간시작", "값": doc["period_from"]},
            {"항목": "기간종료", "값": doc["period_to"]},
            {"항목": "공급가", "값": doc["supply_amount"]},
            {"항목": "VAT", "값": doc["vat"]},
            {"항목": "청구총액", "값": doc["total_amount"]},
            {"항목": "원본파일", "값": doc["source_file"]},
            {"항목": "적재일시", "값": doc["imported_at"]},
        ]
    )

    items_df = pd.DataFrame(items)
    if not items_df.empty:
        items_df = items_df[
            [
                "row_index",
                "category",
                "item_name",
                "quantity",
                "unit_price",
                "amount",
                "formula_ref",
                "remarks",
            ]
        ].rename(
            columns={
                "row_index": "행",
                "category": "카테고리",
                "item_name": "항목",
                "quantity": "수량",
                "unit_price": "단가",
                "amount": "금액",
                "formula_ref": "수식",
                "remarks": "비고",
            }
        )

    issues_df = pd.DataFrame(issues)
    if not issues_df.empty:
        issues_df = issues_df[
            [
                "severity",
                "issue_type",
                "item_name",
                "expected_value",
                "actual_value",
                "diff",
                "description",
            ]
        ].rename(
            columns={
                "severity": "심각도",
                "issue_type": "유형",
                "item_name": "항목",
                "expected_value": "기대값",
                "actual_value": "실제값",
                "diff": "차이",
                "description": "설명",
            }
        )

    _, _, rows = compare_documents(conn, doc["company"], doc["year_month"])
    compare_df = comparison_dataframe(rows)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="요약", index=False)
        if not items_df.empty:
            items_df.to_excel(writer, sheet_name="세부항목", index=False)
        if not issues_df.empty:
            issues_df.to_excel(writer, sheet_name="검수이슈", index=False)
        else:
            pd.DataFrame([{"결과": "이슈 없음"}]).to_excel(
                writer, sheet_name="검수이슈", index=False
            )
        if not compare_df.empty:
            compare_df.to_excel(writer, sheet_name="전월대비", index=False)
    return buffer.getvalue()
