"""모델링 예외 기록 — 계획서 4.1 다 "모델링 과정의 데이터·오류·예외상황을 수집하여 개선에 활용".

예측·최적화·데이터 수집 단계에서 실패하거나 예외 처리(안전 복귀·재학습 거부 등)로 넘어간 일을 curated.db
`modeling_issues`에 모은다. 같은 단계·같은 원인은 한 줄로 묶어 횟수(count)와 처음·마지막 발생 시각만 늘린다.
운영자는 관리 화면에서 원인을 보고 개선 조치를 적어 '조치 완료'로 닫는다. 닫힌 뒤 같은 일이 또 생기면 새 줄(재발)로 남는다.

record()는 절대 예외를 올리지 않는다 — 기록이 실패해도 본래 기능(예측·추천·수집)은 그대로 돈다.
상세(detail)에는 번호판 등 식별정보를 넣지 않는다(호출 측이 차량 ID·파일명 정도만 넘긴다).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from src.pipeline import store

logger = logging.getLogger(__name__)

STAGES = {
    "ingest": "데이터 수집·정제",
    "public_data": "공개 데이터 연계",
    "forecast": "수요 예측",
    "retrain": "모델 재학습",
    "optimize": "충전·방전 최적화",
    "safety": "안전 검증",
    "external_forecast": "㈜아크릴 예측 결과 수신",
}
KINDS = {"error": "오류(실패)", "exception": "예외상황(대체 처리)", "data": "데이터 문제"}
MAX_MESSAGE = 300
MAX_DETAIL = 2000


class IssueError(ValueError):
    """잘못된 조회·조치 요청."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(stage: str, kind: str, message: str) -> str:
    return hashlib.sha256(f"{stage}|{kind}|{message}".encode("utf-8")).hexdigest()[:16]


def record(stage: str, message: str, kind: str = "error", detail: dict | None = None) -> None:
    """예외 한 건을 기록한다. 같은 원인의 열린 기록이 있으면 횟수만 늘린다. 실패해도 조용히 넘어간다."""
    try:
        if stage not in STAGES or kind not in KINDS:
            raise IssueError(f"알 수 없는 단계·종류: {stage}/{kind}")
        message = " ".join(str(message or "").split())[:MAX_MESSAGE] or "(내용 없음)"
        detail_s = json.dumps(detail, ensure_ascii=False, default=str)[:MAX_DETAIL] if detail else None
        fp, at = _fingerprint(stage, kind, message), _now()
        con = store.get_connection()
        try:
            row = con.execute("SELECT issue_id FROM modeling_issues WHERE fingerprint=? AND status='open'", (fp,)).fetchone()
            if row:
                con.execute("UPDATE modeling_issues SET count=count+1, last_seen=?, detail=COALESCE(?, detail) WHERE issue_id=?",
                            (at, detail_s, row[0]))
            else:
                con.execute("INSERT INTO modeling_issues (fingerprint, stage, kind, message, detail, count, first_seen, last_seen, status) "
                            "VALUES (?,?,?,?,?,1,?,?,'open')", (fp, stage, kind, message, detail_s, at, at))
            con.commit()
        finally:
            con.close()
    except Exception:   # noqa: BLE001 — 기록 실패가 본래 기능을 막지 않게
        logger.exception("모델링 예외 기록에 실패했습니다")


def _row(r: tuple, cols: list[str]) -> dict:
    d = dict(zip(cols, r))
    d["detail"] = json.loads(d["detail"]) if d.get("detail") else None
    d["stage_label"] = STAGES.get(d["stage"], d["stage"])
    d["kind_label"] = KINDS.get(d["kind"], d["kind"])
    return d


def list_issues(status: str = "open", stage: str | None = None, limit: int = 200) -> list[dict]:
    if status not in ("open", "resolved", "all"):
        raise IssueError("status는 open·resolved·all 중 하나여야 합니다")
    if stage is not None and stage not in STAGES:
        raise IssueError(f"stage는 {sorted(STAGES)} 중 하나여야 합니다")
    where, args = [], []
    if status != "all":
        where.append("status=?"); args.append(status)
    if stage:
        where.append("stage=?"); args.append(stage)
    sql = "SELECT * FROM modeling_issues" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY last_seen DESC LIMIT ?"
    con = store.get_connection()
    try:
        cur = con.execute(sql, (*args, max(1, min(int(limit), 1000))))
        cols = [c[0] for c in cur.description]
        return [_row(r, cols) for r in cur.fetchall()]
    finally:
        con.close()


def summary() -> dict:
    """단계별 열린 건수·발생 횟수, 조치 완료 건수 — 관리 화면 상단과 결과 자료(엑셀)에 쓴다."""
    con = store.get_connection()
    try:
        rows = con.execute("SELECT stage, status, COUNT(*), COALESCE(SUM(count), 0) FROM modeling_issues GROUP BY stage, status").fetchall()
    finally:
        con.close()
    by_stage = {s: {"label": label, "open": 0, "occurrences": 0, "resolved": 0} for s, label in STAGES.items()}
    for stage, status, n, occ in rows:
        b = by_stage.setdefault(stage, {"label": stage, "open": 0, "occurrences": 0, "resolved": 0})
        if status == "open":
            b["open"] += n
            b["occurrences"] += occ
        else:
            b["resolved"] += n
    return {"open": sum(b["open"] for b in by_stage.values()),
            "occurrences": sum(b["occurrences"] for b in by_stage.values()),
            "resolved": sum(b["resolved"] for b in by_stage.values()),
            "by_stage": by_stage}


def resolve(issue_id: int, actor: str, note: str) -> dict:
    """원인과 개선 조치를 적어 닫는다(예: '최소 보장 배터리 입력 범위 검사 추가, v3 재학습')."""
    note = " ".join((note or "").split())
    if len(note) < 2 or len(note) > 300:
        raise IssueError("개선 조치 내용을 2~300자로 적어 주세요")
    con = store.get_connection()
    try:
        row = con.execute("SELECT status FROM modeling_issues WHERE issue_id=?", (issue_id,)).fetchone()
        if not row:
            raise IssueError("예외 기록을 찾을 수 없습니다")
        if row[0] == "resolved":
            raise IssueError("이미 조치 완료된 기록입니다")
        at = _now()
        con.execute("UPDATE modeling_issues SET status='resolved', resolved_at=?, resolved_by=?, resolution_note=? WHERE issue_id=?",
                    (at, actor, note, issue_id))
        con.commit()
        cur = con.execute("SELECT * FROM modeling_issues WHERE issue_id=?", (issue_id,))
        cols = [c[0] for c in cur.description]
        return _row(cur.fetchone(), cols)
    finally:
        con.close()
