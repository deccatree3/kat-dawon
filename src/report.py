"""검수·분석 결과를 엑셀 리포트로 내보낸다."""
from __future__ import annotations

import io
import sqlite3

import pandas as pd

from .analysis import (
    build_correction_report,
    compare_documents,
    comparison_dataframe,
    detect_anomalies,
    previous_year_month,
    storage_comparison,
)
from .db import get_document, get_issues, get_items
from .validator import issue_label


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
        issues_df["issue_type"] = issues_df["issue_type"].map(issue_label)
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

    # 이상 항목
    anomalies = detect_anomalies(conn, doc["company"], doc["year_month"])
    if anomalies:
        anomaly_df = pd.DataFrame(
            [
                {
                    "구분": a.category,
                    "심각도": a.severity,
                    "항목": a.item_name,
                    "설명": a.description,
                    "전월": a.prev_value,
                    "금월": a.curr_value,
                    "증감": a.diff,
                }
                for a in anomalies
            ]
        )
    else:
        anomaly_df = pd.DataFrame()

    # 보관비 상품별 PLT 분석
    curr_sum, prev_sum, prod_rows = storage_comparison(conn, doc["company"], doc["year_month"])
    if prod_rows:
        storage_df = pd.DataFrame(
            [
                {
                    "상품": r.product_name,
                    "전월재고": r.prev_qty,
                    "금월재고": r.curr_qty,
                    "재고증감": r.qty_diff,
                    "전월PLT": r.prev_plt,
                    "금월PLT": r.curr_plt,
                    "PLT증감": r.plt_diff,
                    "비고": " | ".join(r.flags) if r.flags else "",
                }
                for r in prod_rows
                if r.plt_diff != 0 or r.flags
            ]
        )
    else:
        storage_df = pd.DataFrame()

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
        if not anomaly_df.empty:
            anomaly_df.to_excel(writer, sheet_name="이상항목", index=False)
        if not storage_df.empty:
            storage_df.to_excel(writer, sheet_name="보관비분석", index=False)
    return buffer.getvalue()


def build_correction_xlsx_bytes(conn: sqlite3.Connection, doc_id: int) -> bytes:
    """물류센터 제출용 정정 요청서 엑셀 (확정/의심 2시트 + 요약)."""
    doc = get_document(conn, doc_id)
    if doc is None:
        raise ValueError(f"document {doc_id} not found")
    rep = build_correction_report(conn, doc["company"], doc["year_month"])

    def _df(rows: list[dict], confirmed: bool) -> pd.DataFrame:
        recs = []
        for x in rows:
            rec = {
                "항목": x["item"],
                "결론": x["conclusion"],
                "실제 청구액": x["billed"],
                "정상 청구액": x["normal"],
                "차액(정정금액)": x["diff"],
                "구분": x.get("direction", "확인 요청"),
                "요약시트 셀": x["cell"],
                "근거": x["basis"],
            }
            recs.append(rec)
        cols = ["항목", "결론", "실제 청구액", "정상 청구액",
                "차액(정정금액)", "구분", "요약시트 셀", "근거"]
        return pd.DataFrame(recs, columns=cols)

    t = rep["totals"]
    summary_df = pd.DataFrame([
        {"항목": "화주사", "값": rep["company"]},
        {"항목": "대상 월", "값": rep["year_month"]},
        {"항목": "확정 정정 건수", "값": t.get("confirmed_cnt", 0)},
        {"항목": "의심(확인요청) 건수", "값": t.get("suspected_cnt", 0)},
        {"항목": "환급 요청 합계(원)", "값": round(t.get("refund", 0))},
        {"항목": "부족·누락 정정 합계(원)", "값": round(t.get("shortfall", 0))},
    ])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="요약", index=False)
        _df(rep["confirmed"], True).to_excel(
            writer, sheet_name="확정 정정요청", index=False)
        _df(rep["suspected"], False).to_excel(
            writer, sheet_name="의심 확인요청", index=False)
    return buffer.getvalue()
