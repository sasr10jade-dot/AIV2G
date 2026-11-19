# AI-V2G 실증 시스템 PRD — 현행 소스 분석 기준 (v1.0)

| 항목 | 내용 |
|---|---|
| 작성일 | 2026-11-18 |
| 분석 대상 | `main` 브랜치 `622c319` (src 60여 개 모듈, 테스트 51개 파일) |
| 기준 문서 | 올인원 AI 허브 AX 실증 (에너지·모빌리티)_AI-V2G__사업수행계획서(KJ수정)_261116 (71쪽) |
| 대조 산출물 | `docs/AI-V2G_요구사항정의서_v1.0_261118.xlsx` — 계획서 요구사항 67건 + 소스 추가 기능 13건 판정 |
| 테스트 상태 | `python -m pytest` 525 passed, 3 skipped (2026-11-18) |

이 문서는 **지금 코드가 실제로 하는 일**을 제품 요구사항 형태로 정리한 것이다. 계획서 기준 요구사항과의 차이(누락·미흡·추가)는 요구사항정의서 엑셀의 `판정`·`비고` 열에 있고, 이 문서 7장에 요약했다.

---

## 1. 제품 개요

### 1.1 목적
서포터즈 전기차(OBD)·충전사업자(CPO) 충전기·공공 전력/기상 데이터를 하나의 데이터셋으로 모아, AI 수요예측과 V2G 충·방전 최적화를 **실측 데이터 기반 추론**으로 검증하고, 그 결과를 관리자 대시보드와 서포터즈 모바일 앱으로 제공한다.

### 1.2 수행 전제 (코드 전반에 반영됨)
- 실증 차량·충전기에 양방향 방전 설비가 없으므로 **방전은 AI 추론값**이며 실제 방전 제어는 하지 않는다.
- 모든 화면·보고서는 **실측 / 모의(추론)** 를 구분 표기한다 (`src/pipeline/source.py` provenance, `src/reporting.py` 시트 머리글).
- **합성 데이터는 성과 실적에 넣지 않는다** — `src/kpi.py`가 테이블 출처가 `measured`일 때만 `official_by_year`로 센다.

### 1.3 사용자
| 역할 | 계정 역할 | 주요 화면 |
|---|---|---|
| 최고관리자 | `super_admin` | 대시보드, 가입 승인, 웹앱 관리, 계정 관리, 감사로그 |
| 관리자(운영) | `admin` | 대시보드, 가입 승인, 데이터 업로드·배치 재실행 |
| 서포터즈(차량 등록자) | `owner` | 모바일 앱 `/app` (PWA·Android TWA) |
| 참여기관 ㈜아크릴 | 기관 토큰 (`ACRYL_API_TOKEN`, 허용 IP) | `/partner/acryl/*` API |
| 충전사업자 3사 | 사업자 토큰 (`CPO_*_TOKEN`) | `/cpo/ingest/{cpo_id}` API |

---

## 2. 시스템 구성

```
[수집]                     [정제·저장]                [AI·최적화]                  [제공]
OBD 일 배치(NHN Cloud) ─┐                          ┌ 기준 예측모델(LightGBM) ─┐  관리자 대시보드(/dashboard)
CPO 3사 REST/CSV ───────┼─▶ 표준 스키마·품질검증 ─▶ curated.db ┤ 아크릴 예측 수신(우선)   ├▶ 결과 엑셀·Word/PDF 리포트
공공 API/CSV(SMP·기상·  │    (pipeline/*, cpo/*)   │ V2G LP·SOC MILP·안전검증 │  모바일 앱(/app) 추천·리포트
  특일·전력수요·재생) ──┘                          └ 효용성·이익공유 산정 ────┘  아크릴 제공 API(가명 데이터)
```

| 레이어 | 경로 | 핵심 모듈 |
|---|---|---|
| 수집 | `src/pipeline/`, `src/cpo/` | `toast_fetch`, `batch_ingest`, `obd_ingest`, `kpx_ingest`, `public_api`, `public_csv`, `weather_holiday`, `cpo/adapters`, `cpo/sync` |
| 정제·저장 | `src/pipeline/` | `schema`, `quality`, `ingest`, `store`(curated.db), `migrations`, `source` |
| AI 예측 | `src/model/`, `src/acryl/`, `src/automation/` | `demand_forecast`, `features`, `forecast_service`, `plugin_pattern`, `model_registry`, `scheduler`, `acryl/link` |
| 의사결정 | `src/optimizer/`, `src/advisory/` | `decision`(LP), `soc_planner`(MILP), `safety_guard`, `poc_compare`, `effect_metrics`, `economics`, `profit_sharing`, `owner_guide` |
| 서비스 | `src/api/` | FastAPI 앱 `src/api/__init__.py`, 라우터 16개, 정적 화면 `static/*.html` |
| 보안·개인정보 | `src/auth/`, `src/privacy/`, `src/registry/` | 역할 권한, 로그인 제한, 가명화, 번호판 암호화, 해시체인 감사로그, 동의·파기·탈퇴 |

- 실행: `python -m uvicorn src.api:app --host 127.0.0.1 --port 8000`
  - **주의**: 기존 문서(`docs/README.md`, `docs/developer_handover_prd_v1.0.md`, `인수인계_개발자_필독_가이드.txt`)의 `src.api.main:app`은 존재하지 않는 모듈이다. 앱 객체는 `src/api/__init__.py`에 있다.
- API: 94개 엔드포인트, 권한은 코드 의존성에서 자동 추출되어 `docs/api_reference.md`·`/docs`에 표기된다.
- 저장소: SQLite `data/curated.db`(실증 데이터), `data/privacy.db`(동의·감사로그·등록부), `data/users.json`(계정), `data/model_registry/`(모델).

---

## 3. 기능 요구사항 (구현 기준)

### FR-1 데이터 수집·연계
| ID | 기능 | 구현 | 상태 |
|---|---|---|---|
| FR-1.1 | OBD 일 배치 수신: 버킷 `YYYY-MM-DD/YYYYMMDD-<번호판>-<차명>.csv` → `incoming/` → 원본 보관(SHA-256) → shadow/live 반영 | `toast_fetch`, `batch_ingest`, `scripts/run_obd_batch.py`, `/ingest/batch/*` | 실차 1대 검증 |
| FR-1.2 | OBD 1Hz 원본(신호명 수시 변경)을 시간당 `ev_telemetry`로 집계 | `obd_ingest.resolve_obd_columns`, `aggregate_obd_day_to_hourly` | 구현 |
| FR-1.3 | CPO 세션 연계: REST·CSV·Mock 어댑터, 사업자별 `column_map`, 전역 session_id로 멱등 병합, 사업자별 장애 격리 | `cpo/*`, `/cpo/ingest/{cpo_id}`, `/cpo/specs`, `/cpo/status` | **3사 `enabled=false`** |
| FR-1.4 | 공공데이터: SMP(KPX API), 기온(ASOS 광주)·특일(천문연) API, 전력수요·재생에너지 CSV 업로드 | `kpx_ingest`, `weather_holiday`, `public_csv` | 재생에너지 API endpoint 미설정 |
| FR-1.5 | 요금표: 계시별 단가를 설정에서 읽고 `verified=false`이면 가정값 표기 | `grid/tariff`, `grid/pricing`, `config/tariffs.json` | 가정값 |
| FR-1.6 | 데이터 소스 전환: 업로드된 실측이 있으면 사용, 없으면 합성 시나리오 | `pipeline/source` | 구현 |

### FR-2 데이터 품질·성과 집계
| ID | 기능 | 구현 |
|---|---|---|
| FR-2.1 | 필수 항목·값 범위·중복 검증, 결과를 대시보드·엑셀에 표시 | `pipeline/quality` |
| FR-2.2 | 성과 집계: 데이터 1건 = 유효 충전 세션 + 차량-일 프로파일, 연차(Y1 2026-11-01~2027-04-15 / Y2 2027-05-01~11-30) 배정, 목표 4,000/4,500건 | `kpi.py` |
| FR-2.3 | 인프라 현황: EV 30대·차종 수, 충전기 150기·2종, 사업자 3사, 거점 3곳, 활동 차량(최근 7일) | `kpi.infra_status`, `config/sites.json` |
| FR-2.4 | 수집 속도·목표 도달 전망(일자별) | `kpi.collection_status` |

### FR-3 AI 예측
| ID | 기능 | 구현 |
|---|---|---|
| FR-3.1 | 기준 예측모델: LightGBM, 달력·기상·lag/rolling 피처, **마지막 2주 시간순 홀드아웃**, 지표 MAE·RMSE·sMAPE·WAPE | `model/demand_forecast`, `features` |
| FR-3.2 | 예측 서빙: 아크릴 일 배치 예측이 있으면 우선 사용, 없으면 기준 모델, 응답에 `model_source` 명시 | `forecast_service`, `/forecast/*` |
| FR-3.3 | 다기간 전망(일·주·월) 재귀 예측, Word/PDF 리포트 다운로드 | `forecast_horizon`, `forecast_report` |
| FR-3.4 | 차량별 플러그인 패턴(요일×시간 연결 확률) | `plugin_pattern` |
| FR-3.5 | 모델 레지스트리: 대상별 버전, holdout 지표, 학습 구간(`data_range`), 출처(합성/실측) | `automation/model_registry` |
| FR-3.6 | 재학습 사이클: 드리프트 측정 → 후보 재학습 → 승격/거부 (현재 **합성 데이터 기반 데모**) | `automation/scheduler`, `closed_loop` |
| FR-3.7 | 아크릴 연계: 가명 학습 데이터 제공 묶음 생성·다운로드·파기 기록, 예측 수신, 실측 대비 평가 | `acryl/link`, `/acryl/*`, `/partner/acryl/*` |
| FR-3.8 | 모델 카드 조회 | `model/model_card` — **정적 고정값** (7장 참조) |

### FR-4 V2G 의사결정·검증
| ID | 기능 | 구현 |
|---|---|---|
| FR-4.1 | 선단 충·방전 최적화(LP): 부하 평탄화 1순위, 경제성 2순위 | `optimizer/decision` |
| FR-4.2 | 단일 차량 SOC 상태 MILP: 시간대별 잔량, 충·방전 효율, 열화비용, 방전 횟수 상한, 출발 시각·목표/최소 SOC | `optimizer/soc_planner` |
| FR-4.3 | 독립 안전 검증: 계획이 최소 SOC·방전 한도를 어기면 방전 없는 계획(V1G)으로 복귀 | `advisory/safety_guard` |
| FR-4.4 | 일반 충전 vs AI 최적화 비교: 피크 충전 비중, 충전 비용, 목표 배터리 도달, V2G 예측률 + 조건별(시간대·평일/주말·SOC·수요 3분위) | `poc_compare`, `effect_metrics` |
| FR-4.5 | 경제성: 순이익 = 방전수익 − 충전비용 − 열화비용, 손익분기 판매단가, 수익 백테스트 | `economics`, `model/backtest` |
| FR-4.6 | 이익공유 시뮬레이션: 실제 절감액을 차주·충전사업자·플랫폼 비율로 배분 (포인트·정산 원장 없음) | `profit_sharing` |
| FR-4.7 | 모델링 예외 수집: 단계·원인별 집계, 조치 완료 처리 | `automation/issues` |

### FR-5 관리자 웹
| ID | 기능 | 화면/API |
|---|---|---|
| FR-5.1 | 운영 대시보드: 성과지표, 수집 속도, 거점 3곳, CPO 150기 목록, 공공데이터 차트, 예측 정확도·전망, 효용성 비교, 이익공유, 결과 엑셀 다운로드 | `/dashboard`, `/dashboard/data`, `/dashboard/report.xlsx` |
| FR-5.2 | 가입 승인: 번호판 마스킹 목록, 사유 입력 후 원문 열람, 승인·반려·예비 지정 | `/approvals`, `/registrations/*` |
| FR-5.3 | 웹앱 관리: 공지, 앱 안내 문구, 모델링 예외 조치 | `/admin`, `/admin/notices`, `/admin/app-content`, `/admin/modeling-issues` |
| FR-5.4 | 계정 관리(최고관리자), 감사로그 조회·무결성 검증 | `/auth/users`, `/privacy/audit` |

### FR-6 서포터즈 모바일 앱 (`/app`)
| ID | 기능 | API |
|---|---|---|
| FR-6.1 | 가입: 계정+차량+프로필(차종·직업군·시도/시군구/동·출발 시각·목표 배터리), 동의, 승인 전 차량 데이터 접근 불가 | `/signup`, `/signup/options`, `/owner/registration` |
| FR-6.2 | AI 추천 충전 시간: 출발 시각·현재/필요 SOC 반영, 근거 문장(템플릿) 제공, 추천 응답 기록 | `/owner/charging-guide`, `/owner/guide/{guide_id}/respond` |
| FR-6.3 | 내 충전 리포트(최근 4주, 다른 차량 비교), 충전기록(충전량·횟수·요금 × 일·주·월) | `/owner/report`, `/owner/history` |
| FR-6.4 | 알림함(거주지 맞춤 추천 충전소 포함), 공지, 방전 참여 동의(기본 꺼짐) | `/owner/notifications`, `/owner/notices`, `/owner/consent/v2g` |
| FR-6.5 | 내 정보 수정, 비밀번호 변경, 주행거리 기록, 회원 탈퇴(즉시 삭제) | `/owner/profile`, `/owner/password`, `/owner/odometer`, `/owner/account/withdraw` |

---

## 4. 비기능 요구사항 (구현 기준)

| 구분 | 내용 | 근거 |
|---|---|---|
| 권한 | 3역할, 라우터별 `require_*` 의존성, owner는 세션의 `owned_ev_id` 차량만 조회 | `auth/deps`, `routers/owner.py` |
| 인증 보안 | bcrypt, 계정 8회·IP 40회/10분 실패 시 잠금, 비밀번호 강도 검증, 보안 헤더 | `auth/throttle`, `api/__init__` |
| 개인정보 | HMAC 가명 ID, 번호판 Fernet 암호화+블라인드 인덱스, 마스킹, 열람 사유 기록 | `privacy/pseudonym`, `vault`, `registrations` |
| 감사 | SHA-256 해시체인 감사로그, 접속기록 정기 점검(대량·심야 열람 징후) | `privacy/audit`, `access_review` |
| 동의 | 목적별(collect·share_external·research·profile·v2g_discharge) 부여·철회 이력, 문구 버전 | `privacy/consent` |
| 보관·파기 | 반려 신청·접속기록 보유기간 파기(dry-run 기본), 탈퇴 시 즉시 삭제 | `privacy/retention`, `registry/withdrawal` |
| 재현성 | 모델 버전·학습 구간·데이터 출처 기록, 스키마 추가 전용 마이그레이션 | `model_registry`, `pipeline/migrations` |
| 품질 | pytest 525건 통과 | `tests/` |

---

## 5. 데이터 모델 요약

| 테이블/설정 | 주요 필드 | 비고 |
|---|---|---|
| `ev_telemetry` | timestamp, ev_id, ev_type, zone, soc_pct, connected, driving, charging_kw | 시간당 1행 |
| `charger_sessions` | session_id, charger_id, start_ts, end_ts, energy_kwh, charger_type, connector_type, stop_reason, error_code, operator_id, site_id, ev_id | CPO 표준 세션 |
| `grid_signal` | timestamp, power_demand_mw, renewable_gen_mw, temperature_c, price_krw_per_kwh, is_holiday | 공공데이터 결합 |
| `external_forecasts` | 아크릴 예측 일 배치 | 우선 사용 |
| `modeling_issues` | 단계·원인·횟수·조치 | 예외 기록 |
| `config/*.json` | sites, cpo_sources, public_apis, tariffs, vehicle_models, compensation, economics, retention, regions | 확정 전 값은 `verified=false` |

---

## 6. 운영·배치

| 시각(예정) | 작업 | 스크립트 |
|---|---|---|
| 03:30 | CPO 세션 동기화 | `scripts/run_cpo_sync.py` |
| 04:40 | OBD 일 배치(수신·반영) | `scripts/run_obd_batch.py` (`register_obd_batch_task.ps1`) |
| 매일 | 보유기간 경과 파기(확인 후 `--apply`) | `scripts/purge_expired.py` |
| 정기 | 접속기록 점검(징후 시 종료코드 1) | `scripts/access_log_report.py` |

재학습 사이클(`automation/scheduler.run_cycle`)은 운영 스케줄에 등록되어 있지 않다.

---

## 7. 계획서 대비 갭 요약

판정 기준: **충족**(구현 확인) · **미흡**(일부 구현·수치/정의 불일치·합성/고정값 기반) · **누락**(대응 소스 없음) · **추가**(계획서에 없는 구현) · **대상외**(문서·협약·참여기관 수행).

| 구분 | 충족 | 미흡 | 누락 | 대상외 | 추가 |
|---|---|---|---|---|---|
| 1차년도 (43) | 25 | 12 | 3 | 3 | – |
| 2차년도 (18) | 6 | 6 | 4 | 2 | – |
| 공통 (6) | 3 | 3 | 0 | 0 | – |
| 소스 추가 기능 | – | – | – | – | 13 |

### 7.1 누락 (신규 개발 필요)
| ID | 요구사항 | 계획서 |
|---|---|---|
| C01 | 신재생 출력제어 해소 시나리오 — 과잉발전 시간대 도출, 가상 과잉발전 에뮬레이터, 전후 출력제어량 비교 | p.28 |
| D03 | V2G 이동형 ESS 집중 실증 그룹(5~10대, 관용/정주형 구분)과 전용 데이터셋 | p.30 |
| E03 | 가상 전력 거래 및 모의 정산(포인트) PoC — 참여자별 원장, 피크 방전 실적, 포인트 적립·조회 | p.30~31 |
| S02 | CPO 연계 가상 충전요금 할인·마일리지 BM (2차) | p.48 |
| Y2E01 | 타 지역 비교 데이터(대조군) 수집 | p.33 |
| Y2E04 | 정산 PoC 기반 거래·정산 가능범위 (E03 선행) | p.34 |
| Y2E05 | 참여주체 VoC 설문·인터뷰 수집 | p.34 |

### 7.2 미흡 중 우선 보완
| ID | 문제 | 보완 |
|---|---|---|
| K03·B04 | 모델 카드·대시보드의 MAPE 8.4%, R² 0.892, WAPE 7.2%, "목표 15% 이하", 표본 4,200, n_estimators 200, "최근 4주"가 **코드에 고정된 값**이며 실제 학습(마지막 2주 홀드아웃, n_estimators 800, MAPE·R² 미계산)과 다름. 계획서 목표는 MAPE ≤20%, R² ≥0.70, 80:20 | MAPE·R² 계산 추가, 80:20 분할 평가 경로 추가, 모델 카드를 레지스트리 메타데이터로 자동 생성 |
| K01 | 데이터 1건 = 충전 세션 + **차량-일**. 계획서 산식은 차량 OBD 세션(주 3.5회) + 충전 세션(주 210건) × 13주 | 집계 단위를 계획서 산식에 맞추거나 관리기관과 정의 합의 |
| A04 | CPO 3사 `enabled=false`인데 `status` 블록에 connected=true·누적 1,426건 기록. 이 값은 수신 API가 `config/cpo_sources.json`에 직접 쓰는 것으로, `tests/test_cpo_specs.py` 실행만으로도 증가한다 | 협약 후 활성화, 런타임 상태를 DB로 분리, 테스트는 임시 경로 사용 |
| S01·Q02 | VPP 수익원 3종 금액이 총 절감액 × 고정 비율(62/25/13%), 화면에 실존 기업명 표기 | 재원별(요금 차익·피크 저감·출력제어 완화) 산식 구현, 표기 검토 |
| A02 | 가입 차종에 EV9 포함, IONIQ 9 미정의, 제외 로직 없음 | 제외 차종 차단 |
| Q04 | 동의 목적에 위치정보 없음 | 위치정보 수집·이용 동의 목적 추가 |
| D05 | 근거 설명이 문장 틀(템플릿)인데 화면은 "AI 에이전트"로 표기, 계획서 역할표는 LLM 설명 | LLM 설명 연동 또는 표기 정정 |
| Y2B01·Y2B02 | 재학습·의사결정 갱신 구조가 합성 데이터 재생성 기반 | 실측 누적 데이터 입력 경로 연결, 배치 등록 |

### 7.3 추가 기능 (계획서 미기재)
모바일 PWA·Android TWA, 역할 권한·로그인 제한, 번호판 암호화·해시체인 감사로그·접속기록 점검, 파기·탈퇴·변경 이력, 충전 효율 추정, 수익 백테스트·열화비용, 결과 보고 자동 생성, 합성 데이터·OBD 에뮬레이터, 웹앱 관리, AI 운영 지표, VPP·ESG 카드, 공개 데모·고정 링크, 충전 내역 비식별 집계. 보안·개인정보 기능은 계획서 6장 근거 보강 대상이고, VPP·ESG 카드는 7.2의 S01과 함께 정리 대상이다.

---

## 8. 문서 정합성 메모
- `docs/developer_handover_prd_v1.0.md` 3장의 `src/solar/` 경로는 존재하지 않는다(태양광 연계는 `config/public_apis.json`의 `kpx_solar_regional`, endpoint 미설정).
- 같은 문서의 실행 명령 `src.api.main:app` → `src.api:app`.
- 결과 엑셀 시트는 6종(성과지표·일자별 수집·실증 인프라·데이터 품질·효용성 검증·이익공유 기초자료)에 예외·오류 기록이 있으면 1종이 더해진다.
