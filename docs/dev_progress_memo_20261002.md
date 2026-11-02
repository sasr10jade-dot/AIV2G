# 개발 진행 메모 — 2026-10-02

갭분석·설계안(`Report/[설계안]_갭반영_및_EVBot_Gemini_v1.0_261002`)의 작업 패키지(WP) 구현 현황과 남은 항목.
테스트: 276개 통과 (`python -m pytest -q`, 약 2분).

## 구현 완료

| WP | 내용 | 위치 |
|---|---|---|
| OBD 배치 | 새벽 도착 CSV → 정제 → shadow/live 반영, 멱등·격리·백업 | `src/pipeline/batch_ingest.py`, `scripts/run_obd_batch.py`, `docs/obd_batch_runbook.md` |
| OBD 수집 | TOAST Object Storage에서 읽기 전용 다운로드(크기·MD5 검증, 등록 차량만) | `src/pipeline/toast_fetch.py`, `run_obd_batch.py --fetch` |
| WP1 | 규모 30대/150기/21주, KPI 산식(A 세션 + B 차량-일 + 방전) | `src/kpi.py`, `src/datagen/config.py` |
| WP2 | 스키마 마이그레이션 v1~v3(충전기 마스터, 방전 이벤트, 어드바이저리, 실험 그룹) | `src/pipeline/migrations.py` |
| WP3/4 | CPO 어댑터(CSV/모의), 세션 동기화, 계시별 요금표 | `src/cpo/`, `src/grid/tariff.py`, `config/cpo_sources.json`, `config/tariffs.json` |
| WP5 | 가명화, 동의 관리, 해시체인 감사로그 | `src/privacy/`, `/privacy/*` |
| WP8/9 | 열화비용·효율·방전 횟수 상한, 사용자 입력(출발·SOC), WAPE, 수익 백테스트 | `src/optimizer/economics.py`, `poc_compare.py`, `decision.py`, `src/model/backtest.py` |
| WP10 | 어드바이저리 발행·응답·수용률 | `src/advisory/`, `/advisory/*` |
| WP11 | 실험군 배정, 인센티브 정산, 효과 분석(부트스트랩) | `src/experiment/`, `/experiment/*`, `config/incentive_policy.json` |
| WP12 | OCPP 세션 종료 시 방전 이벤트 기록(기본 TEST) | `src/events/discharge.py`, `central_system.py` |
| WP13 | KPI 대시보드(구성·외부 연계·목표·가정값 표기) | `src/api/static/dashboard.html` |
| WP14 | OCPP 인증(Basic)·접속 한도·프레임 보호, 보안 가이드 | `src/ocpp/security.py`, `docs/ocpp_security.md` |
| WP15 | EV Bot LLM 게이트웨이(마스킹·한도·캐시·재시도·숫자 검증·템플릿), 기본 모델 `gemini-3.5-flash-lite` | `src/copilot/gateway.py`, `service.py` |

## 가정값 — 확정 전까지 결과를 '가정'으로 표기해야 함 (`verified=false`)
- `config/economics.json`: 효율 0.92, 열화 약 47원/kWh(팩 14만원/kWh ÷ 3,000사이클), 일 방전 2회
- `config/tariffs.json`: 요금표 단가는 구조 검증용 예시
- `config/incentive_policy.json`: 지자체 협의 전 예시
- `config/obd_vehicles.json`: 05하 3대의 차종·구역은 43오3450과 동일하게 임시 입력

## 운영 환경 변수
| 변수 | 용도 | 기본 |
|---|---|---|
| `GEMINI_API_KEY`, `GEMINI_MODEL` | EV Bot 키·모델 | 모델 `gemini-3.5-flash-lite` |
| `LLM_PROFILE` | `demo` / `pilot` / `local`(외부 호출 없음) | demo |
| `TOAST_TENANT_ID/USERNAME/PASSWORD/ENDPOINT/BUCKET` | OBD 버킷 접속 | 없음(.env에만 보관) |
| `OBD_BATCH_MODE`, `OBD_BATCH_LIVE_EVS` | shadow/live, live 허용 차량 | shadow |
| `CONSENT_ENFORCE=1` | 수집 동의 없는 차량 보류 | 꺼짐 |
| `OCPP_AUTH_REQUIRED=1`, `OCPP_MAX_CONNECTIONS` | OCPP 인증·접속 한도 | 꺼짐 / 400 |
| `DISCHARGE_RECORD_KIND` | OCPP 방전 기록 종류(V2G 실운영 시) | TEST |
| `PSEUDO_KEY` | 가명화 키 | `data/.pseudo_key` 자동 생성 |
| `KPX_SERVICE_KEY` | SMP 실데이터 | 없음 |

## 미결·확인 필요
1. **EV-TORRES-04(05하7584)**: 30일 전부 속도·SOC 신호 없음(파일 4.4MB, 정상 평균 27.7MB) → 업체 확인.
2. **TOAST 계정**: 비밀번호가 대화에 노출됐으므로 교체, 가능하면 읽기 전용 계정 별도 발급.
3. **live 전환**: shadow 30일 검증 완료, 반영 전 며칠 더 관찰 후 1대부터 `OBD_BATCH_LIVE_EVS`로.
4. **충전사업자**: 실명·연계 방식 확정 전(현재 에뮬레이터). 샘플 CSV/API 명세 필요.
5. **KPX 서비스 키**, 확정 요금표, 열화·효율 근거값.
6. **OCPP 운영 전**: TLS(wss) 종단, 8000 포트 차단, 사업자 충전기의 Basic 인증·TLS 지원 확인.
7. 외부 공개(터널링) 시 위 6번의 인증을 먼저 켤 것 — 개발 기본값은 OCPP 무인증이다.
