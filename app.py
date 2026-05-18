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
    audit_sheets,
    detect_anomalies,
    previous_year_month,
    storage_comparison,
    summary_with_comparison,
)
from src.db import (
    get_conn,
    get_loc_sharing_info,
    get_plt_change_verdict,
    get_storage_summary,
    list_documents,
    get_items,
    get_issues,
)
from src.ingest import ingest_file
from src.report import build_report_bytes


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
        "청구마감 + 검수용 raw 파일 업로드",
        type=["xlsx", "xls"],
        accept_multiple_files=True,
        help=(
            "함께 올리면 자동 분류됩니다:\n"
            "- 청구마감(.xlsx)\n"
            "- 확장주문검색(.xls): BTOB 박스 검증\n"
            "- 확장주문검색 ... 번들작업 검수용(.xls): BTOC 번들 검증\n"
            "- 쿠팡 재고이동건(.xlsx): BTOB 번들 검증"
        ),
    )
    if uploaded:
        # 1) 임시 저장
        tmp_files: list[tuple[str, str]] = []  # (original_name, tmp_path)
        for up in uploaded:
            with tempfile.NamedTemporaryFile(suffix=f"_{up.name}", delete=False) as tmp:
                tmp.write(up.getbuffer())
                tmp_files.append((up.name, tmp.name))

        # 2) 파일 분류 — 파일명 키워드 기반
        billing: list[tuple[str, str]] = []
        ext_orders: list[tuple[str, str]] = []     # BTOB 박스 검증 (.xls 일반)
        bundle_btoc: list[tuple[str, str]] = []    # BTOC 번들 검증 (.xls "번들작업")
        bundle_btob: list[tuple[str, str]] = []    # BTOB 번들 검증 (.xlsx "재고이동")
        for name, p in tmp_files:
            low = name.lower()
            if low.endswith(".xlsx"):
                if "재고이동" in name:
                    bundle_btob.append((name, p))
                else:
                    billing.append((name, p))
            elif low.endswith(".xls"):
                if "번들작업" in name:
                    bundle_btoc.append((name, p))
                else:
                    ext_orders.append((name, p))

        # 3) 청구마감 적재 — 검수 파일들이 함께 있으면 페어링
        try:
            loaded_docs: list[str] = []
            for name, path in billing:
                ext_path = ext_orders[0][1] if ext_orders else None
                btob_path = bundle_btob[0][1] if bundle_btob else None
                btoc_path = bundle_btoc[0][1] if bundle_btoc else None
                doc, issues, _ = ingest_file(
                    path,
                    extended_orders_path=ext_path,
                    bundle_btob_path=btob_path,
                    bundle_btoc_path=btoc_path,
                )
                loaded_docs.append(f"{doc.company} {doc.year_month}")
            if loaded_docs:
                paired_kinds = []
                if ext_orders: paired_kinds.append("BTOB박스")
                if bundle_btob: paired_kinds.append("BTOB번들")
                if bundle_btoc: paired_kinds.append("BTOC번들")
                msg = f"✓ {len(loaded_docs)}건 적재"
                if paired_kinds:
                    msg += f" (검수 raw: {', '.join(paired_kinds)})"
                st.caption(msg)
            if (ext_orders or bundle_btob or bundle_btoc) and not billing:
                st.warning("검수용 파일만 업로드됨 — 청구마감(.xlsx)도 같이 올려야 적용됩니다.")
        except Exception as e:
            st.error(f"적재 실패: {e}")
        finally:
            for _, p in tmp_files:
                Path(p).unlink(missing_ok=True)



# -----------------------------------------------------------------------------
# 데이터 로드
# -----------------------------------------------------------------------------
with get_conn() as conn:
    docs = [dict(r) for r in list_documents(conn)]

if not docs:
    st.info("왼쪽 사이드바에서 청구마감 엑셀을 업로드하세요.")
    st.stop()

docs_df = pd.DataFrame(docs)


# -----------------------------------------------------------------------------
# 유틸
# -----------------------------------------------------------------------------
def severity_emoji(sev: str) -> str:
    return {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(sev, "⚪")


# -----------------------------------------------------------------------------
# 페이지 — 종합 검수 리포트 (단일 페이지)
# -----------------------------------------------------------------------------
if True:
    company = st.selectbox("업체 선택", sorted(docs_df["company"].unique()), key="rpt_co")
    sub = docs_df[docs_df["company"] == company].sort_values("year_month", ascending=False)
    ym = st.selectbox("년월 선택", sub["year_month"].tolist(), key="rpt_ym")
    doc_row = sub[sub["year_month"] == ym].iloc[0]
    doc_id = int(doc_row["id"])
    prev_ym = previous_year_month(ym)

    st.subheader(f"{company} · {ym} 종합 검수 리포트")

    supply = doc_row["supply_amount"]
    total = doc_row["total_amount"]

    # ── 0. 청구총액 요약 (전월 비교) ────────────────────────────────────
    prev_doc = docs_df[(docs_df["company"] == company) & (docs_df["year_month"] == prev_ym)]
    prev_total = float(prev_doc.iloc[0]["total_amount"]) if not prev_doc.empty else None

    gap = (total - prev_total) if prev_total is not None else None
    rate_pct = (gap / prev_total * 100) if (prev_total not in (None, 0)) else None

    summary_total_df = pd.DataFrame([{
        "당월": total,
        "전월": prev_total if prev_total is not None else 0,
        "GAP": gap if gap is not None else 0,
        "%": rate_pct,
    }])
    st.dataframe(
        summary_total_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "당월": st.column_config.NumberColumn("당월 청구총액", format="%,.0f원"),
            "전월": st.column_config.NumberColumn("전월 청구총액", format="%,.0f원"),
            "GAP": st.column_config.NumberColumn("GAP", format="%+,.0f원"),
            "%": st.column_config.NumberColumn("%", format="%+.1f%%"),
        },
    )

    # ── 1. 단순 오류 검출 ────────────────────────────────────────────
    st.markdown("### 1. 오류 검출")
    with get_conn() as conn:
        sum_rows = summary_with_comparison(conn, company, ym)
        audits = audit_sheets(conn, doc_id)
        items_rows = [dict(r) for r in get_items(conn, doc_id)]
        validation_issues = [dict(r) for r in get_issues(conn, doc_id)]

    items_sum = sum(r["amount"] or 0 for r in items_rows)
    error_rows = [r for r in sum_rows if r.severity == "error"]

    # 같은 대상 셀(예: G34)에 대한 여러 검출은 하나의 카드로 묶어 표시
    err_cards: list[dict] = []
    # 금액 정합성
    if abs(items_sum - supply) > 1:
        err_cards.append({
            "sev": "error",
            "title": "항목합계 ≠ 공급가",
            "lines": [f"항목합계 {items_sum:,.0f} vs 공급가 {supply:,.0f} — 차이 {items_sum - supply:+,.0f}원"],
        })
    expected_total = round(supply * 1.1, 1)
    if abs(expected_total - total) > 1:
        err_cards.append({
            "sev": "error",
            "title": "공급가×1.1 ≠ 총액",
            "lines": [f"기대 {expected_total:,.0f} vs 실제 {total:,.0f} — 차이 {total - expected_total:+,.0f}원"],
        })

    # target_cell → 카드 인덱스 (그룹핑용)
    by_cell: dict[str, int] = {}

    def _push(sev, target_cell, title, line, sub_label=None):
        """같은 target_cell이면 기존 카드에 추가, 아니면 새 카드 생성."""
        line_text = (f"[{sub_label}] " if sub_label else "") + line
        if target_cell and target_cell in by_cell:
            idx = by_cell[target_cell]
            existing = err_cards[idx]
            # 더 심각한 severity 채택
            order = {"error": 0, "warning": 1, "ok": 2, "info": 2}
            if order.get(sev, 9) < order.get(existing["sev"], 9):
                existing["sev"] = sev
            existing["lines"].append(line_text)
        else:
            err_cards.append({"sev": sev, "title": title, "lines": [line_text]})
            if target_cell:
                by_cell[target_cell] = len(err_cards) - 1

    # 시트 audit이 있는 셀 — 항목 룰 푸시 시 중복 방지용
    audit_cells = {
        a.target_cell for a in audits
        if a.target_cell and a.severity in ("error", "warning")
    }

    # 항목별 자동 판단 오류 (시트 audit이 잡은 셀은 audit 푸시로 대체되므로 스킵)
    for r in error_rows:
        cell = f"G{r.row_index}"
        if cell in audit_cells:
            continue
        title = f"요약시트 {cell} ({r.item_name})"
        _push("error", cell, title, r.judgment)

    # 시트 검증
    for a in audits:
        if a.target_cell:
            # 항목명을 같이 보이도록 title 구성
            r_match = next((r for r in sum_rows if f"G{r.row_index}" == a.target_cell), None)
            label = f"({r_match.item_name})" if r_match else f"({a.name})"
            title = f"요약시트 {a.target_cell} {label}"
            _push(a.severity, a.target_cell, title, a.description)
        else:
            err_cards.append({"sev": a.severity, "title": a.name, "lines": [a.description]})

    # validator R3~R7 (행 단위 산술 검증) — R1·R2(supply/vat)는 위에서 별도 체크하므로 제외
    _SKIP_ISSUE_TYPES = {"supply_sum_mismatch", "vat_mismatch", "total_mismatch"}
    for issue in validation_issues:
        itype = issue["issue_type"]
        if itype in _SKIP_ISSUE_TYPES:
            continue
        sev_map = {"critical": "error", "warning": "warning", "info": "warning"}
        sev = sev_map.get(issue["severity"], "warning")
        title = f"[{itype}] {issue['item_name'] or ''}".strip()
        detail = issue["description"] or ""
        extras = []
        if issue["expected_value"] is not None:
            extras.append(f"기대 {issue['expected_value']:,.0f}")
        if issue["actual_value"] is not None:
            extras.append(f"실제 {issue['actual_value']:,.0f}")
        if issue["diff"] is not None:
            extras.append(f"차이 {issue['diff']:+,.0f}")
        line = detail + ("  \n" + " / ".join(extras) if extras else "")
        err_cards.append({"sev": sev, "title": title, "lines": [line]})

    # 오류/경고만 표시 — 정상(ok)은 노이즈
    visible = [c for c in err_cards if c["sev"] in ("error", "warning")]
    if not visible:
        st.success("✅ 단순 오류 없음 (항목합계·VAT·시트 합계·단가 모두 정상)")
    for card in visible:
        box = {"error": st.error, "warning": st.warning}.get(card["sev"], st.info)
        body = "\n\n".join(f"- {ln}" for ln in card["lines"]) if len(card["lines"]) > 1 else card["lines"][0]
        box(f"**{card['title']}**\n\n{body}")

    # ── 2. 항목별 비교표 ────────────────────────────────────────────
    st.markdown(f"### 2. 항목별 비교표 ({prev_ym} → {ym})")

    sev_emoji = {"error": "🚨", "warning": "⚠️", "info": "ℹ️", "ok": "✅", "na": "·"}

    def _fmt(v, fmt="{:,.0f}"):
        if v is None:
            return fmt.format(0)
        return fmt.format(v)

    def _fmt_rate(v):
        if v is None:
            return "0%"
        return f"{v*100:+.0f}%"

    table_data = []
    for r in sum_rows:
        table_data.append({
            "구분": r.category or "",
            "항목": r.item_name or "",
            "당월수량": r.curr_qty,
            "당월단가": r.curr_unit,
            "당월금액": r.curr_amount,
            "전월수량": r.prev_qty,
            "전월단가": r.prev_unit,
            "전월금액": r.prev_amount,
            "GAP금액": r.amount_diff,
            "%": r.amount_rate,
            "판단": f"{sev_emoji.get(r.severity, '')} {r.judgment}".strip(),
        })

    cmp_df = pd.DataFrame(table_data)

    def _row_style(row):
        sev = sum_rows[row.name].severity
        color = {
            "error": "background-color: #ffe5e5",
            "warning": "background-color: #fff5d6",
            "info": "background-color: #e8f0ff",
        }.get(sev, "")
        return [color] * len(row)

    styled = cmp_df.style.apply(_row_style, axis=1).format({
        "당월수량": lambda v: _fmt(v),
        "당월단가": lambda v: _fmt(v),
        "당월금액": lambda v: _fmt(v),
        "전월수량": lambda v: _fmt(v),
        "전월단가": lambda v: _fmt(v),
        "전월금액": lambda v: _fmt(v),
        "GAP금액": lambda v: _fmt(v, "{:+,.0f}"),
        "%": _fmt_rate,
    })
    st.dataframe(styled, use_container_width=True, hide_index=True, height=min(900, 50 + len(cmp_df) * 35))

    # 합계 행
    total_curr = sum((r.curr_amount or 0) for r in sum_rows)
    total_prev = sum((r.prev_amount or 0) for r in sum_rows)
    total_diff = total_curr - total_prev
    total_rate = total_diff / total_prev if total_prev else None
    st.caption(
        f"공급가 {total_curr:,.0f}원 (전월 {total_prev:,.0f}원 / GAP {total_diff:+,.0f}"
        + (f" / {total_rate*100:+.1f}%)" if total_rate is not None else ")")
    )

    # ── 3. 세부 검증 ─────────────────────────────────────────────────
    st.markdown("### 3. 세부 검증")

    # ── 전월 대비 이상 항목 ──────────────────────────────────────────
    st.markdown(f"#### 전월 대비 이상 항목 ({prev_ym} → {ym})")
    with get_conn() as conn:
        anomalies = detect_anomalies(conn, company, ym)
    billing_anomalies = [a for a in anomalies if a.category == "billing"]

    if not billing_anomalies:
        st.success("✅ 청구 항목에 특이 변동 없음")
    else:
        for a in billing_anomalies:
            box = {"critical": st.error, "warning": st.warning, "info": st.info}.get(a.severity, st.info)
            box(f"{severity_emoji(a.severity)} **{a.item_name}** — {a.description}")

    # ── 보관비 PLT 분석 ──────────────────────────────────────────────
    st.markdown(f"#### 보관비 PLT 분석 ({prev_ym} → {ym})")
    with get_conn() as conn:
        curr_sum, prev_sum, prod_rows = storage_comparison(conn, company, ym)

    if curr_sum is None:
        st.info("보관비 시트 데이터가 없습니다. 데이터를 재적재하세요.")
    else:
        # 총괄 지표
        st.markdown("#### 총괄")
        if prev_sum:
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("SKU수", f"{curr_sum.sku_count}",
                       delta=f"{curr_sum.sku_count - prev_sum.sku_count:+d}")
            mc2.metric("재고수량", f"{curr_sum.total_qty:,.0f}",
                       delta=f"{curr_sum.total_qty - prev_sum.total_qty:+,.0f}")
            mc3.metric("PLT", f"{curr_sum.total_plt:.0f}",
                       delta=f"{curr_sum.total_plt - prev_sum.total_plt:+.0f}")
            mc4.metric("PLT당 재고", f"{curr_sum.density:,.0f}",
                       delta=f"{curr_sum.density - prev_sum.density:+,.0f}")
        else:
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("SKU수", f"{curr_sum.sku_count}")
            mc2.metric("재고수량", f"{curr_sum.total_qty:,.0f}")
            mc3.metric("PLT", f"{curr_sum.total_plt:.0f}")
            mc4.metric("PLT당 재고", f"{curr_sum.density:,.0f}")

        # 점검 필요 상품 (PLT↑ 재고↓↔) — LOC 단위 검증 컬럼 추가
        flagged = [r for r in prod_rows if r.flags and any("PLT↑" in f for f in r.flags)]
        if flagged:
            st.markdown("#### 점검 필요 (전월 대비 재고가 같거나 감소했는데 PLT 증가한 케이스)")
            # 전월 doc_id 조회
            prev_doc_row = docs_df[(docs_df["company"] == company) & (docs_df["year_month"] == prev_ym)]
            prev_doc_id = int(prev_doc_row.iloc[0]["id"]) if not prev_doc_row.empty else None

            flag_data = []
            with get_conn() as conn:
                for r in flagged:
                    if prev_doc_id is not None:
                        verdict = get_plt_change_verdict(conn, doc_id, prev_doc_id, r.product_code)
                        verdict_label = verdict["summary"]
                        n_real = len(verdict["real_increase_locs"])
                        n_realloc = len(verdict["reallocation_locs"])
                        loc_detail = f"실제↑ {n_real} / 재분배 {n_realloc}"
                    else:
                        verdict_label = "전월 데이터 없음"
                        loc_detail = "-"
                    flag_data.append({
                        "상품": r.product_name,
                        "전월재고": r.prev_qty,
                        "금월재고": r.curr_qty,
                        "재고증감": r.qty_diff,
                        "전월PLT": r.prev_plt,
                        "금월PLT": r.curr_plt,
                        "PLT증감": r.plt_diff,
                        "LOC 검증": verdict_label,
                        "LOC 상세": loc_detail,
                        "비고": " | ".join(r.flags),
                    })
            st.dataframe(
                pd.DataFrame(flag_data), use_container_width=True, hide_index=True,
                column_config={
                    "전월재고": st.column_config.NumberColumn(format="%,.0f"),
                    "금월재고": st.column_config.NumberColumn(format="%,.0f"),
                    "재고증감": st.column_config.NumberColumn(format="%+,.0f"),
                    "PLT증감": st.column_config.NumberColumn(format="%+.0f"),
                },
            )
            excess_plt = sum(r.plt_diff for r in flagged if r.plt_diff > 0)
            st.caption(
                f"위 항목 PLT 과다분 합계 (룰 기준): {excess_plt:.0f}PLT, 월 {excess_plt * 18000:,.0f}원. "
                "LOC 검증이 '실제 증가'가 아닌 행은 SKU의 PLT 표기가 늘었지만 같은 LOC 내 재분배일 가능성 — 운영자 판단."
            )

        # 저밀도 경고 — 룰로 잡힌 모든 케이스 표시, PLT 그룹 공유 정보 컬럼으로 사용자 판단 보조
        low_density = [r for r in prod_rows if r.flags and any("저밀도" in f for f in r.flags)
                       and not any("PLT↑" in f for f in r.flags)]
        if low_density:
            st.markdown("#### 저밀도 경고 (PLT 공간이 낭비되고 있는 것)")
            ld_data = []
            with get_conn() as conn:
                for r in low_density:
                    info = get_loc_sharing_info(conn, doc_id, r.product_code)
                    if info["is_solo"]:
                        share_label = "단독"
                    else:
                        share_label = (
                            f"공유 (같은 PLT그룹에 다른 SKU {info['shared_sku_count']}개, "
                            f"그룹 총수량 {info['total_qty_at_group']:,.0f}, "
                            f"그룹 총 PLT {info['total_plt_at_group']:.0f})"
                        )
                    ld_data.append({
                        "상품": r.product_name,
                        "재고": r.curr_qty,
                        "PLT": r.curr_plt,
                        "밀도(개/PLT)": r.curr_qty / r.curr_plt if r.curr_plt > 0 else 0,
                        "PLT 그룹 점유": share_label,
                    })
            st.dataframe(pd.DataFrame(ld_data), use_container_width=True, hide_index=True,
                         column_config={
                             "재고": st.column_config.NumberColumn(format="%,.0f"),
                             "밀도(개/PLT)": st.column_config.NumberColumn(format="%.0f"),
                         })
            st.caption(
                "재고/PLT < 200 룰로 잡힌 모든 케이스. 'PLT 그룹 점유'가 '단독'이면 실제 저밀도, "
                "'공유'는 같은 PLT 그룹(merged cell)에 다른 SKU도 함께 적재 — 최종 판단은 운영자."
            )

    # ── 엑셀 다운로드 ──────────────────────────────────────────────────
    st.divider()
    with get_conn() as conn:
        report_bytes = build_report_bytes(conn, doc_id)
    st.download_button(
        "📥 검수 리포트 엑셀 다운로드",
        data=report_bytes,
        file_name=f"검수리포트_{company}_{ym}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
