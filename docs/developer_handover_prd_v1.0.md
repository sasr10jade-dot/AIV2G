# AI-V2G 실증 시스템 개발자 인수인계서 및 PRD (Product Requirements Document) v1.0

**과제명**: 올인원 AI 허브 AX 실증 (에너지·모빌리티) — AI 기반 도시형 에너지 이익공유 및 전기차 V2G 기술 실증  
**문서 버전**: v1.0 (2026. 11.)  
**수신 대상**: 신규 지정 전문 개발자 담당자  
**작성 목적**: 신규 담당 개발자가 시스템 아키텍처, 제품 요구사항(PRD), 데이터 파이프라인, 개발/운영 런북 및 사업수행계획서(V1.0) 연계 요구사항을 단기간에 파악하고 즉시 개발 및 운영을 이어갈 수 있도록 기술 전반을 인수인계함.

---

## 제 1 장. 프로젝트 개요 및 핵심 도메인 컨텍스트

### 1.1 사업 기본 정보
* **내역사업명**: 올인원 AI 허브 AX 실증 지원사업 (전남·광주)
* **총 사업기간**: 2026. 11. 01. ~ 2027. 11. 30. (총 13개월)
  - **1차년도 (기술·서비스 적용 타당성 확인)**: 2026. 11. 01. ~ 2027. 04. 15. (5.5개월)
  - **2차년도 (고도화 및 사업화·글로벌 확산)**: 2027. 05. 01. ~ 2027. 11. 30. (7개월)
* **총 사업비**: 933,400천원 (정부지원금 700,000천원 + 민간부담금 233,400천원)
* **컨소시엄 구성**:
  - **주관기관**: 아우토크립트㈜ (지분율 88%) — 사업 총괄, 데이터 파이프라인, V2G 최적화 의사결정 모델, 서포터즈 모바일앱, 운영 대시보드, CPO 3사 연계
  - **참여기관**: ㈜아크릴 (지분율 12%) — AI 기반 수요·공급 예측모델 1종 개발 및 자체 GPU 클러스터 기반 학습·검증
  - **협력기관**: 민간 충전서비스 사업자(CPO 3사: 나이스인프라, 플러그링크, 이브이시스) — 데이터 제공 용역(각 2,000만원, 총 6,000만원) 및 거점 충전기 150기 연계
  - **관리기관**: (재)전남정보문화산업진흥원

### 1.2 1차년도 정량 성과 목표 (Evaluation Metrics)
1. **EV 실증 인프라 확보**: 2종 이상, **실차 30대** (제주 실증 제외 차종인 토레스 EVX, EV6 등 서포터즈 실제 차량 전수 수급)
2. **충전기 실증 인프라 확보**: 2종 이상(급속/완속), **150기** (CPO 3사 운영 충전기)
3. **실증 데이터셋 구축**: **유효 데이터 4,000건 이상** (세션 단위, 실측·공개·참여자입력 출처구분, 연차별 미중복, 품질검증 통과치)
4. **AI 기반 최적 V2G 예측모델**: **1건** (아크릴 예측모델 1종 + 아우토크립트 최적화 추론 연동, 공식 모델 카드 및 성능평가 보고서)
5. **실증 참여 장소**: **3곳 이상** (공공기관 청사, 기업 빌딩, 도시 공용 거점)

### 1.3 핵심 도메인 규칙 및 수행 전제 (필수 숙지)
* **방전 설비 부재 및 정산 제도 미비**: 현재 실증 차량(토레스 EVX 등은 V2L만 지원) 및 충전기에 양방향 방전 하드웨어가 없고 한전 방전 요금 정산 제도가 마련되지 않은 상태임.
* **실측 충전 기반 소프트웨어 Closed-loop 추론**:
  - 따라서 방전은 실측 주행·충전 데이터를 입력으로 하여 **AI가 가용 방전량과 비용 절감 효과를 정밀 추론(모의)하는 소프트웨어 순환 방식**으로 수행함.
  - 시스템의 모든 화면(대시보드, 모바일웹)과 결과보고서에는 **[실측(Actual)]과 [AI 추론/모의(Inferred)]를 엄격하게 구분 표기**해야 함. (합성 데이터는 성과 건수에 미산입)

---

## 제 2 장. 시스템 아키텍처 및 기술 스택

### 2.1 아키텍처 블록 다이어그램

```
[데이터 인입 레이어]
 ├─ 서포터즈 실차 30대 (OBD2) ──(LTE)──> NHN Cloud Object Storage ──(scripts/run_obd_batch.py)──┐
 ├─ CPO 3사 (충전기 150기) ───────(REST)─> POST /cpo/ingest/{cpo_id} ──────────────────────────┼─> [데이터 수집·정제 파이프라인]
 ├─ 공공데이터 6종 (KPX, 기상) ──(API)──> scripts/sync_weather_holiday.py ─────────────────────┤   - IQR 이상치 격리
 └─ 실증 사이트 3곳 (부하모델) ──(JSON)─> config/sites.json ───────────────────────────────────┘   - HMAC-SHA256 가명화
                                                                                                    │
                                                                                                    ▼
                                                                                         [SQLite Curated DB]
                                                                                         (data/curated.db)
                                                                                                    │
                                  ┌─────────────────────────────────────────────────────────────────┴─────────────────────────────────┐
                                  ▼                                                                                                   ▼
                     [AI 수요·공급 예측 레이어]                                                                             [V2G 최적화 & 의사결정 레이어]
                    (㈜아크릴 조나단 GPU 클러스터)                                                                           (아우토크립트 LP HiGHS 스케줄러)
                     - 충전수요 예측 (kW)                                                                                 - EV별 최적 충전·대기 스케줄
                     - 계통 전력수요 예측 (MW)                                                                            - 가용 방전량(kWh) 잠재량 산출
                     - EV 가용 SOC 예측 (%)                                                                               - 피크 저감 & 요금 절감 계산
                     - 모델 카드 (/models/card)                                                                           - 배터리 안전가드 (safety_guard)
                                  │                                                                                                   │
                                  └────────────────────────────────┬──────────────────────────────────────────────────────────────────┘
                                                                   ▼
                                                      [프론트엔드 & 서비스 표출 레이어]
                                     ├─ 서포터즈 모바일웹 PWA (/app, owner.html): 충전시간 알림, 설명형 AI 배너, 빠른로그인
                                     ├─ 실시간 운영 대시보드 (/dashboard, dashboard.html): 계획서 4대 탭, CPO 3사 모니터링
                                     ├─ 결과보고서 자동 생성 (/dashboard/report.xlsx): 6개 시트 엑셀 다운로드
                                     ├─ 웹앱 관리 및 승인 시스템 (/admin, /approvals): 서포터즈 승인, 예비 참여자 관리
                                     └─ 개발자 진입점 (/dev, dev.html): 전 권역 원클릭 로그인 및 환경 점검
```

### 2.2 기술 스택 총람
| 계층 | 기술 스택 | 선정 사유 및 용도 |
|---|---|---|
| **백엔드 코어** | Python 3.14+, FastAPI, Starlette, Uvicorn | 비동기 고성능 REST API, 엄격한 Pydantic 타입 검증, OpenAPI(/docs) 자동 생성 |
| **데이터베이스** | SQLite3 (`data/curated.db`, `data/privacy.db`) | 서버리스 단일 파일 고속 트랜잭션, WAL 모드, 감사로그 무결성 보장 |
| **데이터 처리** | Pandas, NumPy, OpenPyXL | 시계열 데이터 결합, 리포트 엑셀 자동 생성, IQR 통계 처리 |
| **최적화 & AI** | SciPy (HiGHS LP Solver), Scikit-Learn, LightGBM | EV 충·방전 혼합 정수 계획법(MILP/LP), 시계열 앙상블 회귀 예측 |
| **프론트엔드** | Vanilla HTML5, CSS3, JavaScript (ES6+), SVG | 번들러 없는 제로 디펜던시 PWA, 초경량 렌더링, 모바일 반응형 디자인 |
| **외부 연동 & 배포** | Cloudflare Tunnel (`cloudflared`), GitHub Pages | 고정 진입 링크(`github.io/AIV2G`), 인그레스 보안 터널링 |

### 2.3 디렉토리 구조 및 핵심 모듈 매핑
```
프로젝트 루트 (올인원 AI 허브 AX 실증)
├── config/                     # 시스템 정적 설정 (JSON)
│   ├── cpo_sources.json        # CPO 3사(나이스인프라·플러그링크·이브이시스) 연계 규격 및 상태
│   ├── sites.json              # 실증 참여 장소 3곳(공공기관, 기업빌딩, 도시공용) 스펙 및 24h 부하곡선
│   ├── vehicle_models.json     # 토레스 EVX 및 비현대기아 차종 배터리/충전 제원
│   ├── tariffs.json            # 한전 계시별 전기요금표 (경부하/중간부하/최대부하)
│   └── public_apis.json        # 공공데이터포털(KPX, 기상청, 특일) 연동 설정
├── data/                       # 런타임 데이터 저장소
│   ├── curated.db              # 정제된 텔레메트리, 충전세션, 전력망 신호, 추천 이력 SQLite
│   ├── privacy.db              # 개인정보 동의 이력 및 해시체인 감사로그
│   └── users.json              # 사용자 계정(bcrypt 해시 비밀번호, 역할, 연결 차량 ID)
├── docs/                       # 기술 및 기획 문서
│   ├── data_specifications_v1.0.md  # 전주기 데이터 통합 명세서
│   ├── api_reference.md        # 91개 엔드포인트 공식 API 레퍼런스
│   └── developer_handover_prd_v1.0.md # 본 개발자 인수인계서 및 PRD
├── src/                        # 백엔드 핵심 소스코드
│   ├── advisory/               # 서포터즈 추천 및 AI 안내 (owner_guide.py, safety_guard.py)
│   ├── api/                    # FastAPI 웹 애플리케이션 및 엔드포인트
│   │   ├── routers/            # 모듈별 라우터 (owner, dashboard, cpo, models, auth 등)
│   │   └── static/             # 프론트엔드 HTML/JS (owner.html, dashboard.html, dev.html 등)
│   ├── cpo/                    # CPO 연계 어댑터 및 정규화 (adapters.py, base.py)
│   ├── model/                  # AI 예측모델 인터페이스 및 모델 카드 (model_card.py, demand_forecast.py)
│   ├── optimizer/              # V2G 최적화 의사결정 엔진 (decision.py, economics.py, poc_compare.py)
│   ├── pipeline/               # 데이터 수집, 정제, 스키마, 품질검증 (schema.py, quality.py, store.py)
│   └── privacy/                # HMAC 가명화, 동의 관리, 파기 확인 (pseudonym.py, vault.py)
├── tests/                      # 단위 및 통합 테스트 (pytest 525개 테스트)
└── scripts/                    # 운영 배치 및 유틸리티 스크립트
```

---

## 제 3 장. 제품 요구사항 정의서 (PRD, Product Requirements Document)

### 3.1 PRD-01: 서포터즈 모바일웹 PWA (`/app`, `src/api/static/owner.html`)
* **목적**: 서포터즈 30대 차주에게 최적 충전 시간을 안내하고, 자연어 설명형 근거를 제시하며, 수용 여부 피드백을 수집함.
* **주요 기능**:
  1. **개발자 빠른 로그인 (`devlogin-prominent`)**: 로그인 화면 상단에서 원클릭으로 서포터즈(`owner`) 세션 즉시 발급.
  2. **헤더 알림함 (`🔔 notifModal`)**: 오늘의 최적 충전 시간 알림, 피크 시간대 회피 알림 수신 및 읽음 처리.
  3. **홈 하이라이트 배너**: "오늘 밤 23:00~05:00 충전을 추천해요 (최대 30% 절감)" 배너 및 [충전 시간 확인] 원클릭 이동.
  4. **AI 에이전트 설명 배너 (`ai_agent_explanation`)**: 심야 요금 절감액, 계통 부하 완화, 목표 배터리 충족 근거를 자연어로 설명.
  5. **충전 추천 응답 카드**: "추천대로 할게요 / 나중에" 선택 버튼 제공 → 수용률 지표 자동 집계.
  6. **내 충전 리포트 (4개 지표)**: 최근 4주 충전 통계, 다른 차량 대비 비교, AI 전·후 절감액, 누적 주행거리 입력.
* **인수 조건 (Acceptance Criteria)**:
  - 인증 없는 접근 시 로그인 화면이 뜨며, 비밀번호 노출 없이 빠른 로그인이 동작해야 함 (`test_owner_quick_login.py` 통과).
  - 본인 차량(`owned_ev_id`) 데이터만 조회되어야 하며 다른 차량 조회 불가 (`test_owner_auth.py` 통과).

### 3.2 PRD-02: 실시간 운영 대시보드 (`/dashboard`, `src/api/static/dashboard.html`)
* **목적**: 평가위원 및 운영자가 사업수행계획서의 진도율, 인프라 현황, AI 모델 성능, 효용성 검증 결과를 한눈에 검증함.
* **주요 기능**:
  1. **계획서 4대 탭 구성**:
     - `01 실증 인프라·데이터 수집`: 성과 진행률, 일자별 데이터 수집 현황, 참여 장소 3곳, CPO 150기 연계, 차량별 현황.
     - `02 AI 예측`: 실제값 vs 예측값 비교 차트, 충전수요 전망, 재학습 결과, **AI 모델 카드(Model Card) 팝업 뷰어**.
     - `03 V2G 추론·효과 검증`: S1(일반) vs S2(스마트) vs S3(V2G) 5대 지표 비교, 피크 저감률, 비용 절감률.
     - `04 이익공유 기초자료`: 차주 50% / CPO 30% / 플랫폼 20% 배분 시뮬레이터.
  2. **CPO 3사 연계 명세서 모달 (`#cpo-specs-modal`)**: 나이스인프라·플러그링크·이브이시스 연계 현황 및 데이터/API 명세 확인.
  3. **엑셀 결과보고서 다운로드 (`/dashboard/report.xlsx`)**: 6개 핵심 데이터 시트 원클릭 다운로드.
* **인수 조건**:
  - 관리자(`admin`) 권한 세션에서만 접근 가능해야 함.
  - 모든 지표에 [실측]과 [AI 추론] 배지가 명확히 표기되어야 함.

### 3.3 PRD-03: 충전서비스 사업자(CPO 3사) 연계 API (`src/api/routers/cpo.py`)
* **목적**: 계획서 13장 데이터 제공 용역(총 6천만원) 과업에 따라 CPO 3사의 일 단위 충전 세션을 수신하고 명세를 제공함.
* **주요 기능**:
  1. `GET /cpo/specs`: CPO 3사용 공식 데이터 명세서(10개 필드, 코드 정의) 및 REST API 규격 반환 (관리자 전용).
  2. `GET /cpo/status`: 3개 사업자별 연계 상태, 150기 충전기 수, 일일 수신량, 누적 세션수 반환 (관리자 전용).
  3. `POST /cpo/ingest/{cpo_id}`: 외부 CPO 서버에서 일 단위 배치 데이터를 푸시하는 수신 웹훅 (Bearer 토큰 검증).
* **인수 조건**:
  - `session_id`, `charger_id`, `start_ts`, `energy_kwh` 필수 필드 누락 시 422 또는 에러 목록 반환 (`test_cpo_specs.py` 통과).

### 3.4 PRD-04: AI 수요·공급 예측 및 모델 카드 (`src/api/routers/models.py`, `src/model/model_card.py`)
* **목적**: ㈜아크릴이 개발한 예측모델 1종의 메타데이터, 학습 출처, 성능 지표를 공식 모델 카드 규격으로 제공함.
* **주요 기능**:
  1. `GET /models/card`: 모델명, 개발기관(아크릴/아우토크립트), 14개 피처, WAPE(7.2%)/MAPE(8.4%)/RMSE(14.8kW) 성능 지표 반환.
  2. `GET /models/{target_col}`: 특정 타깃(`ev_charging_demand_kw`, `ev_available_v2g_kwh`) 활성 모델 버전 조회.
* **인수 조건**:
  - 관리자 전용 권한 보호 및 사업수행계획서 성과지표와 100% 일치해야 함 (`test_model_card.py` 통과).

### 3.5 PRD-05: V2G 의사결정 및 최적화 엔진 (`src/optimizer/`, `src/advisory/`)
* **목적**: 차량 운행에 지장을 주지 않으면서(이동권 보장) 요금이 싼 시간에 충전하고 비싼 시간에 방전(모의)하는 스케줄을 도출함.
* **주요 기능**:
  1. **HiGHS LP Solver 기반 최적화 (`decision.py`)**: 요금 단가, 출발 예정시각, 목표 배터리(80%), 최소 배터리(20%) 반영.
  2. **독립 안전 가드 (`safety_guard.py`)**: 최적화 출력값이 최소 잔량 미달, 출발 잔량 부족, 동시 충·방전을 일으키면 즉시 일반 V1G 충전으로 폴백.
  3. **효용성 5대 비교 엔진 (`poc_compare.py`)**: S1 일반충전 vs S2 스마트충전 vs S3 V2G 방전가정 비교.

### 3.6 PRD-06: 인증, 권한 및 개인정보 보호 (`src/auth/`, `src/privacy/`)
* **목적**: 서포터즈 30명의 위치정보·개인정보를 철저히 보호하고, 역할 기반 접근 제어(RBAC)를 수행함.
* **주요 기능**:
  1. **4단계 RBAC**: 공개(익명), 이용자(`owner`), 관리자(`admin`), 최고관리자(`super_admin`).
  2. **가명화 (`pseudonym.py`)**: 차량 번호판, 차대번호, ID를 `HMAC-SHA256` 단방향 해시 키로 저장.
  3. **감사로그 (`audit.py`)**: 개인정보 열람·수정 시 해시체인 무결성 로그 기록.
  4. **데이터 파기**: 과제 종료 시 아크릴 제공 데이터 파기 확인서 연계.

---

## 제 4 장. 개발 환경 셋업 및 실행 런북 (Developer Runbook)

### 4.1 로컬 개발 환경 셋업
```powershell
# 1. 저장소 디렉토리 이동
cd "C:\Users\Jade\Desktop\바탕화면\업무\개발\올인원 AI 허브 AX 실증"

# 2. Python 가상환경 활성화 (또는 글로벌 3.14 사용)
python -V  # Python 3.14.x 권장

# 3. 의존성 패키지 설치
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 4.2 로컬 개발 서버 기동
```powershell
# 백엔드 API 서버 기동 (8000번 포트)
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000 --reload
```
* **접속 확인**:
  - 개발 진입점: `http://127.0.0.1:8000/dev`
  - 모바일웹 PWA: `http://127.0.0.1:8000/app`
  - 운영 대시보드: `http://127.0.0.1:8000/dashboard`
  - OpenAPI 문서: `http://127.0.0.1:8000/docs`

### 4.3 외부 접속 터널링(Cloudflare / ngrok) 운영법
외부 시연 또는 모바일 스마트폰 접속 시 터널링을 가동합니다:
```powershell
# Cloudflare Quick Tunnel 실행 (무료 임시 URL 발급)
cloudflared tunnel --url http://127.0.0.1:8000

# 출력 로그에서 발급된 URL (예: https://xxxx.trycloudflare.com) 확인 후 고정 진입 링크 갱신:
python scripts/update_entry_link.py https://xxxx.trycloudflare.com/ --push
```
* 고정 접속 주소: **`https://sasr10jade-dot.github.io/AIV2G/`** (GitHub Pages)

### 4.4 전체 테스트 자동화 실행 (`pytest`)
모든 코드 수정 후 반드시 전체 단위 테스트를 통과해야 합니다:
```powershell
# 1. 전체 단위 테스트 실행 (525개 테스트)
python -m pytest -q

# 2. 핵심 보안 및 권한 테스트 집중 점검
python -m pytest tests/test_owner_auth.py tests/test_owner_quick_login.py tests/test_cpo_specs.py tests/test_model_card.py -v

# 3. API 레퍼런스 문서 최신화 (API 추가/수정 시 필수)
python scripts/build_api_docs.py
```

---

## 제 5 장. 데이터 수급 및 파이프라인 운영 매뉴얼

### 5.1 서포터즈 실차 30대 OBD 배치 프로세스
* **배치 스크립트**: `scripts/run_obd_batch.py`
* **실행 시점**: 매일 새벽 04:40 및 06:00 (윈도우 작업 스케줄러 등록: `scripts/register_obd_batch_task.ps1`)
* **동작 흐름**:
  1. 클라우드 오브젝트 스토리지에서 전일 실차 30대 OBD 원천 CSV 다운로드
  2. 스키마 유효성 검증 (`EV_TELEMETRY_SCHEMA`) 및 이상치 IQR 필터링
  3. `data/curated.db`의 `ev_telemetry` 테이블에 증분 적재
  4. 차량-일(`vehicle-days`) 유효 관측 건수 집계

### 5.2 CPO 3사 충전 세션 수신 운영
* **수신 엔드포인트**: `POST /cpo/ingest/{cpo_id}` (`niceinfra`, `pluglink`, `evsis`)
* **인증**: Header `Authorization: Bearer <token>`
* **모니터링**: 대시보드 `01 실증 인프라` 탭 및 `GET /cpo/status`에서 오늘 수집 건수와 누적 세션수 상시 확인.

### 5.3 ㈜아크릴 1차 학습 데이터셋 전달 절차 (2027. 1. 초)
1. 11~12월 누적 수집된 `ev_telemetry` 및 `charger_sessions` 데이터 추출.
2. `src/privacy/pseudonym.py`를 거쳐 일체의 개인식별정보(이름, 번호판, 차대번호)를 HMAC-SHA256 해시값으로 변환.
3. 아크릴 측 고정 IP 서버로 안전하게 전송하고 "학습 데이터셋 제공 확인서" 징구.

---

## 제 6 장. 인수인계 체크리스트 및 개발자 즉시 착수 과제

### 6.1 신규 개발자 즉시 착수 과제 (11월~12월 마일스톤)
1. **[실차 OBD 단말 연동]**:
   - 토레스 EVX 실차에 장착된 OBD 단말의 CAN 통신 프로토콜 확인 및 BMS 정밀 SOC가 `ev_telemetry`에 정상 적재되는지 첫 배치 로그 검증.
2. **[CPO 3사 API 연동 테스트]**:
   - 용역 계약 체결 후 CPO 3사 개발팀에 `GET /cpo/specs` 규격을 전달하고, `POST /cpo/ingest/{cpo_id}`로 실 충전 데이터가 일일 40건 이상 유입되는지 모니터링.
3. **[공공데이터 키 환경변수 셋업]**:
   - `.env` 파일에 발급받은 공공데이터 상용 키(`DATA_GO_KR_KEY`)를 등록하고 `scripts/sync_weather_holiday.py` 일일 동기화 확인.
4. **[서포터즈 모바일웹 운영]**:
   - 서포터즈 30명이 스마트폰 PWA로 접속 시, 홈 화면의 "오늘의 충전시간 알림"과 "AI 에이전트 설명 배너"가 매일 KST 기준으로 자연스럽게 갱신되는지 확인.

### 6.2 비상 대응 및 트러블슈팅 가이드
* **터널 연결 끊김 (Cloudflare 502/530 오류)**:
  - 원인: 로컬 uvicorn 프로세스가 종료되었거나 quick tunnel 세션이 만료됨.
  - 조치: 기존 cloudflared 프로세스를 종료하고 `cloudflared tunnel --url http://127.0.0.1:8000` 재실행 후 `python scripts/update_entry_link.py <새URL> --push` 실행.
* **테스트 실패 (`pytest` 에러 발생 시)**:
  - API 엔드포인트를 추가/변경했다면 반드시 `python scripts/build_api_docs.py`를 먼저 실행하여 `docs/api_reference.md`를 동기화해야 `test_api_docs.py`가 통과됨.
  - 신규 라우터 추가 시 비인가 접근이 차단되도록 `require_admin` 또는 `require_owner`를 명시해야 `test_owner_auth.py`가 통과됨.

---
**인수인계 완료 확인**:  
본 문서는 사업수행계획서(V1.0), 시스템 소스코드, 데이터 명세서 및 테스트 슈트와 100% 정합성을 유지하도록 작성되었습니다.
