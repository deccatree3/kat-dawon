"""청구마감 문서 검수 규칙.

규칙:
    R1  세부 항목 금액 합계 == 공급가
    R2  공급가 × 1.1 == 청구총액 (VAT 10%)
    R3  수량/금액이 같은 외부시트를 참조하는데 행번호가 다르면 경고
        (네이처뉴트리션 2월 AG2273 버그 탐지)
    R4  같은 외부시트 셀이 요약시트 내에서 중복 참조되면 경고 (지아미 중복)
    R5  수량×단가 ≠ 금액 (소수점 허용)
    R6  수량 > 0 인데 금액 == 0
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .parser import ParsedDocument, ParsedItem


CELL_REF_RE = re.compile(r"'?([^'!=+\-*/,()\s]+)'?!([A-Z]+)(\d+)")


def _col_letter_to_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


@dataclass
class Issue:
    severity: str           # critical / warning / info
    issue_type: str
    item_name: Optional[str]
    expected_value: Optional[float]
    actual_value: Optional[float]
    diff: Optional[float]
    description: str

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "issue_type": self.issue_type,
            "item_name": self.item_name,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "diff": self.diff,
            "description": self.description,
        }


# -----------------------------------------------------------------------------
# 개별 규칙
# -----------------------------------------------------------------------------
def _rule_supply_matches_items(doc: ParsedDocument) -> list[Issue]:
    items_sum = sum(it.amount or 0 for it in doc.items)
    diff = items_sum - doc.supply_amount
    if abs(diff) > 1:
        return [
            Issue(
                severity="critical",
                issue_type="supply_sum_mismatch",
                item_name="합계금액",
                expected_value=items_sum,
                actual_value=doc.supply_amount,
                diff=diff,
                description=f"세부 항목 금액 합계({items_sum:,.0f})와 공급가({doc.supply_amount:,.0f})가 일치하지 않습니다.",
            )
        ]
    return []


def _rule_vat_and_total(doc: ParsedDocument) -> list[Issue]:
    issues: list[Issue] = []
    expected_vat = round(doc.supply_amount * 0.1, 1)
    if abs(expected_vat - doc.vat) > 1:
        issues.append(
            Issue(
                severity="critical",
                issue_type="vat_mismatch",
                item_name="VAT",
                expected_value=expected_vat,
                actual_value=doc.vat,
                diff=doc.vat - expected_vat,
                description=f"VAT(10%) 기대값 {expected_vat:,.0f}원과 실제 {doc.vat:,.0f}원이 다릅니다.",
            )
        )
    expected_total = round(doc.supply_amount * 1.1, 1)
    if abs(expected_total - doc.total_amount) > 1:
        issues.append(
            Issue(
                severity="critical",
                issue_type="total_mismatch",
                item_name="청구총액",
                expected_value=expected_total,
                actual_value=doc.total_amount,
                diff=doc.total_amount - expected_total,
                description=f"청구총액이 공급가×1.1({expected_total:,.0f})과 일치하지 않습니다.",
            )
        )
    return issues


def _extract_refs(formula: Optional[str]) -> list[tuple[str, str, int]]:
    """수식에서 (sheet, col_letters, row) 튜플 리스트 추출."""
    if not formula:
        return []
    return [(s, c, int(r)) for s, c, r in CELL_REF_RE.findall(formula)]


def _rule_qty_amount_row_mismatch(doc: ParsedDocument) -> list[Issue]:
    """수량과 금액이 같은 시트를 참조하는데 행번호가 다르면 경고."""
    issues: list[Issue] = []
    for it in doc.items:
        qty_refs = _extract_refs(it.formula_quantity)
        amt_refs = _extract_refs(it.formula_amount)
        if not qty_refs or not amt_refs:
            continue
        # 첫 번째 참조만 비교 (일반적으로 단일 참조)
        for qs, qc, qr in qty_refs:
            for as_, ac, ar in amt_refs:
                if qs == as_ and qr != ar:
                    # 실제 값 비교로 확신도 높이기
                    qty_cell_val = doc.lookup_value(qs, qr, _col_letter_to_index(qc))
                    amt_row_qty_val = doc.lookup_value(qs, ar, _col_letter_to_index(qc))
                    desc = (
                        f"'{qs}' 시트를 참조할 때 수량은 {qc}{qr}행, 금액은 {ac}{ar}행으로 "
                        f"서로 다른 행을 참조합니다. 수식 업데이트 누락 의심."
                    )
                    if isinstance(qty_cell_val, (int, float)) and isinstance(amt_row_qty_val, (int, float)):
                        desc += f" (수량참조행 {qc}{qr}={qty_cell_val}, 금액참조행의 수량 {qc}{ar}={amt_row_qty_val})"
                    issues.append(
                        Issue(
                            severity="critical",
                            issue_type="formula_row_mismatch",
                            item_name=it.item_name,
                            expected_value=None,
                            actual_value=it.amount,
                            diff=None,
                            description=desc,
                        )
                    )
    return issues


def _rule_duplicate_ref(doc: ParsedDocument) -> list[Issue]:
    """동일 외부시트 셀이 요약시트 내 여러 항목에서 참조되면 중복 의심."""
    seen: dict[tuple[str, str, int], ParsedItem] = {}
    issues: list[Issue] = []
    for it in doc.items:
        for sh, col, row in _extract_refs(it.formula_amount):
            key = (sh, col, row)
            if key in seen:
                prev = seen[key]
                issues.append(
                    Issue(
                        severity="warning",
                        issue_type="duplicate_reference",
                        item_name=it.item_name,
                        expected_value=None,
                        actual_value=it.amount,
                        diff=None,
                        description=(
                            f"'{sh}'!{col}{row} 셀이 '{prev.item_name}'(행 {prev.row_index})과 "
                            f"'{it.item_name}'(행 {it.row_index})에서 중복 참조됩니다. 이중 계상 의심."
                        ),
                    )
                )
            else:
                seen[key] = it
    return issues


def _rule_qty_unit_amount(doc: ParsedDocument) -> list[Issue]:
    issues: list[Issue] = []
    for it in doc.items:
        if it.quantity is None or it.unit_price is None or it.amount is None:
            continue
        if it.unit_price == 0:
            continue
        expected = it.quantity * it.unit_price
        if abs(expected - it.amount) > max(1.0, abs(it.amount) * 0.001):
            issues.append(
                Issue(
                    severity="warning",
                    issue_type="qty_unit_amount_mismatch",
                    item_name=it.item_name,
                    expected_value=expected,
                    actual_value=it.amount,
                    diff=it.amount - expected,
                    description=(
                        f"수량({it.quantity}) × 단가({it.unit_price:,.0f}) = "
                        f"{expected:,.0f}원이지만 금액은 {it.amount:,.0f}원으로 기재."
                    ),
                )
            )
    return issues


def _rule_suspicious_duplicate_amount(doc: ParsedDocument) -> list[Issue]:
    """금액이 0이 아니고 완전히 동일한 항목이 2개 이상이면 중복 의심.

    캐처스 '포장비(지아미)' 시트 참조(row45)와 '부자재 사용내역'의 '지아미'(row54)가
    모두 같은 금액으로 기재되는 케이스를 잡는다.
    """
    issues: list[Issue] = []
    by_amount: dict[float, list] = {}
    for it in doc.items:
        if it.amount and it.amount > 0:
            by_amount.setdefault(it.amount, []).append(it)
    for amt, items in by_amount.items():
        if len(items) < 2:
            continue
        # 완전히 같은 항목명이면 중복 아님(정상 반복일 수 있음) - 이름이 다를 때만
        names = {it.item_name for it in items}
        if len(names) < 2:
            continue
        label = " / ".join(f"{it.item_name}(행{it.row_index})" for it in items)
        issues.append(
            Issue(
                severity="warning",
                issue_type="suspicious_duplicate_amount",
                item_name=items[-1].item_name,
                expected_value=None,
                actual_value=amt,
                diff=None,
                description=(
                    f"서로 다른 항목이 동일 금액({amt:,.0f}원)으로 기재되어 이중 계상 의심: {label}"
                ),
            )
        )
    return issues


def _rule_qty_positive_amount_zero(doc: ParsedDocument) -> list[Issue]:
    issues: list[Issue] = []
    for it in doc.items:
        if it.quantity and it.quantity > 0 and it.amount == 0:
            issues.append(
                Issue(
                    severity="warning",
                    issue_type="qty_positive_amount_zero",
                    item_name=it.item_name,
                    expected_value=None,
                    actual_value=0,
                    diff=None,
                    description=f"수량이 {it.quantity}인데 금액이 0원입니다.",
                )
            )
    return issues


# -----------------------------------------------------------------------------
# 실행
# -----------------------------------------------------------------------------
def validate(doc: ParsedDocument) -> list[Issue]:
    rules = [
        _rule_supply_matches_items,
        _rule_vat_and_total,
        _rule_qty_amount_row_mismatch,
        _rule_duplicate_ref,
        _rule_suspicious_duplicate_amount,
        _rule_qty_unit_amount,
        _rule_qty_positive_amount_zero,
    ]
    issues: list[Issue] = []
    for rule in rules:
        issues.extend(rule(doc))
    return issues
