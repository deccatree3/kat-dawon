"""청구마감 검수·분석 대시보드 (Streamlit).

실행:
    streamlit run app.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from src.analysis import (
    category_breakdown,
    compare_documents,
    comparison_dataframe,
    previous_year_month,
    trend_dataframe,
)
from src.db import get_conn, list_documents, get_items, get_issues
from src.ingest import ingest_file, ingest_directory
from src.report import build_report_bytes

ROOT = Path(__file__).resolve().parent
ORG_DIR = ROOT / "org"


# -----------------------------------------------------------------------------
# 페이지 설정
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="청구마감 검수 대시보드",
    page_icon="📊",
    layout="wide",
)

st.title("📊 청구마감 검수·분석 대시보드")

# -----------------------------------------------------------------------------
# 사이드바 — 데이터 관리
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("데이터 관리")

    uploaded = st.file_uploader(
        "청구마감 엑셀 업로드", type=["xlsx"], accept_multiple_files=True
    )
    if uploaded:
        for up in uploaded:
            with tempfile.NamedTemporaryFile(
                suffix=f"_{up.name}", delete=False
            ) as tmp:
                tmp.write(up.getbuffer())
                tmp_path = tmp.name
            try:
                doc, issues, _ = ingest_file(tmp_path)
                st.success(
                    f"{up.name} → {doc.company} {doc.year_month} "
                    f"(항목 {len(doc.items)}, 이슈 {len(issues)})"
                )
            except Exception as e:
                st.error(f"{up.name}: {e}")
            finally:
                Path(tmp_path).unlink(missing_ok=True)

    st.divider()
    if ORG_DIR.exists():
        if st.button(f"📁 org/ 폴더 일괄 적재 ({len(list(ORG_DIR.glob('*.xlsx')))}개)"):
            with st.spinner("처리 중..."):
                results = ingest_directory(ORG_DIR)
            for name, doc_id, n in results:
                if doc_id > 0:
                    st.write(f"✔ {name} — 이슈 {n}")
                else:
                    st.write(f"✖ {name}")

    st.divider()
    page = st.radio(
        "메뉴",
        ["홈", "문서 상세 · 검수", "전월 대비 분석", "추세"],
    )


# -----------------------------------------------------------------------------
# 데이터 로드
# -----------------------------------------------------------------------------
with get_conn() as conn:
    docs = [dict(r) for r in list_documents(conn)]

if not docs:
    st.info("왼쪽 사이드바에서 청구마감 엑셀을 업로드하거나 `org/` 폴더를 적재하세요.")
    st.stop()

docs_df = pd.DataFrame(docs)


# -----------------------------------------------------------------------------
# 유틸
# -----------------------------------------------------------------------------
def format_krw(v: float | int | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}원"


def severity_emoji(sev: str) -> str:
    return {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(sev, "⚪")


# -----------------------------------------------------------------------------
# 페이지 — 홈
# -----------------------------------------------------------------------------
if page == "홈":
    st.subheader("업체별 현황")

    companies = docs_df["company"].unique().tolist()
    cols = st.columns(max(len(companies), 1))
    for col, company in zip(cols, companies):
        sub = docs_df[docs_df["company"] == company].sort_values(
            "year_month", ascending=False
        )
        latest = sub.iloc[0]
        with col:
            st.metric(
                label=f"{company} · {latest['year_month']}",
                value=format_krw(latest["total_amount"]),
            )
            n_crit = int(latest["n_critical"])
            n_warn = int(latest["n_warning"])
            if n_crit:
                st.error(f"🔴 치명 오류 {n_crit}건")
            if n_warn:
                st.warning(f"🟡 경고 {n_warn}건")
            if n_crit == 0 and n_warn == 0:
                st.success("✅ 검수 이슈 없음")

    st.subheader("전체 문서 목록")
    show = docs_df[
        [
            "company",
            "year_month",
            "period_from",
            "period_to",
            "supply_amount",
            "vat",
            "total_amount",
            "n_critical",
            "n_warning",
            "source_file",
        ]
    ].rename(
        columns={
            "company": "업체",
            "year_month": "년월",
            "period_from": "기간시작",
            "period_to": "기간종료",
            "supply_amount": "공급가",
            "vat": "VAT",
            "total_amount": "청구총액",
            "n_critical": "치명",
            "n_warning": "경고",
            "source_file": "파일",
        }
    )
    st.dataframe(
        show,
        use_container_width=True,
        hide_index=True,
        column_config={
            "공급가": st.column_config.NumberColumn(format="%,.0f"),
            "VAT": st.column_config.NumberColumn(format="%,.0f"),
            "청구총액": st.column_config.NumberColumn(format="%,.0f"),
        },
    )


# -----------------------------------------------------------------------------
# 페이지 — 문서 상세·검수
# -----------------------------------------------------------------------------
elif page == "문서 상세 · 검수":
    company = st.selectbox("업체 선택", sorted(docs_df["company"].unique()))
    sub = docs_df[docs_df["company"] == company].sort_values(
        "year_month", ascending=False
    )
    ym = st.selectbox("년월 선택", sub["year_month"].tolist())
    doc_row = sub[sub["year_month"] == ym].iloc[0]
    doc_id = int(doc_row["id"])

    st.subheader(f"{company} · {ym}")
    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
    c1.metric("공급가", format_krw(doc_row["supply_amount"]))
    c2.metric("VAT", format_krw(doc_row["vat"]))
    c3.metric("청구총액", format_krw(doc_row["total_amount"]))
    with c4:
        with get_conn() as _conn:
            report_bytes = build_report_bytes(_conn, doc_id)
        st.download_button(
            "📥 엑셀 리포트 다운로드",
            data=report_bytes,
            file_name=f"검수리포트_{company}_{ym}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with get_conn() as conn:
        items = [dict(r) for r in get_items(conn, doc_id)]
        issues = [dict(r) for r in get_issues(conn, doc_id)]

    # ── 검수 결과 ─────────────────────────────────────────────────────────
    st.markdown("### 🔍 검수 결과")
    if not issues:
        st.success("✅ 탐지된 이슈가 없습니다.")
    else:
        for issue in issues:
            sev = issue["severity"]
            box = {
                "critical": st.error,
                "warning": st.warning,
                "info": st.info,
            }.get(sev, st.info)
            exp = issue["expected_value"]
            act = issue["actual_value"]
            diff = issue["diff"]
            detail = ""
            if exp is not None:
                detail += f"\n- 기대값: **{exp:,.0f}**"
            if act is not None:
                detail += f"\n- 실제값: **{act:,.0f}**"
            if diff is not None:
                detail += f"\n- 차이: **{diff:,.0f}**"
            box(
                f"{severity_emoji(sev)} **[{issue['issue_type']}]** "
                f"{issue['item_name'] or ''}\n\n"
                f"{issue['description']}{detail}"
            )

    # ── 세부 항목 ─────────────────────────────────────────────────────────
    st.markdown("### 📋 세부 항목")
    items_df = pd.DataFrame(items)
    if not items_df.empty:
        show = items_df[
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
        st.dataframe(
            show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "단가": st.column_config.NumberColumn(format="%,.0f"),
                "금액": st.column_config.NumberColumn(format="%,.0f"),
            },
        )

    # 카테고리 파이
    with get_conn() as conn:
        cat_df = category_breakdown(conn, doc_id)
    if not cat_df.empty:
        fig = px.pie(cat_df, names="카테고리", values="금액", title="카테고리별 비중")
        st.plotly_chart(fig, use_container_width=True)


# -----------------------------------------------------------------------------
# 페이지 — 전월 대비 분석
# -----------------------------------------------------------------------------
elif page == "전월 대비 분석":
    company = st.selectbox("업체 선택", sorted(docs_df["company"].unique()))
    sub = docs_df[docs_df["company"] == company].sort_values(
        "year_month", ascending=False
    )
    ym = st.selectbox("비교 기준월 (금월)", sub["year_month"].tolist())
    prev_ym = previous_year_month(ym)
    st.caption(f"전월: {prev_ym} ↔ 금월: {ym}")

    with get_conn() as conn:
        curr, prev, rows = compare_documents(conn, company, ym)

    if curr is None:
        st.warning("금월 문서가 없습니다.")
        st.stop()
    if prev is None:
        st.info("전월 문서가 DB에 없어 비교할 수 없습니다. 전월 데이터를 먼저 적재하세요.")
        st.stop()

    c1, c2, c3 = st.columns(3)
    diff_total = curr["total_amount"] - prev["total_amount"]
    rate_total = diff_total / prev["total_amount"] * 100 if prev["total_amount"] else 0
    c1.metric(
        "전월 청구총액", format_krw(prev["total_amount"])
    )
    c2.metric(
        "금월 청구총액",
        format_krw(curr["total_amount"]),
        delta=f"{diff_total:+,.0f}원 ({rate_total:+.1f}%)",
    )
    c3.metric("변동", format_krw(diff_total))

    df = comparison_dataframe(rows)
    st.markdown("### 📊 항목별 증감")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "전월": st.column_config.NumberColumn(format="%,.0f"),
            "금월": st.column_config.NumberColumn(format="%,.0f"),
            "증감": st.column_config.NumberColumn(format="%,.0f"),
            "증감률(%)": st.column_config.NumberColumn(format="%+.1f"),
        },
    )

    # 증감 바차트
    if not df.empty:
        top = df.head(15).copy()
        top["항목"] = top["항목"].astype(str)
        fig = px.bar(
            top,
            x="증감",
            y="항목",
            orientation="h",
            color="증감",
            color_continuous_scale="RdBu",
            color_continuous_midpoint=0,
            title="변동폭 상위 15개 항목",
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)


# -----------------------------------------------------------------------------
# 페이지 — 추세
# -----------------------------------------------------------------------------
elif page == "추세":
    company = st.selectbox("업체 선택", sorted(docs_df["company"].unique()))
    with get_conn() as conn:
        trend = trend_dataframe(conn, company)
    if trend.empty:
        st.info("데이터가 없습니다.")
    else:
        st.dataframe(
            trend,
            use_container_width=True,
            hide_index=True,
            column_config={
                "공급가": st.column_config.NumberColumn(format="%,.0f"),
                "VAT": st.column_config.NumberColumn(format="%,.0f"),
                "청구총액": st.column_config.NumberColumn(format="%,.0f"),
            },
        )
        fig = px.line(
            trend,
            x="year_month",
            y=["공급가", "VAT", "청구총액"],
            markers=True,
            title=f"{company} 월별 청구 추이",
        )
        st.plotly_chart(fig, use_container_width=True)
