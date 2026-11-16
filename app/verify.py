"""截图识别 + 舰长校验

判断规则（见 periods.py）：不要求“开通时间在本月”，而是看“今天是否在他的某个舰长月里”。
一次买多个月、自动续费的舰长都能正常验证；每个舰长月登记一条记录（= 一份礼物）。
"""
import asyncio
import hashlib
import re
from datetime import date, datetime

from . import bili, db, periods
from .config import BILI, SHOT_DIR, STREAMER

LEVELS = ["总督", "提督", "舰长"]
START_KW = ["开通", "购买", "支付", "下单", "上舰", "交易", "创建", "续费", "时间"]
EXPIRE_KW = ["到期", "有效期", "过期", "截止"]
DATE_RE = re.compile(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})")
UID_RE = re.compile(r"UID\s*[:：]?\s*(\d{3,})", re.I)

_engine = None


def _ocr(path) -> list[str]:
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        _engine = RapidOCR()
    r = _engine(str(path))
    return [t for t in (r.txts or ()) if t and t.strip()]


def _dates_in(s):
    out = []
    for y, m, d in DATE_RE.findall(s):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def analyze(lines: list[str]) -> dict:
    """从 OCR 文本里提取：是否本直播间、舰长等级、开通时间、到期时间、UID"""
    full = "\n".join(lines)
    compact = full.replace(" ", "")
    has_streamer = any(k and k.replace(" ", "") in compact for k in STREAMER.get("keywords", []))
    level = next((lv for lv in LEVELS if lv in compact), None)
    today = date.today()

    opened = expire = None
    for i, line in enumerate(lines):
        ctx = (lines[i - 1] if i else "") + line   # 关键词和日期可能被识别成两行
        ds = _dates_in(line)
        if not ds:
            continue
        if expire is None and any(k in ctx for k in EXPIRE_KW):
            expire = ds[0]
        elif opened is None and any(k in ctx for k in START_KW):
            opened = ds[0]
    if opened is None and expire is None:                # 没有关键词，只有一个日期：当作开通时间
        past = [d for d in _dates_in(full) if d <= today]
        opened = max(past) if past else None
    if opened and (opened > today or (today - opened).days > 800):
        opened = None
    if expire and (expire - today).days > 800:
        expire = None

    return {
        "has_streamer": has_streamer,
        "level": level,
        "opened": opened,
        "expire": expire,
        "uids": UID_RE.findall(full),
        "text": full,
        "compact": compact,
    }


def resolve(opened: date | None, expire: date | None, in_list: bool | None) -> dict:
    """根据开通 / 到期时间 + 是否在 B站舰长名单，算出今天所在的舰长月。
    返回 {"ok", "start": 本期开始日, "paid_until": 已付费到哪天, "kind": nodate/expired, "msg"}"""
    today = date.today()
    if not opened and not expire:
        return {"ok": False, "kind": "nodate",
                "msg": "截图里没找到开通时间或到期时间，请换一张能看到时间的截图。"}
    if expire and expire <= today and not in_list:
        return {"ok": False, "kind": "expired",
                "msg": f"截图显示你的舰长已在 {expire} 到期，目前不在舰。续舰后再来吧～"}
    active = bool(in_list) or bool(expire and expire > today)
    start = periods.current_period(opened, expire, today, active)
    if start is None:
        why = "暂时无法连接 B站 核对" if in_list is None else "B站舰长名单里也没有找到你"
        return {"ok": False, "kind": "expired",
                "msg": f"截图显示的开通时间是 {opened}，已经超过一个舰长月，{why}，可能已经到期。"
                       "如果你是一次买了好几个月，请换一张能看到「到期时间」的截图。"}
    return {"ok": True, "start": start, "paid_until": expire if expire and expire > today else None}


def period_text(start: date, paid_until: date | None) -> str:
    return f"本期舰长从 {start} 开始" + (f"，已付费到 {paid_until}" if paid_until else "")


async def read_shot(qq, img: bytes):
    """保存截图并 OCR。返回 (info, 文件名, hash, 错误提示)"""
    h = hashlib.sha256(img).hexdigest()
    if db.hash_used_by_other(h, qq):
        return None, None, h, "这张截图已经被其他QQ号使用过，请发送你自己的上舰截图。"
    path = SHOT_DIR / f"{qq}_{datetime.now():%Y%m%d%H%M%S%f}.png"
    path.write_bytes(img)
    lines = await asyncio.to_thread(_ocr, path)
    return analyze(lines), path.name, h, None


def _drop(name):
    if name:
        (SHOT_DIR / name).unlink(missing_ok=True)


def _match_api(info, guards: dict):
    for uid in info["uids"]:
        if uid in guards:
            return guards[uid]
    # 先把主播名从文字里去掉，避免匹配到名字里带主播名的观众
    text = info["compact"]
    for k in sorted([STREAMER.get("name", "")] + list(STREAMER.get("keywords", [])), key=len, reverse=True):
        if k:
            text = text.replace(k.replace(" ", ""), "\0")
    # 名字越长越可信，优先匹配长名字，避免短名字误命中
    for g in sorted(guards.values(), key=lambda g: -len(g["name"])):
        n = g["name"].replace(" ", "")
        if len(n) >= 2 and n in text:
            return g
    return None


def _save(owner, start: date, status, *, paid_until=None, date_source="screenshot", **kw):
    """登记一个舰长月。同一个舰长月已经有记录就更新那条；否则记到本月发货表（本月已有别的舰长月就顺延）"""
    same = db.period_record(start, owner=owner)
    month = same["month"] if same else db.free_month(owner, db.this_month())
    return db.upsert_guard(owner, month, start.isoformat(), status, paid_until=paid_until and paid_until.isoformat(),
                           date_source=date_source, **kw)


async def verify_screenshot(qq, img: bytes) -> tuple[str, str, dict | None]:
    """群里/私聊直接发截图。返回 (结果, 回复文字, 舰长记录)；结果: verified / pending / rejected / not_guard"""
    info, name, h, err = await read_shot(qq, img)
    if err:
        return "rejected", err, None

    if not info["level"] or not info["has_streamer"]:
        _drop(name)
        miss = "舰长/提督/总督字样" if not info["level"] else f"主播「{STREAMER['name']}」的名字"
        return "not_guard", f"没能在截图里识别到{miss}，请发送清晰、完整的上舰截图（需包含主播名和开通 / 到期时间）。", None

    guards = await bili.guard_list() if BILI.get("check_api", True) else None
    matched = _match_api(info, guards) if guards else None
    in_list = True if matched else (None if guards is None else False)
    res = resolve(info["opened"], info["expire"], in_list)
    if not res["ok"]:
        _drop(name)
        return ("not_guard" if res["kind"] == "nodate" else "rejected"), res["msg"], None
    start, paid = res["start"], res["paid_until"]

    same = db.period_record(start, owner=qq, statuses=("verified",))
    if same:
        _drop(name)
        return "verified", f"你这个舰长月已经验证过了（{period_text(start, paid)}）。", same
    common = dict(screenshot=name, shot_hash=h, ocr_text=info["text"][:2000], paid_until=paid)
    if matched:
        if db.uid_used_by_other(matched["uid"], qq, start):
            return "rejected", "该B站账号这个舰长月已经被其他QQ号验证过了，如有疑问请联系主播。", None
        g = _save(qq, start, "verified", bili_uid=matched["uid"], bili_name=matched["name"],
                  guard_level=matched["level"], source="api", **common)
        return "verified", f"验证通过！{matched['name']}（{matched['level']}）\n{period_text(start, paid)}", g

    g = _save(qq, start, "pending", guard_level=info["level"], bili_uid=info["uids"][0] if info["uids"] else None,
              source="screenshot", note="截图识别通过，但未在B站在舰列表中匹配到", **common)
    if g["status"] == "verified":
        return "verified", f"你已经验证过了（{period_text(start, paid)}）。", g
    return "pending", "截图已收到，但暂时没在直播间舰长列表里匹配到你的B站账号。", g


async def dialog_shot(qq, img: bytes, st: dict) -> tuple[str, str, dict | None]:
    """私聊问答中，已经问到 UID 后对方发来的截图。
    返回 (结果, 文字, 记录)；结果: verified / pending / retry / nodate / old（已到期）"""
    info, name, h, err = await read_shot(qq, img)
    if err:
        return "retry", err, None
    if not info["level"]:
        _drop(name)
        return "retry", "截图里没识别到「舰长/提督/总督」字样，请换一张更清晰、完整的截图。", None
    if not info["has_streamer"]:
        _drop(name)
        return "retry", f"截图里没看到主播「{STREAMER['name']}」的名字，请发送本直播间的上舰截图。", None
    res = resolve(info["opened"], info["expire"], st.get("in_list"))
    if not res["ok"]:
        _drop(name)
        if res["kind"] == "nodate":
            return "nodate", "截图里没找到开通时间或到期时间。请换一张能看到时间的截图，或者回复「没有」。", None
        return "old", res["msg"], None
    start, paid = res["start"], res["paid_until"]

    uid, gname = st.get("uid"), st.get("name")
    name_hit = (uid in info["uids"]) or (gname and gname.replace(" ", "") in info["compact"])
    common = dict(bili_uid=uid, bili_name=gname, guard_level=st.get("level") or info["level"],
                  screenshot=name, shot_hash=h, ocr_text=info["text"][:2000], paid_until=paid)
    if st.get("in_list"):
        g = _save(qq, start, "verified", source="dialog",
                  note=f"私聊问答：UID在舰长列表 + 截图{'（含本人昵称）' if name_hit else ''}", **common)
        return "verified", "", g
    g = _save(qq, start, "pending", source="dialog", note="私聊问答：截图在舰期内，但UID不在当前舰长列表", **common)
    return "pending", "", g


async def bind_uid(qq, uid: str) -> tuple[bool, str]:
    """群里「绑定 UID」：在在舰列表里就直接通过"""
    guards = await bili.guard_list()
    if guards is not None and uid not in guards:
        guards = await bili.guard_list(force=True)     # 刚上舰的人缓存里可能还没有
    if guards is None:
        return False, "暂时无法连接B站，请稍后再试或等待主播人工审核。"
    g = guards.get(uid)
    if not g:
        return False, "该UID目前不在本直播间的舰长列表中（刚上舰可能有几分钟延迟，请稍后再试）。"
    # 必须先发过在舰期内的上舰截图，防止有人直接冒用别人的 UID
    rec = db.period_record(date.today(), owner=qq)
    if not rec:
        return False, "请先发送你的上舰截图（能看到开通 / 到期时间），再绑定UID。"
    start = date.fromisoformat(rec["start_date"])
    if db.uid_used_by_other(uid, qq, start):
        return False, "该B站账号这个舰长月已经被其他QQ号验证过了，如有疑问请联系主播。"
    db.upsert_guard(qq, rec["month"], rec["start_date"], "verified", bili_uid=uid, bili_name=g["name"],
                    guard_level=g["level"], source="api", note="UID绑定核验")
    return True, f"验证通过！{g['name']}（{g['level']}），本期舰长从 {rec['start_date']} 开始"


async def web_verify(uid: str, img: bytes | None, claimed: date | None, contact: str = "") -> dict:
    """观众自助网页登记。
    返回 {"result": verified / pending / exists / rejected / retry, "msg": 提示, "guard": 记录}
      exists = 这个 B站账号的当前舰长月已经登记过（为防止别人改地址，修改需要主播确认）"""
    owner = db.web_owner(uid)
    contact = (contact or "").strip()[:40] or None

    guards = await bili.guard_list() if BILI.get("check_api", True) else None
    if guards is not None and uid not in guards:
        guards = await bili.guard_list(force=True)
    in_list = None if guards is None else uid in guards
    g_api = (guards or {}).get(uid) or {}

    level, shot, paid = g_api.get("level"), {}, None
    if img:
        try:
            info, name, h, err = await read_shot(owner, img)
        except Exception:                                     # noqa: BLE001  不是图片 / 图片损坏
            return {"result": "retry", "msg": "图片无法识别，请上传 jpg / png 格式的截图。"}
        if err:
            return {"result": "rejected", "msg": err}
        if not info["level"]:
            _drop(name)
            return {"result": "retry", "msg": "截图里没识别到「舰长 / 提督 / 总督」字样，请换一张更清晰、完整的截图。"}
        if not info["has_streamer"]:
            _drop(name)
            return {"result": "retry", "msg": f"截图里没看到主播「{STREAMER['name']}」的名字，请上传本直播间的上舰截图。"}
        res = resolve(info["opened"], info["expire"], in_list)
        if not res["ok"]:
            _drop(name)
            if res["kind"] == "nodate":
                return {"result": "retry", "msg": res["msg"] + "或者勾选「我没有截图」并填写开通日期。"}
            return {"result": "rejected", "msg": res["msg"]}
        start, paid = res["start"], res["paid_until"]
        name_hit = (uid in info["uids"]) or (g_api.get("name") and g_api["name"].replace(" ", "") in info["compact"])
        shot = dict(screenshot=name, shot_hash=h, ocr_text=info["text"][:2000])
        how = "截图" + ("（含本人昵称）" if name_hit else "")
        level = level or info["level"]
        src = "screenshot"
    else:
        if not claimed:
            return {"result": "retry", "msg": "请上传上舰截图，或者勾选「我没有截图」并填写开通日期。"}
        if claimed > date.today():
            return {"result": "retry", "msg": "开通日期不能晚于今天。"}
        start = periods.current_period(claimed, None, date.today(), bool(in_list))
        if start is None:
            return {"result": "rejected", "msg": f"你填写的开通日期是 {claimed}，已经超过一个舰长月，而且 B站舰长名单里没有找到你，"
                                                 "可能已经到期。续舰后再来吧～"}
        how, src = f"无截图，自述 {claimed} 开通", "claimed"

    existing = db.period_record(start, uid=uid, statuses=("verified",))
    if (existing and existing["qq"] == owner and existing.get("source") == "manual"
            and not db.get_address(owner)):
        return {"result": "verified", "guard": existing, "msg": "验证通过！"}   # 主播手动添加了 UID、还没填过地址
    if existing:
        return {"result": "exists", "guard": existing,
                "msg": "这个 B站账号这个月已经登记过了。为了防止别人冒用你的账号修改地址，"
                       "这次修改需要主播确认后才会生效。"}

    common = dict(bili_uid=uid, bili_name=g_api.get("name"), guard_level=level, source="web", contact=contact,
                  paid_until=paid, date_source=src, **shot)
    if in_list and img:
        g = _save(owner, start, "verified", note=f"网页登记：UID在舰长列表 + {how}", **common)
        return {"result": "verified", "guard": g, "msg": "验证通过！"}

    why = {True: "UID在舰长列表", False: "UID不在当前舰长列表", None: "暂时无法连接B站核对"}[in_list]
    g = _save(owner, start, "pending", note=f"网页登记：{how}；{why}", **common)
    if g["status"] == "verified":
        return {"result": "verified", "guard": g, "msg": "验证通过！"}
    return {"result": "pending", "guard": g,
            "msg": "资料已提交，需要主播人工确认（" + why + "）。确认后，用这台手机再打开本页面就能填写收货信息。"}
