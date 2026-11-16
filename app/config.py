"""配置：读写 config.yaml。图形界面和机器人进程共用。"""
import copy
import json
import secrets
import sys
from pathlib import Path

import yaml

if getattr(sys, "frozen", False):                      # 打包成 exe 后
    ROOT = Path(sys.executable).resolve().parent       # 配置、数据放在 exe 旁边
    RES = Path(getattr(sys, "_MEIPASS", ROOT))         # 打包进去的资源（网页模板等）
else:
    ROOT = RES = Path(__file__).resolve().parent.parent

CONFIG_FILE = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
SHOT_DIR = DATA_DIR / "screenshots"
DATA_DIR.mkdir(exist_ok=True)
SHOT_DIR.mkdir(exist_ok=True)

DEFAULTS = {
    "streamer": {"name": "", "room_id": 0, "keywords": []},
    "bilibili": {"check_api": True},
    "qq": {"enabled": False, "groups": [], "admins": [], "kick_unverified_minutes": 0, "access_token": ""},
    "web": {"port": 8765, "public_url": "http://127.0.0.1:8765", "admin_password": "",
            "link_valid_hours": 24},
    "rules": {"edit_grace_days": 0, "auto_renew": True},
    "napcat": {"path": ""},
    "gui": {"autostart": True},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def load() -> dict:
    raw = {}
    if CONFIG_FILE.exists():
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    c = _merge(DEFAULTS, raw)
    if not c["web"]["admin_password"]:                 # 首次运行自动生成后台密码
        c["web"]["admin_password"] = secrets.token_urlsafe(9)
        save(c)
    elif not CONFIG_FILE.exists():
        save(c)
    return c


def _j(v):
    return json.dumps(v, ensure_ascii=False)          # JSON 也是合法的 YAML


def dump(c: dict) -> str:
    s, b, q, w, r, n = (c[k] for k in ("streamer", "bilibili", "qq", "web", "rules", "napcat"))
    return f"""# ============ 舰长礼物小助手 配置文件 ============
# 推荐直接在图形界面「设置」里修改；也可以用记事本改（冒号后面要有空格）

streamer:
  name: {_j(s["name"])}                # 主播 B站昵称
  room_id: {_j(s["room_id"])}              # 直播间号
  # 截图里出现以下任意一个词，才算“你直播间”的截图（一般填昵称、粉丝牌名）
  keywords: {_j(s["keywords"])}

bilibili:
  check_api: {_j(b["check_api"])}            # 调用 B站 公开接口核对舰长名单（推荐开启）

qq:
  enabled: {_j(q["enabled"])}            # 是否启用 QQ 机器人（需要 LLBot / NapCat 等协议端）
  groups: {_j(q["groups"])}              # 舰长群群号
  admins: {_j(q["admins"])}              # 主播/管理员 QQ（接收待审核提醒）
  kick_unverified_minutes: {_j(q["kick_unverified_minutes"])}   # 新人多少分钟内不验证就踢出，0 = 不踢
  access_token: {_j(q["access_token"])}          # 与协议端里填的 token 一致（可留空）

web:
  port: {_j(w["port"])}                   # 程序使用的端口
  public_url: {_j(w["public_url"])}   # 观众打开填写链接用的地址
  admin_password: {_j(w["admin_password"])}   # 网页后台密码
  link_valid_hours: {_j(w["link_valid_hours"])}           # 填写链接有效小时数

rules:
  edit_grace_days: {_j(r["edit_grace_days"])}            # 每月前几天仍允许上月舰长修改地址，0 = 只允许当月
  auto_renew: {_j(r["auto_renew"])}              # 连续在舰（自动续费 / 一次买多个月）的舰长，每个新的舰长月自动登记

napcat:
  path: {_j(n["path"])}                  # QQ 协议端（LLBot / NapCat）启动程序位置（可选，用于一键启动）

gui:
  autostart: {_j(c["gui"]["autostart"])}            # 打开软件时自动启动服务（登记网页 + 可选的 QQ 机器人）
"""


def save(c: dict, path: Path = CONFIG_FILE):
    path.write_text(dump(c), encoding="utf-8")


cfg = load()

STREAMER = cfg["streamer"]
BILI = cfg["bilibili"]
QQ = cfg["qq"]
WEB = cfg["web"]
RULES = cfg["rules"]
NAPCAT = cfg["napcat"]

QQ_GROUPS = {int(g) for g in QQ.get("groups", [])}
QQ_ADMINS = {int(a) for a in QQ.get("admins", [])}
