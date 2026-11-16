"""机器人进程：QQ 机器人（NoneBot2）+ 观众填写网页（FastAPI），由图形界面在后台启动。"""
import sys


def run_bot():
    from app.config import DATA_DIR, QQ, WEB

    if sys.stdout is None:                      # 无控制台时（打包后单独运行）输出到文件
        sys.stdout = sys.stderr = open(DATA_DIR / "bot_console.log", "a", encoding="utf-8")

    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter
    from nonebot.log import logger

    nonebot.init(
        driver="~fastapi",
        host="0.0.0.0",
        port=int(WEB.get("port", 8765)),
        command_start={"/", ""},               # 命令不用加 / ，直接发“填地址”
        log_level="INFO",
        onebot_access_token=QQ.get("access_token") or None,
    )
    logger.add(DATA_DIR / "bot.log", rotation="5 MB", retention=3, encoding="utf-8", level="INFO")
    nonebot.get_driver().register_adapter(Adapter)

    from app import db, web
    db.init()
    nonebot.get_app().include_router(web.router)
    nonebot.load_plugin("app.plugins.guard_bot")
    nonebot.run()
