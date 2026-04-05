"""청구마감 엑셀 파서.

요약시트에서 문서 메타데이터와 세부 항목을 추출한다.
값과 수식을 동시에 읽어 후속 검수 단계에서 수식 참조 정합성을 검사할 수 있게 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import openpyxl


_CELL_REF_RE = re.compile(r"'?([^'!=+\-*/,()\s]+)'?!([A-Z]+)(\d+)")


def _col_letter_to_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


# -----------------------------------------------------------------------------
# 데이터 모델
# -----------------------------------------------------------------------------
@dataclass
class ParsedItem:
    row_index: int
    category: Optional[str]
    item_name: Optional[str]
    quantity: Optional[float]
    unit_price: Optional[float]
    amount: Optional[float]
    formula_amount: Optional[str]          # G열 원본 수식
    formula_quantity: Optional[str]        # E열 원본 수식
    remarks: Optional[str]


@dataclass
class ParsedDocument:
    company: str
    year_month: str                 # 'YYYY-MM'
    period_from: Optional[str]
    period_to: Optional[str]
    supply_amount: float
    vat: float
    total_amount: float
    source_file: str
    items: list[ParsedItem] = field(default_factory=list)
    # 검수용 외부 시트 값 캐시 — (sheet_name, row, col) → value
    external_cells: dict = field(default_factory=dict)

    def lookup_value(self, sheet: str, row: int, col: int):
        return self.external_cells.get((sheet, row, col))


# -----------------------------------------------------------------------------
# 파서 상수
# -----------------------------------------------------------------------------
# 업체명 매칭용 — 파일명에 포함되면 해당 업체로 판정
COMPANY_KEYWORDS = {
    "네이처뉴트리션": "네이처뉴트리션",
    "캐처스": "캐처스",
}

# 세부 항목 시작 행 (요약시트 기준)
ITEM_START_ROW = 25

# 컬럼 인덱스 (1-based)
COL_CATEGORY = 2    # B
COL_SUB1 = 3        # C
COL_SUB2 = 4        # D
COL_QTY = 5         # E
COL_UNIT = 6        # F
COL_AMOUNT = 7      # G
COL_REMARK = 8      # H


# -----------------------------------------------------------------------------
# 유틸
# -----------------------------------------------------------------------------
def _detect_company(filename: str) -> str:
    for key, name in COMPANY_KEYWORDS.items():
        if key in filename:
            return name
    return "UNKNOWN"


def _parse_period(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """'가. 청구 기간 : 2026년01월 01일 ~ 2026년 01월 31일' → (from, to, ym)."""
    if not text:
        return None, None, None
    # 숫자만 추출
    nums = re.findall(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if len(nums) >= 2:
        y1, m1, d1 = nums[0]
        y2, m2, d2 = nums[1]
        frm = f"{y1}-{int(m1):02d}-{int(d1):02d}"
        to = f"{y2}-{int(m2):02d}-{int(d2):02d}"
        ym = f"{y1}-{int(m1):02d}"
        return frm, to, ym
    return None, None, None


def _cell_v(row: int, col: int):
    return ws.cell(row=row, column=col).value


# -----------------------------------------------------------------------------
# 메인 파서
# -----------------------------------------------------------------------------
def parse_billing_file(path: str | Path) -> ParsedDocument:
    path = Path(path)

    # ── 1단계: 요약시트의 수식만 경량 추출 (data_only=False, read_only) ──
    wb_formulas = openpyxl.load_workbook(path, data_only=False, read_only=True)
    try:
        summary_sheet_name = wb_formulas.sheetnames[0]
        ws_f = wb_formulas[summary_sheet_name]
        formula_qty: dict[int, str] = {}
        formula_amt: dict[int, str] = {}
        for r_idx, row in enumerate(
            ws_f.iter_rows(min_row=1, max_row=80, max_col=COL_AMOUNT, values_only=True),
            start=1,
        ):
            for c_idx, value in enumerate(row, start=1):
                if isinstance(value, str) and value.startswith("="):
                    if c_idx == COL_QTY:
                        formula_qty[r_idx] = value
                    elif c_idx == COL_AMOUNT:
                        formula_amt[r_idx] = value
    finally:
        wb_formulas.close()

    # ── 2단계: 수식이 참조하는 외부 시트 셀 좌표 집합 수집 ──────────────────
    needed_cells: dict[str, set[tuple[int, int]]] = {}
    for formula in list(formula_qty.values()) + list(formula_amt.values()):
        for match in _CELL_REF_RE.finditer(formula):
            sheet, col_letters, row_str = match.group(1), match.group(2), match.group(3)
            col_idx = _col_letter_to_index(col_letters)
            needed_cells.setdefault(sheet, set()).add((int(row_str), col_idx))

    # ── 3단계: 값 모드 read_only 로드 — 요약시트 전체 + 필요한 외부 셀만 추출 ──
    wb_values = openpyxl.load_workbook(path, data_only=True, read_only=True)
    external_cells: dict[tuple[str, int, int], object] = {}
    summary_grid: dict[tuple[int, int], object] = {}
    try:
        # 요약시트: 1~80행 × 1~8열 전체를 dict로
        ws = wb_values[summary_sheet_name]
        for r_idx, row in enumerate(
            ws.iter_rows(min_row=1, max_row=80, max_col=COL_REMARK, values_only=True),
            start=1,
        ):
            for c_idx, value in enumerate(row, start=1):
                if value is not None:
                    summary_grid[(r_idx, c_idx)] = value

        # 외부 시트: 수식이 참조하는 셀만 read_only iter로 추출
        for sheet_name, coords in needed_cells.items():
            if sheet_name == summary_sheet_name or sheet_name not in wb_values.sheetnames:
                continue
            ws_ext = wb_values[sheet_name]
            max_row_needed = max(r for r, _ in coords)
            max_col_needed = max(c for _, c in coords)
            for r_idx, row in enumerate(
                ws_ext.iter_rows(
                    min_row=1, max_row=max_row_needed, max_col=max_col_needed,
                    values_only=True,
                ),
                start=1,
            ):
                for c_idx, value in enumerate(row, start=1):
                    if (r_idx, c_idx) in coords and value is not None:
                        external_cells[(sheet_name, r_idx, c_idx)] = value
    finally:
        wb_values.close()

    def _cell_v(row: int, col: int):
        return summary_grid.get((row, col))

    company = _detect_company(path.name)

    # ── 청구 기간 / year_month ────────────────────────────────────────────
    period_text = ""
    for r in range(16, 22):
        val = _cell_v(r, 2)
        if val and "청구 기간" in str(val):
            period_text = str(val)
            break
    period_from, period_to, year_month = _parse_period(period_text)

    if not year_month:
        # 파일명에서 fallback: '2026년 01월'
        m = re.search(r"(\d{4})\D+(\d{1,2})", path.name)
        if m:
            year_month = f"{m.group(1)}-{int(m.group(2)):02d}"
        else:
            year_month = "UNKNOWN"

    # ── 합계금액 행 탐색 ────────────────────────────────────────────────
    sum_row = None
    for r in range(ITEM_START_ROW, 80):
        for c in range(1, 10):
            v = _cell_v(r, c)
            if v and "합계금액" in str(v):
                sum_row = r
                break
        if sum_row:
            break

    if sum_row is None:
        raise ValueError(f"'합계금액' 행을 찾을 수 없음: {path.name}")

    # 공급가 / VAT / 청구총액 — 요약시트 기준
    # 합계행(sum_row)에 공급가, sum_row+1에 VAT, sum_row+2에 총액 (G열)
    supply = _cell_v(sum_row, COL_AMOUNT) or 0.0
    vat = _cell_v(sum_row + 1, COL_AMOUNT) or 0.0
    total = _cell_v(sum_row + 2, COL_AMOUNT) or 0.0

    # ── 세부 항목 파싱 ───────────────────────────────────────────────────
    items: list[ParsedItem] = []
    current_category: Optional[str] = None

    for r in range(ITEM_START_ROW, sum_row):
        cat = _cell_v(r, COL_CATEGORY)
        if cat and str(cat).strip():
            current_category = str(cat).strip()

        amount = _cell_v(r, COL_AMOUNT)
        qty = _cell_v(r, COL_QTY)
        unit = _cell_v(r, COL_UNIT)

        # 금액이 숫자이고 0 이상인 경우에만 항목으로 기록
        if not isinstance(amount, (int, float)):
            continue

        # 항목명 조립: D열(세부항목)이 대표, 없으면 C열, 그 다음 B열
        sub1 = _cell_v(r, COL_SUB1)
        sub2 = _cell_v(r, COL_SUB2)
        name_parts = [str(v) for v in (sub2, sub1) if v is not None and str(v).strip()]
        item_name = name_parts[0] if name_parts else (str(cat) if cat else "")

        remark = _cell_v(r, COL_REMARK)

        items.append(
            ParsedItem(
                row_index=r,
                category=current_category,
                item_name=str(item_name) if item_name else None,
                quantity=float(qty) if isinstance(qty, (int, float)) else None,
                unit_price=float(unit) if isinstance(unit, (int, float)) else None,
                amount=float(amount),
                formula_amount=formula_amt.get(r),
                formula_quantity=formula_qty.get(r),
                remarks=str(remark) if remark else None,
            )
        )

    return ParsedDocument(
        company=company,
        year_month=year_month,
        period_from=period_from,
        period_to=period_to,
        supply_amount=float(supply),
        vat=float(vat),
        total_amount=float(total),
        source_file=path.name,
        items=items,
        external_cells=external_cells,
    )
