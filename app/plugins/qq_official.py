"""QQ 官方机器人（QQ 开放平台）：在舰长群里核对入群口令。

官方机器人的限制：拿不到 QQ 号（只有 openid），群里默认只收到 @它 的消息，不能随便私聊观众。
所以收地址仍然走登记网页，机器人只负责：
  - 群里「@机器人 口令 XXXXXX」→ 回复是不是在舰的已验证舰长，并记为已进群；
  - （开通「群成员事件」权限并在设置里勾选后）自动审核入群申请：答案里是有效口令就通过；
    群成员退群自动记为「已退群」，以后要重新上提督 / 总督才能再进。
"""
from nonebot import on_message, on_notice
from nonebot.adapters.qq import Bot, GroupAtMessageCreateEvent
from nonebot.log import logger

from app import joincode

try:                                           # 群成员事件是 2026 年新开放的，老版本适配器没有
    from nonebot.adapters.qq import GroupJoinRequestEvent, GroupMemberRemoveEvent
except ImportError:                            # pragma: no cover
    GroupJoinRequestEvent = GroupMemberRemoveEvent = None


def _is_group_at(event) -> bool:
    return isinstance(event, GroupAtMessageCreateEvent)


at_msg = on_message(rule=_is_group_at, priority=10, block=True)


@at_msg.handle()
async def _(bot: Bot, event: GroupAtMessageCreateEvent):
    text = event.get_plaintext()
    reply = joincode.reply_for_at(text, event.get_user_id(), event.group_openid)
    logger.info(f"官方机器人 @消息（群 {event.group_openid[:8]}…）：{text!r} → {reply[:30]}")
    await at_msg.finish(reply)


if GroupJoinRequestEvent is not None:
    def _is_join_req(event) -> bool:
        return isinstance(event, GroupJoinRequestEvent)

    def _is_member_left(event) -> bool:
        return isinstance(event, GroupMemberRemoveEvent)

    join_req = on_notice(rule=_is_join_req, priority=10, block=True)
    member_left = on_notice(rule=_is_member_left, priority=10, block=True)

    @join_req.handle()
    async def _(bot: Bot, event: GroupJoinRequestEvent):
        if event.apply_source == "invited" or event.bot:
            logger.info(f"入群申请：{event.username} 是被邀请的 / 机器人，留给群主处理")
            return
        vi = event.verify_info
        parts = []
        if vi:
            parts.append(vi.verify_message or "")
            parts += [qa.answer for qa in (vi.review_qa_list or [])]
        op, reason = joincode.decide_join_request(" ".join(parts), event.member_openid, event.group_openid)
        try:
            await bot.approval_join_request(group_id=event.group_openid, member_openid=event.member_openid, op=op,
                                            join_request_id=event.join_request_id,
                                            reject_reason=None if op == "approve" else reason)
            logger.info(f"入群申请：{event.username} → {'通过' if op == 'approve' else '拒绝'}（{reason}）")
        except Exception as e:                  # noqa: BLE001
            logger.warning(f"处理入群申请失败（{event.username}）：{e}。可能没有开通群管理权限，请群主手动处理")

    @member_left.handle()
    async def _(event: GroupMemberRemoveEvent):
        codes = joincode.mark_left(event.member_openid)
        if codes:
            logger.info(f"群成员退群：口令 {', '.join(codes)} 记为已退群（以后要上提督 / 总督才能再进）")
