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
| FR-5.5 | 웹앱 관리 > VoC 설문: 작성·마감·집계·CSV | `/admin/surveys*` |
| FR-5.6 | 웹앱 관리 > 리워드·정산: 정산 회차 산정·확정·취소, 경제성 검증·인센티브 비교, 정산 문의 답변·포인트 조정, 미션 설정 | `/vpp/settlement/*`, `/vpp/missions*` |
| FR-5.7 | 대시보드 05 VPP·이익공유 실증: 출력제어 해소 시나리오, 재원별 편익, 정산 산정, ESS 집중 실증 그룹·데이터셋 | `/vpp/curtailment`, `/vpp/revenue-sources`, `/vpp/ess-group*` |
| FR-5.8 | 모델 카드(계획서 기준 MAPE·R² 실계산), 대조 지역 비교, 예비 Pool·대체·관용/민간 지정 | `/models/card`, `/models/evaluation`, `/ingest/regions/*`, `/registrations/reserve-status`, `/registrations/replace`, `/registrations/{id}/affiliation` |

### FR-6 서포터즈 모바일 앱 (`/app`)
| ID | 기능 | API |
|---|---|---|
| FR-6.1 | 가입: 계정+차량+프로필(차종·직업군·시도/시군구/동·출발 시각·목표 배터리), 동의, 승인 전 차량 데이터 접근 불가 | `/signup`, `/signup/options`, `/owner/registration` |
| FR-6.2 | AI 추천 충전 시간: 출발 시각·현재/필요 SOC 반영, 근거 문장(최적화 결과 기반 문장 틀, LLM 아님) 제공, 추천 응답 기록 | `/owner/charging-guide`, `/owner/guide/{guide_id}/respond` |
| FR-6.3 | 내 충전 리포트(최근 4주, 다른 차량 비교), 충전기록(충전량·횟수·요금 × 일·주·월) | `/owner/report`, `/owner/history` |
| FR-6.4 | 알림함(거주지 맞춤 추천 충전소 포함), 공지, 방전 참여 동의(기본 꺼짐) | `/owner/notifications`, `/owner/notices`, `/owner/consent/v2g` |
| FR-6.5 | 내 정보 수정, 비밀번호 변경, 주행거리 기록, 회원 탈퇴(즉시 삭제) | `/owner/profile`, `/owner/password`, `/owner/odometer`, `/owner/account/withdraw` |
| FR-6.6 | 리워드 탭: 등급·확정/대기 포인트, 미션 진행도·보상 받기, 재원별 정산 내역, 정산 문의 | `/owner/points`, `/owner/missions*`, `/owner/points/inquiries` |
| FR-6.7 | 홈 설문 카드(VoC 1회 응답), 마이페이지 위치정보 별도 동의 | `/owner/surveys*`, `/owner/consent/location` |

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

## 7. 계획서 대비 갭 요약 (2026-11-19 현행화)

판정 기준: **충족**(구현 확인) · **미흡**(일부 구현·외부 조건 대기) · **누락**(대응 소스 없음) · **추가**(계획서에 없는 구현) · **대상외**(문서·협약·참여기관 수행).
항목별 구현 방식은 요구사항정의서 엑셀(`AI-V2G_요구사항정의서_v1.0_261118.xlsx`)의 비고, 계산식은 같은 파일 '계산식·AI 방법' 시트에 있다.

| 구분 | 충족 | 미흡 | 누락 | 대상외 | 추가 | 충족률 |
|---|---|---|---|---|---|---|
| 1차년도 (43) | 37 | 3 | 0 | 3 | – | 92.5% |
| 2차년도 (18) | 14 | 2 | 0 | 2 | – | 87.5% |
| 공통 (6) | 6 | 0 | 0 | 0 | – | 100% |
| 소스 추가 기능 | – | – | – | – | 15 | – |
| 합계 | 57 | 5 | 0 | 5 | 15 | **91.9%** (최초 11/18: 54.8%) |

### 7.1 남은 미흡 (외부 조건·2차년도 범위)
| ID | 남은 부분 | 필요 조건 |
|---|---|---|
| A04 | CPO 3사 실데이터 연계(어댑터·수신 API·인증·수신 실적 분리는 완료) | 용역 협약, 사업자 토큰 발급 → `enabled=true` |
| A10 | KPX 지역 태양광 발전량 API 자동 수집, 요금표 확정(전력수요·재생에너지는 CSV 업로드로 반영 가능) | 공공데이터포털 활용신청·명세 확인, 인증키 |
| D05 | 추천 근거 설명의 LLM 연동(현재 최적화 결과 기반 문장 틀, 표기 정정 완료) | LLM 연동 결정 또는 계획서 문구 조정 |
| S02 | CPO 요금 연동 충전요금 할인·마일리지 BM(시뮬레이션은 완료) | 2차년도 범위, CPO 협약 |
| Y2C02 | 거점 부하·충전기·EV 통합자원 운영(입찰·제어) 모델 | 2차년도 범위 |

### 7.2 추가 기능 (계획서 미기재, 15건)
모바일 앱(PWA·TWA), 계정·보안, 개인정보 강화, 보유기간 파기·탈퇴, 충전 효율 추정, 백테스트·열화비용, 결과 보고 자동 생성, 합성 데이터·OBD 에뮬레이터, 웹앱 관리, AI 운영 지표, VPP·ESG 카드(산식 기반으로 정리), 공개 데모, 충전 내역 비식별 집계, **서포터즈 미션·등급(X14)**, **내부망 개발 서버·진입 화면·공용 로딩(X15)**.

## 8. 문서 정합성 메모
- `docs/developer_handover_prd_v1.0.md` 3장의 `src/solar/` 경로는 존재하지 않는다(태양광 연계는 `config/public_apis.json`의 `kpx_solar_regional`, endpoint 미설정).
- 같은 문서의 실행 명령 `src.api.main:app` → `src.api:app`.
- 결과 엑셀 시트는 6종(성과지표·일자별 수집·실증 인프라·데이터 품질·효용성 검증·이익공유 기초자료)에 예외·오류 기록이 있으면 1종이 더해진다.

---

## 9. 2026-11-19 보강 개발 반영 (요구사항정의서 현행화)

| 요구사항 | 보강 내용 | 주요 소스·API |
|---|---|---|
| K03·B04 | 모델 카드 고정값 제거, Train/Test 80:20(시간순)+5겹 시간순 교차검증으로 MAPE·R² 실계산 | `src/model/demand_forecast.py`, `GET /models/card`, `GET /models/evaluation` |
| C01 | 출력제어 해소 에뮬레이터(정제→패턴분석→가상 과잉발전→전후 비교) | `src/optimizer/curtailment.py`, `GET /vpp/curtailment` |
| S01·Y2D01 | 재원별 편익 산식(요금 차익·피크 저감·출력제어 완화), 고정 비율·실존 기업명 제거 | `src/optimizer/vpp_revenue.py`, `GET /vpp/revenue-sources` |
| E03·Y2D03·Y2E04 | 가상 전력 거래 원장·모의 포인트 정산(중복 정산 방지, 관용/민간 집계, 인센티브 유형 비교) | `src/settlement/ledger.py`, `/vpp/settlement/*`, `GET /owner/points` |
| D03 | 이동형 ESS 집중 실증 그룹(5~10대) 선정·지정·전용 데이터셋 | `src/optimizer/ess_group.py`, `/vpp/ess-group*` |
| K01 | 데이터 건수 = 차량 운행·충전 세션 + 충전기 세션(계획서 p.16) | `src/kpi.py` |
| A02 | 제주 V2G 실증 차종(EV9·아이오닉 9) 가입·승인 차단 | `config/vehicle_models.json`, `src/registry/` |
| A07·K02 | 차량 소속(관용/민간), 예비 Pool 경고·대체 이력 | `/registrations/{id}/affiliation`, `/registrations/reserve-status`, `/registrations/replace` |
| Q04 | 위치정보 별도 동의 | `/owner/consent/location` |
| A04 | CPO 수신 실적을 `data/cpo_status.json`으로 분리, 토큰 설정 시 인증 필수 | `src/api/routers/cpo.py` |
| C04·Y2A02·E01 | 운행조건 분석 축, 운영조건 커버리지, 결과 엑셀 '한계·개선 기준' 시트 | `src/optimizer/effect_metrics.py`, `src/reporting.py` |
| D05 | 'AI 에이전트' 표기를 '최적화 결과 기반 추천 근거 설명'으로 정정(LLM 미연동은 남은 과제) | `src/advisory/owner_guide.py` |
| 운영 | 내부망 개발 서버(0.0.0.0:3000), 간편 로그인 내부망 한정 | `scripts/run_dev_lan.cmd`, `src/auth/deps.py` |

남은 미흡·누락: A04(CPO 실연계, 협약 후), A10(재생에너지·전력수요 API 자동수집), A15(원천 행 단위 출처), D05(LLM 설명), S02·Y2B01·Y2B02·Y2C02(2차년도), Y2E01·Y2E05(누락).
가정값은 `config/vpp_settlement.json`(verified=false)에 있으며 모든 결과는 '모의(가정값)'로 표기된다.

---

## 10. 2026-11-19 2차 보강 (리워드 정산 프로세스·미션·VoC·운영)

### 10.1 서포터즈 리워드 — 가상 전력 거래 모의 정산 프로세스 (계획서 p.30~31, p.48)
| 단계 | 내용 | 소스·API |
|---|---|---|
| ① 실적 계측(추론) | 차량·일 단위 피크 시간대(17~21시) 방전 kWh, 과잉발전 흡수 kWh, 요금 절감액. 등록 차량은 **방전 참여 동의가 있을 때만** 방전 실적 인정 | `src/settlement/ledger.py` compute_trades |
| ② 재원별 배분 | 요금 차익(절감액 비율)·피크 저감(방전 kWh 비율)·출력제어 완화(흡수 kWh 비율)로 재원 편익을 차량에 배분 | 〃, `src/optimizer/vpp_revenue.py` |
| ③ 포인트 산출 | 배분액 × 참여자 배분율(70%) ÷ 1원/P — 시민 체감형 가상 포인트 | config/vpp_settlement.json points |
| ④ 정산 회차 | 산정(draft, 원장 미적립) → 관리자 검토 → **확정**(원장 적립) 또는 **취소**(사유 필수, 재산정 가능). 차량·일 중복 정산 없음, 새 실적 없으면 회차 미생성 | `POST /vpp/settlement/runs`, `/runs/{id}/confirm`, `/runs/{id}/cancel` |
| ⑤ 조회·문의·조정 | 차주: 확정 잔액·확정 대기·재원별 내역·조정 내역, 정산 문의. 관리자: 문의 답변(포인트 조정 포함), 별도 조정 | `GET /owner/points`, `POST /owner/points/inquiries`, `/vpp/settlement/inquiries*`, `/vpp/settlement/adjust` |
| ⑥ 경제성 검증 | 지급률, 차량당 월 포인트, 참여 유인 기준(기대 보상 — VoC로 확정) 충족 여부, 정산 가능범위(차량·일 포인트 분포), 인센티브 4종 비교(포인트·할인·수익배분·마일리지) | `GET /vpp/settlement/summary`, `/preview` |

- 화면: 웹앱 관리 > **리워드·정산** 탭(회차·경제성·인센티브 비교·문의·조정·미션 관리), 모바일 **리워드** 탭(등급·미션·정산 내역·문의), 대시보드 05의 '정산 산정'은 산정만 하고 확정은 웹앱 관리에서.
- 원칙: 방전량은 추론값, 단가는 가정값(verified=false), **실제 금전 지급 없음**(제도 마련 전, p.48).

### 10.2 서포터즈 미션·등급 (가상 리워드)
- 미션 6종: 위치정보 동의(일회), 추천 충전 시간 따르기 2회(주간), 피크 방전 10kWh·재생에너지 흡수 20kWh(확정 정산 기준)·설문 응답·주행거리 입력(월간).
- 진행도는 실제 기록에서 계산, 완료 후 [받기] → 기간당 1회 포인트 원장 적립. 등급: 새싹·브론즈(1,000)·실버(5,000)·골드(20,000)·플래티넘(50,000P).
- 관리자: 미션별 사용 여부·보상 포인트 설정, 완료 현황. 소스 `src/settlement/missions.py`, API `/owner/missions*`, `/vpp/missions*`.

### 10.3 참여주체 VoC 설문 (Y2E05)
- 관리자 설문 작성(5점 척도·객관식·주관식, 기본 문항 = **방전 수용도·보상 기대 수준**) → 모바일 홈 카드에서 1회 응답 → 문항별 집계·CSV(차량 ID 없음). `src/appadmin/survey.py`, `/admin/surveys*`, `/owner/surveys*`.

### 10.4 데이터·AI 운영
| ID | 내용 | 소스 |
|---|---|---|
| A15 | curated 저장 시 행마다 `data_source`(obd·cpo·public·participant_input·synthetic)·`generation`(measured·inferred·synthetic) 기록 | `src/pipeline/source.py` annotate_rows, ingest.py, cpo/sync.py |
| Y2E01 | 대조 지역(대전·대구·제주) ASOS 기온 수집·실증 지역 비교 | `src/pipeline/region_compare.py`, `GET /ingest/regions/compare` |
| Y2B01·Y2B02 | 현재 데이터(실측 우선) 드리프트 → 재학습 → 승격/거부 사이클, 서버 launchd 주간 배치(월 05:00) 등록·실행 확인 | `src/automation/scheduler.py` run_cycle_on_data, `scripts/run_retrain_cycle.py`, `scripts/register_retrain_launchd.sh` |
| AI 지속 업무 C1~C4 | 일 배치(매일 04:30): 데이터셋 결합(C4) → 충전수요·가용 배터리 48시간 예측 갱신(C1) → 아크릴 예측 오차(C3) → 승인 서포터즈 차량별 충전 계획·앱 추천 발행(C2). 단계별 실패 격리·예외 기록 | `src/automation/daily_batch.py`, `scripts/run_daily_batch.py`, `scripts/register_daily_launchd.sh`, `GET /kpi/daily-batch` |

### 10.5 화면·운영 개선
- 모든 화면: 데이터 요청 중 **딤 + 프로그레스 링**(`/loading.js`, 0.3초 이상 걸릴 때만, 자동 새로고침 제외). 대시보드의 상단 퍼센트 진행 막대는 제거.
- 모바일 상단 로고: 관리자와 같은 **AX 배지 + "-V2G"**.
- `/dev` 진입 화면: 서버·접속 상태, 역할별 바로 시작, 화면 카드(권한·주소 복사), 보강 기능 바로가기.
- 내부망 개발 서버: 서버 PC(macOS) `~/AIV2G-dev`, 포트 5050, launchd `kr.aiv2g.dev` 상시 구동, 간편 로그인은 사설 IP에서만.
- 버그 수정: 웹앱 관리·가입 승인 화면의 사이드바 초기화 코드 위치 오류, 미션 설정·지급 포인트 키 충돌, 빈 정산 회차 생성·확정.
- DB 마이그레이션 v11(정산 회차·문의·거래 재원 내역), v12(미션 설정·완료 이력) — 서버 기동 시 추가 전용으로 자동 적용.

## 11. 2026-11-20 화면 검증·디버깅 반영

### 11.1 데이터 일관성
- 충전기 150기를 거점별 **50·50·50**(급속 10·완속 40)으로 `config/sites.json`과 합성 데이터(`site_for_charger`)가 일치.
- 대시보드 충전기·차량 관제 목록을 고정 시연 목록에서 **서버 실데이터**로 일원화(`charger_status_list`, `vehicle_status` + 승인 정보). 충전기 상태: 충전 중 / 대기 / 장기 미사용(7일 세션 없음).
- 거점 카드: 고정 숫자 제거, `site_cards`(설정 + 현재 시각 기본 부하 프로파일 추정, 주말 0.6배).

### 11.2 개인정보·회원가입
- 가입 동의 문구에 **선택 수집 항목**(동·읍·면, 출발 시각·목표/최소 배터리·누적 주행거리) 명시 → `CONSENT_VERSION = profile-v2`(기존 동의 기록은 v1 유지).
- 가입·마이페이지: 필수(*)/선택 표시, 선택 칸 기본값 안내, 운행 정보 범위 검사(목표 20~100, 최소 10~90, 최소 < 목표).

### 11.3 모바일 앱 설치(PWA) — 사내 와이파이 HTTPS
- 휴대폰 브라우저는 http에서 서비스 워커·앱 설치를 막으므로 **HTTPS 포트 5443**(launchd `kr.aiv2g.dev-https`)을 추가. 인증서는 `scripts/make_lan_cert.sh`(openssl 내부 CA + 서버 IP 인증서 825일, `certs/` 저장소 제외).
- `/pwa/lan-ca.crt`(공개 인증서만), `/pwa/lan-https.json`, `/install`의 인증서 설치·보안 주소 안내, 앱 http 접속 시 안내 배너.
- 서버 IP 변경(192.168.27.171) — 스크립트가 현재 IP를 자동 감지.
- 서버 IP 재변경(2026-11-24, 192.168.35.92) — 내부 인증서 재발급(루트 인증서 유지), 스크립트 기본값 갱신.

### 11.4 화면 개선·버그 수정
- 로딩 링: 연속 요청 사이 0.4초 유예, 새로고침 시 다음 화면이 이어받아 한 번만 표시.
- 모의 포인트 정산 차량별 표(상위 5대)·ESS 집중 실증 그룹 표(상위 10대+지정 차량) 접기/펼치기.
- 숫자 열 머리글 오른쪽 정렬(조건별 반복 분석 '세션 수' 열이 비어 보이던 문제), 모바일 head 중복 스크립트 예외, 정산 규칙 문구 undefined, 관리자 탭↔주소 연동, 다크 모드 로고 배지.

## 12. 2026-11-20 평가의견 보완 반영 (보완의견 반영문안 대조표 v0.01, 평가의견 14개)

| 의견 | 반영 문안 요지 | 구현 | 소스·API |
|---|---|---|---|
| 1 | 공통 스키마·매핑 레지스트리, VSS·OCPP 1.6J·OCPI 2.2.1 병기, 7단계, 규칙표 행 추가로 편입 | 매핑 레지스트리·7단계·표준 형식 출력(가명) | `config/standard_mapping.json`, `src/pipeline/standards.py`, `GET /ingest/standards`, `GET /ingest/export/standard` |
| 2 | 거점 3곳 가상 마이크로그리드, 3개 시나리오, 흡수량·완화량·피크 감소·SOC 미달 | S1/S2/S3 비교표 | `src/optimizer/curtailment.py` `scenarios`, 대시보드 05 |
| 3 | 계량→원장→재원별 정산→배분→포인트, 월 1회, 민간 개인 포인트·관용 기관 리포트 | 월별 상태·월 단위 산정, 기관 리포트(CSV) | `ledger.month_status`, `ledger.institution_report`, `/vpp/settlement/months`, `/vpp/settlement/institution-report` |
| 4 | (13장으로 갱신) 4,233건(충전 1,170·운행 d + 거점 3,250, 이탈 16%, 결합 성공률 ≥94.5%), 6개 마일스톤, 주간 점검, 90% 2주 연속 대응 | 계획 수치·주간 점검표·경고·마일스톤(2개 입력) | `src/kpi.py` `DATA_PLAN`, `weekly_monitor`, `config/data_plan.json` |
| 5 | 필수성과 5 + 관리지표 7 | 성과·관리 지표판 | `src/management.py`, `GET /kpi/management` |
| 6 | 레퍼런스 4종 | 지표판에 상태 표시(벤치마크는 문서) | 〃 |
| 7 | 리스 사업자 → 개인 참여단·업무용 Fleet | 문서 사항(시스템 변경 없음) | - |
| 8 | 원천별 건수·성과 산입 여부 구분 | 지표판 원천별 표 | 〃 |
| 9 | 제주·수도권(경기) 시간 단위 전력수요·재생에너지·기상, 비교표, 타 지역 예측 적용 | 대조군 교체·지역 CSV 저장·비교표·예측 적용 | `src/pipeline/region_compare.py`, `config/comparison_regions.json`, `/ingest/regions/*` |
| 10 | 충전 시간 이동 절감 → CPO 할인 → DR → V2G 수익 배분 | 보상 3단계(1단계 할인 모의 산정) | `src/management.py` |
| 11 | 정확도 = (1−WAPE)×100 ≥ 70%, 경제적 편익 4개 산출식(한전 EV 요금·SMP) | 주 판정 교체(MAPE·R² 보조), 편익 4종 산출식 표시 | `src/model/demand_forecast.py`, `src/management.py` |
| 12 | 실차 호환 시험, 대체 데이터원(출처 표기), OBD 80% 미만 2주 시 샘플링 확대, 단말 이원화 | SOC 충전량 역산(soc_source), 80% 경고 | `src/pipeline/batch_ingest.py`, `obd_ingest.fill_soc_gaps`, `kpi.weekly_monitor` |
| 13 | 주차 8h 이상·주 3회 이상 8대, 체류·5분 SOC·방전 허용·플러그인 비중, 합산 용량·출력·동시 가용 | 기준·추가 지표·합산 가상 ESS(5분 SOC는 OBD 실측 해상도) | `src/optimizer/ess_group.py`, `/vpp/ess-group` |
| 14 | 1건 정의, CPO 3,250(서포터즈 세션 제외), 중복 제거 전·후 건수 | 집계 단계(session_funnel)·서포터즈 세션 제외 | `src/kpi.py` |

남은 확인 사항: 마일스톤 4개 날짜(보완상세 일정표), 2026-04-16 개편 한전 전기차 요금표 원문(현재 kepco_ev_example에 개편 시간대·토/일·공휴일 규칙·봄가을 주말 11~14시 50% 할인을 잠정 적용, 단가는 2023 게시값, verified=false; 개편 전 표는 kepco_ev_2023; 표준 명칭은 2026-11-21 VSS·OCPP 1.6J·OCPI 2.2.1 원문 대조 완료, verified=true), 대조 지역 공공데이터 수집, 관용차 포인트를 개인 잔액에서 뺄지(현재는 리포트에서 기관 귀속 환산값으로만 구분).

## 13. 2026-11-23 데이터셋 정의·확보량 산식·예측 대상 (문제·답변 v1.1 결정)

| 항목 | 결정 | 구현 |
|---|---|---|
| 1건 정의 | 유효 이벤트 1회(차량 운행·차량 충전·거점 충전) × [① 차량·충전기 결합 + ② AI 학습 블록] | `src/pipeline/dataset.py`, `GET /kpi/dataset`, `kpi.count_by_year(grid_df=)` |
| 확보량 산식 | N = {30×(충전 3.0+운행 d)×13×(1−16%) + 250×13} × r → 4,233건(d=0), r ≥ 94.5% | `kpi.build_data_plan`, `config/data_plan.json` plan_inputs |
| AI 학습 반영 | 거점 세션(서포터즈 제외)을 시간 단위 kW로 배분해 충전수요 라벨에 합산 | `store.site_charging_hourly`, `build_city_hourly(charger_df=)`, `datagen/scenario.py` |
| 차량·충전기 결합 | 서포터즈 충전 인증 태그(idTag) 등록 → 수신 세션 ev_id 결합(태그 원문 미보관) | `src/registry/charge_tags.py`, `/cpo/charge-tags`, `/cpo/ingest` 저장 |
| B형 차량 블록 | 충전기 측 시작·종료 SOC·차종(선택) — CPO 제공 범위 요청 | `schema.CHARGER_SESSION_SCHEMA`, `cpo/base.OPTIONAL_COLUMNS` |
| 수집 기간 | 13주 시작·종료일 명시(마일스톤 확정 후 입력) | `config/data_plan.json` collection, `kpi.weekly_monitor` |
| 예측 대상 | 주 = 시간대별 EV 충전수요, 보조 = EV 가용 배터리 자원, 전력수요 = 입력 변수, '4종' = BM 레퍼런스 | `forecast_service.ALLOWED_TARGETS` |

남은 입력값: 주간 운행 횟수 d(OBD 실측 후), 본 수집 시작·종료일(마일스톤 확정 후).

## 14. 2026-11-23 수행계획서 보완 검토본 v2 대조

기준: `docs/붙임 2. 수행계획서 보완 검토본-2026.11.23-v3-KRIS_JADE.hwpx` (v2 대조 결과를 v3에서 정정, v2는 `docs/Olds`)

| 항목 | 계획서 v2 | 시스템·문서 | 상태 |
|---|---|---|---|
| 데이터 1건 | 유효 이벤트 1회 × ① 차량·충전기 결합 + ② 동일 시각 AI 학습 데이터 | `src/pipeline/dataset.py` | 일치 |
| 확보량 산식 | (A+B)×결합 성공률, 보수적 4,233건, 94.6% → **v3에서 94.5%** | `kpi.DATA_PLAN` 4,233건, **94.5%** | 일치(v3 정정) |
| 13주 기간 | '26.12월 말 ~ '27.3월 말 | `config/data_plan.json` collection(미입력) | 날짜 확정 필요 |
| 예측 대상 | '1종' 문구 유지, EV 충전수요 염두(아크릴 협의 중) | 주 충전수요(성과 1건) + 보조 가용 배터리 | 상세설계 합의 |
| 성능 목표 | 정확도 (1−WAPE)×100 ≥ 70%, MAPE ≤20%·R² ≥0.70 병기 | `demand_forecast.py` 주 판정·보조 지표 | 일치 |
| CPO 용역 | 20,000천원×3, 12개월, REST API 일 단위, 수집 항목(SOC·차종·태그 없음), 거점 3개소 우선 | 수신 API·선택 항목(SOC·차종·id_tag) | 계약 시 추가 협의 |
| 서포터즈 | 이탈률 20~30% 가정 36~39대 모집, 예비 6~9대 | 예비 Pool(K02) | 일치 |
| 레퍼런스 | 4.2 4종 / 4.3 마 → v3에서 '4가지'(벤치마크 추가) | 지표판 4종 | 일치(v3 정정) |
| 표지 | 2차년도 '2026. 5. 1.' → v3에서 2027. 5. 1. | 2027. 5. 1. | 일치(v3 정정) |

현행화 문서: `AI-V2G_실증수집데이터_통합명세서_v1.1_261123.xlsx`, `AI-V2G_데이터수급_및_실증인프라_체크리스트_v1.1_261123.xlsx`,
`AI-V2G_외부데이터_연계_및_인프라_인수인계_명세서_v1.1_261123.xlsx`, `AI-V2G_개발자PL_인수인계_체크리스트_v1.1_261123.xlsx`,
`AI-V2G_데이터셋_산식_예측대상_문제답변_v1.3_261123.xlsx`(각 '현행화_이력'·'계획서v2_반영대조' 시트).
