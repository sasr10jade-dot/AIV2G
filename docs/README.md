# AIV2G — AI-V2G 실증 시스템 문서

올인원 AI 허브 AX 실증(에너지·모빌리티) — AI 기반 도시형 에너지 이익공유 및 전기차 V2G 기술 실증.
이 폴더는 개발자 인수인계 문서와 GitHub Pages 고정 진입 링크(`index.html` → https://sasr10jade-dot.github.io/AIV2G/)를 함께 둔다.

## 먼저 읽을 것

| 문서 | 내용 |
|---|---|
| [`인수인계_개발자_필독_가이드.txt`](./인수인계_개발자_필독_가이드.txt) | **[필독]** 셋업·서버 구동(내부망 5050)·보강 내역·테스트 |
| [`AI-V2G_PRD_소스분석_v1.0_261118.md`](./AI-V2G_PRD_소스분석_v1.0_261118.md) | 현행 소스 기준 PRD — 기능 요구사항, 계획서 대비 갭(충족률), 리워드 정산 프로세스 |
| [`developer_handover_prd_v1.0.md`](./developer_handover_prd_v1.0.md) | 아키텍처·운영 런북(서버 구동·재학습 배치·Git 규칙) |
| [`api_reference.md`](./api_reference.md) | 전체 API 레퍼런스 — 코드에서 자동 생성(`python scripts/build_api_docs.py`) |
| [`data_specifications_v1.0.md`](./data_specifications_v1.0.md) | 데이터 전주기 통합 명세(OBD·CPO·공공데이터·가명화) |

## 운영·보안 문서

| 문서 | 내용 |
|---|---|
| [`obd_batch_runbook.md`](./obd_batch_runbook.md) | OBD 일일 배치(TOAST Object Storage) 운영 |
| [`privacy_incident_response.md`](./privacy_incident_response.md) | 개인정보 침해사고 대응 |
| [`ai_v2g_national_service_fit.md`](./ai_v2g_national_service_fit.md) | 이익공유 모델 정책·제도 적합성 검토 |
| [`meeting_brief_charging_operator.md`](./meeting_brief_charging_operator.md) | 충전사업자 협의용 기능 요약 |
| [`owner_web_roadmap.md`](./owner_web_roadmap.md), [`service_review_20261023.md`](./service_review_20261023.md), [`real_charging_sample_summary.md`](./real_charging_sample_summary.md) | 서비스 로드맵·점검 기록·실충전 샘플 요약 |

## 저장소에 없는 문서(별도 전달)

엑셀·한글·워드·PDF 문서는 `.gitignore` 대상이라 저장소에 올라가지 않는다.
요구사항정의서(`AI-V2G_요구사항정의서_v1.0_261118.xlsx` — 요구사항·API 연계 대상·계산식·AI 방법·판정요약), 사업수행계획서, 데이터 명세 엑셀 등은 USB 등으로 전달한다.

## 빠른 시작

```powershell
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn src.api:app --host 127.0.0.1 --port 5050 --reload   # http://127.0.0.1:5050/dev
python -m pytest -q
```

사내 와이파이 개발 서버: http://192.168.35.92:5050/dev (서버 PC `~/AIV2G-dev`, launchd `kr.aiv2g.dev`).
