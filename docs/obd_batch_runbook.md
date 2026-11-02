# OBD 일일 배치 운영 런북

업체가 전날(00:00~24:00 KST) 차량별 OBD 원본 CSV를 다음날 새벽 02~03시에 보낸다.
구현: `src/pipeline/batch_ingest.py`, 실행: `scripts/run_obd_batch.py`, 점검 API: `/ingest/batch/*`

## 폴더 (기본 `data/batch/`)
| 폴더 | 내용 |
|---|---|
| incoming/ | 업체가 전송하는 위치. 전송 중 파일은 `.part` 등 임시 이름으로 받거나 완료 후 개명(마지막 수정 후 120초 경과 시 처리) |
| raw/YYYYMMDD/ | 원본 무변경 보관(GPS 포함 → 접근 제한, 90일 후 `--purge-days 90`으로 삭제) |
| staging/YYYYMMDD/<ev_id>.csv | 시간당 집계 결과(위치·번호판 없음) |
| quarantine/<사유>/ | 격리 파일 + 사유 JSON. 사유: bad_filename, unknown_vehicle, unparsable(CAN 장애 등), no_valid_hours, quality_gate |
| reports/ | 실행 리포트(latest.json). 도착 대조표(예상 대수 vs 도착, 미도착 ev_id) 포함 |
| backups/ | 운영 EV 데이터 반영 직전 백업(최근 14개) |

## 수집 경로 — TOAST Object Storage (확인됨)
버킷 `<버킷>/YYYY-MM-DD/YYYYMMDD-<번호판>-<차명>.csv` 구조. 업체가 **한국시간 04:12~04:19경**(2026-10-01 실측)
전날치를 올린다. `--fetch`는 등록된 차량(`config/obd_vehicles.json`)의 파일만 읽기 전용으로 받아
incoming/에 놓고(크기·MD5 검증 후 이름 변경) 바로 같은 배치로 처리한다.
접속 정보는 `.env`의 `TOAST_*` 5개 값(`.env.example` 참고) — 저장소·로그·리포트에 남기지 않는다.

## 스케줄 (예: 매일)
- 04:40 `python scripts/run_obd_batch.py --fetch` — 수집 + 본 처리 (업체 업로드가 04:20 전후에 끝나는 것을 확인함)
- 06:00 `python scripts/run_obd_batch.py --fetch` — 늦게 올라온 파일·미도착 재확인 (`--fetch-days 2`가 기본이라 전날도 함께 확인)
- 종료코드 1이면 격리/오류/미도착/다운로드 실패가 있다는 뜻 → 알림 대상
- Windows 작업 스케줄러 또는 cron으로 등록한다. 서버 시간대가 KST가 아니면 `default_target_date`(전날 KST) 기준임을 유의.
- 과거 데이터 백필: `--fetch --fetch-days 30` (30일·4대 약 2.2GB, shadow 처리). 전체 버킷은 약 50GB.
- 격리 사유가 `unparsable`(속도·SOC 신호 없음)이고 파일이 유난히 작으면(정상 평균 27MB 대비 4MB대) 차량 장치 이상이다 → 업체 확인. 2026-09 한 달간 EV-TORRES-04(05하7584)가 이 상태였다.

## 도입 단계 (기본값은 shadow = 운영 데이터에 쓰지 않음)
1. 리플레이: `python scripts/run_obd_batch.py --replay OBDData --limit 20`
2. 그림자 운영 2~3일: `OBD_BATCH_MODE=shadow`(기본). 리포트·격리 사유 확인
3. 1대 반영 1주: `OBD_BATCH_MODE=live OBD_BATCH_LIVE_EVS=EV-TORRES-01`
4. 전체 반영: `OBD_BATCH_MODE=live` (또는 `--promote`로 shadow 결과를 일괄 반영)
5. 11/1 수집 개시

문제 시: `OBD_BATCH_MODE=shadow`로 되돌리고 `backups/`의 직전 `ev_telemetry_*.csv`를 `data/raw/ev_telemetry.csv`로 복원.

## 차량 추가
`config/obd_vehicles.json`에 번호판 → {ev_id, vehicle_model, zone, active}를 추가한다.
(토레스 EVX의 차종 매핑 `차종B(SUV)`는 업체 확인 후 확정 필요)

## 처리 규칙 요약
- 멱등: (SHA-256 + 파일명) 동일하면 재처리하지 않음. 같은 날짜 수정본(내용 다름)은 (ev_id, timestamp)로 덮어씀
- SOC가 없는 시간(슬립·CAN 공백)은 반영에서 제외하고 건수를 리포트(hours_dropped_no_soc)에 기록
- 반영 대상 행에는 결측 1% 미만·중복 0·범위위반 0 게이트 적용, 실패 시 격리
- 운영 파일은 임시 파일에 쓴 뒤 교체(원자적)
