"""以前几个月的表格：导入 + 查询 + 对比 + 信息检查（纯逻辑，不依赖界面）

导入：支持本软件导出的 Excel / CSV，也支持主播自己做的表格。
     自动认表头（昵称 / UID / 收件人 / 手机号 / 地址 …），认不出来的列可以在界面上手动指定。
     导入的每一行 = 那个月的一条舰长记录（已验证，来源“表格导入”），连同当时的收件信息一起保存。
"""
import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, periods

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# ---------------------------------------------------------------- 表头识别
# 字段: (显示名, [(同义词, 只能完全相同)])
FIELDS = {
    "month":    ("月份",      [("发货月份", 0), ("月份", 0), ("批次", 0), ("月", 1)]),
    "start":    ("上舰日期",  [("本期开始", 0), ("上舰日期", 0), ("上舰时间", 0), ("开通日期", 0), ("开通时间", 0),
                               ("续费日期", 0), ("开始日期", 0), ("日期", 1), ("时间", 1)]),
    "level":    ("等级",      [("舰长等级", 0), ("大航海等级", 0), ("等级", 0), ("身份", 1), ("类型", 1)]),
    "name":     ("B站昵称",   [("b站昵称", 0), ("b站用户名", 0), ("b站名称", 0), ("b站名", 0), ("b站id", 1),
                               ("昵称", 0), ("用户名", 0), ("名字", 1), ("id", 1)]),
    "uid":      ("B站UID",    [("b站uid", 0), ("uid", 0), ("mid", 1), ("用户id", 1)]),
    "qq":       ("QQ号",      [("qq号", 0), ("qq号码", 0), ("qq", 1)]),
    "contact":  ("联系方式",  [("qq/联系方式", 0), ("联系方式", 0), ("微信号", 0), ("微信", 0), ("vx", 1), ("wx", 1)]),
    "receiver": ("收件人",    [("收件人", 0), ("收货人", 0), ("收件人姓名", 0), ("收货人姓名", 0), ("真实姓名", 0),
                               ("姓名", 1), ("联系人", 1)]),
    "phone":    ("手机号",    [("手机号", 0), ("手机号码", 0), ("手机", 0), ("联系电话", 0), ("电话号码", 0), ("电话", 0)]),
    "address":  ("收货地址",  [("收货地址", 0), ("详细地址", 0), ("收件地址", 0), ("邮寄地址", 0), ("地址", 0)]),
    "province": ("省",        [("省份", 1), ("省", 1)]),
    "city":     ("市",        [("城市", 1), ("市", 1)]),
    "district": ("区/县",     [("区县", 1), ("区/县", 1), ("区", 1), ("县", 1)]),
    "shipped":  ("已发货",    [("已发货", 0), ("是否发货", 0), ("发货状态", 0), ("发货", 1)]),
}
FIELD_ORDER = list(FIELDS)
# 这些表头不要当成任何字段（例如“到期时间”不是上舰日期）
_IGNORE_HEAD = ("到期", "过期", "应寄", "礼物", "备注", "口令", "来源", "状态说明")


def _norm_head(h) -> str:
    s = str(h or "").strip().lower()
    s = re.sub(r"[\s（）()【】\[\]:：*]", "", s)
    return s


def _score(head: str, field: str) -> int:
    if not head or any(k in head for k in _IGNORE_HEAD):
        return 0
    best = 0
    for syn, exact in FIELDS[field][1]:
        if head == syn:
            best = max(best, 100 + len(syn))
        elif not exact and syn in head:
            best = max(best, 10 + len(syn))
    return best


def guess_mapping(headers: list) -> dict:
    """{字段: 列号}，每列最多对应一个字段，按匹配程度从高到低分配"""
    cands = []
    for j, h in enumerate(headers):
        nh = _norm_head(h)
        for f in FIELDS:
            s = _score(nh, f)
            if s:
                cands.append((s, f, j))
    cands.sort(key=lambda x: -x[0])
    mapping, used = {}, set()
    for _s, f, j in cands:
        if f not in mapping and j not in used:
            mapping[f] = j
            used.add(j)
    return mapping


_KEY_FIELDS = ("name", "uid", "receiver", "phone", "address", "qq")


def find_header(rows: list[list], scan=15) -> tuple[int, dict]:
    """在前几行里找表头行：能认出最多字段的那一行。返回 (表头行号, 映射)；认不出返回 (-1, {})"""
    best = (-1, {})
    for i, row in enumerate(rows[:scan]):
        m = guess_mapping(row)
        if sum(f in m for f in _KEY_FIELDS) >= 1 and len(m) >= 2 and len(m) > len(best[1]):
            best = (i, m)
    return best


# ---------------------------------------------------------------- 读取文件
class Sheet:
    def __init__(self, name, rows):
        self.name = name
        self.rows = [list(r) for r in rows]
        self.header_row, self.mapping = find_header(self.rows)
        self.headers = [str(x or "").strip() for x in self.rows[self.header_row]] if self.header_row >= 0 else []
        self.month = ""                                      # 这张表默认是哪个月（行里没有月份/日期时使用）

    @property
    def data(self):
        if self.header_row < 0:
            return []
        out = []
        for r in self.rows[self.header_row + 1:]:
            if any(str(x or "").strip() for x in r):
                out.append(r)
        return out

    @property
    def usable(self):
        return self.header_row >= 0


def read_file(path) -> list[Sheet]:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True, data_only=True)
        try:
            sheets = [Sheet(ws.title, ws.iter_rows(values_only=True)) for ws in wb.worksheets]
        finally:
            wb.close()
    elif ext in (".csv", ".txt"):
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("读不出这个 CSV 文件的文字编码，请用 Excel 另存为 .xlsx 再导入")
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;") if text.strip() else csv.excel
        sheets = [Sheet(p.stem, csv.reader(io.StringIO(text), dialect))]
    elif ext == ".xls":
        raise ValueError("暂不支持旧版 .xls 文件：请用 Excel 打开它，「另存为」→ 选择 “Excel 工作簿 (*.xlsx)” 后再导入")
    else:
        raise ValueError("只支持 Excel（.xlsx）或 CSV 文件")
    default = guess_month(p.stem)
    for s in sheets:
        s.month = guess_month(s.name) or default
    return sheets


# ---------------------------------------------------------------- 解析单元格
def guess_month(text, today: date | None = None) -> str:
    """从工作表名 / 文件名里猜月份：2026-03、2026年3月、202603、3月（年份取最近的那个）"""
    today = today or date.today()
    t = str(text or "")
    m = re.search(r"(20\d\d)\s*[年\-_./]\s*(\d{1,2})(?!\d)", t) or re.search(r"(20\d\d)(0[1-9]|1[0-2])(?!\d)", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1))}-{int(m.group(2)):02d}"
    m = re.search(r"(?<!\d)(\d{1,2})\s*月", t)
    if m and 1 <= int(m.group(1)) <= 12:
        mo = int(m.group(1))
        y = today.year if mo <= today.month else today.year - 1
        return f"{y}-{mo:02d}"
    return ""


def parse_date(v, month_hint: str = "") -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:      # Excel 日期序号
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    m = re.search(r"(20\d\d)\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(?<!\d)(\d{1,2})\s*[月\-/.]\s*(\d{1,2})(?!\d)", s)
    if m:
        year = int(month_hint[:4]) if month_hint else date.today().year
        try:
            return date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.fullmatch(r"(20\d\d)(\d\d)(\d\d)", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_month(v, month_hint: str = "") -> str:
    if isinstance(v, (datetime, date)):
        return f"{v.year}-{v.month:02d}"
    if isinstance(v, (int, float)) and 1 <= v <= 12 and month_hint:
        return f"{month_hint[:4]}-{int(v):02d}"
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        d = parse_date(v)
        return f"{d.year}-{d.month:02d}"
    s = str(v or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{1,2}", s) and 1 <= int(s) <= 12:
        y = month_hint[:4] if month_hint else str(date.today().year)
        return f"{y}-{int(s):02d}"
    return guess_month(s)


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _digits(v) -> str:
    return re.sub(r"\D", "", _text(v))


def _clean_contact(s: str) -> str:
    """去掉本软件导出时加的说明文字"""
    s = re.sub(r"（(网页登记|表格导入)）$", "", s).strip()
    return "" if s in ("网页登记（未留联系方式）", "网页登记", "表格导入") else s


def parse_level(v) -> str:
    s = _text(v)
    for k in ("总督", "提督", "舰长"):
        if k in s:
            return k
    return "舰长"


_YES = ("是", "已发", "已寄", "已发货", "√", "✓", "✔", "y", "yes", "true", "1", "发了", "完成")
_NO = ("否", "未", "没", "×", "x", "n", "no", "false", "0")


def parse_shipped(v, default: bool) -> bool:
    s = _text(v).lower()
    if not s:
        return default
    if any(s.startswith(k) for k in _NO):
        return False
    if any(k in s for k in _YES):
        return True
    return default


def norm_addr(s) -> str:
    return re.sub(r"[\s,，.。、\-－—#()（）]", "", str(s or ""))


# ---------------------------------------------------------------- 生成导入计划
def sheet_records(sheet: Sheet, mapping: dict, month: str) -> list[dict]:
    """把一张表按列映射转成记录（还没写数据库）"""
    out = []
    g = lambda r, f: r[mapping[f]] if f in mapping and mapping[f] < len(r) else None   # noqa: E731
    for idx, r in enumerate(sheet.data, start=1):
        name, uid = _text(g(r, "name")), _digits(g(r, "uid"))
        receiver, phone = _text(g(r, "receiver")), _digits(g(r, "phone"))
        addr = _text(g(r, "address"))
        parts = [_text(g(r, f)) for f in ("province", "city", "district")]
        prefix = ""
        for part in parts:                                  # “上海市 上海市 浦东新区” 这种重复的只留一个
            if part and part not in prefix and not addr.startswith(part):
                prefix += part
        addr = prefix + addr
        qq = _digits(g(r, "qq"))
        contact = _clean_contact(_text(g(r, "contact")))
        m_qq = re.fullmatch(r"QQ\s*(\d{5,12})", contact)
        if m_qq:                                            # 本软件导出的“QQ 123456”
            qq, contact = qq or m_qq.group(1), ""
        row_month = parse_month(g(r, "month"), month)
        start = parse_date(g(r, "start"), row_month or month)
        m = row_month or (f"{start.year}-{start.month:02d}" if start and not month else "") or month
        rec = {"sheet": sheet.name, "line": idx, "month": m, "start": start, "name": name, "uid": uid,
               "qq": qq if 5 <= len(qq) <= 12 else "", "contact": contact,
               "level": parse_level(g(r, "level")) if "level" in mapping else "",
               "receiver": receiver, "phone": phone, "address": addr, "shipped_raw": g(r, "shipped")}
        if not (name or uid or receiver or phone or qq):
            continue                                            # 空行 / 合计行
        out.append(rec)
    return out


def _owner_lookup():
    """已有记录：UID → 身份标识；昵称 → 身份标识（取最新的一条）"""
    by_uid, by_name = {}, {}
    with db.conn() as c:
        for r in c.execute("SELECT qq, bili_uid, bili_name FROM guards ORDER BY month, id"):
            if r["bili_uid"]:
                by_uid[str(r["bili_uid"])] = r["qq"]
            if r["bili_name"]:
                by_name[r["bili_name"]] = r["qq"]
    return by_uid, by_name


def plan(records: list[dict], shipped_default: bool = True) -> list[dict]:
    """给每一行决定：新增 / 已有记录跳过 / 无法导入（附原因）"""
    by_uid, by_name = _owner_lookup()
    with db.conn() as c:
        old = [dict(r) for r in c.execute("SELECT qq, month, bili_uid, bili_name, guard_level FROM guards ORDER BY month")]
    existing = {(r["qq"], r["month"]) for r in old}
    existing_names = {(r["bili_name"], r["month"]) for r in old if r["bili_name"]}
    # 同一批表格里，有的表有 UID、有的没有：按昵称把 UID 补上，算同一个人
    batch_uid = {}
    for r in records:
        if r["uid"] and r["name"]:
            batch_uid.setdefault(r["name"], r["uid"])
    # 表格没有“等级”列：沿用这个人最近一次的等级（没有就算舰长），免得被当成“等级变了”
    last_level = {}
    for r in old:
        if r["guard_level"]:
            last_level[r["bili_uid"] or r["bili_name"]] = r["guard_level"]
    for r in sorted(records, key=lambda x: x["month"] or ""):
        if r["level"]:
            last_level[r["uid"] or batch_uid.get(r["name"]) or r["name"]] = r["level"]
    seen = set()
    out = []
    for r in records:
        r = dict(r)
        r["shipped"] = parse_shipped(r.pop("shipped_raw", None), shipped_default)
        if not r["uid"] and r["name"] in batch_uid:
            r["uid"] = batch_uid[r["name"]]
        if not r["level"]:
            r["level"] = last_level.get(r["uid"]) or last_level.get(r["name"]) or "舰长"
        if r["uid"]:
            owner = by_uid.get(r["uid"]) or db.web_owner(r["uid"])
        elif r["name"] and r["name"] in by_name:
            owner = by_name[r["name"]]
        elif r["qq"]:
            owner = r["qq"]
        else:
            owner = db.IMPORT_PREFIX + (r["name"] or r["receiver"] or r["phone"])
        r["owner"] = owner
        if r["qq"] and owner != r["qq"]:
            r["contact"] = (r["contact"] + " " if r["contact"] else "") + f"QQ {r['qq']}"
        if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", r["month"] or ""):
            r["action"], r["why"] = "error", "不知道是哪个月（请给这张表填上月份）"
        elif not (r["name"] or r["uid"]):
            r["action"], r["why"] = "error", "没有 B站昵称 或 UID"
        elif (owner, r["month"]) in existing or (not r["uid"] and (r["name"], r["month"]) in existing_names):
            r["action"], r["why"] = "exists", "这个月已经有他的记录"
        elif (owner, r["month"]) in seen:
            r["action"], r["why"] = "dup", "表格里重复（同一个人同一个月出现两次）"
        else:
            r["action"], r["why"] = "new", ""
            seen.add((owner, r["month"]))
        out.append(r)
    return out


def apply(planned: list[dict], file_label: str) -> dict:
    """把计划里“新增”的写进数据库；没有收货地址的人顺便补上（取最新那个月的）"""
    added = 0
    latest_addr = {}
    for r in planned:
        if r["action"] != "new":
            continue
        start = r["start"] or date.fromisoformat(r["month"] + "-01")
        db.import_guard(r["owner"], r["month"], start.isoformat(), bili_uid=r["uid"] or None,
                        bili_name=r["name"] or None, guard_level=r["level"], contact=r["contact"] or None,
                        shipped=r["shipped"], receiver=r["receiver"] or None, phone=r["phone"] or None,
                        address=r["address"] or None, note=f"从表格导入：{file_label}（{r['sheet']} 第 {r['line']} 行）")
        added += 1
        if r["receiver"] and r["address"]:
            if r["owner"] not in latest_addr or r["month"] >= latest_addr[r["owner"]]["month"]:
                latest_addr[r["owner"]] = r
    filled = 0
    for owner, r in latest_addr.items():
        if not db.get_address(owner):
            db.save_address(owner, r["receiver"], r["phone"] or "", r["address"])
            filled += 1
    return {"added": added, "filled": filled}


# ---------------------------------------------------------------- 查询 / 对比 / 检查
LIVE = ("verified", "pending")


def person_key(r) -> str:
    return f"uid:{r['bili_uid']}" if r.get("bili_uid") else f"owner:{r['qq']}"


def person_name(r) -> str:
    return r.get("bili_name") or db.contact_text(r)


def ship_info(r) -> tuple[str, str, str]:
    return (r.get("receiver") or "").strip(), (r.get("phone") or "").strip(), norm_addr(r.get("address"))


def search(rows, kw: str) -> list[dict]:
    kw = kw.strip().lower()
    if not kw:
        return rows
    keys = ("month", "start_date", "bili_name", "bili_uid", "qq", "contact", "join_code", "receiver", "phone",
            "address", "guard_level", "note")
    return [r for r in rows if any(kw in str(r.get(k) or "").lower() for k in keys)]


def _by_person(rows, month):
    out = {}
    for r in rows:
        if r["month"] == month and r["status"] in LIVE:
            out.setdefault(person_key(r), r)
    return out


def _diff_ship(a, b) -> list[str]:
    ra, pa, aa = ship_info(a)
    rb, pb, ab = ship_info(b)
    ch = []
    if ra and rb and ra != rb:
        ch.append(f"收件人 {ra} → {rb}")
    if pa and pb and pa != pb:
        ch.append(f"手机号 {pa} → {pb}")
    if aa and ab and aa != ab:
        ch.append(f"地址 {a.get('address')} → {b.get('address')}")
    return ch


def compare_months(rows, a: str, b: str) -> list[dict]:
    """a = 较早的月份，b = 较晚的月份。结果：新上舰 / 没续 / 连续在舰（含等级、收件信息变化）"""
    A, B = _by_person(rows, a), _by_person(rows, b)
    out = []
    for k, rb in B.items():
        if k not in A:
            out.append({"kind": "新上舰", "r": rb, "detail": f"{b} 有、{a} 没有"})
    for k, ra in A.items():
        if k not in B:
            out.append({"kind": "没续", "r": ra, "detail": f"{a} 有、{b} 没有"})
    for k, rb in B.items():
        if k in A:
            ra = A[k]
            notes = []
            if (ra.get("guard_level") or "舰长") != (rb.get("guard_level") or "舰长"):
                notes.append(f"等级 {ra.get('guard_level') or '舰长'} → {rb.get('guard_level') or '舰长'}")
            notes += _diff_ship(ra, rb)
            out.append({"kind": "连续在舰", "r": rb, "detail": "；".join(notes) or "没有变化", "changed": bool(notes)})
    order = {"新上舰": 0, "没续": 1, "连续在舰": 2}
    out.sort(key=lambda x: (order[x["kind"]], not x.get("changed"), person_name(x["r"])))
    return out


def address_changes(rows) -> list[dict]:
    """同一个人在不同月份留的收件信息不一样"""
    groups = defaultdict(list)
    for r in rows:
        if r["status"] in LIVE:
            groups[person_key(r)].append(r)
    out = []
    for recs in groups.values():
        recs.sort(key=lambda r: (r["month"], r["id"]))
        prev = None
        for r in recs:
            if not any(ship_info(r)):
                continue
            if prev is not None:
                ch = _diff_ship(prev, r)
                if ch:
                    out.append({"r": r, "from": prev["month"], "to": r["month"], "detail": "；".join(ch)})
            prev = r
    out.sort(key=lambda x: (x["to"], person_name(x["r"])), reverse=True)
    return out


def bili_compare(rows, guards: dict, today: date | None = None) -> list[dict]:
    """和 B站 当前舰长名单对比（本月发货表 + 当前覆盖期）"""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    live = [r for r in rows if r["status"] in LIVE]
    covered = set()
    for r in live:
        if r.get("bili_uid") and (r["month"] == month or periods.covers(
                date.fromisoformat(r["start_date"]), today,
                date.fromisoformat(r["paid_until"]) if r.get("paid_until") else None, r.get("paused_days"))):
            covered.add(str(r["bili_uid"]))
    out = []
    names = {}
    for r in sorted(live, key=lambda r: r["month"]):
        if r.get("bili_uid"):
            names[str(r["bili_uid"])] = r
    for uid, g in guards.items():
        if uid not in covered:
            last = names.get(uid)
            hint = f"上一次登记：{last['month']}" if last else "以前没有登记过"
            out.append({"kind": "在舰但没登记", "name": g.get("name") or "", "uid": uid, "level": g.get("level"),
                        "detail": hint, "r": last})
    for r in live:
        if r["month"] != month:
            continue
        uid = str(r.get("bili_uid") or "")
        if not uid:
            out.append({"kind": "没有 UID", "name": person_name(r), "uid": "", "level": r.get("guard_level"),
                        "detail": "记录里没有 B站 UID，没法和名单核对", "r": r})
        elif uid not in guards:
            out.append({"kind": "已不在名单", "name": person_name(r), "uid": uid, "level": r.get("guard_level"),
                        "detail": "本月发货表里有他，但现在不在舰长名单（可能已经到期，属正常情况；也可能登记有误）", "r": r})
        elif periods.is_higher(guards[uid].get("level"), r.get("guard_level")):
            out.append({"kind": "等级不一致", "name": person_name(r), "uid": uid, "level": r.get("guard_level"),
                        "detail": f"名单里是{guards[uid]['level']}，记录是{r.get('guard_level') or '舰长'}"
                                  "（服务运行时会自动升级合并）", "r": r})
    order = {"在舰但没登记": 0, "等级不一致": 1, "已不在名单": 2, "没有 UID": 3}
    out.sort(key=lambda x: (order[x["kind"]], x["name"]))
    return out


_ADDR_DETAIL = re.compile(r"[路街道号村弄巷栋幢楼室组院苑庄屯]|\d")       # 要有门牌 / 路名之类的详细部分


def check_problems(rows, month: str | None = None) -> list[dict]:
    """信息问题检查：缺地址、手机号格式、地址不完整、多人共用手机号/地址、重复登记、待审核"""
    rs = [r for r in rows if r["status"] in LIVE and (not month or r["month"] == month)]
    out = []

    def add(sev, kind, r, detail):
        out.append({"sev": sev, "kind": kind, "r": r, "detail": detail})

    for r in rs:
        rec, ph, ad = (r.get("receiver") or "").strip(), (r.get("phone") or "").strip(), (r.get("address") or "").strip()
        if r["status"] == "pending":
            add("提示", "待审核", r, "还没通过审核，确认后才会进发货表")
        if r["status"] == "verified" and not ad:
            add("问题", "缺收货地址", r, "还没填收货地址")
            continue
        if ad and not rec:
            add("问题", "缺收件人", r, "有地址但没有收件人姓名")
        if ad and not ph:
            add("问题", "缺手机号", r, "有地址但没有手机号")
        elif ph and not PHONE_RE.match(ph):
            add("问题", "手机号格式不对", r, f"「{ph}」不是 11 位大陆手机号")
        if ad and (len(norm_addr(ad)) < 10 or not _ADDR_DETAIL.search(ad)):
            add("问题", "地址可能不完整", r, f"「{ad}」太短或缺少省市区 / 门牌号")
        if not r.get("bili_uid"):
            add("提示", "没有 B站 UID", r, "无法自动核对舰长名单、也不会自动续登")
    # 跨人的重复
    by_month = defaultdict(list)
    for r in rs:
        by_month[r["month"]].append(r)
    for m, mrs in by_month.items():
        for field, kind, fn in (("phone", "多人共用手机号", lambda r: (r.get("phone") or "").strip()),
                                ("address", "多人地址相同", lambda r: norm_addr(r.get("address")))):
            groups = defaultdict(list)
            for r in mrs:
                v = fn(r)
                if v:
                    groups[v].append(r)
            for v, g in groups.items():
                people = {person_key(r) for r in g}
                if len(people) > 1:
                    who_ = "、".join(sorted({person_name(r) for r in g}))
                    for r in g:
                        add("提示" if field == "address" else "问题", kind, r, f"{m}：{who_} 用的是同一个"
                            + ("手机号" if field == "phone" else "地址（一家人 / 代收可以忽略）"))
        uids = defaultdict(list)
        for r in mrs:
            if r.get("bili_uid"):
                uids[str(r["bili_uid"])].append(r)
        for uid, g in uids.items():
            if len(g) > 1:
                for r in g:
                    add("问题", "同一人重复登记", r, f"{m}：UID {uid} 有 {len(g)} 条记录（可能 QQ 和网页各登记了一次）")
    out.sort(key=lambda x: (x["sev"] != "问题", x["kind"], x["r"]["month"], person_name(x["r"])))
    return out


def fetch_guard_list(room_id: int) -> dict:
    """（界面用）同步获取 B站 当前舰长名单 {uid: {uid,name,level}}"""
    import time
    import httpx
    h = {"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"}
    names = {1: "总督", 2: "提督", 3: "舰长"}
    with httpx.Client(headers=h, timeout=10) as c:
        info = c.get("https://api.live.bilibili.com/room/v1/Room/get_info", params={"room_id": room_id}).json()
        if info.get("code") != 0:
            raise RuntimeError(info.get("message") or "直播间不存在")
        rid, ruid = info["data"]["room_id"], info["data"]["uid"]
        out, page, pages = {}, 1, 1
        while page <= pages and page <= 50:
            for attempt in range(3):
                if page > 1 or attempt:
                    time.sleep(0.4 + attempt * 1.5)
                j = c.get("https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList",
                          params={"roomid": rid, "ruid": ruid, "page": page, "page_size": 30}).json()
                if j.get("code") == 0:
                    break
            else:
                raise RuntimeError(f"B站接口第 {page} 页获取失败，请稍后再试")
            d = j.get("data") or {}
            pages = int((d.get("info") or {}).get("page") or 1)
            items = (d.get("top3") or []) + (d.get("list") or []) if page == 1 else (d.get("list") or [])
            for it in items:
                u = it.get("uinfo") or {}
                uid = str(it.get("uid") or u.get("uid") or "")
                if uid:
                    lv = it.get("guard_level") or (u.get("guard") or {}).get("level") or 3
                    out[uid] = {"uid": uid, "name": it.get("username") or (u.get("base") or {}).get("name") or "",
                                "level": names.get(int(lv), "舰长")}
            page += 1
    return out
