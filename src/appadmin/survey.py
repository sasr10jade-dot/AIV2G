"""참여주체 VoC 설문 (요구사항 Y2E05 — 계획서 p.34 레퍼런스 3 '참여주체 VoC(설문·인터뷰)').

관리자가 설문을 만들고(문항: 5점 척도·객관식·주관식) 공개하면, 서포터즈가 모바일 앱에서 한 번 응답한다.
관리자는 문항별 집계(척도 평균·분포, 선택지별 건수, 주관식 목록)와 CSV를 본다.

원칙
- 응답은 차량(ev_id)당 설문 1회 — 다시 보내면 거부한다(중복 집계 방지).
- 응답에 이름·번호판 등 식별정보를 저장하지 않는다(차량 ID만). 집계 화면에는 차량 ID도 보이지 않는다.
- 주관식은 500자 이하로 자르고, 문항 수는 1~15개로 제한한다.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone

from src.privacy import consent

QUESTION_TYPES = ("scale", "choice", "text")
MAX_QUESTIONS = 15
MAX_TEXT = 500


class SurveyError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    con = consent.connect()
    con.executescript(
        """CREATE TABLE IF NOT EXISTS surveys (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
            questions TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            created_by TEXT, created_at TEXT NOT NULL, closed_at TEXT);
        CREATE TABLE IF NOT EXISTS survey_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, survey_id INTEGER NOT NULL, ev_id TEXT NOT NULL,
            answers TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (survey_id, ev_id));"""
    )
    return con


def _clean_questions(questions: list[dict]) -> list[dict]:
    if not questions or len(questions) > MAX_QUESTIONS:
        raise SurveyError(f"문항은 1~{MAX_QUESTIONS}개로 만드세요")
    out = []
    for i, q in enumerate(questions, 1):
        text = " ".join(str(q.get("text", "")).split())[:200]
        qtype = q.get("type")
        if not text:
            raise SurveyError(f"{i}번 문항 내용을 입력하세요")
        if qtype not in QUESTION_TYPES:
            raise SurveyError(f"{i}번 문항 유형이 올바르지 않습니다(scale·choice·text)")
        item = {"id": f"q{i}", "type": qtype, "text": text, "required": bool(q.get("required", qtype != "text"))}
        if qtype == "choice":
            opts = [" ".join(str(o).split())[:60] for o in (q.get("options") or []) if str(o).strip()]
            opts = list(dict.fromkeys(opts))
            if len(opts) < 2 or len(opts) > 8:
                raise SurveyError(f"{i}번 객관식 문항은 선택지를 2~8개 입력하세요")
            item["options"] = opts
        out.append(item)
    return out


def create(title: str, description: str | None, questions: list[dict], actor: str) -> dict:
    title = " ".join((title or "").split())[:80]
    if not title:
        raise SurveyError("설문 제목을 입력하세요")
    qs = _clean_questions(questions)
    con = _connect()
    try:
        cur = con.execute("INSERT INTO surveys (title, description, questions, status, created_by, created_at) VALUES (?,?,?,?,?,?)",
                          (title, (description or "").strip()[:500] or None, json.dumps(qs, ensure_ascii=False), "open", actor, _now()))
        con.commit()
        return get(cur.lastrowid, con)
    finally:
        con.close()


def _row(r) -> dict:
    return {"id": r[0], "title": r[1], "description": r[2], "questions": json.loads(r[3]), "status": r[4],
            "created_by": r[5], "created_at": r[6], "closed_at": r[7]}


_COLS = "id, title, description, questions, status, created_by, created_at, closed_at"


def get(survey_id: int, con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or _connect()
    try:
        r = con.execute(f"SELECT {_COLS} FROM surveys WHERE id=?", (survey_id,)).fetchone()
        if not r:
            raise SurveyError("설문을 찾을 수 없습니다")
        out = _row(r)
        out["responses"] = con.execute("SELECT COUNT(*) FROM survey_responses WHERE survey_id=?", (survey_id,)).fetchone()[0]
        return out
    finally:
        if own:
            con.close()


def list_all() -> list[dict]:
    con = _connect()
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM surveys ORDER BY id DESC").fetchall()]
        return [get(i, con) for i in ids]
    finally:
        con.close()


def close(survey_id: int) -> dict:
    con = _connect()
    try:
        cur = con.execute("UPDATE surveys SET status='closed', closed_at=? WHERE id=? AND status='open'", (_now(), survey_id))
        con.commit()
        if cur.rowcount != 1:
            raise SurveyError("진행 중인 설문을 찾을 수 없습니다")
        return get(survey_id, con)
    finally:
        con.close()


def open_for(ev_id: str) -> list[dict]:
    """서포터즈에게 보일 진행 중 설문 — 이미 응답했는지 표시."""
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_COLS} FROM surveys WHERE status='open' ORDER BY id DESC").fetchall()
        done = {r[0] for r in con.execute("SELECT survey_id FROM survey_responses WHERE ev_id=?", (ev_id,)).fetchall()}
        return [{**_row(r), "answered": r[0] in done} for r in rows]
    finally:
        con.close()


def respond(survey_id: int, ev_id: str, answers: dict) -> None:
    con = _connect()
    try:
        r = con.execute(f"SELECT {_COLS} FROM surveys WHERE id=?", (survey_id,)).fetchone()
        if not r or r[4] != "open":
            raise SurveyError("진행 중인 설문이 아닙니다")
        clean = {}
        for q in _row(r)["questions"]:
            v = (answers or {}).get(q["id"])
            if v is None or (isinstance(v, str) and not v.strip()):
                if q["required"]:
                    raise SurveyError(f"'{q['text']}' 문항에 답해 주세요")
                continue
            if q["type"] == "scale":
                try:
                    v = int(v)
                except (TypeError, ValueError) as exc:
                    raise SurveyError("척도 문항은 1~5 중에서 고르세요") from exc
                if not 1 <= v <= 5:
                    raise SurveyError("척도 문항은 1~5 중에서 고르세요")
            elif q["type"] == "choice":
                if v not in q["options"]:
                    raise SurveyError(f"'{q['text']}' 문항의 선택지가 올바르지 않습니다")
            else:
                v = str(v).strip()[:MAX_TEXT]
            clean[q["id"]] = v
        try:
            con.execute("INSERT INTO survey_responses (survey_id, ev_id, answers, created_at) VALUES (?,?,?,?)",
                        (survey_id, ev_id, json.dumps(clean, ensure_ascii=False), _now()))
            con.commit()
        except sqlite3.IntegrityError as exc:
            raise SurveyError("이미 응답한 설문입니다") from exc
    finally:
        con.close()


def results(survey_id: int) -> dict:
    """문항별 집계 — 척도: 평균·분포, 객관식: 선택지별 건수, 주관식: 응답 목록(차량 ID 없이)."""
    con = _connect()
    try:
        s = get(survey_id, con)
        answers = [json.loads(r[0]) for r in con.execute(
            "SELECT answers FROM survey_responses WHERE survey_id=? ORDER BY id", (survey_id,)).fetchall()]
    finally:
        con.close()
    out = []
    for q in s["questions"]:
        vals = [a[q["id"]] for a in answers if q["id"] in a]
        item = {"id": q["id"], "type": q["type"], "text": q["text"], "answered": len(vals)}
        if q["type"] == "scale":
            item["distribution"] = {str(k): sum(1 for v in vals if v == k) for k in range(1, 6)}
            item["average"] = round(sum(vals) / len(vals), 2) if vals else None
        elif q["type"] == "choice":
            item["counts"] = {o: sum(1 for v in vals if v == o) for o in q["options"]}
        else:
            item["texts"] = vals[-100:]
        out.append(item)
    return {"survey": s, "responses": len(answers), "questions": out}


def results_csv(survey_id: int) -> str:
    """응답 원자료 CSV(차량 ID는 순번으로 대체) — 엑셀에서 열리게 UTF-8 BOM을 붙인다."""
    con = _connect()
    try:
        s = get(survey_id, con)
        rows = con.execute("SELECT answers, created_at FROM survey_responses WHERE survey_id=? ORDER BY id", (survey_id,)).fetchall()
    finally:
        con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["응답 번호", "응답 시각"] + [f"{q['id']}. {q['text']}" for q in s["questions"]])
    for i, (ans, at) in enumerate(rows, 1):
        a = json.loads(ans)
        w.writerow([i, at] + [a.get(q["id"], "") for q in s["questions"]])
    return "﻿" + buf.getvalue()
