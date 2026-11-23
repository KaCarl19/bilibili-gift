"""入群口令核对（QQ 官方机器人用）。

QQ 官方机器人拿不到 QQ 号，只有 openid（每个机器人看到的都不一样）。这里用 openid 记住
“哪个口令被哪位群成员用过”，以此判断：
  - 口令有没有、对应的舰长还在不在舰；
  - 是不是被别人拿去用了（口令每人一个）；
  - 退过群又想进：要在登记页选「以前退过群，想重新进」（只有本期是提督 / 总督才能选）。
"""
import re
from datetime import date

from . import db, periods
from .config import WEB

CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")
OK, AGAIN, REJOIN_OK, NEED_REJOIN, OTHER, EXPIRED, NOT_FOUND, NO_CODE = (
    "ok", "again", "rejoin_ok", "need_rejoin", "other", "expired", "not_found", "no_code")


def extract(text: str) -> str | None:
    """从一段话里找 6 位口令（不分大小写，忽略空格）"""
    t = re.sub(r"\s+", "", (text or "").upper())
    for m in CODE_RE.finditer(t):
        code = m.group(1)
        if any(ch.isalpha() for ch in code):         # 纯数字更像 QQ 号 / UID，不当口令
            return code
    return None


def _join_row(code):
    with db.conn() as c:
        r = c.execute("SELECT * FROM group_joins WHERE join_code=?", (code,)).fetchone()
        return dict(r) if r else None


def _period_end(g) -> str:
    end = periods.period_end(date.fromisoformat(g["start_date"]), g.get("paused_days"))
    paid = g.get("paid_until")
    return max(end.isoformat(), paid) if paid else end.isoformat()


def check(code: str | None, member_openid: str) -> dict:
    """核对口令，不改数据。返回 {"result", "guard"(本期记录), "row"(以前的使用记录)}"""
    if not code:
        return {"result": NO_CODE}
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM guards WHERE join_code=? AND status='verified' ORDER BY start_date DESC, id DESC", (code,))]
    if not rows:
        return {"result": NOT_FOUND}
    g = db.editable_guard(rows[0]["qq"])
    if not g:
        return {"result": EXPIRED, "guard": rows[0]}
    row = _join_row(code)
    if row and row["member_openid"] != member_openid:
        return {"result": OTHER, "guard": g, "row": row}
    if row:                                            # 同一个人又来核对
        if g.get("group_choice") == "rejoin" and db.rejoin_allowed(g):
            return {"result": REJOIN_OK, "guard": g, "row": row}
        if row.get("left_at"):
            return {"result": NEED_REJOIN, "guard": g, "row": row}
        return {"result": AGAIN, "guard": g, "row": row}
    return {"result": OK, "guard": g}


def mark_joined(code: str, member_openid: str, group_openid: str | None, g: dict):
    with db.conn() as c:
        c.execute("""INSERT INTO group_joins (join_code, member_openid, group_openid, joined_at, left_at)
                     VALUES (?,?,?,?,NULL)
                     ON CONFLICT(join_code) DO UPDATE SET member_openid=excluded.member_openid,
                     group_openid=COALESCE(excluded.group_openid, group_openid), joined_at=excluded.joined_at,
                     left_at=NULL""", (code, member_openid, group_openid, db.now()))
    db.set_group_choice(g["id"], "joined")


def mark_left(member_openid: str) -> list[str]:
    """群成员退群：记下来，并把这个人的记录标成「已退群」。返回受影响的口令"""
    with db.conn() as c:
        codes = [r["join_code"] for r in c.execute(
            "SELECT join_code FROM group_joins WHERE member_openid=? AND left_at IS NULL", (member_openid,))]
        c.execute("UPDATE group_joins SET left_at=? WHERE member_openid=? AND left_at IS NULL", (db.now(), member_openid))
    for code in codes:
        with db.conn() as c:
            r = c.execute("SELECT id FROM guards WHERE join_code=? ORDER BY start_date DESC, id DESC", (code,)).fetchone()
        if r:
            db.set_group_choice(r["id"], "left")
    return codes


def _who(g) -> str:
    return f"{g.get('bili_name') or '舰长'}（{g.get('guard_level') or '舰长'}，本期到 {_period_end(g)}）"


def reply_for_at(text: str, member_openid: str, group_openid: str | None) -> str:
    """群里 @机器人 的回复；核对通过就记为已进群"""
    url = WEB.get("public_url", "")
    code = extract(text)
    res = check(code, member_openid)
    r, g = res["result"], res.get("guard")
    if r in (OK, REJOIN_OK):
        mark_joined(code, member_openid, group_openid, g)
        head = "✅ 重新进群核对通过" if r == REJOIN_OK else "✅ 核对通过"
        return f"{head}：{_who(g)}。欢迎！"
    if r == AGAIN:
        return f"✅ 你已经核对过了（{res['row']['joined_at'][:10]}）：{_who(g)}。在群里的不用重复核对。"
    if r == NEED_REJOIN:
        return ("⚠️ 你之前退过舰长群。退群后想再进，需要重新上提督或总督，"
                "并在登记网页填地址时选「以前退过群，想重新进」。有问题请联系主播。")
    if r == OTHER:
        return "⚠️ 这个口令已经有人用过了。入群口令每人一个，请用你自己的；有问题请联系主播。"
    if r == EXPIRED:
        return "这个口令对应的舰长已经到期，暂时不能核对。续舰后在登记网页重新登记就行。"
    if r == NOT_FOUND:
        return (f"没找到这个口令，请检查一下（6 位字母和数字）。还没有口令？打开登记网页 {url} ，"
                "填地址时选「要进舰长群」。")
    return f"发送「@我 口令 你的入群口令」核对舰长身份。还没有口令？打开登记网页 {url} ，填地址时选「要进舰长群」。"


def decide_join_request(answer: str, member_openid: str, group_openid: str | None) -> tuple[str, str]:
    """入群申请：返回 ("approve" | "decline", 原因)。通过的直接记为已进群"""
    url = WEB.get("public_url", "")
    code = extract(answer)
    res = check(code, member_openid)
    r, g = res["result"], res.get("guard")
    if r in (OK, REJOIN_OK):
        mark_joined(code, member_openid, group_openid, g)
        return "approve", ("重新进群：" if r == REJOIN_OK else "") + _who(g)
    if r == AGAIN:                                     # 漏记了退群，又来申请：按重新进群处理
        return "decline", "你之前已经用这个口令进过群。退群后想再进，需要重新上提督或总督，并在登记网页选「以前退过群，想重新进」"
    reasons = {
        NEED_REJOIN: "退群后想再进，需要重新上提督或总督，并在登记网页选「以前退过群，想重新进」",
        OTHER: "这个入群口令已经被别人用过了，请填你自己的口令",
        EXPIRED: "这个口令对应的舰长已经到期，续舰后重新登记",
        NOT_FOUND: f"入群口令不对。请在登记网页 {url} 填地址时选「要进舰长群」拿口令",
        NO_CODE: f"请在入群问题里填写入群口令（在登记网页 {url} 填地址时选「要进舰长群」拿到）",
    }
    return "decline", reasons[r]
