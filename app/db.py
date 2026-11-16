"""SQLite 数据层。所有数据存放在 data/guard.db

“qq” 字段是登记人的身份标识：通过 QQ 机器人登记的是 QQ 号；通过网页登记的是 "web:B站UID"。
"""
import secrets
import string
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import periods
from .config import DATA_DIR, RULES

DB_PATH = DATA_DIR / "guard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    qq           TEXT NOT NULL,
    bili_uid     TEXT,
    bili_name    TEXT,
    guard_level  TEXT,               -- 舰长 / 提督 / 总督
    month        TEXT NOT NULL,      -- 上舰所属月份 YYYY-MM
    start_date   TEXT NOT NULL,      -- 本舰长月开始日 YYYY-MM-DD
    status       TEXT NOT NULL,      -- verified 已验证 / pending 待审核 / rejected 已拒绝
    source       TEXT,               -- api / screenshot / manual
    screenshot   TEXT,
    shot_hash    TEXT,
    ocr_text     TEXT,
    note         TEXT,
    shipped      INTEGER DEFAULT 0,  -- 是否已发货
    created_at   TEXT NOT NULL,
    UNIQUE(qq, month)
);
CREATE TABLE IF NOT EXISTS addresses (
    qq          TEXT PRIMARY KEY,
    receiver    TEXT NOT NULL,
    phone       TEXT NOT NULL,
    address     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS address_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qq          TEXT NOT NULL,
    receiver    TEXT, phone TEXT, address TEXT,
    changed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token       TEXT PRIMARY KEY,
    qq          TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS address_requests (   -- 网页上的地址修改申请（需主播确认）
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qq          TEXT NOT NULL,           -- 要修改的是哪条记录的地址
    bili_uid    TEXT,
    receiver    TEXT, phone TEXT, address TEXT,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / rejected
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS joins (      -- 进群待验证记录
    qq          TEXT NOT NULL,
    group_id    TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    PRIMARY KEY (qq, group_id)
);
"""


# 私聊里观众直接发来的新地址/手机号，打开网页时预填（只存在内存里，保存后清除）
DRAFTS: dict[str, dict] = {}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def this_month() -> str:
    return date.today().strftime("%Y-%m")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


WEB_PREFIX = "web:"


def is_web(owner) -> bool:
    return str(owner).startswith(WEB_PREFIX)


def web_owner(uid) -> str:
    return f"{WEB_PREFIX}{uid}"


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(guards)")}
        # 旧版本数据库补字段：contact 联系方式 / join_code 入群口令 /
        # paid_until 已付费到哪天（一次买多个月时）/ date_source 本期开始日的来源（screenshot/claimed/manual/auto）
        # gifts 这条要寄的礼物（逗号分隔，如“提督,舰长”）/ paused_days 升级时顺延的天数（接上的舰长时间不另算）
        for col in ("contact", "join_code", "paid_until", "date_source", "gifts", "paused_days"):
            if col not in cols:
                c.execute(f"ALTER TABLE guards ADD COLUMN {col} {'INTEGER DEFAULT 0' if col == 'paused_days' else 'TEXT'}")


_CODE_CHARS = "".join(ch for ch in string.ascii_uppercase + string.digits if ch not in "0O1IL")


def _ensure_join_code(c, gid):
    """已验证的记录生成一个入群口令（加舰长群时填写，方便主播核对）"""
    r = c.execute("SELECT status, join_code FROM guards WHERE id=?", (gid,)).fetchone()
    if r and r["status"] == "verified" and not r["join_code"]:
        while True:
            code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
            if not c.execute("SELECT 1 FROM guards WHERE join_code=?", (code,)).fetchone():
                break
        c.execute("UPDATE guards SET join_code=? WHERE id=?", (code, gid))


# ---------------- 舰长记录 ----------------
def upsert_guard(qq, month, start_date, status, *, bili_uid=None, bili_name=None,
                 guard_level=None, source=None, screenshot=None, shot_hash=None,
                 ocr_text=None, note=None, contact=None, paid_until=None, date_source=None,
                 gifts=None, paused_days=None):
    """同一个 QQ 同一个月只保留一条记录；已验证的记录不会被降级为待审核。
    gifts 不传时按等级自动决定（提督 = 提督 + 舰长 …）。"""
    qq = str(qq)
    with conn() as c:
        old = c.execute("SELECT * FROM guards WHERE qq=? AND month=?", (qq, month)).fetchone()
        # 礼物：明确指定的优先；否则新记录、或等级变了时按等级重新算；其余保持不变
        if gifts is None and guard_level and (old is None or old["guard_level"] != guard_level or not old["gifts"]):
            gifts = ",".join(periods.bundle(guard_level))
        if old and old["status"] == "verified" and status != "verified":
            return dict(old)
        if old:
            c.execute(
                """UPDATE guards SET bili_uid=COALESCE(?,bili_uid), bili_name=COALESCE(?,bili_name),
                   guard_level=COALESCE(?,guard_level), start_date=?, status=?, source=?,
                   screenshot=COALESCE(?,screenshot), shot_hash=COALESCE(?,shot_hash),
                   ocr_text=COALESCE(?,ocr_text), note=?, contact=COALESCE(?,contact),
                   paid_until=COALESCE(?,paid_until), date_source=COALESCE(?,date_source),
                   gifts=COALESCE(?,gifts), paused_days=COALESCE(?,paused_days) WHERE id=?""",
                (bili_uid, bili_name, guard_level, start_date, status, source,
                 screenshot, shot_hash, ocr_text, note, contact, paid_until, date_source,
                 gifts, paused_days, old["id"]))
        else:
            c.execute(
                """INSERT INTO guards (qq,bili_uid,bili_name,guard_level,month,start_date,status,
                   source,screenshot,shot_hash,ocr_text,note,contact,paid_until,date_source,gifts,paused_days,
                   created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (qq, bili_uid, bili_name, guard_level, month, start_date, status, source,
                 screenshot, shot_hash, ocr_text, note, contact, paid_until, date_source,
                 gifts or "舰长", int(paused_days or 0), now()))
        gid = c.execute("SELECT id FROM guards WHERE qq=? AND month=?", (qq, month)).fetchone()["id"]
        _ensure_join_code(c, gid)
        return dict(c.execute("SELECT * FROM guards WHERE id=?", (gid,)).fetchone())


def update_guard(gid, **fields):
    """直接改几个字段（升级合并时用）"""
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE guards SET {cols} WHERE id=?", (*fields.values(), gid))


def contact_text(r) -> str:
    """“QQ/联系方式”一栏：QQ 登记的显示 QQ 号；网页登记的显示观众留的联系方式（内部标识 web:UID 不给人看）"""
    if is_web(r["qq"]):
        return f"{r['contact']}（网页登记）" if r.get("contact") else "网页登记（未留联系方式）"
    return f"QQ {r['qq']}"


def gifts_of(r) -> list[str]:
    """这条记录要寄的礼物；老数据没存的按等级算"""
    g = r.get("gifts")
    return [x for x in g.split(",") if x] if g else periods.bundle(r.get("guard_level"))


def get_guard(gid):
    with conn() as c:
        r = c.execute("SELECT * FROM guards WHERE id=?", (gid,)).fetchone()
        return dict(r) if r else None


def set_guard_status(gid, status, note=None):
    with conn() as c:
        c.execute("UPDATE guards SET status=?, note=COALESCE(?,note) WHERE id=?", (status, note, gid))
        _ensure_join_code(c, gid)


def set_shipped(gid, shipped: bool):
    with conn() as c:
        c.execute("UPDATE guards SET shipped=? WHERE id=?", (1 if shipped else 0, gid))


def delete_guard(gid):
    with conn() as c:
        c.execute("DELETE FROM guards WHERE id=?", (gid,))


def hash_used_by_other(shot_hash, qq):
    """同一张截图被别的 QQ 用过 -> 疑似盗图"""
    with conn() as c:
        r = c.execute("SELECT qq FROM guards WHERE shot_hash=? AND qq<>?", (shot_hash, str(qq))).fetchone()
        return r["qq"] if r else None


def uid_used_by_other(bili_uid, qq, start=None):
    """这个 B站账号的当前舰长月，是否已经被别的 QQ / 网页身份验证过"""
    r = period_record(start or date.today(), uid=bili_uid, statuses=("verified",))
    return r["qq"] if r and r["qq"] != str(qq) else None


def _d(s):
    return date.fromisoformat(s) if s else None


def period_record(start: date, *, owner=None, uid=None, statuses=("verified", "pending")):
    """找“同一个舰长月”的记录：本期开始日相差 3 天以内，或者覆盖了 start 那天。"""
    sql, args = "SELECT * FROM guards WHERE status IN (%s)" % ",".join("?" * len(statuses)), list(statuses)
    if owner is not None:
        sql += " AND qq=?"; args.append(str(owner))
    if uid is not None:
        sql += " AND bili_uid=?"; args.append(str(uid))
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql + " ORDER BY start_date DESC", args)]
    for r in rows:
        s = _d(r["start_date"])
        if abs((s - start).days) <= periods.RENEW_BUFFER_DAYS or s <= start < periods.period_end(s, r.get("paused_days")):
            return r
    return None


def free_month(owner, month: str) -> str:
    """这个人在 month 已经有记录了（另一个舰长月），就往后顺延到没有记录的月份"""
    with conn() as c:
        while c.execute("SELECT 1 FROM guards WHERE qq=? AND month=?", (str(owner), month)).fetchone():
            month = periods.next_month(month)
    return month


def latest_by_uid():
    """每个 B站账号最新的一条记录（用于自动续登）"""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT g.* FROM guards g WHERE g.bili_uid IS NOT NULL AND g.id = (
                   SELECT id FROM guards x WHERE x.bili_uid = g.bili_uid
                   ORDER BY x.start_date DESC, x.id DESC LIMIT 1)""")]


def recent_auto(days=10):
    """最近自动续登、还没发货的记录（用于发现误判后撤销）"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM guards WHERE source='auto' AND status='verified' AND shipped=0 AND created_at>=?",
            (since,))]


def guards_of_qq(qq):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM guards WHERE qq=? ORDER BY month DESC", (str(qq),))]


def editable_guard(qq):
    """目前在舰（今天落在他已验证的某个舰长月 / 已付费期内），或本月发货表里有他（含宽限期内的上月），
    就返回这条记录；否则返回 None —— 不允许填写/修改地址"""
    months = [this_month()]
    grace = int(RULES.get("edit_grace_days", 0) or 0)
    today = date.today()
    if grace and today.day <= grace:
        months.append((today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM guards WHERE qq=? AND status='verified' ORDER BY start_date DESC", (str(qq),))]
    for r in rows:
        if periods.covers(_d(r["start_date"]), today, _d(r.get("paid_until")), r.get("paused_days")):
            return r
    return next((r for r in rows if r["month"] in months), None)


def list_guards(month=None, status=None):
    sql = """SELECT g.*, a.receiver, a.phone, a.address, a.updated_at AS addr_updated
             FROM guards g LEFT JOIN addresses a ON a.qq = g.qq WHERE 1=1"""
    args = []
    if month:
        sql += " AND g.month=?"; args.append(month)
    if status:
        sql += " AND g.status=?"; args.append(status)
    sql += " ORDER BY g.start_date DESC, g.id DESC"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def all_months():
    with conn() as c:
        return [r["month"] for r in c.execute("SELECT DISTINCT month FROM guards ORDER BY month DESC")]


# ---------------- 地址 ----------------
def get_address(qq):
    with conn() as c:
        r = c.execute("SELECT * FROM addresses WHERE qq=?", (str(qq),)).fetchone()
        return dict(r) if r else None


def save_address(qq, receiver, phone, address):
    qq = str(qq)
    with conn() as c:
        old = c.execute("SELECT * FROM addresses WHERE qq=?", (qq,)).fetchone()
        if old:
            c.execute("INSERT INTO address_history (qq,receiver,phone,address,changed_at) VALUES (?,?,?,?,?)",
                      (qq, old["receiver"], old["phone"], old["address"], now()))
        c.execute("""INSERT INTO addresses (qq,receiver,phone,address,updated_at) VALUES (?,?,?,?,?)
                     ON CONFLICT(qq) DO UPDATE SET receiver=excluded.receiver, phone=excluded.phone,
                     address=excluded.address, updated_at=excluded.updated_at""",
                  (qq, receiver, phone, address, now()))
    DRAFTS.pop(qq, None)


# ---------------- 填写链接 token ----------------
def month_end_hours() -> int:
    """距离“本月结束 + 宽限天数”还有多少小时（网页登记的链接这个月内一直有效）"""
    today = date.today()
    first_next = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = datetime.combine(first_next + timedelta(days=int(RULES.get("edit_grace_days", 0) or 0)),
                           datetime.min.time())
    return max(1, int((end - datetime.now()).total_seconds() // 3600) + 1)


def new_token(qq, hours):
    t = secrets.token_urlsafe(16)
    exp = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE expires_at < ?", (now(),))
        c.execute("INSERT INTO tokens (token,qq,expires_at) VALUES (?,?,?)", (t, str(qq), exp))
    return t


def qq_of_token(token):
    with conn() as c:
        r = c.execute("SELECT qq FROM tokens WHERE token=? AND expires_at >= ?", (token, now())).fetchone()
        return r["qq"] if r else None


# ---------------- 地址修改申请 ----------------
def add_request(qq, bili_uid, receiver, phone, address, note=""):
    with conn() as c:
        c.execute("DELETE FROM address_requests WHERE qq=? AND status='pending'", (str(qq),))  # 只保留最新一次
        c.execute("""INSERT INTO address_requests (qq,bili_uid,receiver,phone,address,note,created_at)
                     VALUES (?,?,?,?,?,?,?)""", (str(qq), bili_uid, receiver, phone, address, note, now()))


def list_requests(status="pending"):
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT r.*, a.receiver AS old_receiver, a.phone AS old_phone, a.address AS old_address,
                      (SELECT bili_name FROM guards g WHERE g.qq=r.qq ORDER BY month DESC LIMIT 1) AS bili_name
               FROM address_requests r LEFT JOIN addresses a ON a.qq=r.qq
               WHERE r.status=? ORDER BY r.id""", (status,))]


def resolve_request(rid, approve: bool):
    with conn() as c:
        r = c.execute("SELECT * FROM address_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE address_requests SET status=? WHERE id=?", ("approved" if approve else "rejected", rid))
    if approve:
        save_address(r["qq"], r["receiver"], r["phone"], r["address"])
    return dict(r)


def count_pending_requests():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM address_requests WHERE status='pending'").fetchone()[0]


# ---------------- 进群待验证 ----------------
def add_join(qq, group_id):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO joins (qq,group_id,joined_at) VALUES (?,?,?)",
                  (str(qq), str(group_id), now()))


def remove_join(qq):
    with conn() as c:
        c.execute("DELETE FROM joins WHERE qq=?", (str(qq),))


def is_joining(qq):
    with conn() as c:
        return c.execute("SELECT 1 FROM joins WHERE qq=?", (str(qq),)).fetchone() is not None


def expired_joins(minutes):
    limit = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM joins WHERE joined_at < ?", (limit,))]
