"""夜间降速窗口（北京时间）：0–4 点最慢，4–6 点线性回到正常。

为什么需要：任务是 7×24 跑的，全量任务在深夜照样一轮接一轮地打上游。深夜的请求量
本身没有任何业务收益（没人看），却会让上游看到一个"作息异常"的账号——这是风控最容易
盯上的形态。窗口内按比例压低**每小时事务上限**并拉长**任务间隔**，4–6 点再线性放回去，
避免整点台阶式的流量突变。

两个旋钮都由同一个比例驱动，所以"压到 25%"意味着：间隔 ×4、小时额度 ×25%。
窗口内额度撞闸属于**计划内降速**，不推送"检查已自动降速"通知（见 ``throttle.enter``）。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

from app.config import Settings

#: 平台展示用的北京时间（与推送正文、冷却提示一致）。
CST = timezone(timedelta(hours=8))


def _minutes(local: datetime) -> float:
    return local.hour * 60 + local.minute + local.second / 60.0


def factor(settings: Settings, moment: datetime | None = None) -> float:
    """当前时刻的降速比例：1.0 = 正常，<1 = 降速。窗口外恒为 1.0。"""
    if not settings.quiet_hours_enabled:
        return 1.0
    floor = min(1.0, max(0.01, float(settings.quiet_hours_slow_factor)))
    slow_from = float(settings.quiet_hours_slow_from_hour) * 60
    slow_until = float(settings.quiet_hours_slow_until_hour) * 60
    recover_until = float(settings.quiet_hours_recover_until_hour) * 60
    if slow_until <= slow_from:
        return 1.0
    local = (moment or datetime.now(UTC)).astimezone(CST)
    minutes = _minutes(local)
    if slow_from <= minutes < slow_until:
        return floor
    if slow_until <= minutes < recover_until:
        span = recover_until - slow_until
        progress = (minutes - slow_until) / span
        return floor + (1.0 - floor) * progress
    return 1.0


def active(settings: Settings, moment: datetime | None = None) -> bool:
    """是否处在降速窗口内（含 4–6 点的恢复段）。"""
    return factor(settings, moment) < 1.0


def interval(settings: Settings, cadence_seconds: int, moment: datetime | None = None) -> int:
    """降速后的实际间隔：``cadence / factor``（向上取整，永不变快）。"""
    cadence = max(1, int(cadence_seconds))
    scale = factor(settings, moment)
    if scale >= 1.0:
        return cadence
    return max(cadence, int(math.ceil(cadence / scale)))
