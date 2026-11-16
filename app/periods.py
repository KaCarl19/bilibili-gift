"""“舰长月”计算。

主播的规则：
- 一次买多个月 = 每个月都寄一份礼物；自动续费也是每个月一份。
- 连续在舰的舰长，下个月自动登记，不用重新上传截图。

做法：
- 一个“舰长月”按 30 天算。一次买 N 个月 = 连续 N 个舰长月，每个舰长月对应一份礼物。
- 每条舰长记录 = 一个舰长月：start_date 是这个舰长月的开始日，month 是这份礼物在哪个月的发货表里。
- “目前在舰”= 今天落在他的某个舰长月里（不再要求“开通时间必须在本月”）。
"""
from datetime import date, timedelta

PERIOD_DAYS = 30
# 上一个舰长月结束后，还在 B站舰长名单里超过这么多天，才认定续上了
# （留余量：兼容 B站 可能按自然月计算、名单更新有延迟）
RENEW_BUFFER_DAYS = 3


def current_period(opened: date | None, expire: date | None, today: date, active: bool) -> date | None:
    """算出今天所在的舰长月是从哪天开始的。

    opened：截图里的开通时间；expire：截图里的到期时间；
    active：是否能确认目前在舰（在 B站舰长名单里，或到期时间在今天之后）。
    无法确认在舰时返回 None。"""
    if opened and opened > today:
        opened = None                                   # 识别错的未来日期，当作没有
    if opened:
        k = (today - opened).days // PERIOD_DAYS
        if k == 0:
            return opened                               # 开通不到 30 天，肯定还在第一个舰长月
        if active:
            return opened + timedelta(days=PERIOD_DAYS * k)
        return None                                     # 开通超过 30 天，又确认不了还在舰
    if expire and expire > today:
        k = -(-(expire - today).days // PERIOD_DAYS)     # 向上取整
        return expire - timedelta(days=PERIOD_DAYS * k)
    return None


def period_end(start: date) -> date:
    return start + timedelta(days=PERIOD_DAYS)


def covers(start: date, today: date, paid_until: date | None = None) -> bool:
    """这个舰长月（或已付费的多个月）是否覆盖今天"""
    end = period_end(start) + timedelta(days=RENEW_BUFFER_DAYS)
    if paid_until and paid_until > end:
        end = paid_until
    return start <= today < end


def next_month(m: str) -> str:
    y, mm = int(m[:4]), int(m[5:7])
    return f"{y + (mm == 12)}-{(mm % 12) + 1:02d}"
