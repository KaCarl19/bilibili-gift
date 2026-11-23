"""机器人进程：QQ 机器人（NoneBot2）+ 观众填写网页（FastAPI），由图形界面在后台启动。"""
import sys


def run_bot():
    from app.config import DATA_DIR, QQ, WEB

    if sys.stdout is None:                      # 无控制台时（打包后单独运行）输出到文件
        sys.stdout = sys.stderr = open(DATA_DIR / "bot_console.log", "a", encoding="utf-8")

    import nonebot
    from nonebot.log import logger

    official = bool(QQ.get("enabled")) and QQ.get("mode", "official") == "official"
    extra = {}
    if official:                               # 官方机器人：主动连 QQ 开放平台的 WebSocket 网关
        extra["qq_bots"] = [{
            "id": str(QQ.get("appid") or "").strip(),
            "secret": str(QQ.get("secret") or "").strip(),
            "use_websocket": True,
            # 只申请要用的事件。没开通的权限千万别申请，否则连不上
            "intent": {"guilds": False, "guild_members": False, "guild_messages": False,
                       "guild_message_reactions": False, "direct_message": False, "message_audit": False,
                       "at_messages": False, "c2c_group_at_messages": True,
                       "group_members": bool(QQ.get("member_events"))},
        }]
    nonebot.init(
        driver="~fastapi+~httpx+~websockets" if official else "~fastapi",
        host="0.0.0.0",
        port=int(WEB.get("port", 8765)),
        command_start={"/", ""},               # 命令不用加 / ，直接发“填地址”
        log_level="INFO",
        onebot_access_token=QQ.get("access_token") or None,
        **extra,
    )
    logger.add(DATA_DIR / "bot.log", rotation="5 MB", retention=3, encoding="utf-8", level="INFO")
    driver = nonebot.get_driver()

    from app import bili, db, renew, web
    from app.config import RULES
    db.init()
    nonebot.get_app().include_router(web.router)

    if official:
        if not (QQ.get("appid") and QQ.get("secret")):
            logger.warning("QQ 官方机器人：还没填 AppID / AppSecret，机器人不会启动（登记网页照常运行）")
        else:
            from nonebot.adapters.qq import Adapter as QQAdapter
            driver.register_adapter(QQAdapter)
            nonebot.load_plugin("app.plugins.qq_official")
            logger.info("QQ 官方机器人已启用，正在连接 QQ 开放平台"
                        + ("（含入群申请审核、退群记录）" if QQ.get("member_events") else ""))
    elif QQ.get("enabled"):
        from nonebot.adapters.onebot.v11 import Adapter
        driver.register_adapter(Adapter)
        nonebot.load_plugin("app.plugins.guard_bot")
        logger.info("QQ 机器人已启用，等待 QQ 协议端（LLBot / NapCat）连接")
    else:
        logger.info("QQ 机器人未启用：只运行观众登记网页")

    @driver.on_startup
    async def _keep_guard_list():
        import asyncio

        async def loop():
            while True:                       # 每 5 分钟刷新一次 B站舰长名单，顺便检查升级和自动续登
                data = await bili.guard_list()
                try:
                    for msg in renew.run(data, auto_continue=bool(RULES.get("auto_renew", True))):
                        logger.info(msg)
                except Exception as e:          # noqa: BLE001
                    logger.exception(f"自动续登检查出错：{e}")
                await asyncio.sleep(300)
        asyncio.create_task(loop())

    nonebot.run()
