from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from backend.db import connect

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


@app.get("/shows")
def list_shows():
    with connect() as conn:
        rows = conn.execute("SELECT id, title, venue, starts_at FROM shows ORDER BY starts_at, id").fetchall()
    return [{"id": r[0], "title": r[1], "venue": r[2], "startsAt": _iso(r[3])} for r in rows]


@app.get("/shows/{show_id}/seats")
def seat_map(show_id: str, now: datetime = Depends(get_now)):
    if _has_nul(show_id):
        raise NOT_FOUND
    with connect() as conn:
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
    with connect() as conn:
        # 조건부 UPDATE: 행 잠금으로 동시 요청이 직렬화되고, 조건을 만족하는 하나만 성공한다.
        row = conn.execute(
            """UPDATE seats SET status = 'HELD', hold_user_id = %s, hold_expires_at = %s
               WHERE id = %s AND (status = 'AVAILABLE' OR (status = 'HELD' AND hold_expires_at <= %s))
               RETURNING id""",
            (user, expires, seat_id, now),
        ).fetchone()
        if row:
            return {"seatId": seat_id, "userId": user, "expiresAt": _iso(expires)}
        cur = conn.execute("SELECT status FROM seats WHERE id = %s", (seat_id,)).fetchone()
    if not cur:
        raise NOT_FOUND
    if cur[0] == "SOLD":
        raise ApiError(409, "SEAT_SOLD", "이미 판매된 좌석입니다.")
    raise ApiError(409, "SEAT_HELD", "이미 잡힌 좌석입니다.")


@app.post("/bookings", status_code=201)
async def confirm_booking(request: Request, user: str = Depends(require_user), now: datetime = Depends(get_now)):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    seat_id = body.get("seatId") if isinstance(body, dict) else None
    if not isinstance(seat_id, str) or not seat_id or _has_nul(seat_id):
        raise ApiError(400, "INVALID_REQUEST", "seatId 가 필요합니다.")
    # 동기 psycopg 호출은 이벤트 루프를 막지 않도록 스레드풀에서 실행한다.
    return await run_in_threadpool(_confirm, seat_id, user, now)


def _confirm(seat_id: str, user: str, now: datetime):
    with connect() as conn:
        seat = conn.execute(
            "SELECT status, hold_user_id, hold_expires_at FROM seats WHERE id = %s FOR UPDATE", (seat_id,)
        ).fetchone()
        if not seat:
            raise NOT_FOUND
        status, hold_user, hold_expires = seat
        if status == "SOLD":
            raise ApiError(409, "SEAT_SOLD", "이미 판매된 좌석입니다.")
        if status == "AVAILABLE":
            raise ApiError(409, "NO_HOLD", "홀드되지 않은 좌석입니다.")
        if hold_expires <= now:
            raise ApiError(409, "HOLD_EXPIRED", "홀드가 만료되었습니다.")
        if hold_user != user:
            raise ApiError(403, "HOLD_NOT_OWNED", "본인이 홀드한 좌석이 아닙니다.")
        conn.execute("UPDATE seats SET status = 'SOLD', hold_user_id = %s, hold_expires_at = NULL WHERE id = %s",
                     (user, seat_id))
        booking_id = conn.execute(
            "INSERT INTO bookings (seat_id, user_id, created_at) VALUES (%s, %s, %s) RETURNING id",
            (seat_id, user, now),
        ).fetchone()[0]
    return {"id": booking_id, "seatId": seat_id, "userId": user, "createdAt": _iso(now)}
