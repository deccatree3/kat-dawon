# 청구마감 검수·분석 시스템

물류비 정산 청구마감 엑셀을 자동으로 파싱·검수하고, 전월 대비 분석을
Streamlit 대시보드로 제공합니다.

## 구조
```
kat-dawon/
├── org/                    # 원본 청구마감 파일 (*.xlsx)
├── data/billing.db         # SQLite DB (자동 생성)
├── src/
│   ├── parser.py           # 엑셀 → 구조화 데이터
│   ├── validator.py        # 금액 오류 검수 규칙
│   ├── db.py               # SQLite 스키마·입출력
│   ├── analysis.py         # 전월 대비·추세 분석
│   └── ingest.py           # 파싱+검수+DB 저장 오케스트레이터
├── app.py                  # Streamlit 대시보드
└── requirements.txt
```

## 설치
```bash
pip install -r requirements.txt
```

## 실행
```bash
streamlit run app.py
```

브라우저에서 http://localhost:8501 로 접속.

초기 데이터 적재 방법 두 가지:
1. 사이드바 **"org/ 폴더 일괄 적재"** 버튼 클릭
2. 사이드바 **엑셀 업로드**로 개별 파일 드롭

## 검수 규칙
| 규칙 | 설명 |
|------|------|
| R1 | 세부 항목 금액 합계 == 공급가 |
| R2 | 공급가 × 1.1 == 청구총액 |
| R3 | 동일 외부시트를 참조하면서 수량과 금액의 행번호가 다르면 경고 (수식 업데이트 누락) |
| R4 | 동일 외부시트 셀이 요약시트 내 2개 이상 항목에서 참조되면 중복 의심 |
| R5 | 서로 다른 항목이 동일 금액으로 기재되면 이중 계상 의심 |
| R6 | 수량 × 단가 ≠ 금액 |
| R7 | 수량 > 0 인데 금액 == 0 |

## Streamlit Community Cloud 배포

계정: **deccatree3**

1. **GitHub 레포 생성 & 푸시**
   ```bash
   cd C:\claude\kat-dawon
   git init
   git add .
   git commit -m "initial: 청구마감 검수·분석 시스템"
   gh repo create deccatree3/kat-dawon --private --source=. --push
   ```
   `.gitignore`가 `org/`, `data/`, `*.xlsx`를 제외하므로 민감 데이터는 커밋되지 않습니다.

2. **Streamlit Cloud 연결**
   - https://share.streamlit.io 접속 → `deccatree3` 로 로그인
   - **New app** → Repository: `deccatree3/kat-dawon`, Branch: `main`, Main file: `app.py`
   - Deploy 클릭

3. **데이터 적재**
   - 배포된 대시보드에 접속 후 사이드바에서 엑셀을 드래그하여 업로드
   - Streamlit Cloud 환경은 파일시스템이 임시적(ephemeral)이므로 세션이 재시작되면 `data/billing.db`가 초기화됨 → 필요시 Phase 3에서 외부 DB(Supabase / Turso) 연동 가능

4. **재배포**
   - 로컬에서 수정 후 `git push`만 하면 Streamlit Cloud가 자동 재배포

## 성능
- 파일당 파싱+검수 약 **0.5초** (org/ 4개 파일 일괄 적재 ~1.8초)
- 값 모드/수식 모드 모두 `read_only=True` 로 로드하고, 수식이 참조하는 외부 셀만 선택적으로 추출

## DB 스키마
- `billing_document` — 문서 단위 (업체·년월 UNIQUE)
- `billing_item` — 세부 항목
- `validation_issue` — 검수 이슈 (severity: critical / warning / info)
