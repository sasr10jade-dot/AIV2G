# AI-V2G API 레퍼런스

버전 1.0.0 · 엔드포인트 94개 · 이 문서는 `scripts/build_api_docs.py`가 코드에서 생성한다(직접 고치지 않는다).
대화형 문서는 서버의 `/docs`(Swagger)에서 볼 수 있다.

## 권한 단계

| 표시 | 의미 |
|---|---|
| 공개(로그인 불필요) | 누구나 호출 |
| 로그인 필요 | 로그인한 모든 역할 |
| 이용자 계정(승인 전 포함) · 이용자(차량이 연결된 차량 등록자) | 모바일 이용자. 본인 차량만 다룬다 |
| 관리자 이상 | 관리자·최고관리자 |
| 최고관리자 | 계정관리·감사로그 등 |

공통: 로그인은 `POST /auth/login`(세션 쿠키). 실패가 누적되면 429(Retry-After). 금액·포인트는 합성 데이터·가정값 기반 시범(모의) 결과다.

## auth

[01. 인증 및 계정 관리] 세션 로그인·로그아웃, 개발자 빠른 로그인, 계정 발급 및 권한 관리 (최고관리자/관리자/차량등록자).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/auth/login` | 공개(로그인 불필요) | 로그인 |
| POST | `/auth/logout` | 공개(로그인 불필요) | 로그아웃 |
| GET | `/auth/me` | 로그인 필요 | 현재 로그인 사용자 정보 |
| PATCH | `/auth/me/owned-vehicle` | 로그인 필요 | "내 차" 등록 — 계정과 차량 ID 연결 |
| POST | `/auth/quick-login` | 공개(로그인 불필요) | 데모용 빠른 로그인 — .env에 설정된 역할별 계정으로 즉시 로그인 |
| GET | `/auth/quick-login/available` | 공개(로그인 불필요) | 빠른 로그인이 설정된 역할 — 화면이 버튼 표시 여부를 정하는 데 사용 |
| GET | `/auth/users` | 최고관리자 | 계정 목록 (최고관리자 전용) |
| POST | `/auth/users` | 최고관리자 | 계정 생성 (최고관리자 전용) |
| DELETE | `/auth/users/{username}` | 최고관리자 | 계정 삭제 (최고관리자 전용) |

## dashboard

[02. 운영 대시보드 관제] 핵심 성과 요약, 실증 인프라 현황, 차트 데이터, 이익공유 시뮬레이션, 결과 자료(엑셀) 다운로드.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/dashboard/charging-efficiency` | 관리자 이상(관리자·최고관리자) | 차량(OBD) 충전량 vs 충전기 계측 충전량 비교 — 충전 효율(손실률) 추정 |
| GET | `/dashboard/data` | 관리자 이상(관리자·최고관리자) | 대시보드 전체 데이터 — KPI/PoC비교/예측적합도/Closed-loop |
| GET | `/dashboard/report.xlsx` | 관리자 이상(관리자·최고관리자) | 결과 자료 엑셀 — 데이터 확보 현황·일자별 수집·실증 인프라·데이터 품질·효용성 검증·이익공유 기초자료·모델링 예외 기록 |

## owner

[03. 서포터즈 모바일웹 (/app)] 차량 등록자 전용 모바일 앱 — 배터리 모니터링, 충전시간 안내, AI 추천 배너, 거주지 맞춤 추천 충전소, 혼잡 회피 알림함, 충전 리포트.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/owner/account/withdraw` | 이용자 계정(승인 전 포함) | 회원 탈퇴 — 개인정보 삭제(되돌릴 수 없음) |
| GET | `/owner/app-content` | 이용자(차량이 연결된 차량 등록자) | 관리자가 바꾼 앱 문구(없으면 앱 기본 문구를 쓴다) |
| GET | `/owner/charging-guide` | 이용자(차량이 연결된 차량 등록자) | 내 차량의 충전(·방전) 추천 시간과 예상 절감 — 가정값 기준 모의 계산 |
| GET | `/owner/consent/v2g` | 이용자(차량이 연결된 차량 등록자) | 내 방전(V2G) 참여 상태 — 기본은 꺼짐(명시적으로 켜야 방전을 안내) |
| PUT | `/owner/consent/v2g` | 이용자(차량이 연결된 차량 등록자) | 방전 참여 켜기/즉시 중지 — 본인 차량만, 변경 이력과 접속기록을 남긴다 |
| GET | `/owner/demand-forecast` | 이용자(차량이 연결된 차량 등록자) | 충전 수요·V2G 가용량 48시간 예측 요약(시뮬레이션 차량 전체 기준) |
| POST | `/owner/guide/{guide_id}/respond` | 이용자(차량이 연결된 차량 등록자) | 내 충전 추천에 응답 — accepted(추천대로 할게요) / declined(나중에). 본인 추천만 |
| GET | `/owner/history` | 이용자 계정(승인 전 포함) | 내 회원정보 변경 이력 — 처리자는 본인/관리자로만, 접속 IP는 제외 |
| GET | `/owner/me` | 이용자(차량이 연결된 차량 등록자) | 내 계정·연결 차량 정보 |
| GET | `/owner/my-data` | 이용자 계정(승인 전 포함) | 내 개인정보 열람 — 보유 중인 정보와 동의·이력 현황 |
| GET | `/owner/notices` | 이용자(차량이 연결된 차량 등록자) | 공지사항(공개된 것만, 고정 먼저) |
| GET | `/owner/notifications` | 이용자(차량이 연결된 차량 등록자) | 내 알림함 — 충전시간 안내 및 혼잡 회피 알림 목록 |
| POST | `/owner/notifications/read-all` | 이용자(차량이 연결된 차량 등록자) | 알림 전체 읽음 처리 |
| POST | `/owner/notifications/{notif_id}/read` | 이용자(차량이 연결된 차량 등록자) | 알림 개별 읽음 처리 |
| POST | `/owner/odometer` | 이용자(차량이 연결된 차량 등록자) | 누적 주행거리 기록(계기판 값) — 주간 주행·전비·1km당 요금 계산에 쓴다. 직전 값보다 작으면 거부 |
| POST | `/owner/password` | 이용자 계정(승인 전 포함) | 비밀번호 변경 |
| PATCH | `/owner/profile` | 이용자 계정(승인 전 포함) | 내 정보 정정 — 차종·직업군·거주지·운행 입력(출발 시각·목표/최소 배터리·누적 주행거리). 번호판은 관리자 재심사 |
| GET | `/owner/registration` | 이용자 계정(승인 전 포함) | 내 차량 등록 상태(승인 대기·승인·반려) |
| POST | `/owner/registration` | 이용자 계정(승인 전 포함) | 반려된 뒤 차량 정보를 고쳐 다시 신청 |
| GET | `/owner/report` | 이용자(차량이 연결된 차량 등록자) | 내 충전 리포트 — 최근 4주 충전·주행 지표, 다른 실증 차량과 비교, 최소 보장 배터리 제안 |

## signup

[04. 서포터즈 회원가입] 시·도 / 시·군·구 / 동·읍·면 거주지 선택 및 차량·페르소나 정보 등록 (공개).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/signup` | 공개(로그인 불필요) | 회원가입 — 승인 대기로 등록하고 로그인 상태로 만든다 |
| GET | `/signup/options` | 공개(로그인 불필요) | 가입 선택지(차종·직업군·시도)와 동의 문구 |

## registrations

[05. 차량 등록 심사 및 가입 승인] 관리자 승인 화면(/approvals) — 가입 신청 검토, 승인·반려, 실차 EV-ID 매핑, 예비 참여자 관리.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/registrations` | 관리자 이상(관리자·최고관리자) | 차량 등록 신청 목록(번호판 마스킹) |
| GET | `/registrations/member/{username}/history` | 관리자 이상(관리자·최고관리자) | 회원 변경 이력(관리자용 — 처리자·접속 IP 포함, 조회 사실도 기록) |
| POST | `/registrations/{registration_id}/approve` | 관리자 이상(관리자·최고관리자) | 신청 승인 — 계정에 차량 연결 |
| POST | `/registrations/{registration_id}/reject` | 관리자 이상(관리자·최고관리자) | 신청 반려 |
| POST | `/registrations/{registration_id}/reserve` | 관리자 이상(관리자·최고관리자) | 예비 참여자로 두기/해제 — 활동 차량이 빠지면 예비에서 승인해 대체 |
| POST | `/registrations/{registration_id}/reveal-plate` | 관리자 이상(관리자·최고관리자) | 전체 번호판 열람 — 사유 필수, 접속기록에 남음 |

## cpo

[06. CPO 충전데이터 연계] 충전서비스 사업자(CPO 3사: 나이스인프라·플러그링크·이브이시스 150기) 데이터 연계 명세 및 일 단위 세션 수신 REST API.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/cpo/ingest/{cpo_id}` | 공개(로그인 불필요) | CPO 일 단위 충전 세션 수신 API |
| GET | `/cpo/specs` | 관리자 이상(관리자·최고관리자) | CPO 데이터 및 API 명세서 (계획서 13장 산출물) |
| GET | `/cpo/status` | 관리자 이상(관리자·최고관리자) | CPO 3사 연계 및 수집 현황 |

## admin

[07. 웹앱 관리 (/admin)] 서포터즈 앱 공지사항 CRUD, 다국어 앱 문구, AI 모델링 예외상황 기록 관리.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/admin/app-content` | 관리자 이상(관리자·최고관리자) | 수정할 수 있는 앱 문구와 현재 적용값 |
| DELETE | `/admin/app-content/{key}` | 관리자 이상(관리자·최고관리자) | 앱 문구를 기본값으로 되돌리기 |
| PUT | `/admin/app-content/{key}` | 관리자 이상(관리자·최고관리자) | 앱 문구 수정(한국어·영어) |
| GET | `/admin/modeling-issues` | 관리자 이상(관리자·최고관리자) | 모델링 예외 기록 — 예측·최적화·수집 단계의 오류·예외상황(계획서 4.1 다) |
| POST | `/admin/modeling-issues/{issue_id}/resolve` | 관리자 이상(관리자·최고관리자) | 모델링 예외 조치 완료 — 원인·개선 조치를 적어 닫는다 |
| GET | `/admin/notices` | 관리자 이상(관리자·최고관리자) | 공지사항 목록(비공개 포함) |
| POST | `/admin/notices` | 관리자 이상(관리자·최고관리자) | 공지사항 등록 |
| DELETE | `/admin/notices/{notice_id}` | 관리자 이상(관리자·최고관리자) | 공지사항 삭제 |
| PUT | `/admin/notices/{notice_id}` | 관리자 이상(관리자·최고관리자) | 공지사항 수정(공개·고정 포함) |

## models

[08. AI 예측모델 레지스트리] AI 기반 최적 V2G 예측모델 설명서(Model Card 공식 산출물) 및 모델 메타데이터 조회.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/models` | 관리자 이상(관리자·최고관리자) | 등록된 예측 타깃 전체 목록·활성 버전 |
| GET | `/models/card` | 관리자 이상(관리자·최고관리자) | AI 기반 최적 V2G 예측모델 모델 카드 (공식 산출물) |
| GET | `/models/{target_col}` | 관리자 이상(관리자·최고관리자) | 특정 예측 타깃의 모델 상세 정보 |

## forecast

[09. AI 충전수요 전망] 48시간 일별/주별/월별 미래 충전수요 시계열 재귀 예측 서빙 (㈜아크릴 예측 연계).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/forecast/meta/granularities` | 관리자 이상(관리자·최고관리자) | 지원하는 조회 단위(granularity) 목록 |
| GET | `/forecast/plugin-pattern/{ev_id}` | 관리자 이상(관리자·최고관리자) | 차량별 입출차(플러그인/아웃) 패턴 예측 — 요일·시간대 이력 기반 |
| GET | `/forecast/{target_col}` | 관리자 이상(관리자·최고관리자) | 최근 구간 실측 vs 예측 (모델 적합도 확인용) |
| GET | `/forecast/{target_col}/horizon` | 관리자 이상(관리자·최고관리자) | 일별(시간대별)/주별/월별 미래 전망 — ㈜아크릴 예측 우선, 없으면 기준 모델 재귀 예측 |
| GET | `/forecast/{target_col}/horizon/report` | 관리자 이상(관리자·최고관리자) | 일/주/월 전망 리포트 다운로드 (Word .docx 또는 PDF) |

## optimize

[10. AI 최적 V2G 의사결정] 일반 충전(무제어) vs AI-V2G 최적화 피크 저감 및 충전요금 절감 효과 비교 (HiGHS LP).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/optimize/poc-compare` | 관리자 이상(관리자·최고관리자) | 일반충전 vs AI-V2G 최적화 피크저감 비교 |
| GET | `/optimize/price-options` | 관리자 이상(관리자·최고관리자) | 선택 가능한 충전 요금표·방전 보상 시나리오 |

## acryl

[11. ㈜아크릴 AI 예측모델 연계] 참여기관 ㈜아크릴 간 학습용 정제 데이터 제공(가명화) 및 예측 추론 결과 배치 수신.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/acryl/exports` | 관리자 이상(관리자·최고관리자) | 학습 데이터 제공 이력 |
| POST | `/acryl/exports` | 관리자 이상(관리자·최고관리자) | 학습용 정제 데이터 제공 묶음 생성(가명처리·SHA-256 manifest) |
| POST | `/acryl/exports/{export_id}/destruction` | 관리자 이상(관리자·최고관리자) | 제공 묶음 파기 확인 기록(참여기관 파기 확인서 수령 시) |
| GET | `/acryl/forecasts/status` | 관리자 이상(관리자·최고관리자) | 예측 결과 수신 현황(대상별 마지막 배치·모델 버전) |
| GET | `/acryl/forecasts/{target}/evaluation` | 관리자 이상(관리자·최고관리자) | 수신 예측 vs 실측 오차(MAE·예측률) |
| GET | `/partner/acryl/exports` | 참여기관 연계 토큰(㈜아크릴) | [참여기관] 제공 묶음 목록 |
| POST | `/partner/acryl/exports/{export_id}/destruction` | 참여기관 연계 토큰(㈜아크릴) | [참여기관] 제공 묶음 파기 완료 알림 |
| GET | `/partner/acryl/exports/{export_id}/{name}` | 참여기관 연계 토큰(㈜아크릴) | [참여기관] 제공 묶음 파일 내려받기(manifest.json·CSV) |
| POST | `/partner/acryl/forecasts` | 참여기관 연계 토큰(㈜아크릴) | [참여기관] 예측 추론 결과 배치 수신(일 배치) |

## privacy

[12. 개인정보 보호 및 감사로그] 개인정보 가명화(HMAC-SHA256), 동의 이력 관리, 감사로그 해시체인 무결성 검증.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/privacy/audit` | 최고관리자 | 최근 감사로그와 체인 무결성 검증 |
| GET | `/privacy/consents` | 최고관리자 | 동의 이력 조회 |
| POST | `/privacy/consents/grant` | 최고관리자 | 동의 등록 |
| POST | `/privacy/consents/withdraw` | 최고관리자 | 동의 철회 |

## ingest

[13. 데이터 수급 파이프라인] 서포터즈 실차 OBD2 배치 수급 및 전력거래소(KPX)·기상청 공공데이터 동기화.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/ingest/batch/promote` | 최고관리자 | shadow로 검증한 staging 결과를 운영 데이터에 반영(최고관리자) |
| POST | `/ingest/batch/run` | 관리자 이상(관리자·최고관리자) | OBD 일일 배치 즉시 실행(백그라운드) |
| GET | `/ingest/batch/status` | 관리자 이상(관리자·최고관리자) | 최근 OBD 일일 배치 결과(도착 대조·격리·모드) |
| POST | `/ingest/generate` | 관리자 이상(관리자·최고관리자) | 합성 데모 데이터 생성 |
| POST | `/ingest/obd-emulator/run` | 관리자 이상(관리자·최고관리자) | 가상 실증차량 OBD 배치를 합성해 실제 파이프라인으로 흘려보냄 |
| GET | `/ingest/public-apis/status` | 관리자 이상(관리자·최고관리자) | 공개 데이터(기상·공휴일) 연동 준비 상태 — 키·명세 여부만, 값은 노출하지 않음 |
| POST | `/ingest/public-csv/{kind}` | 관리자 이상(관리자·최고관리자) | 공개 데이터 CSV 반영 — 시간별 전력수요(power-demand)·재생에너지 발전량(renewable) |
| POST | `/ingest/run` | 관리자 이상(관리자·최고관리자) | raw → curated 파이프라인 실행 |
| GET | `/ingest/status` | 관리자 이상(관리자·최고관리자) | 현재 데이터 소스 모드 확인 (데모/실증) |
| POST | `/ingest/sync-kpx-smp` | 관리자 이상(관리자·최고관리자) | 한국전력거래소(KPX) 실시간 SMP를 grid_signal.price_krw_per_kwh에 반영 |
| POST | `/ingest/sync-weather-holiday` | 관리자 이상(관리자·최고관리자) | 기상청 시간별 기온·한국천문연구원 공휴일을 grid_signal에 반영 |
| DELETE | `/ingest/upload` | 관리자 이상(관리자·최고관리자) | 업로드된 데이터 삭제 → 데모 모드로 복귀 |
| POST | `/ingest/upload` | 관리자 이상(관리자·최고관리자) | 실증 데이터 업로드 (품질검증 통과 시 반영) |
| POST | `/ingest/upload-obd` | 관리자 이상(관리자·최고관리자) | 실증 차량 OBD 배치 업로드 (원본 1Hz 로그 → 시간당 자동 집계) |

## advisory

[14. AI 운영 지표] AI 추천 발행, 차주 수용률 및 안전 검증 통과율 감사로그 집계.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/advisory` | 관리자 이상(관리자·최고관리자) | 추천 목록과 수용률 |
| GET | `/advisory/ai-ops-kpi` | 관리자 이상(관리자·최고관리자) | AI 운영 지표 — 안전 검증 통과율·방전 참여 철회율(감사로그·동의 이력 집계) |
| POST | `/advisory/issue` | 관리자 이상(관리자·최고관리자) | 추천 계획 생성·발행 |
| POST | `/advisory/{advisory_id}/respond` | 관리자 이상(관리자·최고관리자) | 차주 응답 기록(accepted/declined/expired) |

## health

[15. 서버 헬스체크] 시스템 및 데이터베이스 가동 상태 확인 (공개).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/health` | 공개(로그인 불필요) | 헬스체크 |
