import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from backend.db import app_connection

HOLD_DURATION = timedelta(minutes=5)

app = FastAPI(title="Ticketing API")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError):
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse({"code": "INVALID_REQUEST", "message": "잘못된 요청입니다."}, status_code=400)


def get_now() -> datetime:
    """현재 시각(UTC). 홀드 만료·좌석맵 계산은 전부 이 값을 쓴다(테스트가 override)."""
    return datetime.now(timezone.utc)


def require_user(x_user_id: str | None = Header(default=None)) -> str:
    if x_user_id is None or not x_user_id.strip() or "\x00" in x_user_id:
        raise ApiError(401, "UNAUTHENTICATED", "사용자 식별이 필요합니다.")
    return x_user_id.strip()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


NOT_FOUND = ApiError(404, "NOT_FOUND", "찾을 수 없습니다.")


def _has_nul(value: str) -> bool:
    """PostgreSQL text 는 NUL 을 저장할 수 없으므로 DB 에 보내기 전에 입력 단계에서 거른다."""
    return "\x00" in value


def _not_utf8(value: str) -> bool:
    """JSON \\ud800 같은 이스케이프는 단독 surrogate 로 복원되어 UTF-8 로 인코딩할 수 없고, DB 에 보내면 500 이 된다.
    본문 문자열은 DB 로 보내기 전에 이 검사로 거른다(400 INVALID_REQUEST)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


@app.get("/shows")
def list_shows():
    with app_connection() as conn:
        rows = conn.execute("SELECT id, title, venue, starts_at FROM shows ORDER BY starts_at, id").fetchall()
    return [{"id": r[0], "title": r[1], "venue": r[2], "startsAt": _iso(r[3])} for r in rows]


@app.get("/shows/{show_id}/seats")
def seat_map(show_id: str, now: datetime = Depends(get_now)):
    if _has_nul(show_id):
        raise NOT_FOUND
    with app_connection() as conn:
        if not conn.execute("SELECT 1 FROM shows WHERE id = %s", (show_id,)).fetchone():
            raise NOT_FOUND
        rows = conn.execute(
            """SELECT id, section, row_label, number,
                      CASE WHEN status = 'HELD' AND hold_expires_at <= %s THEN 'AVAILABLE' ELSE status END
               FROM seats WHERE show_id = %s ORDER BY section, row_label, number""",
            (now, show_id),
        ).fetchall()
    return [{"id": r[0], "section": r[1], "row": r[2], "number": r[3], "status": r[4]} for r in rows]


@app.post("/seats/{seat_id}/hold", status_code=201)
def hold_seat(seat_id: str, user: str = Depends(require_user), now: datetime = Depends(get_now)):
    if _has_nul(seat_id):
        raise NOT_FOUND
    expires = now + HOLD_DURATION
    with app_connection() as conn:
        # 조건부 UPDATE: 행 잠금으로 동시 요청이 직렬화되고, 조건을 만족하는 하나만 성공한다.
        row = conn.execute(
            """UPDATE seats SET status = 'HELD', hold_user_id = %s, hold_expires_at = %s,
                                 hold_id = gen_random_uuid()::text
               WHERE id = %s AND (status = 'AVAILABLE' OR (status = 'HELD' AND hold_expires_at <= %s))
               RETURNING id, hold_id""",
            (user, expires, seat_id, now),
        ).fetchone()
        if row:
            return {"holdId": row[1], "seatId": seat_id, "userId": user, "expiresAt": _iso(expires)}
        cur = conn.execute("SELECT status FROM seats WHERE id = %s", (seat_id,)).fetchone()
    if not cur:
        raise NOT_FOUND
    if cur[0] == "SOLD":
        raise ApiError(409, "SEAT_SOLD", "이미 판매된 좌석입니다.")
    raise ApiError(409, "SEAT_HELD", "이미 잡힌 좌석입니다.")


# ---------------------------------------------------------------------------------------------
# 6장: 결제 2단계. 잠금 순서는 언제나 seats 행(FOR UPDATE) → bookings. 시각은 get_now 만 쓴다.
# ---------------------------------------------------------------------------------------------
BOOKING_COLS = "id, seat_id, user_id, status, payment_id, hold_id, hold_expires_at, created_at, updated_at"


def _booking_body(r) -> dict:
    return {"id": r[0], "seatId": r[1], "userId": r[2], "status": r[3], "paymentId": r[4], "holdId": r[5],
            "holdExpiresAt": _iso(r[6]), "createdAt": _iso(r[7]), "updatedAt": _iso(r[8])}


def _bad_key(key: str | None) -> bool:
    return key is None or key == "" or len(key) > 255 or "\x00" in key


@app.post("/bookings")
async def request_payment(request: Request, user: str = Depends(require_user), now: datetime = Depends(get_now)):
    key = request.headers.get("idempotency-key")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    seat_id = body.get("seatId") if isinstance(body, dict) else None
    if _bad_key(key) or not isinstance(seat_id, str) or not seat_id or _has_nul(seat_id) or _not_utf8(seat_id):
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key 또는 seatId 가 잘못되었습니다.")
    # 동기 psycopg 호출은 이벤트 루프를 막지 않도록 스레드풀에서 실행한다.
    return await run_in_threadpool(_request_payment, seat_id, key, user, now)


def _replay_or_mismatch(row, seat_id: str):
    if row[0] != seat_id:
        raise ApiError(409, "IDEMPOTENCY_KEY_MISMATCH", "같은 Idempotency-Key 로 다른 요청을 보냈습니다.")
    return JSONResponse(row[2], status_code=row[1], headers={"Idempotent-Replayed": "true"})


def _request_payment(seat_id: str, key: str, user: str, now: datetime):
    # 같은 사용자·같은 키 / 같은 홀드가 서로 다른 좌석 잠금 없이 겹칠 때의 UNIQUE 위반은 다시 시도해
    # 재조회 결과(재현·409)로 바꾼다. 5xx 로 새지 않는다.
    for _ in range(5):
        try:
            return _request_payment_once(seat_id, key, user, now)
        except UniqueViolation:
            continue
    raise ApiError(409, "PAYMENT_IN_PROGRESS", "결제 요청이 겹쳤습니다. 다시 시도하세요.")


def _stored(conn, user: str, key: str):
    return conn.execute(
        "SELECT request_seat_id, response_status, response_body FROM bookings "
        "WHERE user_id = %s AND idempotency_key = %s", (user, key)).fetchone()


def _request_payment_once(seat_id: str, key: str, user: str, now: datetime):
    with app_connection() as conn:
        row = _stored(conn, user, key)  # 3. 저장된 응답
        if row:
            return _replay_or_mismatch(row, seat_id)
        seat = conn.execute(  # 4. 좌석 — 이 행 잠금이 좌석별 결제 요청·콜백·홀드를 직렬화한다
            "SELECT status, hold_user_id, hold_expires_at, hold_id FROM seats WHERE id = %s FOR UPDATE",
            (seat_id,)).fetchone()
        if not seat:
            raise NOT_FOUND
        row = _stored(conn, user, key)  # 잠금을 기다리는 사이 같은 키 요청이 끝났을 수 있다
        if row:
            return _replay_or_mismatch(row, seat_id)
        status, hold_user, hold_expires, hold_id = seat
        confirmed = conn.execute(  # 5. 본인의 확정 예매 → 200(키 저장 안 함)
            f"SELECT {BOOKING_COLS} FROM bookings WHERE seat_id = %s AND user_id = %s AND status = 'CONFIRMED'",
            (seat_id, user)).fetchone()
        if confirmed:
            return JSONResponse(_booking_body(confirmed), status_code=200)
        if status == "SOLD":  # 6.
            raise ApiError(409, "SEAT_SOLD", "이미 판매된 좌석입니다.")
        if status == "AVAILABLE":  # 7.
            raise ApiError(409, "NO_HOLD", "홀드되지 않은 좌석입니다.")
        if hold_expires <= now:  # 8.
            raise ApiError(409, "HOLD_EXPIRED", "홀드가 만료되었습니다.")
        if hold_user != user:  # 9.
            raise ApiError(403, "HOLD_NOT_OWNED", "본인이 홀드한 좌석이 아닙니다.")
        if conn.execute("SELECT 1 FROM bookings WHERE hold_id = %s AND status = 'PENDING'", (hold_id,)).fetchone():
            raise ApiError(409, "PAYMENT_IN_PROGRESS", "이 홀드에 진행 중인 결제가 있습니다.")  # 10.
        booking = {"id": str(uuid.uuid4()), "seatId": seat_id, "userId": user, "status": "PENDING",  # 11.
                   "paymentId": str(uuid.uuid4()), "holdId": hold_id, "holdExpiresAt": _iso(hold_expires),
                   "createdAt": _iso(now), "updatedAt": _iso(now)}
        conn.execute(
            """INSERT INTO bookings (id, seat_id, user_id, status, payment_id, hold_id, hold_expires_at,
                                     created_at, updated_at, idempotency_key, request_seat_id,
                                     response_status, response_body)
               VALUES (%s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, %s, %s, 202, %s)""",
            (booking["id"], seat_id, user, booking["paymentId"], hold_id, hold_expires, now, now,
             key, seat_id, Jsonb(booking)))
    return JSONResponse(booking, status_code=202)


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str, user: str = Depends(require_user)):
    if _has_nul(booking_id):
        raise NOT_FOUND
    with app_connection() as conn:
        row = conn.execute(f"SELECT {BOOKING_COLS} FROM bookings WHERE id = %s AND user_id = %s",
                           (booking_id, user)).fetchone()
    if not row:
        raise NOT_FOUND
    return _booking_body(row)


def mock_pg_secret() -> str:
    return os.environ.get("MOCK_PG_SECRET", "mock-pg-dev-secret")


@app.post("/payments/callback")
async def payment_callback(request: Request, now: datetime = Depends(get_now)):
    raw = await request.body()
    given = request.headers.get("x-mock-pg-signature") or ""
    expected = hmac.new(mock_pg_secret().encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(given.encode(), expected.encode()):  # 서명은 본문 해석보다 먼저
        raise ApiError(401, "INVALID_SIGNATURE", "서명이 없거나 틀립니다.")
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    payment_id = body.get("paymentId") if isinstance(body, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if (not isinstance(payment_id, str) or not payment_id or _not_utf8(payment_id)
            or result not in ("SUCCEEDED", "FAILED")):
        raise ApiError(400, "INVALID_REQUEST", "paymentId 와 result(SUCCEEDED|FAILED)가 필요합니다.")
    if _has_nul(payment_id):
        raise NOT_FOUND
    return await run_in_threadpool(_apply_callback, payment_id, result, now)


def _apply_callback(payment_id: str, result: str, now: datetime):
    with app_connection() as conn:
        found = conn.execute("SELECT seat_id FROM bookings WHERE payment_id = %s", (payment_id,)).fetchone()
        if not found:
            raise NOT_FOUND
        seat = conn.execute(  # 잠금 순서: seats → bookings (결제 요청·홀드와 같다)
            "SELECT status, hold_expires_at, hold_id FROM seats WHERE id = %s FOR UPDATE", (found[0],)).fetchone()
        b = conn.execute(f"SELECT {BOOKING_COLS} FROM bookings WHERE payment_id = %s FOR UPDATE",
                         (payment_id,)).fetchone()
        status = b[3]
        new_status = None
        if status == "PENDING":
            if result == "FAILED":
                new_status = "FAILED"
            else:
                valid = seat[0] == "HELD" and seat[2] == b[5] and now < seat[1]
                new_status = "CONFIRMED" if valid else "REFUND_REQUIRED"
        elif status == "FAILED":
            new_status = "REFUND_REQUIRED" if result == "SUCCEEDED" else None
        else:  # CONFIRMED / REFUND_REQUIRED
            if result == "FAILED":
                raise ApiError(409, "CALLBACK_CONFLICT", "이미 다른 최종 결과가 반영되었습니다.")
        if new_status is None:
            return {"applied": False, "booking": _booking_body(b)}
        updated = conn.execute(
            f"UPDATE bookings SET status = %s, updated_at = %s WHERE id = %s RETURNING {BOOKING_COLS}",
            (new_status, now, b[0])).fetchone()
        if new_status == "CONFIRMED":
            conn.execute("UPDATE seats SET status = 'SOLD' WHERE id = %s", (b[1],))
    return {"applied": True, "booking": _booking_body(updated)}
