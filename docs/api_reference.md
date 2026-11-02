# AI-V2G API 레퍼런스

버전 0.4.0 · 엔드포인트 107개 · 이 문서는 `scripts/build_api_docs.py`가 코드에서 생성한다(직접 고치지 않는다).
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

로그인·로그아웃·계정관리 — 최고관리자(super_admin)/관리자(admin)/차량 등록자(owner) 권한.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/auth/login` | 공개(로그인 불필요) | 로그인 |
| POST | `/auth/logout` | 공개(로그인 불필요) | 로그아웃 |
| GET | `/auth/me` | 로그인 필요 | 현재 로그인 사용자 정보 |
| PATCH | `/auth/me/owned-vehicle` | 로그인 필요 | "내 차" 등록 — EV Bot 개인화(차량 ID 자동입력)에 사용 |
| POST | `/auth/quick-login` | 공개(로그인 불필요) | 데모용 빠른 로그인 — .env에 설정된 역할별 계정으로 즉시 로그인 |
| GET | `/auth/quick-login/available` | 공개(로그인 불필요) | 빠른 로그인이 설정된 역할 — 화면이 버튼 표시 여부를 정하는 데 사용 |
| GET | `/auth/users` | 최고관리자 | 계정 목록 (최고관리자 전용) |
| POST | `/auth/users` | 최고관리자 | 계정 생성 (최고관리자 전용) |
| DELETE | `/auth/users/{username}` | 최고관리자 | 계정 삭제 (최고관리자 전용) |

## owner

사용자 모바일웹(`/app`) — 차량 등록자(owner)가 본인 차량만 조회·설정. 알림·공지, 충전 시간 안내(안전 검증 포함), 수요 예측, 방전 참여·시범 포인트 적립 동의, 미션·포인트, 상품권 교환·쿠폰함, EV Bot(KR/EN), 내 정보(열람·정정·탈퇴).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/owner/account/withdraw` | 이용자 계정(승인 전 포함) | 회원 탈퇴 — 개인정보 삭제(되돌릴 수 없음) |
| GET | `/owner/app-content` | 이용자(차량이 연결된 차량 등록자) | 관리자가 바꾼 앱 문구(없으면 앱 기본 문구를 쓴다) |
| POST | `/owner/ask` | 이용자(차량이 연결된 차량 등록자) | 이용자용 EV Bot — 앱 사용법·용어·개인정보 안내(본인 차량 정보만 근거) |
| GET | `/owner/charging-guide` | 이용자(차량이 연결된 차량 등록자) | 내 차량의 충전(·방전) 추천 시간과 예상 절감 — 가정값 기준 모의 계산 |
| GET | `/owner/consent` | 이용자(차량이 연결된 차량 등록자) | 내 알림 수신 동의 상태 |
| GET | `/owner/consent/incentive` | 이용자(차량이 연결된 차량 등록자) | 내 시범 포인트 적립 동의 상태 — 동의해야 미션 포인트가 쌓인다 |
| PUT | `/owner/consent/incentive` | 이용자(차량이 연결된 차량 등록자) | 시범 포인트 적립 동의/철회 — 철회하면 이후 적립이 멈춘다(쌓은 포인트는 유지) |
| PUT | `/owner/consent/notify` | 이용자(차량이 연결된 차량 등록자) | 알림 수신 동의/철회 — 본인 차량만 |
| GET | `/owner/consent/v2g` | 이용자(차량이 연결된 차량 등록자) | 내 방전(V2G) 참여 상태 — 기본은 꺼짐(명시적으로 켜야 방전을 안내) |
| PUT | `/owner/consent/v2g` | 이용자(차량이 연결된 차량 등록자) | 방전 참여 켜기/즉시 중지 — 본인 차량만, 변경 이력과 접속기록을 남긴다 |
| GET | `/owner/coupons` | 이용자(차량이 연결된 차량 등록자) | 내 교환 내역·쿠폰함 — 발급된 코드는 본인에게만 보인다 |
| GET | `/owner/demand-forecast` | 이용자(차량이 연결된 차량 등록자) | 충전 수요·V2G 가용량 48시간 예측 요약(시뮬레이션 차량 전체 기준) |
| POST | `/owner/guide/{guide_id}/respond` | 이용자(차량이 연결된 차량 등록자) | 내 충전 추천에 응답 — accepted(추천대로 할게요) / declined(나중에). 본인 추천만 |
| GET | `/owner/history` | 이용자 계정(승인 전 포함) | 내 회원정보 변경 이력 — 처리자는 본인/관리자로만, 접속 IP는 제외 |
| GET | `/owner/me` | 이용자(차량이 연결된 차량 등록자) | 내 계정·연결 차량 정보 |
| GET | `/owner/missions` | 이용자(차량이 연결된 차량 등록자) | 내 미션 진행·시범 포인트 — 달성은 서버가 판정하고 동의한 경우에만 적립 |
| GET | `/owner/my-data` | 이용자 계정(승인 전 포함) | 내 개인정보 열람 — 보유 중인 정보와 동의·이력 현황 |
| GET | `/owner/notices` | 이용자(차량이 연결된 차량 등록자) | 공지사항(공개된 것만, 고정 먼저) |
| GET | `/owner/notifications` | 이용자(차량이 연결된 차량 등록자) | 내 알림함 |
| POST | `/owner/notifications/read-all` | 이용자(차량이 연결된 차량 등록자) | 알림 모두 읽음 |
| POST | `/owner/notifications/{notification_id}/read` | 이용자(차량이 연결된 차량 등록자) | 알림 읽음 처리 |
| POST | `/owner/password` | 이용자 계정(승인 전 포함) | 비밀번호 변경 |
| GET | `/owner/points` | 이용자(차량이 연결된 차량 등록자) | 내 시범 포인트 적립 내역 |
| POST | `/owner/points/redeem` | 이용자(차량이 연결된 차량 등록자) | 상품권 교환 신청 — 기준 포인트를 차감하고 관리자가 코드를 등록하면 쿠폰함에 나타난다 |
| PATCH | `/owner/profile` | 이용자 계정(승인 전 포함) | 내 정보 정정 — 차종·직업군·거주지(번호판은 관리자 재심사) |
| GET | `/owner/registration` | 이용자 계정(승인 전 포함) | 내 차량 등록 상태(승인 대기·승인·반려) |
| POST | `/owner/registration` | 이용자 계정(승인 전 포함) | 반려된 뒤 차량 정보를 고쳐 다시 신청 |

## admin

웹앱 관리(관리자, `/admin`) — 공지사항(영어 포함), 앱 문구(미션 안내) 편집, 상품권 교환 신청 처리·코드 등록(암호화 저장, 반려 시 포인트 환급).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/admin/app-content` | 관리자 이상(관리자·최고관리자) | 수정할 수 있는 앱 문구와 현재 적용값 |
| DELETE | `/admin/app-content/{key}` | 관리자 이상(관리자·최고관리자) | 앱 문구를 기본값으로 되돌리기 |
| PUT | `/admin/app-content/{key}` | 관리자 이상(관리자·최고관리자) | 앱 문구 수정(한국어·영어) |
| GET | `/admin/notices` | 관리자 이상(관리자·최고관리자) | 공지사항 목록(비공개 포함) |
| POST | `/admin/notices` | 관리자 이상(관리자·최고관리자) | 공지사항 등록 |
| DELETE | `/admin/notices/{notice_id}` | 관리자 이상(관리자·최고관리자) | 공지사항 삭제 |
| PUT | `/admin/notices/{notice_id}` | 관리자 이상(관리자·최고관리자) | 공지사항 수정(공개·고정 포함) |
| GET | `/admin/redemptions` | 관리자 이상(관리자·최고관리자) | 상품권 교환 신청 목록(코드 값은 싣지 않음) |
| POST | `/admin/redemptions/{rid}/fulfill` | 관리자 이상(관리자·최고관리자) | 상품권 코드 등록 — 이용자 쿠폰함으로 전달 |
| POST | `/admin/redemptions/{rid}/reject` | 관리자 이상(관리자·최고관리자) | 교환 신청 반려 — 차감한 포인트를 돌려준다 |

## advisory

추천 계획 발행·수용률(관리자) 및 AI 운영 지표 — 안전 검증 통과율·방전 참여 철회율·EV Bot 호출 집계.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/advisory` | 관리자 이상(관리자·최고관리자) | 추천 목록과 수용률 |
| GET | `/advisory/ai-ops-kpi` | 관리자 이상(관리자·최고관리자) | AI 운영 지표 — 안전 검증 통과율·방전 참여 철회율·EV Bot 호출(감사로그·동의 이력 집계) |
| POST | `/advisory/issue` | 관리자 이상(관리자·최고관리자) | 추천 계획 생성·발행 |
| POST | `/advisory/{advisory_id}/respond` | 관리자 이상(관리자·최고관리자) | 차주 응답 기록(accepted/declined/expired) |

## experiment

실험군/대조군 배정과 인센티브 정산 미리보기(동의한 차량만).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/experiment/assign` | 최고관리자 | 실험군/대조군 배정(기존 배정은 유지) |
| GET | `/experiment/incentive` | 최고관리자 | 인센티브 정산 미리보기(동의한 차량만, TEST 제외) |

## privacy

개인정보 보호 — 동의 이력, 해시체인 감사로그와 무결성 검증(최고관리자).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/privacy/audit` | 최고관리자 | 최근 감사로그와 체인 무결성 검증 |
| GET | `/privacy/consents` | 최고관리자 | 동의 이력 조회 |
| POST | `/privacy/consents/grant` | 최고관리자 | 동의 등록 |
| POST | `/privacy/consents/withdraw` | 최고관리자 | 동의 철회 |

## health

서버 상태 확인(공개).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/health` | 공개(로그인 불필요) | 헬스체크 |

## notify

알림 발송(관리자) — 수신 동의한 차량 등록자의 알림함으로 전달.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/notify/send` | 관리자 이상(관리자·최고관리자) | 차량 알림함으로 알림 발송(수신 동의 차량만) |

## solar

태양광 연계 충전소 에뮬레이션(관리자) — 나주 태양광 사이트 모의 운영, 즉시 충전 vs 태양광 우선 충전 비교.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/solar/data-sources` | 관리자 이상(관리자·최고관리자) | 공공데이터 연동 현황 — 키·명세 준비 여부(값은 노출하지 않음) |
| POST | `/solar/emulate` | 관리자 이상(관리자·최고관리자) | 태양광 연계 충전소 모의 운영 — 즉시 충전 vs 태양광 우선 최적 충전 |
| GET | `/solar/pv-day` | 관리자 이상(관리자·최고관리자) | 하루 태양광 발전 곡선 — 맑은 날·평균 날씨·시드 날씨 |
| GET | `/solar/sites` | 관리자 이상(관리자·최고관리자) | 에뮬레이션 가능한 태양광 연계 충전소(가정값) |

## signup

모바일 회원가입 — 계정과 차량·페르소나 정보를 승인 대기로 등록(공개).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/signup` | 공개(로그인 불필요) | 회원가입 — 승인 대기로 등록하고 로그인 상태로 만든다 |
| GET | `/signup/options` | 공개(로그인 불필요) | 가입 선택지(차종·직업군·시도)와 동의 문구 |

## registrations

차량 등록 심사(관리자) — 가입 신청을 PC에서 승인·반려하고 차량을 연결.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/registrations` | 관리자 이상(관리자·최고관리자) | 차량 등록 신청 목록(번호판 마스킹) |
| GET | `/registrations/member/{username}/history` | 관리자 이상(관리자·최고관리자) | 회원 변경 이력(관리자용 — 처리자·접속 IP 포함, 조회 사실도 기록) |
| POST | `/registrations/{registration_id}/approve` | 관리자 이상(관리자·최고관리자) | 신청 승인 — 계정에 차량 연결 |
| POST | `/registrations/{registration_id}/reject` | 관리자 이상(관리자·최고관리자) | 신청 반려 |
| POST | `/registrations/{registration_id}/reveal-plate` | 관리자 이상(관리자·최고관리자) | 전체 번호판 열람 — 사유 필수, 접속기록에 남음 |

## ingest

데이터 연계·적재 (WS1) — 합성 데이터 생성, 실증 데이터 업로드, 파이프라인 실행.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/ingest/batch/promote` | 최고관리자 | shadow로 검증한 staging 결과를 운영 데이터에 반영(최고관리자) |
| POST | `/ingest/batch/run` | 관리자 이상(관리자·최고관리자) | OBD 일일 배치 즉시 실행(백그라운드) |
| GET | `/ingest/batch/status` | 관리자 이상(관리자·최고관리자) | 최근 OBD 일일 배치 결과(도착 대조·격리·모드) |
| POST | `/ingest/generate` | 관리자 이상(관리자·최고관리자) | 합성 데모 데이터 생성 |
| POST | `/ingest/obd-emulator/run` | 관리자 이상(관리자·최고관리자) | 가상 실증차량 OBD 배치를 합성해 실제 파이프라인으로 흘려보냄 |
| POST | `/ingest/run` | 관리자 이상(관리자·최고관리자) | raw → curated 파이프라인 실행 |
| GET | `/ingest/status` | 관리자 이상(관리자·최고관리자) | 현재 데이터 소스 모드 확인 (데모/실증) |
| POST | `/ingest/sync-kpx-smp` | 관리자 이상(관리자·최고관리자) | 한국전력거래소(KPX) 실시간 SMP를 grid_signal.price_krw_per_kwh에 반영 |
| DELETE | `/ingest/upload` | 관리자 이상(관리자·최고관리자) | 업로드된 데이터 삭제 → 데모 모드로 복귀 |
| POST | `/ingest/upload` | 관리자 이상(관리자·최고관리자) | 실증 데이터 업로드 (품질검증 통과 시 반영) |
| POST | `/ingest/upload-obd` | 관리자 이상(관리자·최고관리자) | 실증 차량 OBD 배치 업로드 (원본 1Hz 로그 → 시간당 자동 집계) |

## forecast

AI 수요·공급 예측 서빙 (WS2) — 적합도 확인, 일/주/월 재귀 예측 전망.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/forecast/meta/granularities` | 관리자 이상(관리자·최고관리자) | 지원하는 조회 단위(granularity) 목록 |
| GET | `/forecast/plugin-pattern/{ev_id}` | 관리자 이상(관리자·최고관리자) | 차량별 입출차(플러그인/아웃) 패턴 예측 — 요일·시간대 이력 기반 |
| GET | `/forecast/{target_col}` | 관리자 이상(관리자·최고관리자) | 최근 구간 실측 vs 예측 (모델 적합도 확인용) |
| GET | `/forecast/{target_col}/horizon` | 관리자 이상(관리자·최고관리자) | 일별(시간대별)/주별/월별 미래 전망 (재귀 예측) |
| GET | `/forecast/{target_col}/horizon/report` | 관리자 이상(관리자·최고관리자) | 일/주/월 전망 리포트 다운로드 (Word .docx 또는 PDF) |

## optimize

AI 최적 V2G 의사결정 (WS3/WS4) — 일반충전 vs 최적화 PoC 비교.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/optimize/poc-compare` | 관리자 이상(관리자·최고관리자) | 일반충전 vs AI-V2G 최적화 피크저감 비교 |
| GET | `/optimize/price-options` | 관리자 이상(관리자·최고관리자) | 선택 가능한 충전 요금표·방전 보상 시나리오 |

## models

예측 모델 레지스트리 조회 (Monitoring 레이어 입력).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/models` | 관리자 이상(관리자·최고관리자) | 등록된 예측 대상 전체 목록·활성 버전 |
| GET | `/models/{target_col}` | 관리자 이상(관리자·최고관리자) | 특정 예측 대상의 모델 상세 정보 |

## dashboard

실시간 웹 대시보드 데이터 (KPI·차트·Closed-loop 통합).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/dashboard/charging-efficiency` | 관리자 이상(관리자·최고관리자) | 차량(OBD) 충전량 vs 충전기 계측 충전량 비교 — 충전 효율(손실률) 추정 |
| GET | `/dashboard/data` | 관리자 이상(관리자·최고관리자) | 대시보드 전체 데이터 — KPI/PoC비교/예측적합도/Closed-loop |
| GET | `/dashboard/dr-performance` | 관리자 이상(관리자·최고관리자) | 등록된 DR 이벤트별 방전/부하감축 실적(AI 추론·모의 포함) |

## copilot

EV Bot — Gemini 기반 RAG 질의응답 (사업계획 문서 + 시나리오 데이터 근거).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/copilot/ask` | 관리자 이상(관리자·최고관리자) | 사업계획 문서 + 현재 시나리오 데이터(+선택적으로 내 차량 상태·계획) 기반 질의응답 |
| GET | `/copilot/explain-next-session` | 관리자 이상(관리자·최고관리자) | AI가 예측된 다음 세션(내일)의 충전/방전 계획을 미리 안내 |
| GET | `/copilot/explain-schedule` | 관리자 이상(관리자·최고관리자) | AI가 특정 EV 세션의 최적 충전/방전 계획을 자연어로 설명 |

## ocpp

OCPP 1.6/2.0.1 충전기 통신 (WS4 Control 레이어) — 가상 충전기 에뮬레이터, V2G 방전 포함.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/ocpp/clear-ai-schedule` | 관리자 이상(관리자·최고관리자) | CEMS 명령 전체 해제(다시 자율 시뮬레이션으로 복귀) |
| POST | `/ocpp/dispatch-ai-schedule` | 관리자 이상(관리자·최고관리자) | CEMS(AI 최적화) 스케줄을 CSMS를 거쳐 실제 접속된 충전기에 명령으로 전달 |
| POST | `/ocpp/emulator/start` | 관리자 이상(관리자·최고관리자) | 가상 충전기 접속 시작 |
| POST | `/ocpp/emulator/stop` | 관리자 이상(관리자·최고관리자) | 가상 충전기 전체 정지 |
| GET | `/ocpp/status` | 관리자 이상(관리자·최고관리자) | 접속된 충전기 실시간 상태 조회 |

## grid

한전 계통 수요반응(DR) 이벤트 — CEMS 외부 신호 수신 스텁, 가격 신호로 V2G 최적화에 반영.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | `/grid/dr-emulator/start` | 관리자 이상(관리자·최고관리자) | DR 신호 자동 발생 시작 (데모/개발용 — 실제 한전 연동 아님) |
| GET | `/grid/dr-emulator/status` | 관리자 이상(관리자·최고관리자) | DR 에뮬레이터 실행 상태 |
| POST | `/grid/dr-emulator/stop` | 관리자 이상(관리자·최고관리자) | DR 신호 자동 발생 중지 |
| DELETE | `/grid/dr-events` | 관리자 이상(관리자·최고관리자) | 등록된 DR 이벤트 전체 삭제 |
| GET | `/grid/dr-events` | 관리자 이상(관리자·최고관리자) | 등록된 DR 이벤트 목록 |
| POST | `/grid/dr-events` | 관리자 이상(관리자·최고관리자) | DR 이벤트 등록 (한전 DR 신호 수신 시뮬레이션) |
| DELETE | `/grid/dr-events/{event_id}` | 관리자 이상(관리자·최고관리자) | DR 이벤트 취소 |
