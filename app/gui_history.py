"""界面：「查询对比」页 + 「导入以前的表格」对话框"""
import os
from datetime import date

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLineEdit, QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import db, history
from .gui import APP_NAME, PINK, AddressDialog, btn, gifts_text, label, run_bg, status_text, who

NONE = "（不导入）"


def make_table(cols, stretch=None):
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.setWordWrap(False)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch is not None:
        hh.setSectionResizeMode(stretch, QHeaderView.Stretch)
    return t


def fill(t, rows, colors=None, ids=None, tips=None):
    """rows: 每行一个值列表；colors: {(行, 列): 颜色}；ids: 每行对应的记录 id（双击编辑用）"""
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for i, vals in enumerate(rows):
        for j, v in enumerate(vals):
            it = QTableWidgetItem("" if v is None else str(v))
            if ids:
                it.setData(Qt.UserRole, ids[i])
            c = (colors or {}).get((i, j))
            if c:
                it.setForeground(QColor(c))
            if tips and (i, j) in tips:
                it.setToolTip(tips[(i, j)])
            elif len(str(v or "")) > 24:
                it.setToolTip(str(v))
            t.setItem(i, j, it)
    t.setSortingEnabled(True)


def export_table(parent, t, name):
    if t.rowCount() == 0:
        QMessageBox.information(parent, APP_NAME, "当前没有可以导出的结果。")
        return
    path, _ = QFileDialog.getSaveFileName(parent, "导出结果", os.path.join(os.path.expanduser("~"), "Desktop", name),
                                          "Excel 表格 (*.xlsx)")
    if not path:
        return
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.append([t.horizontalHeaderItem(j).text() for j in range(t.columnCount())])
    for i in range(t.rowCount()):
        ws.append([(t.item(i, j).text() if t.item(i, j) else "") for j in range(t.columnCount())])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="FB7299")
    ws.freeze_panes = "A2"
    try:
        wb.save(path)
    except PermissionError:
        QMessageBox.warning(parent, APP_NAME, "保存失败：文件可能正在被 Excel 打开，请先关闭再导出。")
        return
    if QMessageBox.question(parent, APP_NAME, f"已导出 {t.rowCount()} 行。现在打开吗？") == QMessageBox.Yes:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ============================== 查询对比页 ==============================
class HistoryPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("page")
        self.rows = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("查询对比", "pageTitle"))
        top.addStretch(1)
        self.summary = label("", "hint")
        top.addWidget(self.summary)
        top.addWidget(btn("📥 导入以前的表格", "primary", self.do_import,
                          "把以前几个月收集的 Excel / CSV 表格导入进来，之后可以查询和对比"))
        lay.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs, 1)
        self._build_search()
        self._build_compare()
        self._build_changes()
        self._build_bili()
        self._build_check()
        self.tabs.currentChanged.connect(lambda _i: self.refresh_current())

    # ---------- 公共 ----------
    def _page(self, title, hint):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 10, 0, 0)
        v.setSpacing(8)
        self.tabs.addTab(w, title)
        if hint:
            v.addWidget(label(hint, "hint", True))
        return v

    def _bottom(self, v, t, name, extra=None):
        b = QHBoxLayout()
        info = label("", "hint")
        b.addWidget(info)
        b.addStretch(1)
        b.addWidget(label("双击一行可以编辑收货信息", "hint"))
        if extra:
            b.addWidget(extra)
        b.addWidget(btn("导出这张表", None, lambda: export_table(self, t, name)))
        v.addLayout(b)
        t.doubleClicked.connect(lambda ix, t=t: self._edit(t, ix.row()))
        return info

    def _edit(self, t, row):
        it = t.item(row, 0)
        gid = it.data(Qt.UserRole) if it else None
        r = next((x for x in self.rows if x["id"] == gid), None) if gid else None
        if r and AddressDialog(self, r).exec():
            self.win.data_changed()
            self.reload()

    def _months(self):
        return sorted({r["month"] for r in self.rows}, reverse=True)

    def reload(self):
        self.rows = db.list_guards()
        months = self._months()
        for cb, default in ((self.c_a, 1), (self.c_b, 0)):
            cur = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(months)
            i = cb.findText(cur)
            cb.setCurrentIndex(i if i >= 0 else min(default, len(months) - 1))
            cb.blockSignals(False)
        cur = self.k_month.currentData()
        self.k_month.blockSignals(True)
        self.k_month.clear()
        self.k_month.addItem("全部月份", "")
        for m in months:
            self.k_month.addItem(m, m)
        i = self.k_month.findData(cur if cur is not None else db.this_month())
        self.k_month.setCurrentIndex(max(i, 0))
        self.k_month.blockSignals(False)
        n_imp = sum(r.get("source") == "import" for r in self.rows)
        people = len({history.person_key(r) for r in self.rows})
        self.summary.setText(f"共 {len(self.rows)} 条记录 · {len(months)} 个月 · {people} 人"
                             + (f" · 其中表格导入 {n_imp} 条" if n_imp else ""))
        self.refresh_current()

    def refresh_current(self):
        i = self.tabs.currentIndex()
        [self.run_search, self.run_compare, self.run_changes, lambda: None, self.run_check][i]()

    # ---------- ① 查询 ----------
    S_COLS = ["月份", "本期开始", "状态", "礼物", "B站昵称", "B站UID", "QQ/联系方式", "收件人", "手机号", "收货地址", "发货"]

    def _build_search(self):
        v = self._page("🔍 查询", "在所有月份里搜索：昵称、UID、QQ、联系方式、收件人、手机号、地址都可以，"
                                  "例如输入手机号就能查到他哪几个月上过舰、每次寄到了哪里。")
        bar = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("🔍 输入昵称 / UID / QQ / 收件人 / 手机号 / 地址关键字")
        self.q.setClearButtonEnabled(True)
        self.q.textChanged.connect(self.run_search)
        bar.addWidget(self.q, 1)
        self.q_shipped = QComboBox()
        self.q_shipped.addItems(["全部", "已发货", "未发货"])
        self.q_shipped.currentIndexChanged.connect(self.run_search)
        bar.addWidget(self.q_shipped)
        v.addLayout(bar)
        self.t_search = make_table(self.S_COLS, 9)
        v.addWidget(self.t_search, 1)
        self.i_search = self._bottom(v, self.t_search, "舰长查询结果.xlsx")

    def run_search(self):
        rs = history.search(self.rows, self.q.text())
        s = self.q_shipped.currentIndex()
        if s:
            rs = [r for r in rs if bool(r["shipped"]) == (s == 1)]
        rs = sorted(rs, key=lambda r: (r["month"], r["start_date"]), reverse=True)
        colors = {}
        out = []
        for i, r in enumerate(rs):
            out.append([r["month"], r["start_date"], status_text(r), gifts_text(r), r["bili_name"], r["bili_uid"],
                        who(r), r["receiver"], r["phone"], r["address"], "已发货" if r["shipped"] else ""])
            if r["status"] != "verified":
                colors[(i, 2)] = "#c27c00" if r["status"] == "pending" else "#c62828"
        fill(self.t_search, out, colors, [r["id"] for r in rs])
        people = len({history.person_key(r) for r in rs})
        self.i_search.setText(f"找到 {len(rs)} 条，涉及 {people} 人")

    # ---------- ② 两个月对比 ----------
    def _build_compare(self):
        v = self._page("📅 两个月对比", "选两个月份，列出：新上舰的、没续的、连续在舰的（以及等级、收件信息有没有变化）。")
        bar = QHBoxLayout()
        self.c_a, self.c_b = QComboBox(), QComboBox()
        for cb in (self.c_a, self.c_b):
            cb.setMinimumWidth(110)
            cb.currentIndexChanged.connect(self.run_compare)
        bar.addWidget(label("较早的月份"))
        bar.addWidget(self.c_a)
        bar.addWidget(label("  →  较晚的月份"))
        bar.addWidget(self.c_b)
        self.c_only = QCheckBox("只看有变化的")
        self.c_only.toggled.connect(self.run_compare)
        bar.addWidget(self.c_only)
        bar.addStretch(1)
        v.addLayout(bar)
        self.t_cmp = make_table(["对比结果", "B站昵称", "B站UID", "等级", "QQ/联系方式", "说明"], 5)
        v.addWidget(self.t_cmp, 1)
        self.i_cmp = self._bottom(v, self.t_cmp, "两个月对比.xlsx")

    def run_compare(self):
        a, b = self.c_a.currentText(), self.c_b.currentText()
        if not a or not b:
            fill(self.t_cmp, [])
            self.i_cmp.setText("还没有数据")
            return
        if a > b:
            a, b = b, a
        res = history.compare_months(self.rows, a, b)
        if self.c_only.isChecked():
            res = [x for x in res if x["kind"] != "连续在舰" or x.get("changed")]
        color = {"新上舰": "#1a7f3c", "没续": "#c62828", "连续在舰": "#61666d"}
        out, colors = [], {}
        for i, x in enumerate(res):
            r = x["r"]
            out.append([x["kind"], r["bili_name"], r["bili_uid"], r["guard_level"] or "舰长", who(r), x["detail"]])
            colors[(i, 0)] = color[x["kind"]]
            if x.get("changed"):
                colors[(i, 5)] = "#c27c00"
        fill(self.t_cmp, out, colors, [x["r"]["id"] for x in res])
        cnt = {k: sum(x["kind"] == k for x in res) for k in color}
        ch = sum(bool(x.get("changed")) for x in res)
        self.i_cmp.setText(f"{a} → {b}：新上舰 {cnt['新上舰']} · 没续 {cnt['没续']} · 连续在舰 {cnt['连续在舰']}"
                           f"（其中有变化 {ch}）")

    # ---------- ③ 地址变化 ----------
    def _build_changes(self):
        v = self._page("🏠 地址 / 手机号变化", "同一个人在不同月份留的收件人、手机号、地址不一样时列出来。"
                                           "寄之前核对一下，避免寄到旧地址。（每一份的收件信息在「标记已发货」时记下；导入的表格按表格里的）")
        self.t_chg = make_table(["B站昵称", "B站UID", "QQ/联系方式", "从", "到", "变化内容"], 5)
        v.addWidget(self.t_chg, 1)
        self.i_chg = self._bottom(v, self.t_chg, "收件信息变化.xlsx")

    def run_changes(self):
        res = history.address_changes(self.rows)
        out = [[x["r"]["bili_name"], x["r"]["bili_uid"], who(x["r"]), x["from"], x["to"], x["detail"]] for x in res]
        fill(self.t_chg, out, None, [x["r"]["id"] for x in res])
        self.i_chg.setText(f"共 {len(res)} 处变化" if res else "没有发现变化")

    # ---------- ④ B站名单核对 ----------
    def _build_bili(self):
        v = self._page("📡 和 B站名单对比", "获取直播间现在的舰长名单，和登记记录对比："
                                         "谁在舰但这个月还没登记、谁登记了但已经不在名单、等级对不上的。")
        bar = QHBoxLayout()
        self.b_fetch = btn("获取 B站 名单并对比", "primary", self.run_bili)
        bar.addWidget(self.b_fetch)
        bar.addStretch(1)
        v.addLayout(bar)
        self.t_bili = make_table(["结果", "B站昵称", "B站UID", "等级", "说明"], 4)
        v.addWidget(self.t_bili, 1)
        self.i_bili = self._bottom(v, self.t_bili, "B站名单对比.xlsx")
        self.i_bili.setText("点上面的按钮开始（需要联网）")

    def run_bili(self):
        rid = self.win.cfg["streamer"]["room_id"]
        if not rid:
            QMessageBox.information(self, APP_NAME, "请先在「设置」里填写直播间号。")
            return
        self.b_fetch.setEnabled(False)
        self.i_bili.setText("正在获取 B站 舰长名单……")

        def done(res):
            self.b_fetch.setEnabled(True)
            if isinstance(res, Exception):
                self.i_bili.setText(f"❗ 获取失败：{res}")
                return
            self.rows = db.list_guards()
            out_ = history.bili_compare(self.rows, res)
            color = {"在舰但没登记": "#c62828", "等级不一致": "#7b4fd6", "已不在名单": "#c27c00", "没有 UID": "#9499a0"}
            out, colors = [], {}
            for i, x in enumerate(out_):
                out.append([x["kind"], x["name"], x["uid"], x["level"] or "舰长", x["detail"]])
                colors[(i, 0)] = color[x["kind"]]
            fill(self.t_bili, out, colors, [(x["r"] or {}).get("id") for x in out_])
            cnt = {k: sum(x["kind"] == k for x in out_) for k in color}
            self.i_bili.setText(f"B站名单 {len(res)} 人（{date.today()}）· 在舰但没登记 {cnt['在舰但没登记']} · "
                                f"已不在名单 {cnt['已不在名单']} · 等级不一致 {cnt['等级不一致']} · 没有 UID {cnt['没有 UID']}")
        run_bg(lambda: history.fetch_guard_list(int(rid)), done)

    # ---------- ⑤ 信息检查 ----------
    def _build_check(self):
        v = self._page("✅ 信息检查", "检查收件信息有没有问题：缺地址、手机号格式不对、地址不完整、多人共用一个手机号、"
                                    "同一个人重复登记等。寄礼物前检查一遍。")
        bar = QHBoxLayout()
        self.k_month = QComboBox()
        self.k_month.setMinimumWidth(110)
        self.k_month.currentIndexChanged.connect(self.run_check)
        bar.addWidget(label("月份"))
        bar.addWidget(self.k_month)
        self.k_tips = QCheckBox("也显示「提示」（待审核、没有 UID、地址相同等）")
        self.k_tips.setChecked(True)
        self.k_tips.toggled.connect(self.run_check)
        bar.addWidget(self.k_tips)
        bar.addStretch(1)
        v.addLayout(bar)
        self.t_chk = make_table(["级别", "问题", "月份", "B站昵称", "QQ/联系方式", "收件人", "手机号", "说明"], 7)
        v.addWidget(self.t_chk, 1)
        self.i_chk = self._bottom(v, self.t_chk, "信息检查.xlsx")

    def run_check(self):
        res = history.check_problems(self.rows, self.k_month.currentData() or None)
        if not self.k_tips.isChecked():
            res = [x for x in res if x["sev"] == "问题"]
        out, colors = [], {}
        for i, x in enumerate(res):
            r = x["r"]
            out.append([x["sev"], x["kind"], r["month"], r["bili_name"], who(r), r["receiver"], r["phone"], x["detail"]])
            colors[(i, 0)] = "#c62828" if x["sev"] == "问题" else "#9499a0"
            colors[(i, 1)] = "#c62828" if x["sev"] == "问题" else "#61666d"
        fill(self.t_chk, out, colors, [x["r"]["id"] for x in res])
        n = sum(x["sev"] == "问题" for x in res)
        self.i_chk.setText(f"发现 {n} 个问题" + (f"、{len(res) - n} 条提示" if len(res) > n else "") if res
                           else "没有发现问题 🎉")

    # ---------- 导入 ----------
    def do_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择以前的舰长表格", os.path.join(os.path.expanduser("~"), "Desktop"),
                                              "表格文件 (*.xlsx *.xlsm *.csv *.xls);;所有文件 (*)")
        if not path:
            return
        try:
            sheets = history.read_file(path)
        except Exception as e:     # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"读取失败：{e}")
            return
        if not any(s.rows for s in sheets):
            QMessageBox.warning(self, APP_NAME, "这个文件是空的。")
            return
        if ImportDialog(self, path, sheets).exec():
            self.win.data_changed()
            self.win.guards.reload_all()
            self.reload()


# ============================== 导入对话框 ==============================
class ImportDialog(QDialog):
    def __init__(self, parent, path, sheets):
        super().__init__(parent)
        self.path = path
        self.sheets = sheets
        self.planned = []
        self.setWindowTitle(f"导入表格 — {os.path.basename(path)}")
        self.resize(1080, 720)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        # 工作表
        g1 = QGroupBox("① 选择要导入的工作表，并确认月份")
        v1 = QVBoxLayout(g1)
        v1.addWidget(label("表格里有「月份」或「上舰日期」列的，按每一行的为准；都没有的，用这里填的月份（格式 2026-03）。", "hint", True))
        self.t_sheets = QTableWidget(len(sheets), 4)
        self.t_sheets.setHorizontalHeaderLabels(["导入", "工作表", "识别结果", "月份"])
        self.t_sheets.verticalHeader().setVisible(False)
        self.t_sheets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.t_sheets.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.t_sheets.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_sheets.setSelectionMode(QAbstractItemView.SingleSelection)
        self.t_sheets.setMaximumHeight(min(60 + 34 * len(sheets), 200))
        self.checks, self.month_edits = [], []
        for i, s in enumerate(sheets):
            cb = QCheckBox()
            cb.setChecked(s.usable)
            cb.toggled.connect(self.preview)
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(10, 0, 0, 0)
            h.addWidget(cb)
            self.t_sheets.setCellWidget(i, 0, w)
            self.t_sheets.setItem(i, 1, QTableWidgetItem(s.name))
            info = (f"{len(s.data)} 行 · 认出 {len(s.mapping)} 列：" + "、".join(history.FIELDS[f][0] for f in history.FIELD_ORDER
                                                                               if f in s.mapping)) if s.usable \
                else "没有找到表头（昵称 / UID / 收件人 / 手机号 / 地址 等），默认不导入"
            it = QTableWidgetItem(info)
            if not s.usable:
                it.setForeground(QColor("#9499a0"))
            self.t_sheets.setItem(i, 2, it)
            me = QLineEdit(s.month)
            me.setPlaceholderText("例如 2026-03")
            me.textChanged.connect(self.preview)
            self.t_sheets.setCellWidget(i, 3, me)
            self.checks.append(cb)
            self.month_edits.append(me)
        self.t_sheets.itemSelectionChanged.connect(self.show_mapping)
        v1.addWidget(self.t_sheets)
        lay.addWidget(g1)

        # 列对应
        self.g2 = QGroupBox("② 核对每一列对应什么（认错了可以改）")
        self.grid = QGridLayout(self.g2)
        self.grid.setHorizontalSpacing(14)
        self.combos = {}
        for k, f in enumerate(history.FIELD_ORDER):
            cb = QComboBox()
            cb.setMinimumWidth(140)
            cb.currentIndexChanged.connect(self._mapping_changed)
            self.combos[f] = cb
            r, c = divmod(k, 4)
            self.grid.addWidget(label(history.FIELDS[f][0]), r, c * 2)
            self.grid.addWidget(cb, r, c * 2 + 1)
        lay.addWidget(self.g2)

        # 选项 + 预览
        opt = QHBoxLayout()
        self.o_shipped = QCheckBox("这些记录都算「已发货」（表格里有「已发货」一列的，以表格为准）")
        self.o_shipped.setChecked(True)
        self.o_shipped.toggled.connect(self.preview)
        opt.addWidget(self.o_shipped)
        opt.addStretch(1)
        lay.addLayout(opt)
        self.t_prev = make_table(["处理", "工作表", "行", "月份", "等级", "B站昵称", "B站UID", "收件人", "手机号", "收货地址", "已发货", "说明"], 9)
        lay.addWidget(self.t_prev, 1)

        bottom = QHBoxLayout()
        self.info = label("", "hint", True)
        bottom.addWidget(self.info, 1)
        bottom.addWidget(btn("取消", None, self.reject))
        self.b_go = btn("导入", "primary", self.go)
        bottom.addWidget(self.b_go)
        lay.addLayout(bottom)

        self._cur = None
        first = next((i for i, s in enumerate(sheets) if s.usable), 0)
        self.t_sheets.selectRow(first)
        self.show_mapping()
        self.preview()

    def show_mapping(self):
        rows = self.t_sheets.selectionModel().selectedRows()
        i = rows[0].row() if rows else 0
        self._cur = None
        s = self.sheets[i]
        heads = s.headers if s.usable else [str(x or "") for x in (s.rows[0] if s.rows else [])]
        self.g2.setTitle(f"② 核对「{s.name}」每一列对应什么（认错了可以改）")
        for f, cb in self.combos.items():
            cb.blockSignals(True)
            cb.clear()
            cb.addItem(NONE, None)
            for j, h in enumerate(heads):
                cb.addItem(f"第{j + 1}列：{h}" if h else f"第{j + 1}列", j)
            idx = cb.findData(s.mapping.get(f)) if f in s.mapping else 0
            cb.setCurrentIndex(max(idx, 0))
            cb.blockSignals(False)
        self._cur = i

    def _mapping_changed(self):
        if self._cur is None:
            return
        s = self.sheets[self._cur]
        s.mapping = {f: cb.currentData() for f, cb in self.combos.items() if cb.currentData() is not None}
        if s.header_row < 0 and s.mapping:
            s.header_row = 0
            s.headers = [str(x or "") for x in s.rows[0]]
            self.checks[self._cur].setChecked(True)
        self.preview()

    def preview(self):
        recs = []
        for s, cb, me in zip(self.sheets, self.checks, self.month_edits):
            if cb.isChecked() and s.usable:
                m = history.parse_month(me.text().strip()) if me.text().strip() else ""
                recs += history.sheet_records(s, s.mapping, m)
        self.planned = history.plan(recs, self.o_shipped.isChecked())
        label_ = {"new": "✔ 新增", "exists": "已有，跳过", "dup": "重复，跳过", "error": "✘ 无法导入"}
        color = {"new": "#1a7f3c", "exists": "#9499a0", "dup": "#9499a0", "error": "#c62828"}
        out, colors = [], {}
        for i, r in enumerate(self.planned):
            out.append([label_[r["action"]], r["sheet"], r["line"], r["month"], r["level"], r["name"], r["uid"],
                        r["receiver"], r["phone"], r["address"], "是" if r["shipped"] else "否", r["why"]])
            colors[(i, 0)] = color[r["action"]]
            if r["action"] == "error":
                colors[(i, 11)] = "#c62828"
        fill(self.t_prev, out, colors)
        cnt = {k: sum(r["action"] == k for r in self.planned) for k in label_}
        months = sorted({r["month"] for r in self.planned if r["action"] == "new"})
        self.info.setText(f"将新增 <b>{cnt['new']}</b> 条" + (f"（{months[0]} ～ {months[-1]}）" if months else "")
                          + f" · 已有记录跳过 {cnt['exists']} · 表格内重复 {cnt['dup']}"
                          + (f" · <span style='color:#c62828'>无法导入 {cnt['error']}（见「说明」列）</span>" if cnt["error"] else ""))
        self.info.setTextFormat(Qt.RichText)
        self.b_go.setEnabled(cnt["new"] > 0)

    def go(self):
        n = sum(r["action"] == "new" for r in self.planned)
        if QMessageBox.question(self, APP_NAME, f"确定导入 {n} 条记录吗？\n\n导入的记录状态为「表格导入」（已验证），"
                                                "可以在「舰长管理」里查看或删除。") != QMessageBox.Yes:
            return
        res = history.apply(self.planned, os.path.basename(self.path))
        QMessageBox.information(self, APP_NAME, f"已导入 {res['added']} 条记录。"
                                + (f"\n其中 {res['filled']} 人原来没有收货地址，已用表格里最新的地址补上。" if res["filled"] else ""))
        self.accept()
