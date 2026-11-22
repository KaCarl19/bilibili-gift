"""自动续登 + 升级合并

自动续登：连续在舰的舰长（自动续费 / 一次买了多个月），每个新的舰长月自动生成一条记录（= 一份礼物），
沿用之前的收货地址，舰长不用重新上传截图。
  判断方法：上一期结束后（超过 3 天余量），他还在 B站舰长名单里 → 说明续上了（或者本来就买了多个月）。
  截图里有到期时间、能确定是一次买了多个月的，不用等余量。
  防误判：自动续登后，如果连续两次发现他已经不在名单里（其实没续费），就自动撤销这条记录（已发货的不动）。
  服务停过：中间漏掉的期一起补登，记入各自原本的月份。已付费期内的（一次买多个月）直接登记；
  按月续费、确认不了中间有没有断过的，设为「待审核」请主播核实。

升级合并：舰长中途开了提督（名单里的等级变高了）：
  - 这条记录还没发货 → 直接升级成「提督礼物 + 舰长礼物」（合并成一份）；
  - 已经发货了 → 另记一条，只补寄新增的「提督礼物」。
  B站会把没用完的舰长时间暂停、等提督到期后接着算，这段时间不再另算：
  记录的覆盖期顺延 30 天（paused_days），这期间不会触发自动续登。
"""
from datetime import date, timedelta

from . import db, periods

_absent: dict[str, int] = {}          # 自动续登后，连续几次刷新没在名单里
MAX_BACKFILL = 12                     # 服务停机后最多往回补几期


def _span_end(r) -> date:
    return periods.period_end(date.fromisoformat(r["start_date"]), r.get("paused_days"))


def upgrade(r: dict, new_level: str, when: date) -> dict:
    """把记录 r 升级到 new_level（when = 升级日期）。返回升级后的记录（可能是新记录）。"""
    if not periods.is_higher(new_level, r.get("guard_level")):
        return r
    paused = int(r.get("paused_days") or 0)
    if not r["shipped"]:
        paid = r.get("paid_until")
        if paid:                                         # 舰长剩余的已付费时间也一起往后顺延
            paid = (date.fromisoformat(paid) + timedelta(days=periods.PERIOD_DAYS)).isoformat()
        note = (r.get("note") or "") + f"；{when} 升级为{new_level}（合并成一份，暂停的舰长时间顺延到{new_level}之后，不另算）"
        db.update_guard(r["id"], guard_level=new_level, gifts=",".join(periods.bundle(new_level)),
                        paused_days=paused + periods.PERIOD_DAYS, paid_until=paid, note=note.lstrip("；"))
        return db.get_guard(r["id"])
    # 已经发过货：另记一条，只补寄新增的礼物
    left = [g for g in periods.bundle(new_level) if g not in db.gifts_of(r)]
    remain = max(0, (_span_end(r) - when).days)          # 被暂停的舰长时间，接在新等级后面，不另算
    month = db.free_month(r["qq"], max(when.strftime("%Y-%m"), db.this_month()))
    return db.upsert_guard(r["qq"], month, when.isoformat(), "verified", bili_uid=r.get("bili_uid"),
                           bili_name=r.get("bili_name"), guard_level=new_level, gifts=",".join(left or [new_level]),
                           paused_days=remain, source="auto", date_source="auto", contact=r.get("contact"),
                           note=f"升级为{new_level}：{r['month']} 那份已发货，这次只补寄{'、'.join(left)}礼物")


def run(guards: dict | None, today: date | None = None, auto_continue: bool = True) -> list[str]:
    """guards：B站当前舰长名单 {uid: {...}}；返回要写进日志的说明"""
    if not guards:
        return []
    today = today or date.today()
    this_month = today.strftime("%Y-%m")
    logs = []

    # ① 撤销误判的自动续登
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

    for r in db.latest_by_uid():
        uid = r["bili_uid"]
        if r["status"] != "verified" or uid not in guards:
            continue                                   # 最新一条在等审核 / 被拒 / 已撤销，或者已经不在舰
        g = guards[uid]
        start = date.fromisoformat(r["start_date"])
        end = _span_end(r)

        # ② 升级合并：还在这条记录的覆盖期内，名单里的等级变高了
        if today < end and periods.is_higher(g.get("level"), r.get("guard_level")):
            new = upgrade(r, g["level"], today)
            how = "合并成一份" if new["id"] == r["id"] else "已发货，另记一条补寄"
            logs.append(f"升级：{g.get('name') or uid}（UID {uid}）{r.get('guard_level')} → {g['level']}，{how}")
            continue

        # ③ 自动续登
        if not auto_continue or today < end:
            continue                                   # 当前这期（含顺延）还没结束
        if r.get("source") == "import" and (today - end).days > periods.PERIOD_DAYS:
            continue                                   # 旧表格里很久以前的记录，中间断没断不知道，不自动续
        k = (today - end).days // periods.PERIOD_DAYS
        cur = end + timedelta(days=periods.PERIOD_DAYS * k)
        paid = date.fromisoformat(r["paid_until"]) if r.get("paid_until") else None
        prepaid = bool(paid and paid > cur)            # 截图显示一次买了多个月，确定还在付费期内
        if not prepaid and (today - cur).days < periods.RENEW_BUFFER_DAYS:
            continue                                   # 刚到期没几天，先等等，防止名单更新延迟造成误判
                                                       # （漏掉的期也等这几天，和本期一起补，保证最新一条是本期）
        # ③-1 服务停过、中间漏掉的期：一期一份礼物，补登到各自原本的月份
        for j in range(max(0, k - MAX_BACKFILL), k):
            missed = end + timedelta(days=periods.PERIOD_DAYS * j)
            if db.period_record(missed, uid=uid, statuses=("verified", "pending", "rejected")):
                continue                               # 已有记录（含主播核实后拒绝的），不再补
            m = db.free_month(r["qq"], missed.strftime("%Y-%m"))
            base = dict(bili_uid=uid, bili_name=g.get("name") or r["bili_name"], guard_level=r["guard_level"],
                        source="auto", contact=r.get("contact"), paid_until=r.get("paid_until"), date_source="auto")
            if paid and paid > missed:                 # 一次买了多个月，这期确定付过钱
                db.upsert_guard(r["qq"], m, missed.isoformat(), "verified", **base,
                                note=f"补登：{missed} 开始的这一期在服务停止期间开始，当时没能自动登记"
                                     "（一次买了多个月，仍在付费期内）")
                logs.append(f"补登：{g.get('name') or uid}（UID {uid}）服务停止期间漏掉的一期（{missed} 开始），"
                            f"已记入 {m} 发货表")
            else:                                      # 按月续费的：名单只能说明他现在在舰，中间断没断过不知道
                db.upsert_guard(r["qq"], m, missed.isoformat(), "pending", **base,
                                note=f"补登待核实：{missed} 开始的这一期在服务停止期间开始，当时没能自动登记。"
                                     "他现在仍在舰长名单，但无法确认这期间是否一直在舰，请核实后通过或拒绝")
                logs.append(f"补登待核实：{g.get('name') or uid}（UID {uid}）服务停止期间漏掉的一期（{missed} 开始），"
                            f"已记入 {m} 发货表，状态为待审核，请核实后通过或拒绝")
        if db.period_record(cur, uid=uid, statuses=("verified", "pending")):
            continue
        month = db.free_month(r["qq"], this_month)
        how = "一次买了多个月，仍在付费期内" if prepaid else "上一期结束后仍在舰长名单（自动续费或多月购买）"
        db.upsert_guard(r["qq"], month, cur.isoformat(), "verified", bili_uid=uid,
                        bili_name=g.get("name") or r["bili_name"], guard_level=g.get("level") or r["guard_level"],
                        source="auto", contact=r.get("contact"), paid_until=r.get("paid_until"),
                        date_source="auto", note=f"自动续登：{how}")
        logs.append(f"自动续登：{g.get('name') or uid}（UID {uid}）新的舰长月从 {cur} 开始，记入 {month} 发货表")
    return logs
