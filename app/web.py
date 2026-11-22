"""网页部分：观众自助登记 / 填写修改地址 + 主播后台"""
import asyncio
import csv
import io
import re
import secrets
import time
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db, notify, verify
from .config import RES, SHOT_DIR, STREAMER, WEB

router = APIRouter()
templates = Jinja2Templates(directory=str(RES / "app" / "templates"))
security = HTTPBasic()

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
STATUS_CN = {"verified": "已验证", "pending": "待审核", "rejected": "已拒绝"}


def admin(cred: HTTPBasicCredentials = Depends(security)):
    if not secrets.compare_digest(cred.password.encode(), str(WEB["admin_password"]).encode()):
        raise HTTPException(401, "密码错误", headers={"WWW-Authenticate": "Basic"})
    return True


def _contact_cfg() -> dict:
    """登记页“联系方式”一项（标题 / 是否必填 / 提示文字），主播可在设置里改"""
    return {"label": (WEB.get("contact_label") or "").strip() or "QQ / 微信",
            "required": bool(WEB.get("contact_required")),
            "hint": (WEB.get("contact_hint") or "").strip()}


def _page(request, name, **ctx):
    ctx.setdefault("streamer", STREAMER["name"])
    ctx.setdefault("contact", _contact_cfg())
    return templates.TemplateResponse(request, name, ctx)


# ======================= 观众自助登记 =======================
COOKIE = "gg_t"                         # 记住观众身份（同一台手机再打开就能直接修改）
MAX_UPLOAD = 8 * 1024 * 1024
_hits: dict[str, list] = defaultdict(list)
_ocr_slots = asyncio.Semaphore(2)       # 同时最多识别 2 张截图，避免电脑卡死
_tickets: dict[str, dict] = {}          # 地址修改申请的临时凭证


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _rate_ok(ip: str, limit=15, window=600) -> bool:
    now = time.time()
    _hits[ip] = [t for t in _hits[ip] if now - t < window]
    if len(_hits[ip]) >= limit:
        return False
    _hits[ip].append(now)
    return True


def _remember(resp, token):
    resp.set_cookie(COOKIE, token, max_age=db.month_end_hours() * 3600, httponly=True, samesite="lax")
    return resp


def _check_addr(receiver, phone, address):
    errors = []
    if not (1 <= len(receiver) <= 20):
        errors.append("请填写收件人姓名")
    if not PHONE_RE.match(phone):
        errors.append("手机号格式不正确（需11位大陆手机号）")
    if not (8 <= len(address) <= 200):
        errors.append("请填写完整的收货地址（省市区 + 详细地址）")
    return errors


def _home(request, **ctx):
    ctx.setdefault("errors", [])
    ctx.setdefault("form", {})
    ctx.setdefault("me", None)
    ctx.setdefault("today", date.today().isoformat())
    ctx.setdefault("month", date.today().month)
    return _page(request, "home.html", **ctx)


@router.get("/")
async def home(request: Request):
    me = None
    t = request.cookies.get(COOKIE)
    owner = db.qq_of_token(t) if t else None
    if owner:
        rec = next((r for r in db.guards_of_qq(owner) if r["month"] == db.this_month()), None)
        me = {"t": t, "guard": db.editable_guard(owner), "rec": rec}
    return _home(request, me=me)


@router.post("/verify")
async def verify_submit(request: Request, uid: str = Form(""), contact: str = Form(""),
                        no_shot: str = Form(""), claimed: str = Form(""),
                        shot: UploadFile | None = File(None)):
    form = {"uid": uid, "contact": contact, "no_shot": bool(no_shot), "claimed": claimed}
    uid = re.sub(r"\D", "", uid)
    if not (3 <= len(uid) <= 16):
        return _home(request, form=form, errors=["请填写正确的 B站 UID（一串数字，在 B站 App「我的」→ 点头像 可以看到）"])
    cc = _contact_cfg()
    if cc["required"] and len(contact.strip()) < 2:
        return _home(request, form=form, errors=[f"请填写{cc['label']}"])
    if not _rate_ok(_ip(request)):
        return _home(request, form=form, errors=["提交太频繁了，请 10 分钟后再试。"])
    img = None
    if not no_shot:
        if shot is None or not shot.filename:
            return _home(request, form=form, errors=["请上传上舰截图，或者勾选「我没有截图」并填写开通日期。"])
        img = await shot.read(MAX_UPLOAD + 1)
        if len(img) > MAX_UPLOAD:
            return _home(request, form=form, errors=["截图太大了（超过 8MB），请重新截图后上传。"])
    claimed_day = None
    if no_shot:
        try:
            claimed_day = date.fromisoformat(claimed)
        except ValueError:
            return _home(request, form=form, errors=["请填写开通日期。"])

    async with _ocr_slots:
        res = await verify.web_verify(uid, img, claimed_day, contact)
    r = res["result"]
    if r in ("retry", "rejected"):
        return _home(request, form=form, errors=[res["msg"]])

    if r == "exists":
        g = res["guard"]
        cookie_t = request.cookies.get(COOKIE) or ""
        if db.qq_of_token(cookie_t) == g["qq"]:   # 本人（同一台手机）→ 直接去修改
            return RedirectResponse(f"/fill?t={cookie_t}", status_code=303)
        ticket = secrets.token_urlsafe(12)
        _tickets[ticket] = {"qq": g["qq"], "uid": uid, "exp": time.time() + 1800}
        return _page(request, "request.html", ticket=ticket, msg=res["msg"], errors=[], addr={})

    token = db.new_token(db.web_owner(uid), db.month_end_hours())
    if r == "verified":
        return _remember(RedirectResponse(f"/fill?t={token}", status_code=303), token)
    return _remember(_page(request, "message.html", title="已提交，等待主播确认", msg=res["msg"], home=True), token)


@router.post("/request")
async def request_submit(request: Request, ticket: str = Form(""), receiver: str = Form(""),
                         phone: str = Form(""), address: str = Form("")):
    tk = _tickets.get(ticket)
    if not tk or tk["exp"] < time.time():
        return _page(request, "message.html", title="页面已过期", msg="请回到登记页面重新验证。", home=True)
    receiver, phone, address = receiver.strip(), re.sub(r"\D", "", phone), address.strip()
    errors = _check_addr(receiver, phone, address)
    if errors:
        return _page(request, "request.html", ticket=ticket, msg="", errors=errors,
                     addr={"receiver": receiver, "phone": phone, "address": address})
    _tickets.pop(ticket, None)
    db.add_request(tk["qq"], tk["uid"], receiver, phone, address, note=f"网页提交，IP {_ip(request)}")
    return _page(request, "message.html", title="修改申请已提交", ok=True,
                 msg="主播确认后，新的收货信息就会生效。感谢上舰！")


# ======================= 观众填写 =======================
def _check_token(t):
    qq = db.qq_of_token(t or "")
    if not qq:
        return None, None, "链接无效或已过期。请重新打开登记页面验证（或私聊机器人发送「填地址」）。"
    g = db.editable_guard(qq)
    if not g:
        return qq, None, "系统查询到你目前不在舰期内（或还没通过验证），暂时不能填写或修改地址。如有疑问请联系主播。"
    return qq, g, None


@router.get("/fill")
async def fill_page(request: Request, t: str = ""):
    qq, g, err = _check_token(t)
    if err:
        return _page(request, "message.html", title="无法填写", msg=err, home=True)
    addr = dict(db.get_address(qq) or {})
    draft = db.DRAFTS.get(qq)
    if draft:
        addr.update(draft)
    gifts = " + ".join(f"{x}礼物" for x in db.gifts_of(g))
    resp = _page(request, "fill.html", t=t, guard=g, addr=addr, errors=[], draft=bool(draft), gifts=gifts)
    return _remember(resp, t)


@router.post("/fill")
async def fill_submit(request: Request, t: str = Form(""), receiver: str = Form(""),
                      phone: str = Form(""), address: str = Form("")):
    qq, g, err = _check_token(t)          # 提交时再查一次，防止链接留到下个月再改
    if err:
        return _page(request, "message.html", title="无法提交", msg=err)
    receiver, phone, address = receiver.strip(), re.sub(r"\D", "", phone), address.strip()
    errors = _check_addr(receiver, phone, address)
    if errors:
        return _page(request, "fill.html", t=t, guard=g, errors=errors,
                     gifts=" + ".join(f"{x}礼物" for x in db.gifts_of(g)),
                     addr={"receiver": receiver, "phone": phone, "address": address})
    first = db.get_address(qq) is None
    db.save_address(qq, receiver, phone, address)
    return _page(request, "message.html", title="提交成功", ok=True, home=True, join_code=g.get("join_code"),
                 msg=("地址已保存，礼物会寄到这里，感谢上舰！" if first else "地址已更新！")
                 + " 之后需要修改的话，用这台手机重新打开登记页面即可。")


# ======================= 主播后台 =======================
@router.get("/admin")
async def admin_page(request: Request, month: str = "", status: str = "", _=Depends(admin)):
    month = month or db.this_month()
    rows = db.list_guards(month if month != "all" else None, status or None)
    for r in rows:                     # 给人看的字段（和桌面版一致）
        r["who"] = db.contact_text(r)
        r["gifts_text"] = " + ".join(db.gifts_of(r))
        r["auto"] = r["status"] == "verified" and r.get("source") == "auto"
        r["shipping"] = db.shipping_view(r)
    months = sorted(set(db.all_months()) | {db.this_month()}, reverse=True)
    stat = {
        "total": len(rows),
        "verified": sum(r["status"] == "verified" for r in rows),
        "pending": sum(r["status"] == "pending" for r in rows),
        "no_addr": sum(r["status"] == "verified" and not r["address"] for r in rows),
    }
    return _page(request, "admin.html", rows=rows, months=months, month=month, status=status,
                 stat=stat, status_cn=STATUS_CN, shipping_views=db.SHIPPING_VIEWS, today=date.today().isoformat())


@router.post("/admin/guard/{gid}")
async def admin_guard_action(gid: int, action: str = Form(...), back: str = Form("/admin"),
                             _=Depends(admin)):
    g = db.get_guard(gid)
    if action == "approve":
        db.set_guard_status(gid, "verified", "主播人工审核通过")
        if g and g["month"] == db.this_month():
            asyncio.create_task(notify.send_private(
                g["qq"], f"🎉 你的舰长身份（{g['start_date']} 上舰）已通过主播审核！发送「改地址」即可填写收货信息。"))
    elif action == "reject":
        db.set_guard_status(gid, "rejected", "主播人工拒绝")
        if g:
            asyncio.create_task(notify.send_private(
                g["qq"], "很抱歉，你提交的舰长信息没有通过主播审核。如有疑问请直接联系主播。"))
    elif action == "ship":
        db.set_shipped(gid, True)
    elif action == "unship":
        db.set_shipped(gid, False)
    elif action == "delete":
        db.delete_guard(gid)
    return RedirectResponse(back if back.startswith("/admin") else "/admin", status_code=303)


@router.post("/admin/add")
async def admin_add(qq: str = Form(...), start_date: str = Form(...), bili_name: str = Form(""),
                    bili_uid: str = Form(""), guard_level: str = Form("舰长"), _=Depends(admin)):
    qq = re.sub(r"\D", "", qq)
    if not qq:
        raise HTTPException(400, "QQ号不能为空")
    date.fromisoformat(start_date)
    db.upsert_guard(qq, start_date[:7], start_date, "verified", bili_uid=bili_uid or None,
                    bili_name=bili_name or None, guard_level=guard_level, source="manual",
                    note="主播手动添加")
    return RedirectResponse(f"/admin?month={start_date[:7]}", status_code=303)


@router.post("/admin/address")
async def admin_address(qq: str = Form(...), receiver: str = Form(...), phone: str = Form(...),
                        address: str = Form(...), back: str = Form("/admin"), _=Depends(admin)):
    db.save_address(qq, receiver.strip(), re.sub(r"\D", "", phone), address.strip())
    return RedirectResponse(back if back.startswith("/admin") else "/admin", status_code=303)


@router.get("/admin/export.csv")
async def admin_export(month: str = "", status: str = "", _=Depends(admin)):
    month = month or db.this_month()
    rows = db.list_guards(month if month != "all" else None, status or "verified")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["月份", "本期开始", "等级", "应寄礼物", "B站昵称", "B站UID", "QQ/联系方式", "收件人", "手机号",
                "收货地址", "已发货"])
    for r in rows:
        if r["status"] != "verified":
            continue
        w.writerow([r["month"], r["start_date"], r["guard_level"], " + ".join(db.gifts_of(r)), r["bili_name"] or "",
                    r["bili_uid"] or "", db.contact_text(r), r["receiver"] or "", r["phone"] or "",
                    r["address"] or "", "是" if r["shipped"] else "否"])
    data = "﻿" + buf.getvalue()           # 带 BOM，Excel 直接打开不乱码
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=guards_{month}.csv"})


@router.get("/admin/shot/{name}")
async def admin_shot(name: str, _=Depends(admin)):
    p = (SHOT_DIR / name).resolve()
    if p.parent != SHOT_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ======================= 图形界面调用的接口 =======================
@router.get("/api/status")
async def api_status(_=Depends(admin)):
    from nonebot import get_bots
    from . import bili
    data = bili._cache["data"]
    from .config import QQ
    return {"bots": list(get_bots().keys()), "qq_enabled": bool(QQ.get("enabled")),
            "pending_requests": db.count_pending_requests(),
            "guards": len(data) if data is not None else None,
            "guards_time": bili._cache["t"]}


@router.post("/api/refresh_guards")
async def api_refresh(_=Depends(admin)):
    from . import bili
    data = await bili.guard_list(force=True)
    return {"guards": len(data) if data is not None else None}


@router.post("/api/notify")
async def api_notify(qq: str = Form(...), text: str = Form(...), _=Depends(admin)):
    return {"ok": await notify.send_private(qq, text)}


@router.post("/api/remind")
async def api_remind(_=Depends(admin)):
    """私聊提醒本月已验证但还没填地址的舰长"""
    rows = [r for r in db.list_guards(db.this_month(), "verified") if not r["address"]]
    ok = 0
    for r in rows:
        ok += await notify.send_private(
            r["qq"], f"你好～你是本月「{STREAMER['name']}」的{r['guard_level'] or '舰长'}，"
                     "还没有填写礼物收货地址哦。私聊我发送「填地址」即可填写～")
        await asyncio.sleep(1.5)            # 发太快容易被 QQ 风控
    return {"total": len(rows), "sent": ok}


