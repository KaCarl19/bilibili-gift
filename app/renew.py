"""自动续登：连续在舰的舰长（自动续费 / 一次买了多个月），每个新的舰长月自动生成一条记录（= 一份礼物），
沿用之前的收货地址，舰长不用重新上传截图。

判断方法：上一个舰长月结束后（超过 3 天余量），他还在 B站舰长名单里 → 说明续上了（或者本来就买了多个月）。
如果截图里有到期时间、能确定是一次买了多个月，就不用等余量。
防误判：自动续登后几天内，如果连续两次发现他已经不在舰长名单里（说明其实没续费，只是到期日比我们估计的晚），
就自动撤销这条记录（已发货的不动）。
"""
from datetime import date, timedelta

from . import db, periods

_absent: dict[str, int] = {}          # 自动续登后，连续几次刷新没在名单里


def run(guards: dict | None, today: date | None = None) -> list[str]:
    """guards：B站当前舰长名单 {uid: {...}}；返回要写进日志的说明"""
    if not guards:
        return []
    today = today or date.today()
    this_month = today.strftime("%Y-%m")
    logs = []

    # ① 撤销误判
    for r in db.recent_auto(days=10):
        uid = r["bili_uid"]
        if uid in guards:
            _absent.pop(uid, None)
            continue
        _absent[uid] = _absent.get(uid, 0) + 1
        if _absent[uid] >= 2:
            db.set_guard_status(r["id"], "rejected",
                                f"自动续登已撤销：{today} 起已不在舰长名单（上一期结束后没有续费）")
            _absent.pop(uid, None)
            logs.append(f"撤销自动续登：{r['bili_name'] or uid}（UID {uid}）已不在舰长名单")

    # ② 续登
    for r in db.latest_by_uid():
        uid = r["bili_uid"]
        if r["status"] != "verified" or uid not in guards:
            continue                                   # 最新一条在等审核 / 被拒 / 已撤销，或者已经不在舰
        start = date.fromisoformat(r["start_date"])
        k = (today - start).days // periods.PERIOD_DAYS
        if k < 1:
            continue                                   # 当前舰长月还没结束
        cur = start + timedelta(days=periods.PERIOD_DAYS * k)
        paid = date.fromisoformat(r["paid_until"]) if r.get("paid_until") else None
        prepaid = bool(paid and paid > cur)            # 截图显示一次买了多个月，确定还在付费期内
        if not prepaid and (today - cur).days < periods.RENEW_BUFFER_DAYS:
            continue                                   # 刚到期没几天，先等等，防止名单更新延迟造成误判
        if db.period_record(cur, uid=uid, statuses=("verified", "pending")):
            continue
        g = guards[uid]
        month = db.free_month(r["qq"], this_month)
        how = "一次买了多个月，仍在付费期内" if prepaid else "上一期结束后仍在舰长名单（自动续费或多月购买）"
        db.upsert_guard(r["qq"], month, cur.isoformat(), "verified", bili_uid=uid,
                        bili_name=g.get("name") or r["bili_name"], guard_level=g.get("level") or r["guard_level"],
                        source="auto", contact=r.get("contact"), paid_until=r.get("paid_until"),
                        date_source="auto", note=f"自动续登：{how}")
        logs.append(f"自动续登：{g.get('name') or uid}（UID {uid}）新的舰长月从 {cur} 开始，记入 {month} 发货表")
    return logs
