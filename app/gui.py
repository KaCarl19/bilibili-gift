"""舰长礼物小助手 —— 图形界面（PySide6）

界面进程负责：设置、舰长管理（直接读写本地数据库）、导出 Excel、显示日志。
QQ 机器人 + 观众填写网页在一个后台子进程里运行（同一个程序加 --bot 参数），
界面通过本机接口查看机器人状态、让机器人给观众发 QQ 消息。
"""
import base64
import html
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import date
from urllib.parse import urlencode

import httpx
from PySide6.QtCore import QByteArray, QDate, QItemSelectionModel, QObject, QProcess, QProcessEnvironment, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

from . import config as C
from . import db, periods

APP_NAME = "舰长礼物小助手"
PINK = "#fb7299"
STATUS_CN = {"verified": "已验证", "pending": "待审核", "rejected": "已拒绝"}
STATUS_COLOR = {"verified": "#1a7f3c", "pending": "#c27c00", "rejected": "#c62828"}


def status_text(r) -> str:
    if r["status"] == "verified" and r.get("source") == "auto":
        return "自动续登"
    if r["status"] == "verified" and r.get("source") == "import":
        return "表格导入"
    return STATUS_CN.get(r["status"], r["status"])


def gifts_text(r, sep="+") -> str:
    return sep.join(db.gifts_of(r))


def gift_counts(rows) -> str:
    """已验证记录里每种礼物要准备几份"""
    from collections import Counter
    c = Counter(g for r in rows if r["status"] == "verified" and not r["shipped"] for g in db.gifts_of(r))
    return " · ".join(f"{k}礼物 {c[k]} 份" for k in ("总督", "提督", "舰长") if c[k]) or "暂无"


def detail_tip(r) -> str:
    """状态单元格的悬停提示：这条记录是怎么来的、付费到哪天"""
    end = periods.period_end(__import__("datetime").date.fromisoformat(r["start_date"]), r.get("paused_days"))
    lines = [f"等级：{r.get('guard_level') or '舰长'}　应寄：{gifts_text(r, ' + ')}",
             f"覆盖期：{r['start_date']} ～ {end}"]
    if r.get("paused_days"):
        lines.append(f"含顺延 {r['paused_days']} 天（升级时暂停的舰长时间，接在后面，不另算）")
    if r.get("paid_until"):
        lines.append(f"已付费到：{r['paid_until']}（一次买了多个月）")
    if r.get("note"):
        lines.append(r["note"])
    return "\n".join(lines)
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _ui_assets():
    """运行时画出复选框对勾、下拉箭头图标（样式表需要图片文件）"""
    import tempfile
    from pathlib import Path
    from PySide6.QtGui import QPen, QPolygonF
    from PySide6.QtCore import QPointF
    d = Path(tempfile.gettempdir()) / "guardgift_ui"
    d.mkdir(exist_ok=True)

    def draw(name, fn, color):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        fn(p, QColor(color))
        p.end()
        pm.save(str(d / name))

    def check(p, c):
        pen = QPen(c, 4.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(7, 17), QPointF(13, 23), QPointF(25, 9)]))

    def tri(points):
        def f(p, c):
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        return f
    draw("check.png", check, "#ffffff")
    draw("down.png", tri([(6, 11), (26, 11), (16, 23)]), "#61666d")
    draw("up.png", tri([(6, 21), (26, 21), (16, 9)]), "#61666d")
    return d.as_posix()


def build_style():
    u = _ui_assets()
    return f"""
* {{ font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 10pt; color: #18191c; }}
QMainWindow, #page, QScrollArea, #scrollInner {{ background: #f6f7f9; }}
#sidebar {{ background: #ffffff; border: none; border-right: 1px solid #e3e5e7; padding-top: 6px; outline: 0; }}
#sidebar::item {{ height: 46px; padding-left: 14px; border-radius: 8px; margin: 2px 8px; }}
#sidebar::item:hover {{ background: #f6f7f9; }}
#sidebar::item:selected {{ background: #ffecf1; color: {PINK}; }}
#brand {{ font-size: 13pt; font-weight: bold; color: {PINK}; padding: 18px 16px 10px; background: #fff; }}
#pageTitle {{ font-size: 16pt; font-weight: bold; }}
#hint {{ color: #9499a0; font-size: 9pt; }}
#card {{ background: #ffffff; border: 1px solid #eceef0; border-radius: 12px; }}
#cardTitle {{ color: #61666d; font-size: 9pt; }}
#cardValue {{ font-size: 17pt; font-weight: bold; }}
#cardSub {{ color: #9499a0; font-size: 9pt; }}
QPushButton {{ background: #ffffff; border: 1px solid #dcdfe3; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {PINK}; color: {PINK}; }}
QPushButton:disabled {{ color: #c0c4cc; border-color: #eceef0; }}
QPushButton[kind="primary"] {{ background: {PINK}; color: #ffffff; border: none; font-weight: bold; }}
QPushButton[kind="primary"]:hover {{ background: #fc8bab; color: #ffffff; }}
QPushButton[kind="ok"] {{ background: #2fb45a; color: #ffffff; border: none; }}
QPushButton[kind="ok"]:hover {{ background: #45c46e; color: #ffffff; }}
QPushButton[kind="danger"] {{ color: #e5484d; border-color: #f3c0c2; }}
QPushButton[kind="primary"]:disabled, QPushButton[kind="ok"]:disabled {{ background: #e3e5e7; color: #ffffff; }}
QPushButton[kind="danger"]:disabled {{ color: #e8b4b6; border-color: #f3e0e1; }}
QLineEdit, QComboBox, QSpinBox, QDateEdit, QPlainTextEdit {{ background: #ffffff; border: 1px solid #dcdfe3;
    border-radius: 6px; padding: 5px 8px; selection-background-color: {PINK}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus, QPlainTextEdit:focus {{ border-color: {PINK}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ background: #f3f4f6; color: #a8abb2; border-color: #e8eaed; }}
QTableWidget {{ background: #ffffff; border: 1px solid #eceef0; border-radius: 8px; gridline-color: #f1f2f3;
    selection-background-color: #ffe3eb; selection-color: #18191c; alternate-background-color: #fbfbfc; }}
QHeaderView::section {{ background: #fafafa; border: none; border-bottom: 1px solid #e3e5e7; padding: 7px 6px;
    color: #61666d; font-weight: bold; }}
QGroupBox {{ background: #ffffff; border: 1px solid #eceef0; border-radius: 10px; margin-top: 16px;
    padding: 18px 14px 12px; font-weight: bold; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 4px; color: {PINK}; }}
QGroupBox QLabel, QGroupBox QCheckBox {{ font-weight: normal; }}
#log {{ font-family: Consolas, "Microsoft YaHei UI"; font-size: 9pt; background: #1e1f22; color: #d4d4d4;
    border: none; border-radius: 10px; padding: 8px; }}
QTextBrowser {{ background: #ffffff; border: 1px solid #eceef0; border-radius: 10px; padding: 12px; }}
QDialog, QMessageBox {{ background: #ffffff; }}
QDialog QScrollArea, QDialog QScrollArea > QWidget > QWidget {{ background: #f6f7f9; }}
QMenu {{ background: #ffffff; border: 1px solid #dcdfe3; border-radius: 6px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: #ffecf1; color: {PINK}; }}
QMenu::separator {{ height: 1px; background: #eceef0; margin: 4px 8px; }}
QToolTip {{ background: #ffffff; color: #18191c; border: 1px solid #dcdfe3; padding: 4px 6px; }}
QStatusBar {{ background: #ffffff; color: #61666d; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 16px;
    margin-right: 4px; color: #61666d; }}
QTabBar::tab:hover {{ color: {PINK}; }}
QTabBar::tab:selected {{ color: {PINK}; border-bottom: 2px solid {PINK}; font-weight: bold; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid #c0c4cc; border-radius: 4px; background: #ffffff; }}
QCheckBox::indicator:hover {{ border-color: {PINK}; }}
QCheckBox::indicator:checked {{ background: {PINK}; border-color: {PINK}; image: url({u}/check.png); }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 22px; border: none;
    border-left: 1px solid #e3e5e7; background: #fafafa; }}
QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 6px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 6px; border-top: 1px solid #e3e5e7; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #ffecf1; }}
QSpinBox::up-arrow {{ image: url({u}/up.png); width: 9px; height: 9px; }}
QSpinBox::down-arrow {{ image: url({u}/down.png); width: 9px; height: 9px; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down, QDateEdit::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url({u}/down.png); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: #ffffff; border: 1px solid #dcdfe3; selection-background-color: #ffecf1;
    selection-color: {PINK}; outline: 0; }}
"""


# ============================== 小工具 ==============================
def app_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(PINK))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 120, 120, 28, 28)
    p.setPen(QColor("white"))
    f = QFont("Microsoft YaHei UI", 58, QFont.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "舰")
    p.end()
    return QIcon(pm)


def btn(text, kind=None, slot=None, tip=None):
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if slot:
        b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def label(text, name=None, wrap=False):
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def ints(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text or "")]


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class _Call(QObject):
    done = Signal(object)


_calls = set()


def run_bg(fn, cb):
    """在后台线程执行 fn，结果回到界面线程交给 cb（异常也会作为结果传回）"""
    c = _Call()
    _calls.add(c)
    c.done.connect(cb)
    c.done.connect(lambda _r: _calls.discard(c))

    def target():
        try:
            r = fn()
        except Exception as e:     # noqa: BLE001
            r = e
        c.done.emit(r)
    threading.Thread(target=target, daemon=True).start()


def check_room(room_id: int) -> dict:
    """查询直播间：真实房间号、主播昵称、当前舰长人数"""
    h = {"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"}
    with httpx.Client(headers=h, timeout=8) as c:
        r = c.get("https://api.live.bilibili.com/room/v1/Room/get_info", params={"room_id": room_id}).json()
        if r.get("code") != 0:
            raise RuntimeError(r.get("message") or "直播间不存在")
        d = r["data"]
        u = c.get("https://api.live.bilibili.com/live_user/v1/Master/info", params={"uid": d["uid"]}).json()
        name = (u.get("data") or {}).get("info", {}).get("uname", "")
        g = c.get("https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList",
                  params={"roomid": d["room_id"], "ruid": d["uid"], "page": 1, "page_size": 30}).json()
        num = ((g.get("data") or {}).get("info") or {}).get("num")
    return {"room_id": d["room_id"], "uid": d["uid"], "name": name, "guards": num}


def who(r) -> str:
    """表格里“QQ/联系方式”一栏（和网页后台共用同一套规则）"""
    return db.contact_text(r)


def export_xlsx(path, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "发货表"
    head = ["月份", "本期开始", "等级", "应寄礼物", "B站昵称", "B站UID", "QQ/联系方式", "收件人", "手机号", "收货地址",
            "已发货", "来源"]
    ws.append(head)
    src = {"auto": "自动续登", "manual": "手动添加", "web": "网页登记", "import": "表格导入"}
    for r in rows:
        ws.append([r["month"], r["start_date"], r["guard_level"] or "", gifts_text(r, " + "), r["bili_name"] or "",
                   str(r["bili_uid"] or ""), who(r), r["receiver"] or "", str(r["phone"] or ""),
                   r["address"] or "", "是" if r["shipped"] else "否", src.get(r.get("source"), "QQ 验证")])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="FB7299")
        cell.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCDEFGHIJKL", [9, 12, 7, 20, 18, 13, 13, 10, 14, 48, 8, 10]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    # 第二张表：每种礼物要准备几份
    from collections import Counter
    cnt = Counter(g for r in rows for g in db.gifts_of(r))
    ws2 = wb.create_sheet("礼物汇总")
    ws2.append(["礼物", "份数"])
    for k in ("总督", "提督", "舰长"):
        if cnt[k]:
            ws2.append([f"{k}礼物", cnt[k]])
    for cell in ws2[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="FB7299")
    ws2.column_dimensions["A"].width = 14
    wb.save(path)


# ============================== 机器人进程 ==============================
class BotController(QObject):
    log = Signal(str)
    running_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.proc: QProcess | None = None
        self._buf = b""

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def start(self, port: int) -> str | None:
        if self.running():
            return None
        if port_in_use(port):
            return f"端口 {port} 已被其他程序占用。请到「设置」里换一个端口（例如 {port + 1}）后再启动。"
        p = QProcess(self)
        if getattr(sys, "frozen", False):
            p.setProgram(sys.executable)
            p.setArguments(["--bot"])
        else:
            p.setProgram(sys.executable)
            p.setArguments([str(C.ROOT / "main.py"), "--bot"])
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("NO_COLOR", "1")
        p.setProcessEnvironment(env)
        p.setWorkingDirectory(str(C.ROOT))
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.readyReadStandardOutput.connect(self._read)
        p.finished.connect(self._finished)
        p.errorOccurred.connect(lambda e: self.log.emit(f"[界面] 服务进程出错：{e}"))
        self.proc = p
        self.log.emit("[界面] 正在启动服务……")
        p.start()
        self.running_changed.emit(True)
        return None

    def stop(self):
        if self.running():
            self.log.emit("[界面] 正在停止服务……")
            self.proc.kill()
            self.proc.waitForFinished(3000)

    def _read(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for ln in lines:
            t = ANSI_RE.sub("", ln.decode("utf-8", "replace")).rstrip()
            if t:
                self.log.emit(t)

    def _finished(self, code, _status):
        self.log.emit(f"[界面] 服务已停止（退出码 {code}）")
        self.running_changed.emit(False)


class Api(QObject):
    """调用机器人进程里的本机接口（异步，不卡界面）"""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.nam = QNetworkAccessManager(self)

    def _req(self, path):
        cfg = self.win.cfg
        req = QNetworkRequest(QUrl(f"http://127.0.0.1:{cfg['web']['port']}{path}"))
        tok = base64.b64encode(f"gui:{cfg['web']['admin_password']}".encode()).decode()
        req.setRawHeader(b"Authorization", f"Basic {tok}".encode())
        req.setTransferTimeout(120000 if path == "/api/remind" else 15000)
        return req

    def _wrap(self, reply: QNetworkReply, cb):
        def done():
            ok = reply.error() == QNetworkReply.NoError
            data = None
            if ok:
                try:
                    data = json.loads(bytes(reply.readAll()).decode("utf-8") or "null")
                except ValueError:
                    data = None
            reply.deleteLater()
            if cb:
                cb(ok, data)
        reply.finished.connect(done)

    def get(self, path, cb=None):
        self._wrap(self.nam.get(self._req(path)), cb)

    def post(self, path, form: dict | None = None, cb=None):
        req = self._req(path)
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/x-www-form-urlencoded")
        self._wrap(self.nam.post(req, QByteArray(urlencode(form or {}).encode())), cb)


# ============================== 页面：运行状态 ==============================
class Card(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)
        self.title = label(title, "cardTitle")
        self.value = label("—", "cardValue")
        self.sub = label("", "cardSub", wrap=True)
        self.btns = QHBoxLayout()
        self.btns.setSpacing(6)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)
        lay.addStretch(1)
        lay.addLayout(self.btns)

    def set(self, value, sub=None, color=None):
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color};" if color else "")
        if sub is not None:
            self.sub.setText(sub)


class DashboardPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)
        lay.addWidget(label("运行状态", "pageTitle"))

        grid = QGridLayout()
        grid.setSpacing(14)
        self.c_bot = Card("⚙ 后台服务")
        self.b_toggle = btn("启动服务", "primary", win.toggle_bot)
        self.c_bot.btns.addWidget(self.b_toggle)
        self.c_bot.btns.addStretch(1)

        self.c_web = Card("🌐 观众登记网页")
        self.c_web.btns.addWidget(btn("复制网址", None, win.copy_public_url))
        self.c_web.btns.addWidget(btn("二维码", None, win.show_qr, "生成二维码图片，可以发群公告、动态、直播间"))
        self.c_web.btns.addWidget(btn("打开", None, win.open_public_url))
        self.c_web.btns.addStretch(1)

        self.c_qq = Card("💬 QQ 机器人（可选）")
        self.b_qq_launch = btn("启动协议端", None, win.launch_napcat)
        self.b_qq_copy = btn("复制连接地址", None, win.copy_ws_url)
        self.b_qq_set = btn("去设置", None, lambda: win.nav.setCurrentRow(3))
        for b in (self.b_qq_launch, self.b_qq_copy, self.b_qq_set):
            self.c_qq.btns.addWidget(b)
        self.c_qq.btns.addStretch(1)

        self.c_bili = Card("📺 B站舰长名单")
        self.c_bili.btns.addWidget(btn("立即刷新", None, win.refresh_guard_list))
        self.c_bili.btns.addStretch(1)

        self.c_month = Card(f"📦 本月舰长")
        self.c_month.btns.addWidget(btn("提醒未填地址的舰长", None, win.remind,
                                        "QQ 机器人会私聊本月发货表里、还没填地址的舰长"))
        self.c_month.btns.addStretch(1)

        self.c_req = Card("📝 地址修改申请")
        self.c_req.btns.addWidget(btn("去处理", None, win.open_requests))
        self.c_req.btns.addStretch(1)

        for i, c in enumerate([self.c_bot, self.c_web, self.c_qq, self.c_bili, self.c_month, self.c_req]):
            c.setMinimumHeight(150)
            grid.addWidget(c, i // 3, i % 3)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        lay.addLayout(grid)

        row = QHBoxLayout()
        row.addWidget(label("运行日志", "cardTitle"))
        row.addStretch(1)
        row.addWidget(btn("打开网页后台", None, win.open_web_admin))
        row.addWidget(btn("打开数据文件夹", None, lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(C.DATA_DIR)))))
        row.addWidget(btn("清空日志", None, lambda: self.log.clear()))
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        lay.addWidget(self.log, 1)

    def append_log(self, text):
        color = "#d4d4d4"
        if "ERROR" in text or "出错" in text or "Traceback" in text:
            color = "#ff7b7b"
        elif "WARNING" in text or "失败" in text:
            color = "#e6b450"
        elif "SUCCESS" in text or "connected" in text or "验证结果" in text:
            color = "#7ee787"
        elif text.startswith("[界面]"):
            color = "#79c0ff"
        self.log.appendHtml(f'<span style="color:{color}">{html.escape(text)}</span>')


# ============================== 页面：舰长管理 ==============================
COLS = ["本期开始", "状态", "礼物", "B站昵称", "B站UID", "QQ/联系方式", "舰长群", "收件人", "手机号", "收货地址", "发货"]


class GuardsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)

        top = QHBoxLayout()
        top.addWidget(label("舰长管理", "pageTitle"))
        top.addStretch(1)
        self.summary = label("", "hint")
        top.addWidget(self.summary)
        lay.addLayout(top)

        bar = QHBoxLayout()
        self.month = QComboBox()
        self.month.setMinimumWidth(110)
        self.status = QComboBox()
        self.status.addItem("全部状态", "")
        for k, v in db.SHIPPING_VIEWS.items():
            self.status.addItem(v, k)
        self.status.insertSeparator(self.status.count())
        for k, v in STATUS_CN.items():
            self.status.addItem(v, k)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索 QQ / 昵称 / UID / 入群口令 / 收件人 / 手机号")
        self.search.setClearButtonEnabled(True)
        self.month.currentIndexChanged.connect(self.load)
        self.status.currentIndexChanged.connect(self.load)
        self.search.textChanged.connect(self.load)
        bar.addWidget(label("月份"))
        bar.addWidget(self.month)
        bar.addWidget(self.status)
        bar.addWidget(self.search, 1)
        lay.addLayout(bar)
        bar = QHBoxLayout()
        bar.addWidget(btn("刷新", None, self.reload_all))
        bar.addWidget(btn("＋ 手动添加", None, self.add_guard))
        self.b_req = btn("📝 修改申请", None, win.open_requests, "观众在网页上提交的地址修改申请")
        bar.addWidget(self.b_req)
        bar.addStretch(1)
        bar.addWidget(btn("导出当前发货表", "primary", self.export, "导出当前筛选结果中已验证的记录（Excel）"))
        lay.addLayout(bar)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(9, QHeaderView.Stretch)
        self.table.horizontalHeaderItem(6).setToolTip("观众登记时选的是否进舰长群。选「要进群」的会拿到入群口令（每人一个，续费不变），"
                                                     "加群时让他填这个，你在这里搜索就能核对。退过群的要上提督或总督才能选「退群后重新进」")
        self.table.doubleClicked.connect(lambda _i: self.edit_address())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        lay.addWidget(self.table, 1)

        acts = QHBoxLayout()
        self.b_ok = btn("✔ 通过", "ok", self.approve, "通过审核，并私聊通知对方")
        self.b_no = btn("✘ 拒绝", "danger", self.reject, "拒绝，并私聊通知对方")
        self.b_ship = btn("📦 标记已发货", None, lambda: self.ship(True))
        self.b_unship = btn("取消发货", None, lambda: self.ship(False))
        self.b_addr = btn("✏ 编辑地址", None, self.edit_address)
        self.b_shot = btn("🖼 查看截图", None, self.view_shot)
        self.b_del = btn("删除", "danger", self.delete)
        for b in (self.b_ok, self.b_no, self.b_ship, self.b_unship, self.b_addr, self.b_shot):
            acts.addWidget(b)
        acts.addStretch(1)
        acts.addWidget(label("Ctrl / Shift 多选；双击编辑地址；鼠标停在「状态」上看详情", "hint"))
        acts.addWidget(self.b_del)
        lay.addLayout(acts)
        self.rows = []
        self._sel_changed()

    # ---------- 数据 ----------
    def reload_months(self):
        cur = self.month.currentData()
        months = sorted(set(db.all_months()) | {db.this_month()}, reverse=True)
        self.month.blockSignals(True)
        self.month.clear()
        for m in months:
            self.month.addItem(m, m)
        self.month.addItem("全部月份", "")
        idx = self.month.findData(cur if cur is not None else db.this_month())
        self.month.setCurrentIndex(max(idx, 0))
        self.month.blockSignals(False)

    def reload_all(self):
        self.reload_months()
        self.load()

    def load(self):
        selected = set(self.selected_ids())
        rows = db.list_guards(self.month.currentData() or None, self.status.currentData() or None)
        kw = self.search.text().strip().lower()
        if kw:
            rows = [r for r in rows if any(kw in str(r.get(k) or "").lower() for k in
                                           ("qq", "contact", "join_code", "bili_name", "bili_uid", "receiver",
                                            "phone", "address"))]
        self.rows = rows
        t = self.table
        t.clearSelection()
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = [r["start_date"], status_text(r), gifts_text(r),
                    r["bili_name"] or "", r["bili_uid"] or "", who(r), db.group_text(r), r["receiver"] or "",
                    r["phone"] or "", r["address"] or ("（未填写）" if r["status"] == "verified" else ""),
                    db.SHIPPING_VIEWS.get(db.shipping_view(r), "—")]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setData(Qt.UserRole, r["id"])
                if j == 1:
                    it.setForeground(QColor(STATUS_COLOR.get(r["status"], "#18191c")))
                    f = it.font(); f.setBold(True); it.setFont(f)
                if j == 9 and not r["address"]:
                    it.setForeground(QColor("#c27c00"))
                if j == 10 and r["shipped"]:
                    it.setForeground(QColor("#1a7f3c"))
                if j == 6:
                    it.setForeground(QColor(PINK if r.get("join_code") and r.get("group_choice") in ("join", "rejoin", None)
                                            else "#9499a0"))
                if j == 9:
                    it.setToolTip(str(v))
                if j in (0, 1, 2):
                    it.setToolTip(detail_tip(r))
                if j == 2 and r.get("guard_level") in ("提督", "总督"):
                    it.setForeground(QColor("#7b4fd6"))
                t.setItem(i, j, it)
        if selected:
            for i, r in enumerate(rows):
                if r["id"] in selected:
                    t.selectionModel().select(t.model().index(i, 0),
                                              QItemSelectionModel.Select | QItemSelectionModel.Rows)
        v = sum(r["status"] == "verified" for r in rows)
        p = sum(r["status"] == "pending" for r in rows)
        na = sum(r["status"] == "verified" and not r["address"] for r in rows)
        sh = sum(bool(r["shipped"]) for r in rows)
        au = sum(r["status"] == "verified" and r.get("source") == "auto" for r in rows)
        self.summary.setText(f"共 {len(rows)} 条 · 已验证 {v}（自动续登 {au}）· 待审核 {p} · 未填地址 {na} · 已发货 {sh}"
                             f"\n尚需备货：{gift_counts(rows)}")
        self.summary.setAlignment(Qt.AlignRight)
        self._sel_changed()

    def selected_ids(self):
        return list(dict.fromkeys(self.table.item(ix.row(), 0).data(Qt.UserRole)
                                  for ix in self.table.selectionModel().selectedRows()
                                  if self.table.item(ix.row(), 0)))

    def _one(self):
        ids = self.selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, APP_NAME, "请先选中一行。")
            return None
        return next((r for r in self.rows if r["id"] == ids[0]), None)

    def _sel_changed(self):
        ids = set(self.selected_ids())
        n = len(ids)
        selected = [r for r in self.rows if r["id"] in ids]
        for b in (self.b_ok, self.b_no, self.b_del):
            b.setEnabled(n > 0)
        self.b_ship.setEnabled(bool(selected) and all(r["status"] == "verified" and not r["shipped"]
                                                      for r in selected))
        self.b_unship.setEnabled(bool(selected) and all(r["shipped"] for r in selected))
        for b in (self.b_addr, self.b_shot):
            b.setEnabled(n == 1)

    def menu(self, pos):
        if not self.selected_ids():
            return
        m = QMenu(self)
        m.addAction("✔ 通过", self.approve)
        m.addAction("✘ 拒绝", self.reject)
        m.addSeparator()
        m.addAction("📦 标记已发货", lambda: self.ship(True)).setEnabled(self.b_ship.isEnabled())
        m.addAction("取消发货", lambda: self.ship(False)).setEnabled(self.b_unship.isEnabled())
        m.addSeparator()
        m.addAction("✏ 编辑地址", self.edit_address)
        m.addAction("🖼 查看截图", self.view_shot)
        m.addAction("📋 复制收件信息", self.copy_info)
        m.addSeparator()
        m.addAction("删除", self.delete)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---------- 操作 ----------
    def approve(self):
        for gid in self.selected_ids():
            g = db.get_guard(gid)
            if not g or g["status"] == "verified":
                continue
            db.set_guard_status(gid, "verified", "主播人工审核通过")
            if g["month"] == db.this_month():
                self.win.notify_qq(g["qq"], f"🎉 你的舰长身份（{g['start_date']} 上舰）已通过主播审核！"
                                            "发送「改地址」即可填写收货信息。")
        self.win.data_changed()

    def reject(self):
        ids = self.selected_ids()
        if QMessageBox.question(self, APP_NAME, f"确定拒绝选中的 {len(ids)} 条记录吗？\n机器人会私聊通知对方。") \
                != QMessageBox.Yes:
            return
        for gid in ids:
            g = db.get_guard(gid)
            if not g or g["status"] == "rejected":
                continue
            db.set_guard_status(gid, "rejected", "主播人工拒绝")
            self.win.notify_qq(g["qq"], "很抱歉，你提交的舰长信息没有通过主播审核。如有疑问请直接联系主播。")
        self.win.data_changed()

    def ship(self, shipped):
        for gid in self.selected_ids():
            db.set_shipped(gid, shipped)
        self.win.data_changed()

    def delete(self):
        ids = self.selected_ids()
        if QMessageBox.question(self, APP_NAME, f"确定删除选中的 {len(ids)} 条舰长记录吗？\n"
                                                "（收货地址会保留，删除后不可恢复）") != QMessageBox.Yes:
            return
        for gid in ids:
            db.delete_guard(gid)
        self.win.data_changed()

    def copy_info(self):
        lines = []
        for gid in self.selected_ids():
            r = next((x for x in self.rows if x["id"] == gid), None)
            if r and r["address"]:
                lines.append(f"{r['receiver']} {r['phone']} {r['address']}")
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.win.toast(f"已复制 {len(lines)} 条收件信息")

    def edit_address(self):
        r = self._one()
        if r and AddressDialog(self, r).exec():
            self.win.data_changed()

    def view_shot(self):
        r = self._one()
        if r:
            ShotDialog(self, r).exec()

    def add_guard(self):
        if AddGuardDialog(self).exec():
            self.win.data_changed()

    def export(self):
        month = self.month.currentData() or ""
        rows = [r for r in self.rows if r["status"] == "verified"]
        if not rows:
            QMessageBox.information(self, APP_NAME, "当前筛选结果中没有已验证的记录。可以调整月份、状态或搜索条件。")
            return
        name = f"舰长发货表_{month or '全部'}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "导出发货表",
                                              os.path.join(os.path.expanduser("~"), "Desktop", name),
                                              "Excel 表格 (*.xlsx)")
        if not path:
            return
        try:
            export_xlsx(path, rows)
        except PermissionError:
            QMessageBox.warning(self, APP_NAME, "保存失败：文件可能正在被 Excel 打开，请先关闭再导出。")
            return
        na = sum(not r["address"] for r in rows)
        msg = f"已导出 {len(rows)} 条已验证记录。" + (f"\n其中 {na} 条还没填地址。" if na else "")
        if QMessageBox.question(self, APP_NAME, msg + "\n\n现在打开这个表格吗？") == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class AddressDialog(QDialog):
    def __init__(self, parent, r):
        super().__init__(parent)
        self.r = r
        self.setWindowTitle(f"编辑收货信息 — {r['bili_name'] or who(r)}")
        self.setMinimumWidth(460)
        lay = QFormLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        self.receiver = QLineEdit(r["receiver"] or "")
        self.phone = QLineEdit(r["phone"] or "")
        self.addr = QPlainTextEdit(r["address"] or "")
        self.addr.setFixedHeight(80)
        lay.addRow(label(f"{who(r)} · {gifts_text(r, ' + ')} · 本期从 {r['start_date']} 开始", "hint"))
        lay.addRow("收件人", self.receiver)
        lay.addRow("手机号", self.phone)
        lay.addRow("收货地址", self.addr)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def save(self):
        rec, ph, ad = self.receiver.text().strip(), re.sub(r"\D", "", self.phone.text()), self.addr.toPlainText().strip()
        if not rec or not ad:
            QMessageBox.warning(self, APP_NAME, "收件人和地址不能为空。")
            return
        if not PHONE_RE.match(ph) and QMessageBox.question(
                self, APP_NAME, "手机号看起来不是 11 位大陆手机号，仍然保存吗？") != QMessageBox.Yes:
            return
        db.save_address(self.r["qq"], rec, ph, ad)
        if self.r.get("snap_address"):             # 已发货 / 表格导入的这一份：同时改正这条记录上的收件信息
            db.set_snapshot(self.r["id"], rec, ph, ad)
        self.accept()


class AddGuardDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("手动添加舰长")
        self.setMinimumWidth(420)
        lay = QFormLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        self.qq = QLineEdit()
        self.qq.setPlaceholderText("QQ 号和 B站 UID 至少填一个")
        self.name = QLineEdit()
        self.uid = QLineEdit()
        self.uid.setPlaceholderText("填了 UID，之后每个月会自动续登")
        self.months = QSpinBox()
        self.months.setRange(1, 12)
        self.months.setSuffix(" 个月")
        self.level = QComboBox()
        self.level.addItems(["舰长", "提督", "总督"])
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        lay.addRow(label("适用于识别失败的观众。只填 UID 的，对方打开登记网页验证后即可填写地址；"
                         "填了 QQ 的，对方私聊机器人「改地址」即可填写。", "hint", True))
        lay.addRow("QQ 号", self.qq)
        lay.addRow("B站昵称", self.name)
        lay.addRow("B站 UID", self.uid)
        lay.addRow("等级", self.level)
        lay.addRow("开通日期", self.day)
        lay.addRow("一次购买", self.months)
        self.purchase_summary = label("", "hint", True)
        lay.addRow("礼物安排", self.purchase_summary)
        self.months.valueChanged.connect(self.preview_purchase)
        self.level.currentTextChanged.connect(self.preview_purchase)
        self.day.dateChanged.connect(self.preview_purchase)
        self.uid.textChanged.connect(self.preview_purchase)
        self.preview_purchase()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("添加")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def preview_purchase(self):
        count = self.months.value()
        bundle = " + ".join(f"{level}礼物" for level in periods.bundle(self.level.currentText()))
        summary = f"每期寄 {bundle}，本次购买共 {count} 期。"
        if count > 1:
            last = self.day.date().addDays(periods.PERIOD_DAYS * (count - 1)).toString("yyyy-MM-dd")
            summary += f"末期从 {last} 开始。"
            if not self.uid.text().strip():
                summary += "填写 B站 UID 后可按舰长名单自动续登。"
        self.purchase_summary.setText(summary)

    def save(self):
        qq = re.sub(r"\D", "", self.qq.text())
        uid = re.sub(r"\D", "", self.uid.text())
        if not qq and not uid:
            QMessageBox.warning(self, APP_NAME, "QQ 号和 B站 UID 至少填一个。")
            return
        d = self.day.date().toString("yyyy-MM-dd")
        n = self.months.value()
        paid = None
        if n > 1:
            from datetime import date, timedelta
            paid = (date.fromisoformat(d) + timedelta(days=periods.PERIOD_DAYS * n)).isoformat()
        owner = qq or db.web_owner(uid)
        db.upsert_guard(owner, db.free_month(owner, d[:7]), d, "verified", bili_uid=uid or None,
                        bili_name=self.name.text().strip() or None, guard_level=self.level.currentText(),
                        source="manual", paid_until=paid, date_source="manual",
                        note="主播手动添加" + (f"（一次买 {n} 个月）" if n > 1 else ""))
        self.accept()


class ShotDialog(QDialog):
    def __init__(self, parent, r):
        super().__init__(parent)
        self.setWindowTitle(f"上舰截图 — {r['bili_name'] or who(r)}")
        self.resize(560, 720)
        lay = QVBoxLayout(self)
        p = C.SHOT_DIR / (r["screenshot"] or "")
        if r["screenshot"] and p.exists():
            pm = QPixmap(str(p))
            img = QLabel()
            img.setAlignment(Qt.AlignCenter)
            img.setPixmap(pm.scaledToWidth(min(pm.width(), 500), Qt.SmoothTransformation))
            sa = QScrollArea()
            sa.setWidget(img)
            sa.setWidgetResizable(True)
            lay.addWidget(sa, 1)
        else:
            lay.addWidget(label("这条记录没有截图（可能是手动添加或私聊问答登记的）。", "hint"))
        lay.addWidget(label("识别出的文字 / 备注：", "cardTitle"))
        t = QPlainTextEdit((r.get("ocr_text") or "") + ("\n\n备注：" + r["note"] if r.get("note") else ""))
        t.setReadOnly(True)
        t.setFixedHeight(130)
        lay.addWidget(t)
        lay.addWidget(btn("关闭", None, self.accept), 0, Qt.AlignRight)


# ============================== 页面：设置 ==============================
class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("scrollInner")
        sa.setWidget(inner)
        outer.addWidget(sa)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(6)
        lay.addWidget(label("设置", "pageTitle"))

        # --- 直播间 ---
        g1 = QGroupBox("① 你的直播间")
        f1 = QFormLayout(g1)
        self.room = QLineEdit()
        self.room.setPlaceholderText("直播间网址 live.bilibili.com/ 后面的数字")
        rr = QHBoxLayout()
        rr.addWidget(self.room, 1)
        self.b_check = btn("检测直播间", "primary", self.check)
        rr.addWidget(self.b_check)
        f1.addRow("直播间号", rr)
        self.room_info = label("填好后点「检测直播间」，会自动填写主播昵称", "hint", True)
        f1.addRow("", self.room_info)
        self.name = QLineEdit()
        f1.addRow("主播昵称", self.name)
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("用逗号分隔，例如：七海Nana7mi，七海")
        f1.addRow("截图关键词", self.keywords)
        f1.addRow("", label("观众的上舰截图里出现其中任意一个词，才算是你直播间的截图。一般填主播昵称、粉丝牌名。", "hint", True))
        self.check_api = QCheckBox("调用 B站 公开接口核对舰长名单（推荐，可以防止 P 图）")
        f1.addRow("", self.check_api)
        lay.addWidget(g1)

        # --- QQ ---
        g2 = QGroupBox("② QQ 机器人（可选）")
        f2 = QFormLayout(g2)
        self.qq_on = QCheckBox("启用 QQ 机器人")
        f2.addRow("", self.qq_on)
        f2.addRow("", label("收集地址不需要 QQ 机器人：观众打开「登记网页」就能自助验证和填写。", "hint", True))
        self.qq_mode = QComboBox()
        self.qq_mode.addItem("QQ 官方机器人（推荐，不会封号）", "official")
        self.qq_mode.addItem("LLBot / NapCat（非官方，有封号风险）", "onebot")
        f2.addRow("机器人类型", self.qq_mode)
        # 官方机器人
        self.box_official = QWidget()
        fo = QFormLayout(self.box_official)
        fo.setContentsMargins(0, 0, 0, 0)
        fo.addRow("", label("在舰长群里核对入群口令：新人发「@机器人 口令 XXXXXX」，机器人回复他是不是在舰的已验证舰长。"
                            "到 q.qq.com 注册（个人认证即可）→ 创建机器人 →「开发设置」里复制 AppID 和 AppSecret，"
                            "再把机器人拉进你当群主的舰长群。详细步骤见「使用帮助」。", "hint", True))
        self.appid = QLineEdit()
        self.appid.setPlaceholderText("开发设置里的 AppID（一串数字）")
        fo.addRow("AppID", self.appid)
        sr = QHBoxLayout()
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret.setPlaceholderText("开发设置里的 AppSecret，不要告诉别人")
        sr.addWidget(self.secret, 1)
        show_s = QCheckBox("显示")
        show_s.toggled.connect(lambda on: self.secret.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        sr.addWidget(show_s)
        fo.addRow("AppSecret", sr)
        self.member_events = QCheckBox("自动审核入群申请、记录退群")
        fo.addRow("", self.member_events)
        fo.addRow("", label("入群申请的答案是有效口令就自动通过，否则拒绝并说明原因；有人退群会记下来，以后要重新上提督或总督才能再进。"
                            "需要先在开放平台开通「群成员事件」相关权限，没开通请不要勾，否则机器人连不上。", "hint", True))
        f2.addRow(self.box_official)
        # LLBot / NapCat
        self.box_onebot = QWidget()
        fb = QFormLayout(self.box_onebot)
        fb.setContentsMargins(0, 0, 0, 0)
        f2 = fb
        f2.addRow("", label("群里自动识别上舰截图、私聊改地址、审核结果通知、新人超时踢出。"
                            "需要另外运行 LLBot 等 QQ 协议端（NapCat 已停止维护），"
                            "这类非官方工具有被 QQ 风控的风险，请务必用小号。", "hint", True))
        self.groups = QLineEdit()
        self.groups.setPlaceholderText("多个群用逗号分隔")
        f2.addRow("舰长群群号", self.groups)
        self.admins = QLineEdit()
        self.admins.setPlaceholderText("你自己的 QQ 号，多个用逗号分隔")
        f2.addRow("管理员 QQ", self.admins)
        f2.addRow("", label("有人需要审核、有人冒用账号时，机器人会私聊提醒管理员。", "hint", True))
        self.kick = QSpinBox()
        self.kick.setRange(0, 1440)
        self.kick.setSuffix(" 分钟")
        self.kick.setSpecialValueText("不踢人")
        f2.addRow("新人未验证踢出", self.kick)
        self.token = QLineEdit()
        self.token.setPlaceholderText("可留空；如果在协议端（LLBot / NapCat）里设置了 token，这里填一样的")
        f2.addRow("连接令牌 token", self.token)
        nr = QHBoxLayout()
        self.napcat = QLineEdit()
        self.napcat.setPlaceholderText("可选：选择 QQ 协议端（LLBot / NapCat）的启动文件，之后可一键启动")
        nr.addWidget(self.napcat, 1)
        self.b_browse = btn("浏览…", None, self.browse_napcat)
        nr.addWidget(self.b_browse)
        f2.addRow("协议端位置", nr)
        g2.layout().addRow(self.box_onebot)
        self.qq_on.toggled.connect(self._qq_toggle)
        self.qq_mode.currentIndexChanged.connect(lambda _i: self._qq_toggle(self.qq_on.isChecked()))
        lay.addWidget(g2)

        # --- 网页 ---
        g3 = QGroupBox("③ 观众登记网页")
        f3 = QFormLayout(g3)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        f3.addRow("端口", self.port)
        self.public_url = QLineEdit()
        f3.addRow("观众访问地址", self.public_url)
        f3.addRow("", label("观众用手机打开的登记网址。http://127.0.0.1:端口 只有这台电脑能打开；"
                            "要让观众能打开，请填内网穿透或云服务器的地址（见「使用帮助」）。", "hint", True))
        pr = QHBoxLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        pr.addWidget(self.password, 1)
        show = QCheckBox("显示")
        show.toggled.connect(lambda on: self.password.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        pr.addWidget(show)
        f3.addRow("网页后台密码", pr)
        self.hours = QSpinBox()
        self.hours.setRange(1, 720)
        self.hours.setSuffix(" 小时")
        f3.addRow("填写链接有效期", self.hours)
        cr = QHBoxLayout()
        self.contact_label = QLineEdit()
        self.contact_label.setPlaceholderText("QQ / 微信")
        cr.addWidget(self.contact_label, 1)
        self.contact_required = QCheckBox("必填")
        cr.addWidget(self.contact_required)
        f3.addRow("联系方式提问", cr)
        self.contact_hint = QLineEdit()
        self.contact_hint.setPlaceholderText("方便主播有问题时联系你")
        f3.addRow("输入框提示", self.contact_hint)
        f3.addRow("", label("登记页上让舰长留的联系方式，例如「QQ 号」「微信号」「QQ / 微信」。"
                            "勾选「必填」后不填就不能提交。", "hint", True))
        lay.addWidget(g3)

        # --- 规则 ---
        g4 = QGroupBox("④ 规则与启动")
        f4 = QFormLayout(g4)
        self.grace = QSpinBox()
        self.grace.setRange(0, 15)
        self.grace.setSuffix(" 天")
        self.grace.setSpecialValueText("不宽限")
        f4.addRow("跨月宽限", self.grace)
        f4.addRow("", label("在舰期间随时可以修改地址。例如设为 3：每月 1～3 号，上个月发货表里的舰长即使已经到期，也还能修改地址。", "hint", True))
        self.auto_renew = QCheckBox("连续在舰的舰长（自动续费 / 一次买多个月）每个新的舰长月自动登记")
        f4.addRow("", self.auto_renew)
        f4.addRow("", label("每个舰长月寄一份礼物。上一期结束后还在 B站舰长名单里，就自动生成下个月的记录、沿用原来的地址，"
                            "不用舰长重新上传截图。", "hint", True))
        self.autostart = QCheckBox("打开软件时自动启动服务")
        f4.addRow("", self.autostart)
        lay.addWidget(g4)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn("恢复为已保存的设置", None, lambda: self.load(self.win.cfg)))
        row.addWidget(btn("💾 保存设置", "primary", self.save))
        lay.addSpacing(8)
        lay.addLayout(row)
        lay.addStretch(1)
        self._last_port = None
        self.port.valueChanged.connect(self._port_changed)

    def load(self, c):
        s, q, w = c["streamer"], c["qq"], c["web"]
        self.room.setText(str(s["room_id"] or ""))
        self.name.setText(s["name"])
        self.keywords.setText("，".join(s["keywords"]))
        self.check_api.setChecked(bool(c["bilibili"]["check_api"]))
        self.groups.setText(", ".join(map(str, q["groups"])))
        self.admins.setText(", ".join(map(str, q["admins"])))
        self.kick.setValue(int(q["kick_unverified_minutes"] or 0))
        self.token.setText(q["access_token"] or "")
        self.napcat.setText(c["napcat"]["path"] or "")
        self.qq_on.setChecked(bool(q.get("enabled")))
        self.qq_mode.setCurrentIndex(max(0, self.qq_mode.findData(q.get("mode") or "official")))
        self.appid.setText(str(q.get("appid") or ""))
        self.secret.setText(str(q.get("secret") or ""))
        self.member_events.setChecked(bool(q.get("member_events")))
        self._qq_toggle(self.qq_on.isChecked())
        self._last_port = int(w["port"])
        self.port.blockSignals(True)
        self.port.setValue(int(w["port"]))
        self.port.blockSignals(False)
        self.public_url.setText(w["public_url"])
        self.password.setText(w["admin_password"])
        self.hours.setValue(int(w["link_valid_hours"]))
        self.contact_label.setText(w.get("contact_label") or "")
        self.contact_required.setChecked(bool(w.get("contact_required")))
        self.contact_hint.setText(w.get("contact_hint") or "")
        self.grace.setValue(int(c["rules"]["edit_grace_days"] or 0))
        self.autostart.setChecked(bool(c["gui"]["autostart"]))
        self.auto_renew.setChecked(bool(c["rules"].get("auto_renew", True)))

    def _port_changed(self, v):
        # 观众地址还是本机默认地址时，跟着端口一起改
        u = self.public_url.text().strip()
        if self._last_port and re.fullmatch(rf"http://(127\.0\.0\.1|localhost):{self._last_port}/?", u):
            self.public_url.setText(f"http://127.0.0.1:{v}")
        self._last_port = v

    def _qq_toggle(self, on):
        official = self.qq_mode.currentData() == "official"
        self.qq_mode.setEnabled(on)
        self.box_official.setVisible(official)
        self.box_onebot.setVisible(not official)
        for w in (self.appid, self.secret, self.member_events, self.groups, self.admins, self.kick, self.token,
                  self.napcat, self.b_browse):
            w.setEnabled(on)

    def browse_napcat(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 NapCat 启动文件", "", "启动文件 (*.bat *.exe *.cmd *.ps1);;所有文件 (*)")
        if p:
            self.napcat.setText(p)

    def check(self):
        ids = ints(self.room.text())
        if not ids:
            self.room_info.setText("❗ 请先填写直播间号")
            return
        self.b_check.setEnabled(False)
        self.room_info.setText("正在查询……")

        def done(r):
            self.b_check.setEnabled(True)
            if isinstance(r, Exception):
                self.room_info.setText(f"❗ 查询失败：{r}")
                return
            self.room.setText(str(r["room_id"]))
            if r["name"]:
                if not self.name.text().strip():
                    self.name.setText(r["name"])
                kws = [k for k in re.split(r"[,，\s]+", self.keywords.text()) if k]
                if r["name"] not in kws:
                    self.keywords.setText("，".join([r["name"]] + kws))
            g = "未知" if r["guards"] is None else f"{r['guards']} 人"
            self.room_info.setText(f"✅ 找到直播间 {r['room_id']}：主播「{r['name']}」，当前舰长 {g}")
        run_bg(lambda: check_room(ids[0]), done)

    def collect(self):
        c = json.loads(json.dumps(self.win.cfg))
        rid = ints(self.room.text())
        if not rid:
            raise ValueError("请填写直播间号")
        kws = [k for k in re.split(r"[,，、\s]+", self.keywords.text()) if k]
        name = self.name.text().strip()
        if not name:
            raise ValueError("请填写主播昵称（可以点「检测直播间」自动填写）")
        if not kws:
            kws = [name]
        url = self.public_url.text().strip().rstrip("/")
        if not re.match(r"^https?://", url):
            raise ValueError("观众访问地址要以 http:// 或 https:// 开头")
        if len(self.password.text()) < 6:
            raise ValueError("网页后台密码至少 6 位")
        c["streamer"].update(name=name, room_id=rid[0], keywords=kws)
        c["bilibili"]["check_api"] = self.check_api.isChecked()
        c["qq"].update(groups=ints(self.groups.text()), admins=ints(self.admins.text()),
                       kick_unverified_minutes=self.kick.value(), access_token=self.token.text().strip())
        c["web"].update(port=self.port.value(), public_url=url, admin_password=self.password.text(),
                        link_valid_hours=self.hours.value(),
                        contact_label=self.contact_label.text().strip() or "QQ / 微信",
                        contact_required=self.contact_required.isChecked(),
                        contact_hint=self.contact_hint.text().strip())
        c["rules"]["edit_grace_days"] = self.grace.value()
        c["napcat"]["path"] = self.napcat.text().strip()
        c["qq"]["enabled"] = self.qq_on.isChecked()
        c["qq"].update(mode=self.qq_mode.currentData(), appid=re.sub(r"\s", "", self.appid.text()),
                       secret=self.secret.text().strip(), member_events=self.member_events.isChecked())
        if c["qq"]["enabled"] and c["qq"]["mode"] == "official" and not (c["qq"]["appid"] and c["qq"]["secret"]):
            raise ValueError("启用 QQ 官方机器人需要填写 AppID 和 AppSecret（在 q.qq.com 机器人的「开发设置」里）")
        c["gui"]["autostart"] = self.autostart.isChecked()
        c["rules"]["auto_renew"] = self.auto_renew.isChecked()
        return c

    def save(self):
        try:
            c = self.collect()
        except ValueError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        if c["qq"]["enabled"] and not c["qq"]["groups"]:
            if QMessageBox.question(self, APP_NAME, "还没有填写舰长群群号，机器人将不会处理任何群消息。\n仍然保存吗？") \
                    != QMessageBox.Yes:
                return
        old_port = self.win.cfg["web"]["port"]
        C.save(c)
        self.win.cfg = c
        self.win.settings_saved()
        if self.win.bot.running():
            if QMessageBox.question(self, APP_NAME, "设置已保存。需要重启服务才能生效，现在重启吗？") == QMessageBox.Yes:
                self.win.restart_bot(old_port)
        else:
            self.win.toast("设置已保存")


# ============================== 页面：帮助 ==============================
class HelpPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        top = QHBoxLayout()
        top.addWidget(label("使用帮助", "pageTitle"))
        top.addStretch(1)
        top.addWidget(btn("复制群公告", None, self.copy_notice))
        top.addWidget(btn("复制登记网址", None, win.copy_public_url))
        lay.addLayout(top)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        lay.addWidget(self.text, 1)

    def notice(self):
        url = self.win.cfg["web"]["public_url"]
        return ("📢 上舰的宝宝们看这里～舰长礼物登记：\n"
                f"1. 手机打开 {url} （或扫码）\n"
                "2. 填 B站 UID，上传上舰截图（要能看到主播名和开通 / 到期时间）\n"
                "3. 验证通过后填写收件人、手机号、地址\n"
                "4. 要修改：用同一台手机再打开上面的网址就行\n"
                "⚠️ 只有目前在舰的舰长才能登记和修改。自动续费、一次买了好几个月的，"
                "之后每个月会自动登记，不用重复提交")

    def copy_notice(self):
        QGuiApplication.clipboard().setText(self.notice())
        self.win.toast("群公告已复制，可以直接粘贴到 QQ 群 / 动态")

    def refresh(self):
        c = self.win.cfg
        port, tok, url = c["web"]["port"], c["qq"]["access_token"], c["web"]["public_url"]
        ws = f"ws://127.0.0.1:{port}/onebot/v11/ws"
        notice = html.escape(self.notice()).replace("\n", "<br>")
        local = "127.0.0.1" in url or "localhost" in url
        self.text.setHtml(f"""
<style>h3{{color:{PINK};margin-top:18px}} code{{background:#f1f2f3;padding:2px 6px;border-radius:4px}}
li{{margin:4px 0}} .box{{background:#fff6e0;padding:10px;border-radius:8px}}</style>
<h3>第一次使用，按顺序做这 3 步</h3>
<ol>
<li><b>填设置：</b>左边「⚙ 设置」→ 填直播间号 → 点「检测直播间」→ 保存。</li>
<li><b>让观众能打开登记网页：</b>{"现在的地址是 <code>" + html.escape(url) + "</code>，<b>只有这台电脑能打开</b>，请先看下面「让观众用手机打开登记网页」。" if local else "登记网址：<code>" + html.escape(url) + "</code>"}</li>
<li><b>发给舰长们：</b>「🏠 运行状态」→「🌐 观众登记网页」点「二维码」保存图片，或者点上方「复制群公告」，
  发到舰长群公告、B站动态、直播间公告都可以。</li>
</ol>
<h3>舰长怎么用（可复制为群公告）</h3>
<div class="box">{notice}</div>
<h3>你平时要做的</h3>
<ul>
<li>看到「舰长管理 🔴」：选中待审核的那条 →「🖼 查看截图」→ 没问题点「✔ 通过」。观众刷新登记页面就能填写地址。</li>
<li>看到「📝 地址修改申请」：观众换了手机修改地址时需要你确认，核对后点「同意」。</li>
<li>准备寄礼物：「舰长管理」选好月份 →「导出 Excel 发货表」；寄出后选中点「📦 标记已发货」。</li>
<li>观众私信你新地址：在「舰长管理」双击他那一行，直接帮他改。</li>
<li>管理舰长群：观众登记时会选<b>是否进舰长群</b>（要进群 / 已在群里 / 不进群 / 退群后重新进）。选「要进群」的会拿到<b>入群口令</b>，
  每人一个、续费不变。可以把 QQ 群设成「需要回答问题」让他填这个口令，你在「舰长管理」搜索口令就能核对。
  退过群的，要重新上提督或总督才能选「退群后重新进」。</li>
<li><b>自动续费 / 一次买多个月：</b>每个舰长月寄一份礼物。上一期结束后他还在 B站舰长名单里，软件会自动生成下个月的记录
  （状态显示「自动续登」，沿用原来的地址）。如果之后发现他其实没续费，这条记录会自动撤销。</li>
</ul>
<h3>让观众用手机打开登记网页</h3>
<p>默认的 <code>127.0.0.1</code> 只有这台电脑能打开。任选一种，然后把得到的网址填到「设置 → 观众访问地址」：</p>
<ul>
<li><b>内网穿透（简单）：</b>用 cpolar 等工具把本机 <code>{port}</code> 端口映射出去。电脑关机或软件关闭时观众打不开。</li>
<li><b>云服务器（长期推荐）：</b>把整个软件文件夹放到 Windows 云服务器上运行，开放 <code>{port}</code> 端口，
  「观众访问地址」填 <code>http://服务器IP:{port}</code>。</li>
</ul>
<h3>QQ 官方机器人（可选，推荐）</h3>
<p>不用 QQ 机器人也能收集地址。官方机器人在舰长群里帮你<b>核对入群口令</b>，不会封号：</p>
<ol>
<li>打开 <a href="https://q.qq.com/">q.qq.com</a>（QQ 开放平台），用你自己的 QQ 注册，个人实名认证即可，然后「创建机器人」。</li>
<li>机器人的「开发设置」里复制 <b>AppID</b> 和 <b>AppSecret</b>；同一页的 <b>IP 白名单</b>加上这台电脑（或云服务器）的公网 IP。</li>
<li>把机器人拉进你当群主的舰长群（个人认证的机器人只能进你当群主或管理员的群）。</li>
<li>「⚙ 设置」→「QQ 机器人」勾选启用，类型选「QQ 官方机器人」，填 AppID 和 AppSecret，保存并重启服务。「💬 QQ 机器人」显示 <b>已连接</b> 即可。</li>
<li>群里试一下：发「<b>@机器人 口令 你的入群口令</b>」。核对通过会记为「已进群」，「舰长管理」的「舰长群」一栏能看到。</li>
<li>可选：在开放平台开通「群成员事件」相关权限后，到设置里勾「自动审核入群申请、记录退群」。
  QQ 群的加群方式设成「需要回答问题」，答案填入群口令：口令有效自动通过，无效自动拒绝并说明原因；
  有人退群会记为「已退群」，以后要重新上提督或总督、在登记页选「以前退过群，想重新进」才能再进。</li>
</ol>
<p>官方机器人拿不到 QQ 号，也不能随便私聊观众，所以地址还是在登记网页收。</p>
<h3>LLBot / NapCat（可选，非官方）</h3>
<p>想让机器人在群里自动识别截图、私聊处理改地址、通知审核结果、新人超时踢出，可以改用非官方协议端：</p>
<ol>
<li>NapCat 已停止维护，推荐使用 <a href="https://luckylilliadoc.pages.dev/">LLBot</a>（接入方式和 NapCat 一样，都支持 OneBot 11），
  用 <b>QQ 小号</b>登录。⚠️ 这类非官方工具有被 QQ 风控、封号的风险。</li>
<li>在协议端里开启 OneBot 11 的<b>反向 WebSocket</b>，地址填 <code>{ws}</code>，
  {"Token 填 <code>" + html.escape(tok) + "</code>" if tok else "Token 留空"}。</li>
<li>「⚙ 设置」勾选「启用 QQ 机器人」，类型选「LLBot / NapCat」，填舰长群号和你的 QQ，保存并重启服务。「💬 QQ 机器人」显示 <b>已连接</b> 即可。</li>
</ol>
<h3>数据与隐私</h3>
<p>所有数据都在软件目录下的 <code>data</code> 文件夹（「运行状态」页可一键打开）。请定期备份，
<b>不要发给别人</b>：里面有舰长的手机号和地址。</p>
""")


class QrDialog(QDialog):
    """登记网址二维码，可保存成图片发群公告 / 动态"""

    def __init__(self, parent, url, streamer):
        super().__init__(parent)
        import qrcode
        self.setWindowTitle("登记网页二维码")
        q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(url)
        q.make(fit=True)
        m = q.get_matrix()
        n = len(m)
        cell = max(6, 420 // n)
        qr = cell * n
        W, H = qr + 100, qr + 210
        pm = QPixmap(W, H)
        pm.fill(QColor("white"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(PINK))
        p.setFont(QFont("Microsoft YaHei UI", 20, QFont.Bold))
        p.drawText(0, 22, W, 44, Qt.AlignCenter, "🚢 舰长礼物登记")
        p.setPen(QColor("#61666d"))
        p.setFont(QFont("Microsoft YaHei UI", 12))
        p.drawText(0, 62, W, 28, Qt.AlignCenter, f"{streamer} 的直播间" if streamer else "")
        x0, y0 = 50, 100
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#18191c"))
        for y, row in enumerate(m):
            for x, on in enumerate(row):
                if on:
                    p.drawRect(x0 + x * cell, y0 + y * cell, cell, cell)
        p.setPen(QColor("#18191c"))
        p.setFont(QFont("Microsoft YaHei UI", 12))
        p.drawText(0, y0 + qr + 18, W, 28, Qt.AlignCenter, "微信 / QQ / 浏览器扫码，登记收货地址")
        p.setPen(QColor("#9499a0"))
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.drawText(0, y0 + qr + 50, W, 24, Qt.AlignCenter, url)
        p.end()
        self.pm = pm

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        if "127.0.0.1" in url or "localhost" in url:
            lay.addWidget(label("⚠️ 当前网址只有这台电脑能打开，观众扫码会打不开。请先在「设置 → 观众访问地址」"
                                "填写内网穿透或云服务器的地址（见「使用帮助」）。", "hint", True))
        img = QLabel()
        img.setPixmap(pm.scaledToWidth(min(W, 420), Qt.SmoothTransformation))
        img.setAlignment(Qt.AlignCenter)
        lay.addWidget(img)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn("复制图片", None, self.copy))
        row.addWidget(btn("保存图片", "primary", self.save))
        row.addWidget(btn("关闭", None, self.accept))
        lay.addLayout(row)

    def copy(self):
        QGuiApplication.clipboard().setPixmap(self.pm)
        self.parent().toast("二维码图片已复制，可以直接粘贴到 QQ / 微信")

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存二维码", os.path.join(os.path.expanduser("~"), "Desktop",
                                              "舰长礼物登记二维码.png"), "图片 (*.png)")
        if path and self.pm.save(path):
            self.parent().toast(f"已保存：{path}")


class RequestsDialog(QDialog):
    """观众换了设备在网页上提交的地址修改，需要主播确认"""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("地址修改申请")
        self.resize(900, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(label("观众用<b>其他手机 / 浏览器</b>修改收货信息时，为防止别人冒用 UID 改地址，需要你确认。"
                            "可以对照原地址、或联系本人核实后再同意。", "hint", True))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["提交时间", "舰长", "原收货信息", "新收货信息"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.empty = label("", "hint")
        row.addWidget(self.empty)
        row.addStretch(1)
        row.addWidget(btn("✔ 同意修改", "ok", lambda: self.resolve(True)))
        row.addWidget(btn("✘ 拒绝", "danger", lambda: self.resolve(False)))
        row.addWidget(btn("关闭", None, self.accept))
        lay.addLayout(row)
        self.load()

    def load(self):
        self.rows = db.list_requests()
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            old = "\n".join(x for x in (r["old_receiver"], r["old_phone"], r["old_address"]) if x) or "（之前未填写）"
            new = f"{r['receiver']}\n{r['phone']}\n{r['address']}"
            vals = [r["created_at"][5:16], f"{r['bili_name'] or ''}\nUID {r['bili_uid'] or '-'}", old, new]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, r["id"])
                if j == 3:
                    it.setForeground(QColor("#1a7f3c"))
                if r.get("note"):
                    it.setToolTip(r["note"])
                self.table.setItem(i, j, it)
        self.table.resizeRowsToContents()
        self.empty.setText("" if self.rows else "暂时没有待确认的修改申请 🎉")

    def resolve(self, approve):
        ids = list(dict.fromkeys(self.table.item(ix.row(), 0).data(Qt.UserRole)
                                 for ix in self.table.selectionModel().selectedRows()))
        if not ids:
            QMessageBox.information(self, APP_NAME, "请先选中一条申请。")
            return
        if not approve and QMessageBox.question(self, APP_NAME, f"确定拒绝选中的 {len(ids)} 条修改申请吗？") \
                != QMessageBox.Yes:
            return
        for rid in ids:
            r = db.resolve_request(rid, approve)
            if r and approve:
                self.win.notify_qq(r["qq"], "✅ 你的舰长礼物收货信息修改已经主播确认生效。")
        self.load()
        self.win.data_changed()


# ============================== 主窗口 ==============================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = C.load()
        db.init()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1180, 760)
        self.status_info = {}

        self.bot = BotController()
        self.api = Api(self)

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QWidget()
        side.setFixedWidth(200)
        side.setStyleSheet("background:#ffffff;")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(label("🚢 舰长礼物小助手", "brand"))
        self.nav = QListWidget()
        self.nav.setObjectName("sidebar")
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for t in ["🏠  运行状态", "🚢  舰长管理", "📊  查询对比", "⚙  设置", "❓  使用帮助"]:
            self.nav.addItem(QListWidgetItem(t))
        sl.addWidget(self.nav, 1)
        self.side_status = label("", "hint", True)
        self.side_status.setContentsMargins(16, 8, 12, 14)
        sl.addWidget(self.side_status)
        h.addWidget(side)

        self.stack = QStackedWidget()
        self.dash = DashboardPage(self)
        self.guards = GuardsPage(self)
        from .gui_history import HistoryPage
        self.history = HistoryPage(self)
        self.settings = SettingsPage(self)
        self.help = HelpPage(self)
        for p in (self.dash, self.guards, self.history, self.settings, self.help):
            self.stack.addWidget(p)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.currentRowChanged.connect(self._nav)

        self.bot.log.connect(self.dash.append_log)
        self.bot.running_changed.connect(self._bot_state)

        self.settings.load(self.cfg)
        self.help.refresh()
        self.guards.reload_all()
        self._last_counts = None
        self.update_counts()
        self._bot_state(False)

        self.poll = QTimer(self)
        self.poll.timeout.connect(self._tick)
        self.poll.start(3000)

        self.nav.setCurrentRow(0)
        QTimer.singleShot(300, self._first_run)

    # ---------- 导航 ----------
    def _nav(self, i):
        self.stack.setCurrentIndex(i)
        if i == 1:
            self.guards.reload_all()
        elif i == 2:
            self.history.reload()

    def _first_run(self):
        if not self.cfg["streamer"]["room_id"]:
            self.nav.setCurrentRow(3)
            QMessageBox.information(self, APP_NAME,
                                    "欢迎使用舰长礼物小助手！\n\n第一次使用，请先在「设置」里填写你的直播间号，"
                                    "然后点「保存设置」。\n\n之后把「观众登记网页」的网址或二维码发给舰长们，"
                                    "他们就能自助验证、填写收货地址。详细步骤见左边的「使用帮助」。")
        elif self.cfg["gui"]["autostart"]:
            self.start_bot()

    def toast(self, text):
        self.statusBar().showMessage(text, 4000)

    # ---------- 后台服务 ----------
    def toggle_bot(self):
        if self.bot.running():
            self.bot.stop()
        else:
            self.start_bot()

    def start_bot(self):
        if not self.cfg["streamer"]["room_id"]:
            QMessageBox.information(self, APP_NAME, "请先在「设置」里填写直播间号并保存。")
            self.nav.setCurrentRow(3)
            return
        err = self.bot.start(int(self.cfg["web"]["port"]))
        if err:
            self.dash.append_log("[界面] " + err)
            QMessageBox.warning(self, APP_NAME, err)

    def restart_bot(self, old_port=None):
        self.bot.stop()
        port = int(old_port or self.cfg["web"]["port"])

        def wait_then_start(n=0):
            if port_in_use(port) and n < 20:          # 等旧进程彻底释放端口
                QTimer.singleShot(250, lambda: wait_then_start(n + 1))
            else:
                self.start_bot()
        wait_then_start()

    def _bot_state(self, running):
        self.dash.b_toggle.setText("停止服务" if running else "启动服务")
        self.dash.b_toggle.setProperty("kind", "danger" if running else "primary")
        self.dash.b_toggle.style().unpolish(self.dash.b_toggle)
        self.dash.b_toggle.style().polish(self.dash.b_toggle)
        if running:
            self.dash.c_bot.set("● 启动中…", "正在加载（第一次启动可能要十几秒）", "#c27c00")
        else:
            self.status_info = {}
            self.dash.c_bot.set("○ 未运行", "点下面的按钮启动。服务运行时，请不要关闭本窗口。", "#9499a0")
            self._qq_card(None)
            self.dash.c_bili.set("—", "服务启动后显示")
        self._web_card()
        self._side()

    def _web_card(self):
        url = self.cfg["web"]["public_url"]
        local = "127.0.0.1" in url or "localhost" in url
        if not self.bot.running():
            self.dash.c_web.set("○ 未开放", url + "\n启动服务后观众才能打开", "#9499a0")
        elif local:
            self.dash.c_web.set("● 仅本机", url + "\n观众用手机打不开，请在设置里填写观众访问地址（见使用帮助）", "#c27c00")
        else:
            self.dash.c_web.set("● 已开放", url + "\n把网址或二维码发给舰长们", "#1a7f3c")

    def _qq_card(self, data):
        d = self.dash
        on = bool(self.cfg["qq"]["enabled"])
        official = self.cfg["qq"].get("mode", "official") == "official"
        d.b_qq_launch.setVisible(on and not official)
        d.b_qq_copy.setVisible(on and not official)
        d.b_qq_set.setVisible(not on or official)
        if not on:
            d.c_qq.set("未启用", "收集地址不需要 QQ 机器人。想在舰长群里核对入群口令，可在设置里启用 QQ 官方机器人",
                       "#9499a0")
        elif data is None:
            d.c_qq.set("—", "服务启动后显示")
        elif official and data.get("bots"):
            d.c_qq.set("● 已连接", "QQ 官方机器人 · 群里「@机器人 口令 XXXXXX」核对入群口令"
                       + (" · 自动审核入群申请" if self.cfg["qq"].get("member_events") else ""), "#1a7f3c")
        elif official:
            d.c_qq.set("○ 未连接", "正在连接 QQ 开放平台。一直连不上的话：检查 AppID / AppSecret，"
                       "以及开放平台「开发设置」的 IP 白名单（见运行日志）", "#c27c00")
        elif data.get("bots"):
            d.c_qq.set("● 已连接", f"机器人 QQ：{', '.join(data['bots'])}", "#1a7f3c")
        else:
            d.c_qq.set("○ 未连接", f"请启动 QQ 协议端（LLBot 等），开启反向 WebSocket：{self.ws_url()}", "#c27c00")

    def _tick(self):
        self.update_counts()
        if not self.bot.running():
            return

        def got(ok, data):
            if not self.bot.running():
                return
            if not ok or not isinstance(data, dict):
                self.dash.c_bot.set("● 启动中…", "正在加载（第一次启动可能要十几秒）", "#c27c00")
                return
            self.status_info = data
            self.dash.c_bot.set("● 运行中", "登记网页" + ("和 QQ 机器人" if data.get("qq_enabled") else "") +
                                "正在运行，请不要关闭本窗口", "#1a7f3c")
            self._web_card()
            self._qq_card(data)
            n = data.get("guards")
            if n is None:
                self.dash.c_bili.set("获取中…" if not data.get("guards_time") else "获取失败",
                                     f"直播间 {self.cfg['streamer']['room_id']}", "#c27c00")
            else:
                t = time.strftime("%H:%M", time.localtime(data.get("guards_time") or time.time()))
                self.dash.c_bili.set(f"{n} 人", f"直播间 {self.cfg['streamer']['room_id']} · 更新于 {t}（每 5 分钟自动更新）")
            self._side()
        self.api.get("/api/status", got)

    def update_counts(self):
        rows = db.list_guards(db.this_month())
        v = sum(r["status"] == "verified" for r in rows)
        p = sum(r["status"] == "pending" for r in rows)
        na = sum(r["status"] == "verified" and not r["address"] for r in rows)
        sh = sum(r["status"] == "verified" and bool(r["shipped"]) for r in rows)
        self.dash.c_month.title.setText(f"📦 本月舰长（{db.this_month()}）")
        au = sum(r["status"] == "verified" and r.get("source") == "auto" for r in rows)
        old, p_all = db.backlog_counts(db.this_month())   # 往期没发货的（含服务停机后补登的）、所有月份的待审核
        self.dash.c_month.set(f"已验证 {v} 人", f"其中自动续登 {au} · 待审核 {p} · 未填地址 {na} · 已发货 {sh}\n"
                                             f"需准备：{gift_counts(rows)}"
                                             + (f"\n⚠ 往期还有 {old} 条未发货" if old else ""))
        self.dash.c_month.setToolTip("往期未发货的记录：在「舰长管理」选「全部月份」和「待发货」查看" if old else "")
        rq = db.count_pending_requests()
        self.dash.c_req.set(f"{rq} 条待确认" if rq else "暂无", "观众换了手机修改地址时，需要你确认后才生效",
                            "#c27c00" if rq else "#9499a0")
        self.guards.b_req.setText(f"📝 修改申请（{rq}）" if rq else "📝 修改申请")
        self.nav.item(1).setText("🚢  舰长管理" + (f"  🔴 {p_all + rq}" if p_all + rq else ""))
        tip = f"{p_all} 条待审核" + (f"（其中 {p_all - p} 条在往期月份，选「全部月份」查看）" if p_all > p else "")
        self.nav.item(1).setToolTip(f"{tip}，{rq} 条地址修改申请" if p_all + rq else "")
        counts = (len(rows), v, p_all, na, sum(len(r["address"] or "") for r in rows), rq, sh, p, old)
        if self._last_counts is not None and counts != self._last_counts and self.stack.currentIndex() == 1:
            self.guards.load()                        # 有新数据时自动刷新表格
        if self._last_counts is not None and p_all > self._last_counts[2]:
            self.toast(f"🔔 有新的待审核舰长（{p_all} 条待审核）")
            QApplication.alert(self)
        elif self._last_counts is not None and rq > self._last_counts[5]:
            self.toast(f"🔔 有新的地址修改申请（{rq} 条待确认）")
            QApplication.alert(self)
        self._last_counts = counts

    def _side(self):
        run = self.bot.running()
        bots = self.status_info.get("bots") or []
        qq_line = ""
        if run and self.cfg["qq"]["enabled"]:
            qq_line = "\n🟢 QQ 已连接" if bots else "\n🟠 QQ 未连接"
        self.side_status.setText(("🟢 服务运行中" if run else "⚪ 服务未运行") + qq_line)

    def data_changed(self):
        self.guards.load()
        self.update_counts()

    def settings_saved(self):
        self.help.refresh()
        self._web_card()
        self._qq_card(self.status_info if self.bot.running() else None)
        self._tick()

    # ---------- 登记网页 ----------
    def copy_public_url(self):
        QGuiApplication.clipboard().setText(self.cfg["web"]["public_url"])
        self.toast(f"已复制登记网址：{self.cfg['web']['public_url']}")

    def open_public_url(self):
        if not self.bot.running():
            QMessageBox.information(self, APP_NAME, "请先启动服务。")
            return
        QDesktopServices.openUrl(QUrl(f"http://127.0.0.1:{self.cfg['web']['port']}/"))

    def show_qr(self):
        QrDialog(self, self.cfg["web"]["public_url"], self.cfg["streamer"]["name"]).exec()

    def open_requests(self):
        RequestsDialog(self).exec()
        self.update_counts()

    # ---------- 需要机器人帮忙的操作 ----------
    def notify_qq(self, qq, text):
        if not db.is_qq(qq):
            self.dash.append_log("[界面] 这位舰长不是通过 QQ 登记的，没法私聊通知（网页登记的再次打开登记页面就能看到结果）")
            return
        if not self.cfg["qq"]["enabled"]:
            self.dash.append_log(f"[界面] 未启用 QQ 机器人，没有通知 QQ {qq}")
            return
        if self.cfg["qq"].get("mode", "official") == "official":
            self.dash.append_log(f"[界面] QQ 官方机器人不能按 QQ 号私聊，没有通知 QQ {qq}（可以手动告诉对方）")
            return
        if not self.bot.running() or not (self.status_info.get("bots")):
            self.dash.append_log(f"[界面] QQ 未连接，没能通知 {qq}（数据已保存，可以之后手动告诉对方）")
            return

        def done(ok, data):
            sent = ok and isinstance(data, dict) and data.get("ok")
            self.dash.append_log(f"[界面] 通知 QQ {qq}：{'已发送' if sent else '发送失败'}")
        self.api.post("/api/notify", {"qq": qq, "text": text}, done)

    def remind(self):
        rows = [r for r in db.list_guards(db.this_month(), "verified") if not r["address"]]
        if not rows:
            QMessageBox.information(self, APP_NAME, "本月已验证的舰长都填好地址啦 🎉")
            return
        qq_rows = [r for r in rows if db.is_qq(r["qq"])]
        if not self.cfg["qq"]["enabled"] or self.cfg["qq"].get("mode", "official") == "official" or not qq_rows:
            QGuiApplication.clipboard().setText(self.help.notice())
            QMessageBox.information(self, APP_NAME, f"本月还有 {len(rows)} 位舰长没填地址。\n\n"
                                    "没有启用 QQ 机器人（或他们是网页登记的），没法私聊提醒。"
                                    "群公告已复制到剪贴板，可以发到舰长群 / 动态里提醒大家。")
            return
        if not self.status_info.get("bots"):
            QMessageBox.warning(self, APP_NAME, "QQ 还没连接，无法发送提醒。请先启动服务并连接 QQ 协议端。")
            return
        if QMessageBox.question(self, APP_NAME, f"将私聊提醒 {len(rows)} 位还没填地址的舰长（每条间隔 1.5 秒），确定吗？") \
                != QMessageBox.Yes:
            return
        self.dash.append_log(f"[界面] 开始提醒 {len(rows)} 位舰长填写地址……")

        def done(ok, data):
            if ok and isinstance(data, dict):
                self.dash.append_log(f"[界面] 提醒完成：成功 {data.get('sent')} / 共 {data.get('total')}")
            else:
                self.dash.append_log("[界面] 提醒请求失败")
        self.api.post("/api/remind", {}, done)

    def refresh_guard_list(self):
        if not self.bot.running():
            QMessageBox.information(self, APP_NAME, "请先启动服务。")
            return
        self.dash.c_bili.set("刷新中…", None, "#c27c00")
        self.api.post("/api/refresh_guards", {}, lambda ok, d: self._tick())

    def ws_url(self):
        return f"ws://127.0.0.1:{self.cfg['web']['port']}/onebot/v11/ws"

    def copy_ws_url(self):
        QGuiApplication.clipboard().setText(self.ws_url())
        self.toast(f"已复制：{self.ws_url()}（粘贴到 QQ 协议端的反向 WebSocket 地址）")

    def launch_napcat(self):
        p = (self.cfg["napcat"]["path"] or "").strip()
        if not p or not os.path.exists(p):
            if QMessageBox.question(self, APP_NAME, "还没有设置 QQ 协议端（LLBot / NapCat）的位置。\n\n"
                                                    "现在选择它的启动文件吗？（还没下载的话，请先看「使用帮助」）") == QMessageBox.Yes:
                self.nav.setCurrentRow(3)
                self.settings.browse_napcat()
                if self.settings.napcat.text():
                    self.settings.save()
            return
        if not QProcess.startDetached(p, [], os.path.dirname(p))[0]:
            os.startfile(p)                           # noqa: S606  兜底：用系统方式打开
        self.toast("已启动 QQ 协议端，请在弹出的窗口里登录机器人 QQ")

    def open_web_admin(self):
        if not self.bot.running():
            QMessageBox.information(self, APP_NAME, "请先启动服务。")
            return
        QGuiApplication.clipboard().setText(self.cfg["web"]["admin_password"])
        QDesktopServices.openUrl(QUrl(f"http://127.0.0.1:{self.cfg['web']['port']}/admin"))
        self.toast("后台密码已复制到剪贴板：用户名随便填，密码直接粘贴")

    def closeEvent(self, e):
        if self.bot.running():
            if QMessageBox.question(self, APP_NAME, "关闭窗口会同时停止后台服务，观众将打不开登记网页。\n\n确定退出吗？") \
                    != QMessageBox.Yes:
                e.ignore()
                return
            self.bot.stop()
        e.accept()


def apply_theme(app):
    """固定使用浅色主题：系统是深色模式时也不会出现黑底黑字"""
    from PySide6.QtGui import QPalette
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except AttributeError:
        pass
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in [(QPalette.Window, "#f6f7f9"), (QPalette.WindowText, "#18191c"), (QPalette.Base, "#ffffff"),
                      (QPalette.AlternateBase, "#fbfbfc"), (QPalette.Text, "#18191c"), (QPalette.Button, "#ffffff"),
                      (QPalette.ButtonText, "#18191c"), (QPalette.ToolTipBase, "#ffffff"),
                      (QPalette.ToolTipText, "#18191c"), (QPalette.PlaceholderText, "#9499a0"),
                      (QPalette.Highlight, PINK), (QPalette.HighlightedText, "#ffffff"), (QPalette.Link, PINK)]:
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#c0c4cc"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#c0c4cc"))
    app.setPalette(pal)
    app.setStyleSheet(build_style())


def run_gui():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("guardgift.app")  # 任务栏显示自己的图标
        except Exception:      # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # 对话框按钮显示中文（是/否/取消）
    from PySide6.QtCore import QLibraryInfo, QTranslator
    tr = QTranslator(app)
    for d in (QLibraryInfo.path(QLibraryInfo.TranslationsPath), str(C.RES / "PySide6" / "translations")):
        if tr.load("qtbase_zh_CN", d):
            app.installTranslator(tr)
            break
    apply_theme(app)
    app.setWindowIcon(app_icon())
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
