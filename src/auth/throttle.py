"""로그인·비밀번호 확인 시도 제한 — 무차별 대입(brute force) 방어.

계정(아이디)별과 접속지(IP)별로 실패 횟수를 세서, 창(기본 10분) 안에 한도(계정 8회·IP 40회)를 넘으면 잠금 시간(기본 10분) 동안
시도를 거절한다(429). 성공하면 그 계정의 실패 기록을 지운다. 메모리 기반이라 서버를 다시 시작하면 초기화된다(단일 프로세스 전제).

프록시·터널 뒤에서는 모든 요청의 접속지가 같게 보일 수 있어 IP 한도는 넉넉하게 두고, 실제 방어는 계정별 한도가 맡는다.
설정: LOGIN_MAX_FAILS(기본 8), LOGIN_MAX_FAILS_PER_IP(40), LOGIN_WINDOW_S(600), LOGIN_LOCK_S(600).
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from src.auth.deps import client_ip

_lock = threading.Lock()
_fails: dict[str, deque] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _cfg(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def reset() -> None:
    with _lock:
        _fails.clear()
        _locked_until.clear()


def _keys(request: Request, username: str) -> list[tuple[str, int]]:
    return [(f"u:{(username or '').strip().lower()}", _cfg("LOGIN_MAX_FAILS", 8)),
            (f"ip:{client_ip(request) or 'unknown'}", _cfg("LOGIN_MAX_FAILS_PER_IP", 40))]


def check(request: Request, username: str) -> None:
    """잠겨 있으면 429. 로그인 처리 전에 부른다."""
    now = time.monotonic()
    with _lock:
        for key, _limit in _keys(request, username):
            until = _locked_until.get(key, 0.0)
            if until > now:
                raise HTTPException(status_code=429, detail="시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
                                    headers={"Retry-After": str(int(until - now) + 1)})


MAX_TRACKED_KEYS = 5000   # 없는 아이디를 무작위로 시도해 메모리를 채우지 못하게 추적 개수를 제한한다


def _prune(now: float, window: int) -> None:
    for key in [k for k, q in _fails.items() if not q or now - q[-1] > window]:
        _fails.pop(key, None)
    for key in [k for k, until in _locked_until.items() if until <= now]:
        _locked_until.pop(key, None)


def fail(request: Request, username: str) -> None:
    now = time.monotonic()
    window, lock_s = _cfg("LOGIN_WINDOW_S", 600), _cfg("LOGIN_LOCK_S", 600)
    with _lock:
        if len(_fails) > MAX_TRACKED_KEYS:
            _prune(now, window)
        for key, limit in _keys(request, username):
            q = _fails[key]
            q.append(now)
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                _locked_until[key] = now + lock_s
                q.clear()


def success(request: Request, username: str) -> None:
    with _lock:
        key = _keys(request, username)[0][0]
        _fails.pop(key, None)
        _locked_until.pop(key, None)
