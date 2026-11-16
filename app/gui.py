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
from PySide6.QtCore import QByteArray, QDate, QObject, QProcess, QProcessEnvironment, Qt, QTimer, QUrl, Signal
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
from . import db

APP_NAME = "舰长礼物小助手"
PINK = "#fb7299"
STATUS_CN = {"verified": "已验证", "pending": "待审核", "rejected": "已拒绝"}
STATUS_COLOR = {"verified": "#1a7f3c", "pending": "#c27c00", "rejected": "#c62828"}
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


def export_xlsx(path, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "发货表"
    head = ["月份", "上舰日期", "等级", "B站昵称", "B站UID", "QQ", "收件人", "手机号", "收货地址", "已发货"]
    ws.append(head)
    for r in rows:
        ws.append([r["month"], r["start_date"], r["guard_level"] or "", r["bili_name"] or "",
                   str(r["bili_uid"] or ""), str(r["qq"]), r["receiver"] or "", str(r["phone"] or ""),
                   r["address"] or "", "是" if r["shipped"] else "否"])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="FB7299")
        cell.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCDEFGHIJ", [9, 12, 7, 18, 13, 13, 10, 14, 48, 8]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
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
        p.errorOccurred.connect(lambda e: self.log.emit(f"[界面] 机器人进程出错：{e}"))
        self.proc = p
        self.log.emit("[界面] 正在启动机器人……")
        p.start()
        self.running_changed.emit(True)
        return None

    def stop(self):
        if self.running():
            self.log.emit("[界面] 正在停止机器人……")
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
        self.log.emit(f"[界面] 机器人已停止（退出码 {code}）")
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
        self.c_bot = Card("🤖 QQ 机器人")
        self.b_toggle = btn("启动机器人", "primary", win.toggle_bot)
        self.c_bot.btns.addWidget(self.b_toggle)
        self.c_bot.btns.addStretch(1)

        self.c_qq = Card("💬 QQ 连接（NapCat）")
        self.c_qq.btns.addWidget(btn("启动 NapCat", None, win.launch_napcat))
        self.c_qq.btns.addWidget(btn("复制连接地址", None, win.copy_ws_url))
        self.c_qq.btns.addStretch(1)

        self.c_bili = Card("📺 B站舰长名单")
        self.c_bili.btns.addWidget(btn("立即刷新", None, win.refresh_guard_list))
        self.c_bili.btns.addStretch(1)

        self.c_month = Card(f"📦 本月舰长")
        self.c_month.btns.addWidget(btn("提醒未填地址的舰长", None, win.remind,
                                        "机器人会私聊本月已验证、但还没填地址的舰长"))
        self.c_month.btns.addStretch(1)

        for i, c in enumerate([self.c_bot, self.c_qq, self.c_bili, self.c_month]):
            c.setMinimumHeight(150)
            grid.addWidget(c, i // 2, i % 2)
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
COLS = ["上舰日期", "状态", "等级", "B站昵称", "B站UID", "QQ", "收件人", "手机号", "收货地址", "发货", "备注"]


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
        for k, v in STATUS_CN.items():
            self.status.addItem(v, k)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索 QQ / 昵称 / UID / 收件人 / 手机号")
        self.search.setClearButtonEnabled(True)
        self.month.currentIndexChanged.connect(self.load)
        self.status.currentIndexChanged.connect(self.load)
        self.search.textChanged.connect(self.load)
        bar.addWidget(label("月份"))
        bar.addWidget(self.month)
        bar.addWidget(self.status)
        bar.addWidget(self.search, 1)
        bar.addWidget(btn("刷新", None, self.reload_all))
        bar.addWidget(btn("＋ 手动添加", None, self.add_guard))
        bar.addWidget(btn("导出 Excel 发货表", "primary", self.export))
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
        hh.setSectionResizeMode(8, QHeaderView.Stretch)
        hh.setSectionResizeMode(10, QHeaderView.Interactive)
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
        acts.addWidget(label("可按住 Ctrl / Shift 多选；双击一行编辑地址", "hint"))
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
                                           ("qq", "bili_name", "bili_uid", "receiver", "phone", "address"))]
        self.rows = rows
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = [r["start_date"], STATUS_CN.get(r["status"], r["status"]), r["guard_level"] or "",
                    r["bili_name"] or "", r["bili_uid"] or "", r["qq"], r["receiver"] or "",
                    r["phone"] or "", r["address"] or ("（未填写）" if r["status"] == "verified" else ""),
                    "已发货" if r["shipped"] else "", r["note"] or ""]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setData(Qt.UserRole, r["id"])
                if j == 1:
                    it.setForeground(QColor(STATUS_COLOR.get(r["status"], "#18191c")))
                    f = it.font(); f.setBold(True); it.setFont(f)
                if j == 8 and not r["address"]:
                    it.setForeground(QColor("#c27c00"))
                if j == 9 and r["shipped"]:
                    it.setForeground(QColor("#1a7f3c"))
                if j in (8, 10):
                    it.setToolTip(str(v))
                t.setItem(i, j, it)
        t.setColumnWidth(10, 220)
        if selected:
            for i, r in enumerate(rows):
                if r["id"] in selected:
                    t.selectRow(i)
        v = sum(r["status"] == "verified" for r in rows)
        p = sum(r["status"] == "pending" for r in rows)
        na = sum(r["status"] == "verified" and not r["address"] for r in rows)
        sh = sum(bool(r["shipped"]) for r in rows)
        self.summary.setText(f"共 {len(rows)} 条 · 已验证 {v} · 待审核 {p} · 未填地址 {na} · 已发货 {sh}")
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
        n = len(self.selected_ids())
        for b in (self.b_ok, self.b_no, self.b_ship, self.b_unship, self.b_del):
            b.setEnabled(n > 0)
        for b in (self.b_addr, self.b_shot):
            b.setEnabled(n == 1)

    def menu(self, pos):
        if not self.selected_ids():
            return
        m = QMenu(self)
        m.addAction("✔ 通过", self.approve)
        m.addAction("✘ 拒绝", self.reject)
        m.addSeparator()
        m.addAction("📦 标记已发货", lambda: self.ship(True))
        m.addAction("取消发货", lambda: self.ship(False))
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
        rows = db.list_guards(month or None, "verified")
        if not rows:
            QMessageBox.information(self, APP_NAME, "当前月份还没有已验证的舰长。")
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
        msg = f"已导出 {len(rows)} 位已验证舰长。" + (f"\n其中 {na} 人还没填地址。" if na else "")
        if QMessageBox.question(self, APP_NAME, msg + "\n\n现在打开这个表格吗？") == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class AddressDialog(QDialog):
    def __init__(self, parent, r):
        super().__init__(parent)
        self.r = r
        self.setWindowTitle(f"编辑收货信息 — {r['bili_name'] or r['qq']}")
        self.setMinimumWidth(460)
        lay = QFormLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        self.receiver = QLineEdit(r["receiver"] or "")
        self.phone = QLineEdit(r["phone"] or "")
        self.addr = QPlainTextEdit(r["address"] or "")
        self.addr.setFixedHeight(80)
        lay.addRow(label(f"QQ {r['qq']} · {r['guard_level'] or ''} · 上舰 {r['start_date']}", "hint"))
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
        self.qq.setPlaceholderText("必填")
        self.name = QLineEdit()
        self.uid = QLineEdit()
        self.level = QComboBox()
        self.level.addItems(["舰长", "提督", "总督"])
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        lay.addRow(label("适用于识别失败或没进群的观众。添加后对方私聊机器人「改地址」即可填写。", "hint", True))
        lay.addRow("QQ 号", self.qq)
        lay.addRow("B站昵称", self.name)
        lay.addRow("B站 UID", self.uid)
        lay.addRow("等级", self.level)
        lay.addRow("上舰日期", self.day)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("添加")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def save(self):
        qq = re.sub(r"\D", "", self.qq.text())
        if not qq:
            QMessageBox.warning(self, APP_NAME, "请填写 QQ 号。")
            return
        d = self.day.date().toString("yyyy-MM-dd")
        db.upsert_guard(qq, d[:7], d, "verified", bili_uid=re.sub(r"\D", "", self.uid.text()) or None,
                        bili_name=self.name.text().strip() or None, guard_level=self.level.currentText(),
                        source="manual", note="主播手动添加")
        self.accept()


class ShotDialog(QDialog):
    def __init__(self, parent, r):
        super().__init__(parent)
        self.setWindowTitle(f"上舰截图 — {r['bili_name'] or r['qq']}")
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
        g2 = QGroupBox("② QQ 机器人")
        f2 = QFormLayout(g2)
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
        self.token.setPlaceholderText("可留空；如果在 NapCat 里设置了 token，这里填一样的")
        f2.addRow("连接令牌 token", self.token)
        nr = QHBoxLayout()
        self.napcat = QLineEdit()
        self.napcat.setPlaceholderText("可选：选择 NapCat 的启动文件（.bat 或 .exe），之后可一键启动")
        nr.addWidget(self.napcat, 1)
        nr.addWidget(btn("浏览…", None, self.browse_napcat))
        f2.addRow("NapCat 位置", nr)
        lay.addWidget(g2)

        # --- 网页 ---
        g3 = QGroupBox("③ 地址填写网页")
        f3 = QFormLayout(g3)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        f3.addRow("端口", self.port)
        self.public_url = QLineEdit()
        f3.addRow("观众访问地址", self.public_url)
        f3.addRow("", label("观众在手机上打开填写链接用的地址。只在本机测试用 http://127.0.0.1:端口；"
                            "要让观众能打开，请填内网穿透或云服务器的地址（见「帮助」）。", "hint", True))
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
        lay.addWidget(g3)

        # --- 规则 ---
        g4 = QGroupBox("④ 规则与启动")
        f4 = QFormLayout(g4)
        self.grace = QSpinBox()
        self.grace.setRange(0, 15)
        self.grace.setSuffix(" 天")
        self.grace.setSpecialValueText("不宽限（只允许本月舰长修改）")
        f4.addRow("跨月宽限", self.grace)
        f4.addRow("", label("例如设为 3：每月 1～3 号，上个月的舰长也还能修改地址。", "hint", True))
        self.autostart = QCheckBox("打开软件时自动启动机器人")
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
        self._last_port = int(w["port"])
        self.port.blockSignals(True)
        self.port.setValue(int(w["port"]))
        self.port.blockSignals(False)
        self.public_url.setText(w["public_url"])
        self.password.setText(w["admin_password"])
        self.hours.setValue(int(w["link_valid_hours"]))
        self.grace.setValue(int(c["rules"]["edit_grace_days"] or 0))
        self.autostart.setChecked(bool(c["gui"]["autostart"]))

    def _port_changed(self, v):
        # 观众地址还是本机默认地址时，跟着端口一起改
        u = self.public_url.text().strip()
        if self._last_port and re.fullmatch(rf"http://(127\.0\.0\.1|localhost):{self._last_port}/?", u):
            self.public_url.setText(f"http://127.0.0.1:{v}")
        self._last_port = v

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
                        link_valid_hours=self.hours.value())
        c["rules"]["edit_grace_days"] = self.grace.value()
        c["napcat"]["path"] = self.napcat.text().strip()
        c["gui"]["autostart"] = self.autostart.isChecked()
        return c

    def save(self):
        try:
            c = self.collect()
        except ValueError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        if not c["qq"]["groups"]:
            if QMessageBox.question(self, APP_NAME, "还没有填写舰长群群号，机器人将不会处理任何群消息。\n仍然保存吗？") \
                    != QMessageBox.Yes:
                return
        old_port = self.win.cfg["web"]["port"]
        C.save(c)
        self.win.cfg = c
        self.win.settings_saved()
        if self.win.bot.running():
            if QMessageBox.question(self, APP_NAME, "设置已保存。需要重启机器人才能生效，现在重启吗？") == QMessageBox.Yes:
                self.win.restart_bot(old_port)
        else:
            self.win.toast("设置已保存")


# ============================== 页面：帮助 ==============================
class HelpPage(QWidget):
    NOTICE = ("📢 上舰的宝宝们看这里～\n"
              "1. 在群里发一张上舰截图（要能看到主播名和开通时间），机器人会自动帮你验证\n"
              "2. 验证通过后，私聊机器人发「填地址」，打开链接填写收件人、手机号、地址\n"
              "3. 地址或手机号要改？私聊机器人说「我要改地址」就行\n"
              "4. 想看自己填的信息？私聊机器人发「我的信息」\n"
              "⚠️ 只有本月上舰的舰长才能填写和修改地址哦")

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
        top.addWidget(btn("复制 NapCat 连接地址", None, win.copy_ws_url))
        lay.addLayout(top)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        lay.addWidget(self.text, 1)

    def copy_notice(self):
        QGuiApplication.clipboard().setText(self.NOTICE)
        self.win.toast("群公告已复制，可以直接粘贴到 QQ 群")

    def refresh(self):
        c = self.win.cfg
        port, tok = c["web"]["port"], c["qq"]["access_token"]
        ws = f"ws://127.0.0.1:{port}/onebot/v11/ws"
        notice = html.escape(self.NOTICE).replace("\n", "<br>")
        self.text.setHtml(f"""
<style>h3{{color:{PINK};margin-top:18px}} code{{background:#f1f2f3;padding:2px 6px;border-radius:4px}}
li{{margin:4px 0}} .box{{background:#fff6e0;padding:10px;border-radius:8px}}</style>
<h3>第一次使用，按顺序做这 4 步</h3>
<ol>
<li><b>填设置：</b>左边「⚙ 设置」→ 填直播间号 → 点「检测直播间」→ 填舰长群号、你的 QQ → 保存。</li>
<li><b>准备机器人 QQ：</b>下载 <a href="https://github.com/NapNeko/NapCatQQ/releases">NapCat</a>
  （选名字带 Shell 和 Windows 的一键包），解压后启动，用 <b>QQ 小号</b> 扫码登录。</li>
<li><b>让 NapCat 连上本软件：</b>打开 NapCat 网页控制台 → 网络配置 → 新建 <b>WebSocket 客户端</b>：
  <ul><li>URL 填：<code>{ws}</code>（上方按钮可一键复制）</li>
  <li>消息格式：<code>Array</code>；{'Token 填：<code>' + html.escape(tok) + '</code>' if tok else 'Token 留空'}；打开「启用」并保存</li></ul></li>
<li><b>启动：</b>回到「🏠 运行状态」点「启动机器人」。「QQ 连接」显示 <b>已连接</b> 就大功告成了！
  再把机器人小号拉进舰长群，设成管理员。</li>
</ol>
<h3>舰长怎么用（可复制为群公告）</h3>
<div class="box">{notice}</div>
<h3>你平时要做的</h3>
<ul>
<li>机器人私聊你「待审核」时：到「🚢 舰长管理」选中那条 →「🖼 查看截图」→ 没问题就点「✔ 通过」，机器人会自动通知对方。</li>
<li>准备寄礼物：「舰长管理」选好月份 →「导出 Excel 发货表」；寄出后选中点「📦 标记已发货」。</li>
<li>有人没填地址：「运行状态」点「提醒未填地址的舰长」，机器人会挨个私聊提醒。</li>
<li>观众私信你新地址：在「舰长管理」双击他那一行，直接帮他改。</li>
</ul>
<h3>让观众用手机打开填写链接</h3>
<p>默认的 <code>127.0.0.1</code> 只有这台电脑能打开。要让观众用手机填写，任选一种：</p>
<ul>
<li><b>内网穿透（简单）：</b>用 cpolar 等工具把本机 <code>{port}</code> 端口映射出去，把得到的网址填到设置里的「观众访问地址」。电脑关机时观众填不了。</li>
<li><b>云服务器（长期推荐）：</b>把整个软件文件夹和 NapCat 放到云服务器上运行，开放 <code>{port}</code> 端口，「观众访问地址」填 <code>http://服务器IP:{port}</code>。</li>
</ul>
<h3>数据与隐私</h3>
<p>所有数据都在软件目录下的 <code>data</code> 文件夹（上方「运行状态」页可一键打开）。请定期备份，
<b>不要发给别人</b>：里面有舰长的手机号和地址。</p>
""")


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
        for t in ["🏠  运行状态", "🚢  舰长管理", "⚙  设置", "❓  使用帮助"]:
            self.nav.addItem(QListWidgetItem(t))
        sl.addWidget(self.nav, 1)
        self.side_status = label("", "hint", True)
        self.side_status.setContentsMargins(16, 8, 12, 14)
        sl.addWidget(self.side_status)
        h.addWidget(side)

        self.stack = QStackedWidget()
        self.dash = DashboardPage(self)
        self.guards = GuardsPage(self)
        self.settings = SettingsPage(self)
        self.help = HelpPage(self)
        for p in (self.dash, self.guards, self.settings, self.help):
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

    def _first_run(self):
        if not self.cfg["streamer"]["room_id"]:
            self.nav.setCurrentRow(2)
            QMessageBox.information(self, APP_NAME,
                                    "欢迎使用舰长礼物小助手！\n\n第一次使用，请先在「设置」里填写你的直播间号、"
                                    "舰长群群号和你自己的 QQ，然后点「保存设置」。\n\n"
                                    "接入 QQ 的步骤可以看左边的「使用帮助」。")
        elif self.cfg["gui"]["autostart"]:
            self.start_bot()

    def toast(self, text):
        self.statusBar().showMessage(text, 4000)

    # ---------- 机器人 ----------
    def toggle_bot(self):
        if self.bot.running():
            self.bot.stop()
        else:
            self.start_bot()

    def start_bot(self):
        if not self.cfg["streamer"]["room_id"]:
            QMessageBox.information(self, APP_NAME, "请先在「设置」里填写直播间号并保存。")
            self.nav.setCurrentRow(2)
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
        self.dash.b_toggle.setText("停止机器人" if running else "启动机器人")
        self.dash.b_toggle.setProperty("kind", "danger" if running else "primary")
        self.dash.b_toggle.style().unpolish(self.dash.b_toggle)
        self.dash.b_toggle.style().polish(self.dash.b_toggle)
        if running:
            self.dash.c_bot.set("● 启动中…", "正在加载（第一次启动可能要十几秒）", "#c27c00")
        else:
            self.status_info = {}
            self.dash.c_bot.set("○ 未运行", "点下面的按钮启动。机器人运行时，请不要关闭本窗口。", "#9499a0")
            self.dash.c_qq.set("—", "机器人启动后显示")
            self.dash.c_bili.set("—", "机器人启动后显示")
        self._side()

    def _tick(self):
        self.update_counts()
        if not self.bot.running():
            return

        def got(ok, data):
            if not self.bot.running():
                return
            port = self.cfg["web"]["port"]
            if not ok or not isinstance(data, dict):
                self.dash.c_bot.set("● 启动中…", "正在加载（第一次启动可能要十几秒）", "#c27c00")
                return
            self.status_info = data
            self.dash.c_bot.set("● 运行中", f"观众填写网页：{self.cfg['web']['public_url']}", "#1a7f3c")
            bots = data.get("bots") or []
            if bots:
                self.dash.c_qq.set("● 已连接", f"机器人 QQ：{', '.join(bots)}", "#1a7f3c")
            else:
                self.dash.c_qq.set("○ 未连接",
                                   f"请启动 NapCat，并在其中添加 WebSocket 客户端："
                                   f"ws://127.0.0.1:{port}/onebot/v11/ws（详见「使用帮助」）", "#c27c00")
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
        self.dash.c_month.set(f"已验证 {v} 人", f"待审核 {p} · 未填地址 {na} · 已发货 {sh}")
        self.nav.item(1).setText("🚢  舰长管理" + (f"  🔴 {p}" if p else ""))
        self.nav.item(1).setToolTip(f"{p} 人待审核" if p else "")
        counts = (len(rows), v, p, na, sum(len(r["address"] or "") for r in rows))
        if self._last_counts is not None and counts != self._last_counts and self.stack.currentIndex() == 1:
            self.guards.load()                        # 有新数据时自动刷新表格
        if self._last_counts is not None and p > self._last_counts[2]:
            self.toast(f"🔔 有新的待审核舰长（{p} 人待审核）")
            QApplication.alert(self)
        self._last_counts = counts

    def _side(self):
        run = self.bot.running()
        bots = self.status_info.get("bots") or []
        self.side_status.setText(("🟢 机器人运行中" if run else "⚪ 机器人未运行") +
                                 ("\n🟢 QQ 已连接" if bots else ("\n🟠 QQ 未连接" if run else "")))

    def data_changed(self):
        self.guards.load()
        self.update_counts()

    def settings_saved(self):
        self.help.refresh()
        self._tick()

    # ---------- 需要机器人帮忙的操作 ----------
    def notify_qq(self, qq, text):
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
        if not self.status_info.get("bots"):
            QMessageBox.warning(self, APP_NAME, "QQ 还没连接，无法发送提醒。请先启动机器人并连接 NapCat。")
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
            QMessageBox.information(self, APP_NAME, "请先启动机器人。")
            return
        self.dash.c_bili.set("刷新中…", None, "#c27c00")
        self.api.post("/api/refresh_guards", {}, lambda ok, d: self._tick())

    def ws_url(self):
        return f"ws://127.0.0.1:{self.cfg['web']['port']}/onebot/v11/ws"

    def copy_ws_url(self):
        QGuiApplication.clipboard().setText(self.ws_url())
        self.toast(f"已复制：{self.ws_url()}（粘贴到 NapCat 的 WebSocket 客户端 URL）")

    def launch_napcat(self):
        p = (self.cfg["napcat"]["path"] or "").strip()
        if not p or not os.path.exists(p):
            if QMessageBox.question(self, APP_NAME, "还没有设置 NapCat 的位置。\n\n现在选择 NapCat 的启动文件吗？"
                                                    "（还没下载的话，请先看「使用帮助」）") == QMessageBox.Yes:
                self.nav.setCurrentRow(2)
                self.settings.browse_napcat()
                if self.settings.napcat.text():
                    self.settings.save()
            return
        if not QProcess.startDetached(p, [], os.path.dirname(p))[0]:
            os.startfile(p)                           # noqa: S606  兜底：用系统方式打开
        self.toast("已启动 NapCat，请在弹出的窗口里扫码登录机器人 QQ")

    def open_web_admin(self):
        if not self.bot.running():
            QMessageBox.information(self, APP_NAME, "请先启动机器人（网页后台和机器人一起运行）。")
            return
        QGuiApplication.clipboard().setText(self.cfg["web"]["admin_password"])
        QDesktopServices.openUrl(QUrl(f"http://127.0.0.1:{self.cfg['web']['port']}/admin"))
        self.toast("后台密码已复制到剪贴板：用户名随便填，密码直接粘贴")

    def closeEvent(self, e):
        if self.bot.running():
            if QMessageBox.question(self, APP_NAME, "关闭窗口会同时停止 QQ 机器人，观众将无法验证和填写地址。\n\n确定退出吗？") \
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
