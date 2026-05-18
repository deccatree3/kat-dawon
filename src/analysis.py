"""전월 대비 비교 및 추세 분석."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# 보관비 PLT 비교
# ---------------------------------------------------------------------------
@dataclass
class StorageSummary:
    sku_count: int
    total_qty: float
    total_plt: float
    density: float  # total_qty / total_plt


@dataclass
class StorageProductRow:
    product_code: str
    product_name: str
    prev_qty: float
    curr_qty: float
    prev_plt: float
    curr_plt: float
    qty_diff: float
    plt_diff: float
    flags: list[str] = field(default_factory=list)


def storage_comparison(
    conn: sqlite3.Connection, company: str, year_month: str
) -> tuple[Optional[StorageSummary], Optional[StorageSummary], list[StorageProductRow]]:
    """금월/전월 보관비 시트 비교. (curr_summary, prev_summary, product_rows)."""
    prev_ym = previous_year_month(year_month)

    curr_doc = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
    prev_doc = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, prev_ym),
    ).fetchone()

    def _summary(doc_id: int) -> Optional[StorageSummary]:
        row = conn.execute(
            """SELECT COUNT(DISTINCT product_code) AS sku,
                      SUM(quantity) AS qty, SUM(pallet_count) AS plt
               FROM storage_inventory WHERE document_id=?""",
            (doc_id,),
        ).fetchone()
        if row is None or row["sku"] == 0:
            return None
        plt = row["plt"] or 1
        return StorageSummary(row["sku"], row["qty"], row["plt"], row["qty"] / plt)

    def _products(doc_id: int) -> dict[str, tuple[str, float, float]]:
        rows = conn.execute(
            """SELECT product_code, product_name,
                      SUM(quantity) AS qty, SUM(pallet_count) AS plt
               FROM storage_inventory WHERE document_id=?
               GROUP BY product_code""",
            (doc_id,),
        ).fetchall()
        return {r["product_code"]: (r["product_name"], r["qty"], r["plt"]) for r in rows}

    if curr_doc is None:
        return None, None, []

    curr_summary = _summary(curr_doc["id"])
    prev_summary = _summary(prev_doc["id"]) if prev_doc else None

    curr_prods = _products(curr_doc["id"])
    prev_prods = _products(prev_doc["id"]) if prev_doc else {}

    all_codes = list(dict.fromkeys(list(prev_prods.keys()) + list(curr_prods.keys())))
    product_rows: list[StorageProductRow] = []
    for code in all_codes:
        p_name, p_qty, p_plt = prev_prods.get(code, ("", 0, 0))
        c_name, c_qty, c_plt = curr_prods.get(code, ("", 0, 0))
        name = c_name or p_name

        flags: list[str] = []
        plt_diff = c_plt - p_plt
        qty_diff = c_qty - p_qty

        if plt_diff > 0 and qty_diff <= 0:
            flags.append("PLT↑ 재고↓↔")
        if c_plt > 0 and c_qty > 0 and c_qty / c_plt < 200:
            flags.append(f"저밀도({c_qty / c_plt:.0f}/PLT)")
        if c_qty == 0 and c_plt > 0:
            flags.append("재고0 PLT잔존")
        if code not in prev_prods and c_plt > 0:
            flags.append("신규")
        if code not in curr_prods:
            flags.append("소멸")

        product_rows.append(
            StorageProductRow(
                product_code=code,
                product_name=name,
                prev_qty=p_qty,
                curr_qty=c_qty,
                prev_plt=p_plt,
                curr_plt=c_plt,
                qty_diff=qty_diff,
                plt_diff=plt_diff,
                flags=flags,
            )
        )

    product_rows.sort(key=lambda r: (-abs(r.plt_diff), -abs(r.qty_diff)))
    return curr_summary, prev_summary, product_rows


# ---------------------------------------------------------------------------
# 이상 항목 탐지
# ---------------------------------------------------------------------------
@dataclass
class Anomaly:
    category: str       # 'billing' / 'storage'
    severity: str       # 'critical' / 'warning' / 'info'
    item_name: str
    description: str
    prev_value: float
    curr_value: float
    diff: float


def detect_anomalies(
    conn: sqlite3.Connection, company: str, year_month: str
) -> list[Anomaly]:
    """전월 대비 이상 항목 자동 탐지."""
    prev_ym = previous_year_month(year_month)
    anomalies: list[Anomaly] = []

    curr_doc = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
    prev_doc = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, prev_ym),
    ).fetchone()

    if curr_doc is None:
        return []

    # --- billing_item 기준 이상 ---
    # (category, item_name, row_index) 별로 (amount, display_name) 매핑.
    # row_index를 키에 포함해 같은 (cat, name)이 두 번 나오는 경우(예: 조업비/PLT)도 분리.
    def _item_map(doc_id: int) -> dict[tuple[str, str, int], tuple[float, str]]:
        rows = conn.execute(
            """SELECT row_index, category, item_name, amount
               FROM billing_item WHERE document_id=? ORDER BY row_index""",
            (doc_id,),
        ).fetchall()
        out: dict[tuple[str, str, int], tuple[float, str]] = {}
        seen: dict[tuple[str, str], int] = {}
        for r in rows:
            cat = r["category"] or ""
            name = r["item_name"] or ""
            occ = seen.get((cat, name), 0)
            seen[(cat, name)] = occ + 1
            display = _RENAME_LOOKUP.get((cat, name, occ), name)
            out[(cat, name, r["row_index"])] = (float(r["amount"] or 0), display)
        return out

    curr_items = _item_map(curr_doc["id"])
    prev_items = _item_map(prev_doc["id"]) if prev_doc else {}

    # 키 매칭은 (cat, name, occurrence) — row_index가 다를 수 있어 occurrence로 매칭
    def _occ_key(items):
        seen: dict[tuple[str, str], int] = {}
        out = {}
        for (cat, name, _), v in sorted(items.items(), key=lambda kv: kv[0][2]):
            occ = seen.get((cat, name), 0)
            seen[(cat, name)] = occ + 1
            out[(cat, name, occ)] = v
        return out

    curr_by_occ = _occ_key(curr_items)
    prev_by_occ = _occ_key(prev_items)
    all_keys = set(list(curr_by_occ.keys()) + list(prev_by_occ.keys()))
    for key in all_keys:
        cat, orig_name, _ = key
        p_amt, _ = prev_by_occ.get(key, (0.0, orig_name))
        c_amt, c_disp = curr_by_occ.get(key, (0.0, orig_name))
        display = c_disp  # 당월 display name 우선
        diff = c_amt - p_amt

        if p_amt == 0 and c_amt >= 100_000:
            anomalies.append(Anomaly(
                "billing", "warning", display,
                f"신규 발생 (전월 0 → 금월 {c_amt:,.0f}원)",
                p_amt, c_amt, diff,
            ))
        elif c_amt == 0 and p_amt >= 100_000:
            anomalies.append(Anomaly(
                "billing", "info", display,
                f"소멸 (전월 {p_amt:,.0f}원 → 금월 0)",
                p_amt, c_amt, diff,
            ))
        elif p_amt > 0 and abs(diff) / p_amt > 0.5 and abs(diff) >= 50_000:
            rate = diff / p_amt * 100
            sev = "warning" if abs(diff) >= 500_000 else "info"
            anomalies.append(Anomaly(
                "billing", sev, display,
                f"전월 대비 {rate:+.0f}% 변동 ({p_amt:,.0f} → {c_amt:,.0f})",
                p_amt, c_amt, diff,
            ))

    # --- storage 기준 이상 (PLT 과다) ---
    if prev_doc:
        def _storage_map(doc_id: int) -> dict[str, tuple[str, float, float]]:
            rows = conn.execute(
                """SELECT product_code, product_name,
                          SUM(quantity) AS qty, SUM(pallet_count) AS plt
                   FROM storage_inventory WHERE document_id=?
                   GROUP BY product_code""",
                (doc_id,),
            ).fetchall()
            return {r["product_code"]: (r["product_name"], r["qty"], r["plt"]) for r in rows}

        curr_st = _storage_map(curr_doc["id"])
        prev_st = _storage_map(prev_doc["id"])

        for code, (name, c_qty, c_plt) in curr_st.items():
            _, p_qty, p_plt = prev_st.get(code, ("", 0, 0))
            plt_diff = c_plt - p_plt
            qty_diff = c_qty - p_qty

            if plt_diff >= 2 and qty_diff <= 0:
                anomalies.append(Anomaly(
                    "storage", "warning", name,
                    f"재고 {p_qty:,.0f}→{c_qty:,.0f} (변동 없거나 감소)인데 PLT {p_plt:.0f}→{c_plt:.0f} (+{plt_diff:.0f}) 증가",
                    p_plt, c_plt, plt_diff,
                ))

    anomalies.sort(key=lambda a: (0 if a.severity == "critical" else 1 if a.severity == "warning" else 2, -abs(a.diff)))
    return anomalies


# ---------------------------------------------------------------------------
# 항목별 비교표 (요약시트 스프레드뷰)
# ---------------------------------------------------------------------------
@dataclass
class SummaryRow:
    row_index: int
    category: Optional[str]
    item_name: Optional[str]
    # 당월
    curr_qty: Optional[float]
    curr_unit: Optional[float]
    curr_amount: Optional[float]
    # 전월
    prev_qty: Optional[float]
    prev_unit: Optional[float]
    prev_amount: Optional[float]
    # GAP
    qty_diff: Optional[float]
    unit_diff: Optional[float]
    amount_diff: float
    # %
    qty_rate: Optional[float]
    unit_rate: Optional[float]
    amount_rate: Optional[float]
    # 판단
    severity: str   # 'error' / 'warning' / 'info' / 'ok' / 'na'
    judgment: str


# 항상 0이어야 하는 항목 (발생 자체가 오류)
_MUST_BE_ZERO = {"추가운임"}

# BTOC 택배비 변동률을 기준으로 비례 변동을 판정할 항목들
_ORDER_LINKED = {
    "국내포장비", "포장비 해외",
    "감열무지라벨 40*20", "스트레치 필름", "OPP/투명/48*40",
}

# 단가가 운송장/건마다 다른 항목 — 트렌드 룰 부적합 (당월 자체는 정상이어도 전월 단가
# fluctuation 때문에 알람이 잘못 뜨는 케이스). 대신 시트 직접 검증 룰로 대체.
_UNIT_VARIES = {"택배착불"}

# raw 재계산이 판단의 절대 기준인 항목 — 비교표 '판단'을 전월대비 룰 대신
# 항상 우리(raw) 계산값으로 덮어씀. 테이블 수치는 원본 billing 값 그대로 유지.
_AUTHORITATIVE_AUDITS = {"번들작업 청구", "BTOB 박스 청구"}

# 항목명 표시용 매핑 — (구분, 원본명)을 등장 순서대로 적용
# (DB의 원본 item_name은 보존, 대시보드 표시 시점에만 치환)
# 같은 (구분, 원본명)이 두 번 나오면 룰도 두 번 (예: 조업비/PLT = BTOB 출고비, 입고비)
_RENAME_RULES: list[tuple[str, str, str]] = [
    ("보관비", "PLT", "보관비"),
    ("보관비", "WMS", "WMS 이용료"),
    ("조업비", "BTOC택배비", "BTOC 택배비"),
    ("조업비", "추가운임", "BTOC 추가 택배비"),
    ("조업비", "국내포장비", "BTOC 포장비"),
    ("조업비", "포장비 해외", "BTOC 해외포장비(라벨 부착 등)"),
    ("조업비", "번들작업", "BTOB 번들작업비"),
    ("조업비", "BTOB", "BTOB 출고비(박스)"),
    ("조업비", "PLT", "BTOB 출고비(팔레트)"),
    ("조업비", "반품 택배", "반품 택배비"),
    ("조업비", "반품 작업비", "반품 작업비"),
    ("조업비", "PLT", "입고비(팔레트)"),
    ("조업비", "택배입고", "입고비(택배)"),
    ("기타", "보관", "팔레트 사용비"),
    ("기타", "이동", "팔레트 이동비"),
    ("기타", "임가공", "임가공비"),
    ("기타", "기타비용", "기타비용"),
    ("기타", "포장비", "포장비(지아미)"),
    ("기타", "용차", "용차 비용"),
    ("기타", "택배착불", "택배 착불비"),
    ("기타", "클레임비용", "클레임비용"),
    ("기타", "에어캡", "부자재비용(에어캡)"),
    ("기타", "감열무지라벨 40*20", "부자재비용(감열무지라벨)"),
    ("기타", "스트레치 필름", "부자재비용(스트레치필름)"),
    ("기타", "OPP/투명/48*40", "부자재비용(OPP)"),
    ("기타", "지아미", "부자재비용(지아미)"),
]


def _build_rename_lookup() -> dict[tuple[str, str, int], str]:
    """(category, item_name, occurrence_idx) → new_name 사전."""
    seen: dict[tuple[str, str], int] = {}
    out: dict[tuple[str, str, int], str] = {}
    for cat, name, new in _RENAME_RULES:
        key = (cat, name)
        idx = seen.get(key, 0)
        out[(cat, name, idx)] = new
        seen[key] = idx + 1
    return out


_RENAME_LOOKUP = _build_rename_lookup()


def _safe_diff(prev, curr):
    if prev is None and curr is None:
        return None
    return (curr or 0) - (prev or 0)


def _safe_rate(diff, prev):
    if diff is None or prev is None or prev == 0:
        return None
    return diff / prev


def _judge_row(
    name: str,
    prev_qty, curr_qty,
    prev_unit, curr_unit,
    prev_amt, curr_amt,
    btoc_rate: Optional[float],
) -> tuple[str, str]:
    """단순 룰 기반 자동 판단. memory: feedback_judgment_criteria 참조."""
    p_amt = prev_amt or 0
    c_amt = curr_amt or 0

    # 1. 단가 변동 (둘 다 양수일 때만 의미 있음)
    if prev_unit and curr_unit and abs(prev_unit - curr_unit) > 0.01:
        if curr_qty:
            expected = prev_unit * curr_qty
            diff = c_amt - expected
            verdict = "과다 청구" if diff > 0 else "부족 청구"
            return (
                "error",
                f"{diff:+,.0f}원 {verdict} — 단가 {prev_unit:,.0f} → {curr_unit:,.0f} 변경. "
                f"전월 단가 유지 시 정상 청구 = 전월 단가 × 당월 수량 = "
                f"{prev_unit:,.0f} × {curr_qty:,.0f} = {expected:,.0f}원. "
                f"실제 {c_amt:,.0f}원",
            )
        return ("error", f"단가 변동 — {prev_unit:,.0f} → {curr_unit:,.0f}")

    # 2. 항상-0 항목 발생
    if name in _MUST_BE_ZERO and c_amt > 0:
        return (
            "error",
            f"{c_amt:,.0f}원 중복 청구 — BTOC 택배비에 이미 포함된 항목. 정상 청구 0원",
        )

    # 3. 둘 다 0
    if p_amt == 0 and c_amt == 0:
        return ("ok", "")

    # 4. 변동 없음 (수량·금액 동일)
    if (prev_qty or 0) == (curr_qty or 0) and abs(c_amt - p_amt) < 1:
        return ("ok", "")

    # 5. 신규 발생
    if p_amt == 0 and c_amt >= 100_000:
        return ("warning", f"신규 발생 {c_amt:,.0f}원 — 시트 확인")
    if p_amt == 0 and c_amt > 0:
        return ("info", f"신규 소액 {c_amt:,.0f}원")

    # 6. 소멸
    if p_amt >= 100_000 and c_amt == 0:
        return ("info", f"소멸 ({p_amt:,.0f}원 → 0) — 운영자 확인")
    if p_amt > 0 and c_amt == 0:
        return ("info", "소멸")

    # 7. 변동률 분석
    diff = c_amt - p_amt
    rate = diff / p_amt if p_amt else None

    # 7-1. 수량·금액 트렌드 일치 — 두 월 모두 수량·금액 있으면 변동률이 비슷해야 함
    # 단가가 고정/거의 일정하면 qty%와 amt%는 같아야 정상. 차이 크면 단가 변경 또는 청구 오류
    # 단가가 매월 fluctuating한 항목(택배착불 등)은 제외 — 시트 검증 룰로 대체
    if (
        name not in _UNIT_VARIES
        and prev_qty is not None and curr_qty is not None
        and (prev_qty or 0) > 0 and p_amt > 0 and c_amt >= 0
    ):
        qty_rate = (curr_qty - prev_qty) / prev_qty
        if rate is not None and abs(qty_rate - rate) > 0.05:
            # 단가 일정 가정 시 정상 추정 = 전월금액 × (당월수량/전월수량)
            expected = p_amt * curr_qty / prev_qty
            diff = c_amt - expected
            verdict = "과다 청구" if diff > 0 else "부족 청구"
            return (
                "error",
                f"{diff:+,.0f}원 {verdict} — 단가 변경 의심 "
                f"(수량 {qty_rate*100:+.0f}% vs 금액 {rate*100:+.0f}% 추세 불일치). "
                f"전월 단가 유지 시 정상 청구 = 전월금액 × (당월수량/전월수량) = "
                f"{p_amt:,.0f} × ({curr_qty:.0f}/{prev_qty:.0f}) = {expected:,.0f}원. "
                f"실제 청구 {c_amt:,.0f}원",
            )

    # 주문량 연동 항목: BTOC% 와 비교
    if name in _ORDER_LINKED and rate is not None and btoc_rate is not None:
        if abs(rate - btoc_rate) <= 0.10:
            return ("ok", "")
        else:
            return ("warning", f"BTOC와 추세 다름 (BTOC {btoc_rate*100:+.0f}%)")

    # 급변동
    if rate is not None and abs(rate) >= 0.5 and abs(diff) >= 50_000:
        return ("warning", f"급변동 {rate*100:+.0f}% — 사유 확인")

    return ("ok", "")


def summary_with_comparison(
    conn: sqlite3.Connection, company: str, year_month: str
) -> list[SummaryRow]:
    """요약시트 항목별 당월·전월 비교 + 자동 판단."""
    prev_ym = previous_year_month(year_month)

    curr = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
    prev = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, prev_ym),
    ).fetchone()
    if curr is None:
        return []

    def _items_by_key(doc_id: int) -> dict[tuple[str, str, int], dict]:
        """(category, item_name, occurrence_idx) → row dict. 같은 항목이 2회 이상 나오면 발생 순서로 구분."""
        rows = conn.execute(
            """SELECT row_index, category, item_name, quantity, unit_price, amount
               FROM billing_item WHERE document_id=? ORDER BY row_index""",
            (doc_id,),
        ).fetchall()
        out: dict[tuple[str, str, int], dict] = {}
        seen: dict[tuple[str, str], int] = {}
        for r in rows:
            cat = r["category"] or ""
            name = r["item_name"] or ""
            k = (cat, name)
            idx = seen.get(k, 0)
            seen[k] = idx + 1
            out[(cat, name, idx)] = dict(r)
        return out

    curr_items = _items_by_key(curr["id"])
    prev_items = _items_by_key(prev["id"]) if prev else {}

    # BTOC 택배비 변동률 산출 (비례 판정 기준)
    btoc_rate: Optional[float] = None
    btoc_curr = next((v for k, v in curr_items.items() if k[1] == "BTOC택배비"), None)
    btoc_prev = next((v for k, v in prev_items.items() if k[1] == "BTOC택배비"), None)
    if btoc_curr and btoc_prev and btoc_prev["amount"]:
        btoc_rate = ((btoc_curr["amount"] or 0) - btoc_prev["amount"]) / btoc_prev["amount"]

    # 키 합집합 — 당월 행 우선 정렬, 전월에만 있는 항목은 뒤에
    curr_keys = sorted(curr_items.keys(), key=lambda k: curr_items[k]["row_index"])
    prev_only_keys = sorted(
        (k for k in prev_items if k not in curr_items),
        key=lambda k: prev_items[k]["row_index"],
    )
    all_keys = curr_keys + prev_only_keys

    # 시트 audit 결과를 row 단위로 매핑 — 같은 G셀에 대한 audit 결과로 판단 강화
    audit_by_cell: dict[str, "SheetAudit"] = {}
    for a in audit_sheets(conn, curr["id"]):
        if a.target_cell:
            audit_by_cell[a.target_cell] = a
    _SEV_ORDER = {"error": 0, "warning": 1, "info": 2, "ok": 3, "na": 4}

    out: list[SummaryRow] = []
    for key in all_keys:
        c = curr_items.get(key, {})
        p = prev_items.get(key, {})
        cat, name, occ = key

        c_qty, c_unit, c_amt = c.get("quantity"), c.get("unit_price"), c.get("amount")
        p_qty, p_unit, p_amt = p.get("quantity"), p.get("unit_price"), p.get("amount")
        r_idx = c.get("row_index") or p.get("row_index") or 0

        sev, judg = _judge_row(name, p_qty, c_qty, p_unit, c_unit, p_amt, c_amt, btoc_rate)
        # 시트 audit 결과 병합
        a = audit_by_cell.get(f"G{r_idx}")
        if a and a.name in _AUTHORITATIVE_AUDITS:
            # 번들작업·BTOB박스: raw 재계산이 곧 판단 기준 — 전월대비 룰 무시하고 항상 우리 계산값 표시
            sev = a.severity
            judg = a.verdict_short or a.description
        elif a and _SEV_ORDER.get(a.severity, 9) < _SEV_ORDER.get(sev, 9):
            # 그 외 시트 audit은 더 심각한 등급일 때만 덮어씀
            sev = a.severity
            judg = a.description

        # 보관비(PLT): 비교표 단순 판단은 무의미 — 별도 PLT/LOC 분석(3번 섹션)이 본 판단.
        # 네뉴·캐처스 공통, _judge_row·audit 결과와 무관하게 안내 문구로 고정.
        if cat == "보관비" and name == "PLT":
            sev = "info"
            judg = "별도 분석 필요"

        display = _RENAME_LOOKUP.get((cat, name, occ), name)

        out.append(SummaryRow(
            row_index=r_idx,
            category=cat,
            item_name=display,
            curr_qty=c_qty, curr_unit=c_unit, curr_amount=c_amt,
            prev_qty=p_qty, prev_unit=p_unit, prev_amount=p_amt,
            qty_diff=_safe_diff(p_qty, c_qty),
            unit_diff=_safe_diff(p_unit, c_unit),
            amount_diff=_safe_diff(p_amt, c_amt) or 0,
            qty_rate=_safe_rate(_safe_diff(p_qty, c_qty), p_qty),
            unit_rate=_safe_rate(_safe_diff(p_unit, c_unit), p_unit),
            amount_rate=_safe_rate(_safe_diff(p_amt, c_amt), p_amt),
            severity=sev,
            judgment=judg,
        ))
    return out


# ---------------------------------------------------------------------------
# 시트 검증 — 보관비 시트 합계가 요약 PLT와 일치하는지
# ---------------------------------------------------------------------------
@dataclass
class SheetAudit:
    name: str
    severity: str   # 'error' / 'warning' / 'ok'
    description: str
    expected: Optional[float] = None
    actual: Optional[float] = None
    target_cell: Optional[str] = None  # 같은 셀 묶어서 표시하기 위함 (예: 'G34')
    verdict_short: Optional[str] = None  # 비교표 '판단' 칸용 한 줄 요약 (raw 재계산 항목)


def audit_sheets(conn: sqlite3.Connection, doc_id: int) -> list[SheetAudit]:
    """DB만으로 가능한 시트 검증. 4월 보관비 SUM 수식 누락 / 반품택배 청구 오류 등 탐지."""
    audits: list[SheetAudit] = []

    # 보관비 PLT: 요약시트 보관료 PLT 행 vs storage_inventory 합계
    summary_plt_row = conn.execute(
        """SELECT row_index, quantity FROM billing_item
           WHERE document_id=? AND category='보관비' AND item_name='PLT'""",
        (doc_id,),
    ).fetchone()
    storage_total = conn.execute(
        "SELECT COALESCE(SUM(pallet_count), 0) AS plt FROM storage_inventory WHERE document_id=?",
        (doc_id,),
    ).fetchone()

    if summary_plt_row and storage_total:
        summary_plt = float(summary_plt_row["quantity"] or 0)
        sheet_plt = float(storage_total["plt"] or 0)
        plt_cell = f"E{summary_plt_row['row_index']}"
        diff = sheet_plt - summary_plt
        expected_amt = sheet_plt * 18000
        actual_amt = summary_plt * 18000
        amt_diff = expected_amt - actual_amt
        target_cell_amt = f"G{summary_plt_row['row_index']}"
        if abs(diff) >= 0.5:
            sev = "error" if abs(diff) >= 1 else "warning"
            verdict = "누락 청구" if amt_diff > 0 else "과다 청구"
            audits.append(SheetAudit(
                name="보관비 시트",
                severity=sev,
                description=(
                    f"**{amt_diff:+,.0f}원 {verdict}** — 보관비 시트 G1 SUM 수식 범위 누락 가능성\n\n"
                    f"**산출식**: 시트 G열 합계 × 18,000원/PLT  \n"
                    f"= {sheet_plt:.0f} × 18,000 = **{expected_amt:,.0f}원** (정상)\n\n"
                    f"**요약시트 청구**: {plt_cell} {summary_plt:.0f} PLT / {actual_amt:,.0f}원  \n"
                    f"(차이 {diff:+.0f} PLT)"
                ),
                expected=expected_amt,
                actual=actual_amt,
                target_cell=target_cell_amt,
            ))
        else:
            audits.append(SheetAudit(
                name="보관비 시트",
                severity="ok",
                description=f"보관비 시트 G열 합계 = 요약시트 {plt_cell} ({sheet_plt:.0f} PLT)",
                expected=sheet_plt,
                actual=summary_plt,
                target_cell=target_cell_amt,
            ))

    # 반품택배 청구 = 반품택배 시트 AG열 합계 / 1.1
    return_total_row = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name LIKE '%반품택배%' AND metric_key='total'""",
        (doc_id,),
    ).fetchone()
    return_billed_row = conn.execute(
        """SELECT row_index, amount FROM billing_item
           WHERE document_id=? AND category='조업비'
             AND (item_name='반품 택배' OR item_name='반품택배')""",
        (doc_id,),
    ).fetchone()
    if return_total_row and return_billed_row and return_total_row["metric_value"]:
        sheet_total = float(return_total_row["metric_value"])
        expected = sheet_total / 1.1
        actual = float(return_billed_row["amount"] or 0)
        cell = f"G{return_billed_row['row_index']}"
        diff = actual - expected
        if abs(diff) > 1:
            verdict = "과다 청구" if diff > 0 else "부족 청구"
            audits.append(SheetAudit(
                name="반품택배 청구",
                severity="error",
                description=(
                    f"**{diff:+,.0f}원 {verdict}** — 요약시트 {cell} 금액 셀이 "
                    f"시트 AG열을 참조하지 않고 하드코딩되었을 가능성\n\n"
                    f"**산출식**: 반품택배 시트 AG열 합계 / 1.1  \n"
                    f"= {sheet_total:,.0f} / 1.1 = **{expected:,.0f}원** (정상)\n\n"
                    f"**요약시트 청구**: {cell} {actual:,.0f}원"
                ),
                expected=expected,
                actual=actual,
                target_cell=cell,
            ))
        else:
            audits.append(SheetAudit(
                name="반품택배 청구",
                severity="ok",
                description=(
                    f"산출식: 시트 AG합계 / 1.1 = {sheet_total:,.0f} / 1.1 = "
                    f"{expected:,.0f}원 = 요약시트 {cell} 청구액"
                ),
                expected=expected,
                actual=actual,
                target_cell=cell,
            ))

    # 택배착불 청구 = 착불 시트 C열 금액 합계 / 1.1
    chakbul_total_row = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name LIKE '%착불%' AND metric_key='total'""",
        (doc_id,),
    ).fetchone()
    chakbul_billed_row = conn.execute(
        """SELECT row_index, amount FROM billing_item
           WHERE document_id=? AND category='기타'
             AND (item_name='택배착불' OR item_name='착불')""",
        (doc_id,),
    ).fetchone()
    if chakbul_total_row and chakbul_billed_row and chakbul_total_row["metric_value"]:
        sheet_total = float(chakbul_total_row["metric_value"])
        expected = sheet_total / 1.1
        actual = float(chakbul_billed_row["amount"] or 0)
        cell = f"G{chakbul_billed_row['row_index']}"
        diff = actual - expected
        if abs(diff) > 1:
            verdict = "과다 청구" if diff > 0 else "부족 청구"
            audits.append(SheetAudit(
                name="택배착불 청구",
                severity="error",
                description=(
                    f"**{diff:+,.0f}원 {verdict}** — 요약시트 {cell} 금액이 "
                    f"착불 시트 C열 합계와 일치하지 않음\n\n"
                    f"**산출식**: 착불 시트 C열 합계 / 1.1  \n"
                    f"= {sheet_total:,.0f} / 1.1 = **{expected:,.0f}원** (정상)\n\n"
                    f"**요약시트 청구**: {cell} {actual:,.0f}원"
                ),
                expected=expected, actual=actual, target_cell=cell,
            ))
        else:
            audits.append(SheetAudit(
                name="택배착불 청구",
                severity="ok",
                description=f"착불 시트 C합계/1.1 = {expected:,.0f}원 = 요약시트 {cell} 청구액",
                expected=expected, actual=actual, target_cell=cell,
            ))

    # 번들작업 청구 = btob번들작업 + btoc번들작업 시트의 입고수량 합계 vs 요약 청구 수량
    # (요약시트 수식이 잘못된 셀을 참조하는 경우 검출 — 4월 네이처: btoc!E134 → 빈셀, 정상은 E40)
    btob_total = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='btob번들작업' AND metric_key='total'""",
        (doc_id,),
    ).fetchone()
    btoc_total = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='btoc번들작업' AND metric_key='total'""",
        (doc_id,),
    ).fetchone()
    bundle_billed = conn.execute(
        """SELECT row_index, quantity, unit_price, amount FROM billing_item
           WHERE document_id=? AND category='조업비' AND item_name='번들작업'""",
        (doc_id,),
    ).fetchone()
    # btoc번들작업 시트 정석 위반 (비-스키니 & 비-선물세트 입고 행) — 발견 시 경고
    btoc_anomaly = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='btoc번들작업' AND metric_key='anomaly_count'""",
        (doc_id,),
    ).fetchone()
    btoc_anomaly_qty = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='btoc번들작업' AND metric_key='anomaly_qty'""",
        (doc_id,),
    ).fetchone()
    if btoc_anomaly and btoc_anomaly["metric_value"] >= 1:
        n = int(btoc_anomaly["metric_value"])
        q = btoc_anomaly_qty["metric_value"] if btoc_anomaly_qty else 0
        audits.append(SheetAudit(
            name="btoc번들작업 시트 정석 위반",
            severity="warning",
            description=(
                f"**비정상 행 {n}개 (입고수량 합 {q:.0f}) 발견**\n\n"
                "BTOC 번들작업 시트에는 '선물세트(N개입)' 형식 또는 "
                "'스키니퓨리티 ...' 외의 입고 행이 와선 안 되는 것이 정석.\n\n"
                "해당 행은 자동 필터로 합산에서 제외됐으나, 시트 자체에 들어온 것 자체가 "
                "잘못 기록된 것이거나 새 SKU 분류 기준이 추가됐을 가능성 — "
                "btoc번들작업 시트 직접 확인 필요. (콘솔 로그에 R번호·상품명 출력됨)"
            ),
        ))

    # 번들 raw (외부 검수용) 메트릭 — 청구마감 시트 검증용
    btob_raw = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='__번들_raw' AND metric_key='btob_input_total'""",
        (doc_id,),
    ).fetchone()
    btoc_raw = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='__번들_raw' AND metric_key='btoc_order_total'""",
        (doc_id,),
    ).fetchone()

    if (btob_total or btoc_total) and bundle_billed:
        btob_sheet = btob_total["metric_value"] if btob_total else 0
        btoc_sheet = btoc_total["metric_value"] if btoc_total else 0
        sheet_sum = btob_sheet + btoc_sheet
        billed_qty = float(bundle_billed["quantity"] or 0)
        unit = float(bundle_billed["unit_price"] or 700)
        cell_qty = f"E{bundle_billed['row_index']}"
        cell_amt = f"G{bundle_billed['row_index']}"

        btob_raw_v = btob_raw["metric_value"] if btob_raw else None
        btoc_raw_v = btoc_raw["metric_value"] if btoc_raw else None
        has_raw = btob_raw_v is not None or btoc_raw_v is not None
        true_btob = btob_raw_v if btob_raw_v is not None else btob_sheet
        true_btoc = btoc_raw_v if btoc_raw_v is not None else btoc_sheet
        true_qty = true_btob + true_btoc

        expected_amt = true_qty * unit
        actual_amt = float(bundle_billed["amount"] or 0)
        amt_diff = actual_amt - expected_amt
        diff_qty = billed_qty - true_qty

        # 두 가지 오류 분리 검출:
        # (a) 요약 수식 오참조: 청구마감 번들 시트 합계 vs 요약 청구
        # (b) 청구마감 시트가 raw와 불일치: 시트 합계 vs raw 합계
        formula_diff = sheet_sum - billed_qty   # +면 시트가 더 큼 = 요약이 누락
        sheet_btob_diff = btob_sheet - (btob_raw_v if btob_raw_v is not None else btob_sheet)
        sheet_btoc_diff = btoc_sheet - (btoc_raw_v if btoc_raw_v is not None else btoc_sheet)

        if abs(diff_qty) >= 0.5:
            verdict = "과다 청구" if amt_diff > 0 else "부족 청구"
            desc = (
                f"**{amt_diff:+,.0f}원 {verdict}**\n\n"
                f"**정상 청구액 (raw 검수 기준)**: "
                f"BTOB {true_btob:.0f} + BTOC {true_btoc:.0f} = "
                f"{true_qty:.0f}건 × {unit:,.0f}원 = **{expected_amt:,.0f}원**\n\n"
                f"**요약시트 청구**: {cell_qty} {billed_qty:.0f}건 / "
                f"{cell_amt} {actual_amt:,.0f}원 (차이 {diff_qty:+.0f}건)"
            )
            # (a) 수식 오참조 — 청구마감 번들 시트 합과 요약 청구가 다름
            if abs(formula_diff) >= 0.5:
                desc += (
                    "\n\n**🔧 수식 오참조 (요약시트 vs 청구마감 번들 시트)**:  \n"
                    f"청구마감 시트 합계 = btob번들작업 {btob_sheet:.0f} + "
                    f"btoc번들작업(필터 적용) {btoc_sheet:.0f} = "
                    f"**{sheet_sum:.0f}건**  \n"
                    f"요약시트 {cell_qty} 청구 = **{billed_qty:.0f}건**  \n"
                    f"→ 차이 {formula_diff:+.0f}건. "
                    f"요약시트 {cell_qty} 수식이 번들 시트의 합계 셀을 정확히 참조하지 않음 "
                    "(예: btoc번들작업 합계 셀 E40 대신 빈 셀 E134 같은 잘못된 위치를 가리킴)."
                )
            # (b) 청구마감 번들 시트가 raw와 다름
            if has_raw and (abs(sheet_btob_diff) >= 0.5 or abs(sheet_btoc_diff) >= 0.5):
                desc += "\n\n**📋 청구마감 번들 시트 vs raw 검수 불일치**:"
                if btob_raw_v is not None:
                    desc += (
                        f"  \n- BTOB: 쿠팡 재고이동건 raw {btob_raw_v:.0f} "
                        f"vs 청구마감 btob번들작업 {btob_sheet:.0f} "
                        f"(차이 {sheet_btob_diff:+.0f})"
                    )
                if btoc_raw_v is not None:
                    desc += (
                        f"  \n- BTOC: 확장주문검색 검수용 raw {btoc_raw_v:.0f} "
                        f"vs 청구마감 btoc번들작업(필터 적용) {btoc_sheet:.0f} "
                        f"(차이 {sheet_btoc_diff:+.0f})"
                    )
            audits.append(SheetAudit(
                name="번들작업 청구",
                severity="error",
                description=desc,
                expected=expected_amt,
                actual=actual_amt,
                target_cell=cell_amt,
                verdict_short=(
                    f"{amt_diff:+,.0f}원 {verdict} — 정상(raw) {true_qty:.0f}건×"
                    f"{unit:,.0f} = {expected_amt:,.0f}원 / 요약 청구 {actual_amt:,.0f}원"
                ),
            ))
        elif abs(formula_diff) >= 0.5:
            # 전체 청구는 정상 추정과 거의 일치하지만 시트합과 요약이 다른 경우 (드문 케이스)
            audits.append(SheetAudit(
                name="번들작업 청구",
                severity="warning",
                description=(
                    f"**수식 오참조 의심**\n\n"
                    f"청구마감 시트 합계 {sheet_sum:.0f}건 vs "
                    f"요약시트 {cell_qty} 청구 {billed_qty:.0f}건 "
                    f"(차이 {formula_diff:+.0f})"
                ),
                expected=expected_amt,
                actual=actual_amt,
                target_cell=cell_amt,
                verdict_short=(
                    f"수식 오참조 의심 — 청구마감 시트합 {sheet_sum:.0f}건 vs "
                    f"요약 청구 {billed_qty:.0f}건 (차이 {formula_diff:+.0f})"
                ),
            ))
        else:
            audits.append(SheetAudit(
                name="번들작업 청구",
                severity="ok",
                description=(
                    f"입고수량 합 {true_qty:.0f}건 = 요약시트 {cell_qty} 청구 {billed_qty:.0f}건"
                ),
                expected=expected_amt,
                actual=actual_amt,
                target_cell=cell_amt,
                verdict_short=(
                    f"정상 — raw {true_qty:.0f}건 = 요약 청구 {billed_qty:.0f}건 "
                    f"(정상 청구액 {expected_amt:,.0f}원)"
                ),
            ))

    # BTOB 박스 청구 검증 (네이처: 확장주문검색 + 입수량 마스터 기반 정산 박스수)
    btob_box_row = conn.execute(
        """SELECT metric_value FROM sheet_metric
           WHERE document_id=? AND sheet_name='__BTOB_정산'
             AND metric_key='settlement_boxes'""",
        (doc_id,),
    ).fetchone()
    btob_billed_row = conn.execute(
        """SELECT row_index, quantity, unit_price, amount FROM billing_item
           WHERE document_id=? AND category='조업비' AND item_name='BTOB'""",
        (doc_id,),
    ).fetchone()
    if btob_box_row and btob_billed_row:
        expected_boxes = float(btob_box_row["metric_value"])
        actual_boxes = float(btob_billed_row["quantity"] or 0)
        unit = float(btob_billed_row["unit_price"] or 300)
        cell = f"E{btob_billed_row['row_index']}"
        amt_cell = f"G{btob_billed_row['row_index']}"
        diff_box = actual_boxes - expected_boxes
        expected_amt = expected_boxes * unit
        actual_amt = float(btob_billed_row["amount"] or 0)
        amt_diff = actual_amt - expected_amt
        if abs(diff_box) >= 0.5:
            verdict = "과다 청구" if amt_diff > 0 else "부족 청구"
            audits.append(SheetAudit(
                name="BTOB 박스 청구",
                severity="error",
                description=(
                    f"**{amt_diff:+,.0f}원 {verdict}** — "
                    "쿠팡서현커머스 시트 박스수 합산이 [밀크런] 제외 기준을 적용하지 않았을 가능성\n\n"
                    f"**산출식**: 확장주문검색 raw 데이터 기준 정산 박스수  \n"
                    f"= {expected_boxes:.2f}박스 (밀크런 제외, 입수량 마스터로 산출)  \n"
                    f"정상 청구액 = 박스수 × {unit:,.0f}원 = **{expected_amt:,.0f}원**\n\n"
                    f"**요약시트 청구**: {cell} {actual_boxes:.0f}박스 / {amt_cell} {actual_amt:,.0f}원"
                ),
                expected=expected_amt,
                actual=actual_amt,
                target_cell=amt_cell,
                verdict_short=(
                    f"{amt_diff:+,.0f}원 {verdict} — 정상(raw) {expected_boxes:.0f}박스×"
                    f"{unit:,.0f} = {expected_amt:,.0f}원 / 요약 청구 "
                    f"{actual_boxes:.0f}박스 {actual_amt:,.0f}원"
                ),
            ))
        else:
            audits.append(SheetAudit(
                name="BTOB 박스 청구",
                severity="ok",
                description=(
                    f"확장주문검색 정산 박스수 {expected_boxes:.2f} = 청구 박스 {actual_boxes:.0f}"
                ),
                expected=expected_amt,
                actual=actual_amt,
                target_cell=amt_cell,
                verdict_short=(
                    f"정상 — raw {expected_boxes:.0f}박스 = 요약 청구 {actual_boxes:.0f}박스 "
                    f"(정상 청구액 {expected_amt:,.0f}원)"
                ),
            ))

    return audits


# ---------------------------------------------------------------------------
# 물류센터 제출용 정정 요청서 — 잘못 청구된 내역을 한 곳에 모아 구체적으로 제시
# ---------------------------------------------------------------------------
def _plain(text: Optional[str]) -> str:
    """마크다운(**, 머리표, 줄바꿈)을 제거해 한 줄 평문으로."""
    if not text:
        return ""
    t = str(text).replace("**", "").replace("  \n", " ").replace("\n", " ")
    t = t.replace("- ", " ").strip()
    while "  " in t:
        t = t.replace("  ", " ")
    return t


def _headline(a: "SheetAudit") -> str:
    """audit에서 결론 한 줄만 추출 (산출식·요약 청구 상세 앞부분)."""
    if a.verdict_short:
        return _plain(a.verdict_short)
    t = _plain(a.description)
    for stop in ("산출식", "요약시트 청구", "요약 청구"):
        i = t.find(stop)
        if i > 8:
            return t[:i].strip(" -—·")
    return t[:120]


def build_correction_report(
    conn: sqlite3.Connection, company: str, year_month: str
) -> dict:
    """청구마감 분석 결과에서 '잘못 청구된 내역'만 추려 물류센터 제출용 구조로 반환.

    Returns: {
      'company','year_month',
      'confirmed': [ {item,billed,normal,diff,direction,conclusion,basis,cell}, ... ],  # 확정
      'suspected': [ {item,billed,normal,diff,conclusion,basis,cell}, ... ],            # 의심(확인 요청)
      'totals': {'refund': 환급요청합계, 'shortfall': 부족·누락 정정합계,
                 'confirmed_cnt','suspected_cnt'}
    }
    direction: diff>0 → '환급 요청'(회사 과다부담), diff<0 → '정정 필요'(과소청구·누락)
    """
    doc = conn.execute(
        "SELECT id FROM billing_document WHERE company=? AND year_month=?",
        (company, year_month),
    ).fetchone()
    if doc is None:
        return {"company": company, "year_month": year_month,
                "confirmed": [], "suspected": [], "totals": {}}
    doc_id = doc["id"] if isinstance(doc, sqlite3.Row) else doc[0]

    rows = summary_with_comparison(conn, company, year_month)
    audits = audit_sheets(conn, doc_id)

    confirmed: list[dict] = []
    suspected: list[dict] = []
    used_cells: set[str] = set()

    # 1) 시트 audit — error→확정 / warning→의심 (ok·info는 제외)
    for a in audits:
        if a.target_cell:
            used_cells.add(a.target_cell)
        if a.severity not in ("error", "warning"):
            continue
        if a.expected is None or a.actual is None:
            # 금액 비구조 audit(예: 정석 위반) — 의심(확인 요청)으로
            suspected.append({
                "item": a.name, "billed": None, "normal": None, "diff": None,
                "conclusion": _headline(a),
                "basis": _plain(a.description), "cell": a.target_cell or "",
            })
            continue
        diff = a.actual - a.expected
        rec = {
            "item": a.name,
            "billed": a.actual,
            "normal": a.expected,
            "diff": diff,
            "direction": "환급 요청" if diff > 0 else "정정 필요",
            "conclusion": _headline(a),
            "basis": _plain(a.description),
            "cell": a.target_cell or "",
        }
        (confirmed if a.severity == "error" else suspected).append(rec)

    # 2) 시트 audit이 못 잡는 요약 단계 오류 — 중복청구(정상 0) 확정 / 그 외 추세는 의심
    for r in rows:
        if r.severity not in ("error", "warning"):
            continue
        cell = f"G{r.row_index}"
        if cell in used_cells:
            continue
        j = r.judgment or ""
        if r.severity == "error" and (r.curr_amount or 0) and (
            "중복 청구" in j or "정상 청구 0" in j
        ):
            billed = float(r.curr_amount or 0)
            confirmed.append({
                "item": r.item_name, "billed": billed, "normal": 0.0,
                "diff": billed, "direction": "환급 요청",
                "conclusion": _plain(j)[:140], "basis": _plain(j), "cell": cell,
            })
        else:
            suspected.append({
                "item": r.item_name, "billed": r.curr_amount, "normal": None,
                "diff": None, "conclusion": _plain(j)[:140],
                "basis": _plain(j), "cell": cell,
            })
        used_cells.add(cell)

    refund = sum(x["diff"] for x in confirmed if x["diff"] and x["diff"] > 0)
    shortfall = sum(-x["diff"] for x in confirmed if x["diff"] and x["diff"] < 0)
    return {
        "company": company,
        "year_month": year_month,
        "confirmed": confirmed,
        "suspected": suspected,
        "totals": {
            "refund": refund,
            "shortfall": shortfall,
            "confirmed_cnt": len(confirmed),
            "suspected_cnt": len(suspected),
        },
    }
