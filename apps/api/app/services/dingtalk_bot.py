"""平台钉钉机器人：账号↔会话绑定 + 关键字指令。

平台自己持有一个钉钉企业内部应用机器人（AppKey/AppSecret）。用户在“账户与
额度”里生成一次性验证码，在群里或单聊里把它发给机器人，平台就记住这个会话：

* 之后发关键字（任务/检查/暂停/启用/额度/变化/状态/解绑）时，用**绑定账号**的鉴权和数据回答；
* 提醒也推进这个会话（自动生成一个钉钉渠道，走既有的 outbox 投递）。

没有绑定请求时不会连出：连接只在配置了机器人凭据且进程存活期间保持。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import UTC, datetime, timedelta, timezone

from dingtalk_stream import (
    AckMessage,
    CallbackHandler,
    CallbackMessage,
    ChatbotMessage,
    DingTalkStreamClient,
)
from dingtalk_stream import (
    Credential as StreamCredential,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    ChangeEvent,
    Credential,
    DingTalkBotLink,
    DingTalkLinkCode,
    MonitorTask,
    NotificationChannel,
    NotificationPlatform,
    ObservationLatest,
    ScanJob,
    ScanJobStore,
    Store,
    StoreJobStatus,
    Style,
    Tenant,
    UsageDaily,
    User,
)
from app.security import encrypt_secret, secret_fingerprint
from app.services import nike_auth, push
from app.services.credentials import CredentialError, CredentialService
from app.services.jobs import enqueue_job, requeue_failed_stores

logger = logging.getLogger("nike_mp.bot")

HELP_TEXT = (
    "可用指令：\n"
    "· 绑定 <验证码>：把本会话绑到你的平台账号（验证码在“账户与额度 → 消息推送”生成）\n"
    "· 概览：今天的检查概况\n"
    "· 任务：查看我的监控任务\n"
    "· 检查 [任务名]：马上检查一次\n"
    "· 暂停 <任务名> / 启用 <任务名>：停止或恢复自动检查\n"
    "· 新建 <任务名> <货号…> [门店…]：建监控任务（不写门店就是全部门店）\n"
    "· 加货号 <货号…> 到 <任务名> / 删货号 <货号…> 到 <任务名>\n"
    "· 加门店 <门店…> 到 <任务名> / 删门店 <门店…> 到 <任务名>\n"
    "· 频率 <时间> 到 <任务名>：改检查节奏，如 频率 30分钟 到 钉钉测试任务\n"
    "· 盯 <门店…> 到 <任务名>：改成只盯这几家；也可以：盯 全部门店 到 <任务名>\n"
    "· 通知 <任务名> [补货开/关 售罄开/关 变价开/关]：看/改提醒开关\n"
    "· 上次检查 [任务名]：看最近一次检查结果\n"
    "· 失败门店 [任务名]：看上次失败的门店和原因\n"
    "· 重试失败 [任务名]：把上次失败的门店再跑一遍\n"
    "· 删除 <任务名>：删除任务（要写完整名字）\n"
    "· 额度：今天用掉的检查额度\n"
    "· 变化：最近 24 小时的补货、售罄、变价\n"
    "· 详情 <货号>：某个货号的门店情况\n"
    "· 门店：已登记的门店\n"
    "· 货号：在监控的货号\n"
    "· 状态：Nike 连接状态\n"
    "· 解绑：取消本会话的绑定（只影响这个会话）\n"
    "· 注销：断开 Nike 连接（会先让你确认一次）"
)
NOT_LINKED_TEXT = (
    "本会话还没有绑定账号。请到「账户与额度 → 消息推送 → 钉钉机器人」生成验证码，"
    "然后发给我，例如：绑定 123456"
)
UNLINKED_TEXT = "已解除本会话的绑定；提醒不会再发到这里。"


def issue_code(session: Session, settings: Settings, user_id) -> DingTalkLinkCode:
    """生成/刷新该用户的一次性绑定码。"""
    ttl = timedelta(seconds=settings.dingtalk_link_code_ttl_seconds)
    active = {
        row.code
        for row in session.execute(
            select(DingTalkLinkCode).where(DingTalkLinkCode.expires_at > datetime.now(UTC))
        ).scalars()
    }
    code = ""
    for _ in range(50):
        candidate = f"{random.randint(0, 999999):06d}"
        if candidate not in active:
            code = candidate
            break
    if not code:
        code = f"{random.randint(0, 999999):06d}"
    row = session.get(DingTalkLinkCode, user_id)
    if row is None:
        row = DingTalkLinkCode(owner_user_id=user_id, code=code)
        session.add(row)
    row.code = code
    row.expires_at = datetime.now(UTC) + ttl
    session.flush()
    return row


def find_code_owner(session: Session, text: str) -> tuple[DingTalkLinkCode, User] | None:
    """消息里包含某个未过期的绑定码时返回它和主人。"""
    for row in session.execute(
        select(DingTalkLinkCode).where(DingTalkLinkCode.expires_at > datetime.now(UTC))
    ).scalars():
        if row.code and row.code in text:
            user = session.get(User, row.owner_user_id)
            if user is not None:
                return row, user
    return None


def provision_channel(session: Session, settings: Settings, link: DingTalkBotLink) -> None:
    """给一个已绑定会话配上推送渠道（平台机器人的凭据）。"""
    if not (settings.dingtalk_bot_client_id and settings.dingtalk_bot_client_secret):
        return
    target = push.build_bot_target(
        settings.dingtalk_bot_client_id,
        settings.dingtalk_bot_client_secret,
        conversation_id="" if link.conversation_type == 1 else link.conversation_id,
        user_id=link.dingtalk_user_id if link.conversation_type == 1 else "",
    )
    fingerprint = secret_fingerprint(target, purpose="push")
    existing = session.execute(
        select(NotificationChannel).where(
            NotificationChannel.owner_user_id == link.owner_user_id,
            NotificationChannel.target_fingerprint == fingerprint,
        )
    ).scalar_one_or_none()
    name = (link.conversation_title or ("钉钉单聊" if link.conversation_type == 1 else "钉钉群"))[
        :64
    ]
    if existing is None:
        session.add(
            NotificationChannel(
                owner_user_id=link.owner_user_id,
                platform=NotificationPlatform.DINGTALK_APP.value,
                name=name,
                target_enc=encrypt_secret(target),
                target_fingerprint=fingerprint,
                target_hint=push.target_hint(NotificationPlatform.DINGTALK_APP.value, target),
                enabled=True,
            )
        )
    else:
        existing.name = name
        existing.target_hint = push.target_hint(NotificationPlatform.DINGTALK_APP.value, target)
    session.flush()


def drop_channel(session: Session, settings: Settings, link: DingTalkBotLink) -> None:
    if not (settings.dingtalk_bot_client_id and settings.dingtalk_bot_client_secret):
        return
    target = push.build_bot_target(
        settings.dingtalk_bot_client_id,
        settings.dingtalk_bot_client_secret,
        conversation_id="" if link.conversation_type == 1 else link.conversation_id,
        user_id=link.dingtalk_user_id if link.conversation_type == 1 else "",
    )
    fingerprint = secret_fingerprint(target, purpose="push")
    for row in session.execute(
        select(NotificationChannel).where(
            NotificationChannel.owner_user_id == link.owner_user_id,
            NotificationChannel.target_fingerprint == fingerprint,
        )
    ).scalars():
        session.delete(row)


def link_conversation(
    session: Session, settings: Settings, owner_user_id, data: dict
) -> DingTalkBotLink:
    """把回调里的会话写成/更新成该账号的绑定，并自动配好推送渠道。"""
    conversation_id = str(data.get("conversationId") or "")
    link = session.execute(
        select(DingTalkBotLink).where(DingTalkBotLink.conversation_id == conversation_id)
    ).scalar_one_or_none()
    if link is None:
        link = DingTalkBotLink(conversation_id=conversation_id, owner_user_id=owner_user_id)
        session.add(link)
    link.owner_user_id = owner_user_id
    link.dingtalk_user_id = str(data.get("senderStaffId") or "")[:64]
    link.dingtalk_corp_id = str(data.get("chatbotCorpId") or "")[:64]
    link.conversation_type = int(data.get("conversationType") or 2)
    link.conversation_title = str(data.get("conversationTitle") or "")[:160]
    session.flush()
    provision_channel(session, settings, link)
    return link


def unlink_conversation(session: Session, settings: Settings, conversation_id: str) -> bool:
    link = session.execute(
        select(DingTalkBotLink).where(DingTalkBotLink.conversation_id == conversation_id)
    ).scalar_one_or_none()
    if link is None:
        return False
    drop_channel(session, settings, link)
    session.delete(link)
    session.flush()
    return True


def _tasks_reply(session: Session, owner_user_id) -> str:
    rows = (
        session.execute(
            select(MonitorTask)
            .where(MonitorTask.created_by == owner_user_id)
            .order_by(MonitorTask.created_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "你还没有监控任务。到网页的「监控任务 → 新建监控任务」建一个，我再帮你盯。"
    lines = [f"你的监控任务（{len(rows)} 个）："]
    for task in rows:
        state = "启用" if task.is_active else "暂停"
        styles = len(task.style_codes or [])
        stores = "全部" if task.all_stores else len(task.store_codes or [])
        minutes = max(1, int((task.cadence_seconds or 1800) / 60))
        nxt = ""
        if task.is_active and task.next_run_at is not None:
            nxt = f"，下次 {task.next_run_at.astimezone(_CST).strftime('%H:%M')}"
        lines.append(f"· {task.name}（{state}，每 {minutes} 分钟{nxt}，货号 {styles}，门店 {stores}）")
    return "\n".join(lines)


def _changes_reply(session: Session, owner_user_id) -> str:
    since = datetime.now(UTC) - timedelta(hours=24)
    rows = (
        session.execute(
            select(ChangeEvent)
            .where(ChangeEvent.owner_user_id == owner_user_id, ChangeEvent.detected_at >= since)
            .order_by(ChangeEvent.detected_at.desc())
            .limit(5)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "最近 24 小时没有补货、售罄或变价。"
    label = {"RESTOCK": "补货", "SOLD_OUT": "售罄", "PRICE_CHANGE": "变价"}
    lines = ["最近 24 小时的变化："]
    for row in rows:
        code = row.style.style_code if row.style is not None else "—"
        store = row.store.store_code if row.store is not None else "—"
        parts = [label.get(row.event_type, row.event_type), code]
        if row.size_label:
            parts.append(row.size_label)
        detail = "  ".join(parts)
        if (
            row.event_type == "PRICE_CHANGE"
            and row.before_total_price is not None
            and row.after_total_price is not None
        ):
            detail += f"（¥{float(row.before_total_price):g} → ¥{float(row.after_total_price):g}）"
        lines.append(f"· {detail} · {store}")
    return "\n".join(lines)


def _stores_reply(session: Session, owner_user_id) -> str:
    codes = (
        session.execute(
            select(Store.store_code)
            .where(Store.owner_user_id == owner_user_id, Store.is_active.is_(True))
            .order_by(Store.store_code)
            .limit(200)
        )
        .scalars()
        .all()
    )
    if not codes:
        return "还没有登记门店。到「账户与额度」连接 Nike 账号后会自动获取。"
    head = "、".join(codes[:10])
    more = f" 等 {len(codes)} 家" if len(codes) > 10 else ""
    return f"已登记 {len(codes)} 家门店：{head}{more}"


def _styles_reply(session: Session, owner_user_id) -> str:
    codes = (
        session.execute(
            select(Style.style_code)
            .where(Style.owner_user_id == owner_user_id)
            .order_by(Style.created_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    if not codes:
        return "还没有监控货号。在「监控任务」里新建任务并填上货号即可。"
    return "在监控的货号：" + "、".join(codes)


def _status_reply(session: Session, owner_user_id) -> str:
    credential = session.execute(
        select(Credential).where(Credential.owner_user_id == owner_user_id)
    ).scalar_one_or_none()
    state = credential.status if credential is not None else "NOT_CONNECTED"
    state_label = {"ACTIVE": "已连接", "EXPIRING": "即将到期", "REVOKED": "已断开"}.get(state, "未连接")
    return f"Nike 连接：{state_label}"


def _pick_task(session: Session, owner_user_id, name: str):
    """按名字挑任务：精确 > 前缀 > 包含；没给名字时只有单个任务才自动选中。"""
    rows = (
        session.execute(
            select(MonitorTask)
            .where(MonitorTask.created_by == owner_user_id)
            .order_by(MonitorTask.created_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    if not rows:
        return None, "你还没有监控任务。到网页的「监控任务 → 新建监控任务」建一个，我再帮你盯。"
    key = (name or "").strip().lower()
    if not key:
        if len(rows) == 1:
            return rows[0], None
        return None, f"你有 {len(rows)} 个任务，请带上任务名重发，比如：{rows[0].name}"
    exact = [task for task in rows if task.name.lower() == key]
    if exact:
        return exact[0], None
    prefix = [task for task in rows if task.name.lower().startswith(key)]
    hits = prefix or [task for task in rows if key in task.name.lower()]
    if not hits:
        return None, f"没找到名字里含「{name.strip()}」的任务。发「任务」看看列表。"
    if len(hits) > 1:
        names = "、".join(task.name for task in hits[:5])
        return None, f"匹配到多个任务：{names}。发完整一点的名字。"
    return hits[0], None


def _trigger_check(session: Session, settings: Settings, owner_user_id, name: str) -> str:
    task, problem = _pick_task(session, owner_user_id, name)
    if problem:
        return problem
    if not task.is_active:
        return f"「{task.name}」是暂停状态。先发「启用 {task.name}」再检查。"
    if task.all_stores:
        store_codes = list(
            session.execute(
                select(Store.store_code)
                .where(Store.owner_user_id == owner_user_id, Store.is_active.is_(True))
                .order_by(Store.store_code)
            ).scalars()
        )
    else:
        wanted = list(task.store_codes or [])
        known = set(
            session.execute(
                select(Store.store_code).where(
                    Store.owner_user_id == owner_user_id, Store.store_code.in_(wanted)
                )
            ).scalars()
        )
        store_codes = [code for code in wanted if code in known]
    style_codes = [str(code).upper() for code in (task.style_codes or [])]
    if not store_codes:
        return "这个任务还没有可用的门店，到网页的任务设置里补上。"
    if not style_codes:
        return "这个任务还没有货号，到网页的任务设置里补上。"
    user = session.get(User, owner_user_id)
    if user is None:
        return "账号状态异常，请稍后再试。"
    try:
        binding = CredentialService(settings, owner_user_id).binding()
        enqueue_job(
            session,
            tenant_id=user.tenant_id,
            store_codes=store_codes,
            style_codes=style_codes,
            task_id=task.id,
            requested_by=owner_user_id,
            kind="SCAN",
            owner_user_id=owner_user_id,
            credential_binding=binding,
        )
    except CredentialError:
        return "需要先在网页上连接 Nike 账号（「账户与额度」），才能检查。"
    session.commit()
    return (
        f"已开始检查「{task.name}」：门店 {len(store_codes)} 家、货号 {len(style_codes)} 个。"
        "有补货、售罄或变价会推到这里。"
    )


def _toggle_task(session: Session, owner_user_id, name: str, active: bool) -> str:
    task, problem = _pick_task(session, owner_user_id, name)
    if problem:
        return problem
    if task.is_active == active:
        return f"「{task.name}」已经是{'启用' if active else '暂停'}状态。"
    task.is_active = active
    task.next_run_at = datetime.now(UTC) if active else None
    session.commit()
    if active:
        return f"已启用「{task.name}」，之后按原节奏检查。"
    return f"已暂停「{task.name}」，不会再自动检查。"


def _quota_reply(session: Session, owner_user_id) -> str:
    user = session.get(User, owner_user_id)
    if user is None:
        return "账号状态异常，请稍后再试。"
    now = datetime.now(UTC)
    day = datetime(now.year, now.month, now.day, tzinfo=UTC)
    row = session.execute(
        select(UsageDaily).where(UsageDaily.tenant_id == user.tenant_id, UsageDaily.day == day)
    ).scalar_one_or_none()
    tenant = session.get(Tenant, user.tenant_id)
    budget = int(tenant.daily_review_budget) if tenant is not None else 0
    reviews = int(row.review_requests) if row is not None else 0
    prices = int(row.price_requests) if row is not None else 0
    used = reviews + prices
    lines = [f"今日额度：已用 {used} / {budget}（门店检查 {reviews}、价格查询 {prices}）。"]
    if budget and used >= budget * 0.8:
        lines.append("今天剩得不多了，检查节奏会放慢。")
    return "\n".join(lines)


def _command_arg(command: str, *keywords: str) -> str | None:
    for keyword in keywords:
        if command.startswith(keyword):
            return command[len(keyword):].strip(" ：:，,。.！!")
    return None


_TOKEN_SPLIT = re.compile(r"[\s,，、;；]+")
_STORE_LIKE = re.compile(r"\d{3,6}")


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(text.strip()) if token]


def _registered_stores(session: Session, owner_user_id) -> set[str]:
    return {
        str(code).upper()
        for code in session.execute(
            select(Store.store_code).where(
                Store.owner_user_id == owner_user_id, Store.is_active.is_(True)
            )
        ).scalars()
    }


def _classify_codes(
    session: Session, owner_user_id, tokens: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """纯数字且已登记 → 门店；其余 → 货号；纯数字但没登记 → 提示里列出。"""
    stores = _registered_stores(session, owner_user_id)
    store_codes: list[str] = []
    style_codes: list[str] = []
    unknown: list[str] = []
    for token in tokens:
        code = token.strip().upper()
        if not code:
            continue
        if code in stores:
            if code not in store_codes:
                store_codes.append(code)
        elif _STORE_LIKE.fullmatch(code):
            unknown.append(code)
        elif code not in style_codes:
            style_codes.append(code)
    return store_codes, style_codes, unknown


def _ensure_style(session: Session, owner_user_id, code: str) -> None:
    existing = session.execute(
        select(Style).where(Style.owner_user_id == owner_user_id, Style.style_code == code)
    ).scalar_one_or_none()
    if existing is None:
        session.add(Style(style_code=code, title=code, owner_user_id=owner_user_id))


def _split_target(body: str) -> tuple[str, str] | None:
    for sep in (" 到 ", " 从 "):
        if sep in body:
            head, _, tail = body.rpartition(sep)
            head, tail = head.strip(" ：:，,。.！!"), tail.strip(" ：:，,。.！!")
            if head and tail:
                return head, tail
    return None


def _create_task(session: Session, owner_user_id, body: str) -> str:
    parts = _tokens(body)
    if len(parts) < 2:
        return "格式：新建 <任务名> <货号…> [门店…]。比如：新建 盯紧外套 DN3707-202 3402 3408"
    name, code_tokens = parts[0], parts[1:]
    rows = (
        session.execute(
            select(MonitorTask).where(MonitorTask.created_by == owner_user_id)
        ).scalars().all()
    )
    if any(task.name.lower() == name.lower() for task in rows):
        return f"已经有叫「{name}」的任务了。换个名字，或发「任务」看看。"
    store_codes, style_codes, unknown = _classify_codes(session, owner_user_id, code_tokens)
    if unknown:
        return "这些门店号还没登记：" + "、".join(unknown[:5]) + "。发「门店」看已登记的门店。"
    if not style_codes:
        return "至少要给一个货号。比如：新建 盯紧外套 DN3707-202"
    user = session.get(User, owner_user_id)
    if user is None:
        return "账号状态异常，请稍后再试。"
    for code in style_codes:
        _ensure_style(session, owner_user_id, code)
    task = MonitorTask(
        tenant_id=user.tenant_id,
        name=name,
        store_codes=store_codes,
        style_codes=style_codes,
        all_stores=not store_codes,
        cadence_seconds=1800,
        is_active=True,
        created_by=owner_user_id,
    )
    session.add(task)
    session.commit()
    scope = f"门店 {len(store_codes)} 家" if store_codes else "全部门店"
    return (
        f"已新建「{name}」：货号 {len(style_codes)} 个、{scope}，每 30 分钟检查一次。"
        f"发「检查 {name}」可以马上跑一次。"
    )


def _edit_codes(session: Session, owner_user_id, body: str, *, styles: bool, add: bool) -> str:
    what = "货号" if styles else "门店"
    parsed = _split_target(body)
    if parsed is None:
        return f"格式：{'加' if add else '删'}{what} <{what}…> 到 <任务名>"
    codes_text, task_name = parsed
    task, problem = _pick_task(session, owner_user_id, task_name)
    if problem:
        return problem
    tokens = _tokens(codes_text)
    if not tokens:
        return f"没给{what}。格式：{'加' if add else '删'}{what} <{what}…> 到 <任务名>"
    if styles:
        wanted = [token.upper() for token in tokens]
        current = list(task.style_codes or [])
        if add:
            changed = [code for code in wanted if code not in current]
            if not changed:
                return f"「{task.name}」已经有这些货号了。"
            for code in changed:
                _ensure_style(session, owner_user_id, code)
            task.style_codes = current + changed
        else:
            changed = [code for code in wanted if code in current]
            if not changed:
                return f"「{task.name}」没有这些货号：{'、'.join(wanted[:5])}"
            remaining = [code for code in current if code not in set(wanted)]
            if not remaining:
                return f"删完「{task.name}」就没有货号了。想换个目标，直接「删货号」之前先加新的。"
            task.style_codes = remaining
        session.commit()
        verb = "加了" if add else "删了"
        now = len(task.style_codes or [])
        return f"给「{task.name}」{verb}货号：{'、'.join(changed)}。现在货号 {now} 个。"
    stores = _registered_stores(session, owner_user_id)
    wanted = []
    unknown = []
    for token in tokens:
        code = token.upper()
        if code in stores:
            if code not in wanted:
                wanted.append(code)
        else:
            unknown.append(token)
    if unknown:
        return "这些门店号还没登记：" + "、".join(unknown[:5]) + "。发「门店」看已登记的门店。"
    if task.all_stores:
        return f"「{task.name}」现在盯的是全部门店。只想盯几家的话，到网页上把门店改成指定几家。"
    current = [str(code).upper() for code in (task.store_codes or [])]
    if add:
        changed = [code for code in wanted if code not in current]
        if not changed:
            return f"「{task.name}」已经有这些门店了。"
        task.store_codes = current + changed
    else:
        changed = [code for code in wanted if code in current]
        if not changed:
            return f"「{task.name}」没有这些门店：{'、'.join(wanted[:5])}"
        remaining = [code for code in current if code not in set(wanted)]
        if not remaining:
            return f"删完「{task.name}」就没有门店了。想只盯几家请先在网页上改。"
        task.store_codes = remaining
    session.commit()
    verb = "加了" if add else "删了"
    now = len(task.store_codes or [])
    return f"给「{task.name}」{verb}门店：{'、'.join(changed)}。现在门店 {now} 家。"


def _parse_cadence(text: str) -> int | None:
    match = re.fullmatch(r"(\d+)\s*(分钟?|分|小时|时|h|hour)?", text.strip().lower())
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2) or ""
    seconds = amount * 3600 if unit in {"小时", "时", "h", "hour"} else amount * 60
    if not (300 <= seconds <= 86400):
        return None
    return seconds


def _set_cadence(session: Session, owner_user_id, body: str) -> str:
    parsed = _split_target(body)
    if parsed is None:
        return "格式：频率 <时间> 到 <任务名>。比如：频率 30分钟 到 钉钉测试任务"
    time_text, task_name = parsed
    seconds = _parse_cadence(time_text)
    if seconds is None:
        return "时间要在 5 分钟到 24 小时之间。比如：频率 30分钟 到 钉钉测试任务"
    task, problem = _pick_task(session, owner_user_id, task_name)
    if problem:
        return problem
    task.cadence_seconds = seconds
    session.commit()
    minutes = max(1, seconds // 60)
    return f"「{task.name}」改成每 {minutes} 分钟检查一次。"


def _delete_task(session: Session, owner_user_id, name: str) -> str:
    key = name.strip()
    if not key:
        return "格式：删除 <任务名>（要写完整名字）"
    rows = (
        session.execute(
            select(MonitorTask).where(MonitorTask.created_by == owner_user_id)
        ).scalars().all()
    )
    exact = [task for task in rows if task.name.lower() == key.lower()]
    if not exact:
        near = [task for task in rows if key.lower() in task.name.lower()][:5]
        if near:
            return "没找到完全同名的任务。是想删这些吗：" + "、".join(t.name for t in near) + "。发完整名字。"
        return f"没找到叫「{key}」的任务。发「任务」看看列表。"
    task = exact[0]
    session.delete(task)
    session.commit()
    return f"已删除「{task.name}」。"


_CST = timezone(timedelta(hours=8))
_NOTIFY_LABELS = {
    "补货": "notify_on_restock",
    "售罄": "notify_on_sold_out",
    "变价": "notify_on_price_change",
}
_JOB_STATE = {
    "QUEUED": "排队中",
    "RUNNING": "检查中",
    "SUCCEEDED": "完成",
    "PARTIAL": "部分完成",
    "FAILED": "失败",
    "CANCELLED": "已取消",
}


def _set_stores(session: Session, owner_user_id, body: str) -> str:
    parsed = _split_target(body)
    if parsed is None:
        return "格式：盯 <门店…> 到 <任务名>。比如：盯 3402 3408 到 盯外套；也可以：盯 全部门店 到 盯外套"
    stores_text, task_name = parsed
    task, problem = _pick_task(session, owner_user_id, task_name)
    if problem:
        return problem
    tokens = _tokens(stores_text)
    if any(token in {"全部门店", "全部", "全国"} for token in tokens):
        task.all_stores = True
        task.store_codes = []
        session.commit()
        return f"「{task.name}」改为盯全部门店。"
    stores = _registered_stores(session, owner_user_id)
    wanted: list[str] = []
    unknown: list[str] = []
    for token in tokens:
        code = token.upper()
        if code in stores:
            if code not in wanted:
                wanted.append(code)
        else:
            unknown.append(token)
    if unknown:
        return "这些门店号还没登记：" + "、".join(unknown[:5]) + "。发「门店」看已登记的门店。"
    if not wanted:
        return f"至少要给一家门店。比如：盯 3402 到 {task.name}"
    task.all_stores = False
    task.store_codes = wanted
    session.commit()
    return f"「{task.name}」改为盯 {len(wanted)} 家门店：{'、'.join(wanted[:10])}。"


def _notify_reply(session: Session, owner_user_id, body: str) -> str:
    tokens = _tokens(body)
    settings_tokens: list[str] = []
    while tokens and tokens[-1][-1] in {"开", "关"} and tokens[-1][:-1] in _NOTIFY_LABELS:
        settings_tokens.insert(0, tokens.pop())
    name = " ".join(tokens).strip()
    if not name:
        return "格式：通知 <任务名> [补货开/关 售罄开/关 变价开/关]。比如：通知 盯外套 补货开 变价关"
    task, problem = _pick_task(session, owner_user_id, name)
    if problem:
        return problem
    state = lambda flag: "开" if flag else "关"  # noqa: E731
    if not settings_tokens:
        return (
            f"「{task.name}」提醒设置：补货 {state(task.notify_on_restock)}、"
            f"售罄 {state(task.notify_on_sold_out)}、变价 {state(task.notify_on_price_change)}。"
        )
    changed = []
    for token in settings_tokens:
        label, flag = token[:-1], token[-1] == "开"
        setattr(task, _NOTIFY_LABELS[label], flag)
        changed.append(f"{label} {'开' if flag else '关'}")
    session.commit()
    return f"「{task.name}」提醒设置已更新：" + "、".join(changed) + "。"


def _latest_job(session: Session, task_id):
    return session.execute(
        select(ScanJob).where(ScanJob.task_id == task_id).order_by(ScanJob.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def _last_check_reply(session: Session, owner_user_id, name: str) -> str:
    task, problem = _pick_task(session, owner_user_id, name)
    if problem:
        return problem
    job = _latest_job(session, task.id)
    if job is None:
        return f"「{task.name}」还没有检查过。发「检查 {task.name}」马上跑一次。"
    label = _JOB_STATE.get(job.status, job.status)
    when = job.finished_at or job.started_at or job.created_at
    stamp = when.astimezone(_CST).strftime("%m-%d %H:%M") if when else "—"
    lines = [
        f"「{task.name}」上次检查：{label}（{stamp}）。",
        f"门店 {job.done_stores}/{job.total_stores} 完成，{job.failed_stores} 家失败。",
    ]
    if job.failed_stores and job.status not in {"QUEUED", "RUNNING"}:
        lines.append(f"发「重试失败 {task.name}」可以把失败的再跑一遍。")
    return "\n".join(lines)


def _retry_failed_reply(session: Session, settings: Settings, owner_user_id, name: str) -> str:
    task, problem = _pick_task(session, owner_user_id, name)
    if problem:
        return problem
    job = _latest_job(session, task.id)
    if job is None:
        return f"「{task.name}」还没有检查过。发「检查 {task.name}」马上跑一次。"
    if job.status in {"QUEUED", "RUNNING"}:
        return f"「{task.name}」还在排队或检查中，等这次跑完再说。"
    try:
        binding = CredentialService(settings, owner_user_id).binding()
    except CredentialError:
        return "需要先在网页上连接 Nike 账号（「账户与额度」），才能重试。"
    if job.credential_id != binding.credential_id or job.credential_generation != binding.generation:
        return f"账号连接换过了。发「检查 {task.name}」重新跑一轮。"
    count = requeue_failed_stores(session, job)
    if count == 0:
        return f"「{task.name}」上次没有失败的门店，不用重试。"
    session.commit()
    return f"「{task.name}」已重试 {count} 家失败门店，结果会推到这里。"


def _disconnect_reply(session: Session, settings: Settings, owner_user_id, command: str) -> str | None:
    if "注销" not in command:
        return None
    if command.strip().strip("。.!！") != "确认注销":
        return "这一步会断开 Nike 连接、停掉所有自动检查（任务和渠道都保留）。确认请回复：确认注销"
    nike_auth.disconnect(settings=settings, user_id=owner_user_id)
    return "已断开 Nike 连接，检查会停。想恢复就到网页「账户与额度」重新连接。"


def _overview_reply(session: Session, owner_user_id) -> str:
    user = session.get(User, owner_user_id)
    if user is None:
        return "账号状态异常，请稍后再试。"
    today = datetime.now(UTC)
    day = datetime(today.year, today.month, today.day, tzinfo=UTC)
    tasks = (
        session.execute(
            select(MonitorTask).where(MonitorTask.created_by == owner_user_id)
        ).scalars().all()
    )
    active = [task for task in tasks if task.is_active]
    counts: dict[str, int] = {}
    for event in ("RESTOCK", "SOLD_OUT", "PRICE_CHANGE"):
        counts[event] = int(
            session.execute(
                select(func.count())
                .select_from(ChangeEvent)
                .where(
                    ChangeEvent.owner_user_id == owner_user_id,
                    ChangeEvent.tenant_id == user.tenant_id,
                    ChangeEvent.event_type == event,
                    ChangeEvent.detected_at >= day,
                )
            ).scalar_one()
        )
    last_job = session.execute(
        select(ScanJob)
        .where(ScanJob.owner_user_id == owner_user_id)
        .order_by(ScanJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    last = ""
    if last_job is not None:
        when = last_job.finished_at or last_job.started_at or last_job.created_at
        stamp = when.astimezone(_CST).strftime("%m-%d %H:%M") if when else "—"
        last = f"最近一次检查：{_JOB_STATE.get(last_job.status, last_job.status)}（{stamp}）。"
    restock, sold_out, price_change = counts["RESTOCK"], counts["SOLD_OUT"], counts["PRICE_CHANGE"]
    lines = [
        f"监控任务 {len(active)} 个启用（共 {len(tasks)} 个）。",
        f"今天：补货 {restock} 条、售罄 {sold_out} 条、变价 {price_change} 条。",
    ]
    if last:
        lines.append(last)
    if active and all(task.next_run_at is None for task in active):
        lines.append("任务还没有排下一次检查，发「检查 <任务名>」可以马上跑。")
    return "\n".join(lines)


def _failed_stores_reply(session: Session, owner_user_id, name: str) -> str:
    task, problem = _pick_task(session, owner_user_id, name)
    if problem:
        return problem
    job = _latest_job(session, task.id)
    if job is None:
        return f"「{task.name}」还没有检查过。发「检查 {task.name}」马上跑一次。"
    failed_statuses = [StoreJobStatus.FAILED.value, StoreJobStatus.UNKNOWN.value]
    total = int(
        session.execute(
            select(func.count())
            .select_from(ScanJobStore)
            .where(ScanJobStore.job_id == job.id, ScanJobStore.status.in_(failed_statuses))
        ).scalar_one()
    )
    if total == 0:
        return f"「{task.name}」上次检查没有失败的门店。"
    rows = (
        session.execute(
            select(ScanJobStore, Store)
            .join(Store, Store.id == ScanJobStore.store_id)
            .where(ScanJobStore.job_id == job.id, ScanJobStore.status.in_(failed_statuses))
            .order_by(ScanJobStore.id)
            .limit(10)
        )
        .all()
    )
    lines = [f"「{task.name}」上次检查有 {total} 家失败："]
    for row, store in rows:
        detail = (row.message or row.error_code or "未知原因")[:40]
        lines.append(f"· {store.store_code}（{detail}）")
    if total > len(rows):
        lines.append(f"…另有 {total - len(rows)} 家")
    lines.append(f"发「重试失败 {task.name}」可以把它们再跑一遍。")
    return "\n".join(lines)


_AVAILABILITY_LABEL = {
    "IN_STOCK": "有货",
    "OUT_OF_STOCK": "无货",
    "NOT_CARRIED": "未上架",
}


def _style_detail_reply(session: Session, owner_user_id, text: str) -> str:
    code = text.strip().upper()
    if not code:
        return "格式：详情 <货号>，比如：详情 DN3707-202"
    style = session.execute(
        select(Style).where(Style.owner_user_id == owner_user_id, Style.style_code == code)
    ).scalar_one_or_none()
    if style is None:
        return f"没有找到货号「{code}」。发「货号」看看在监控的清单。"
    names = [
        task.name
        for task in session.execute(
            select(MonitorTask).where(MonitorTask.created_by == owner_user_id)
        ).scalars()
        if code in [str(item).upper() for item in (task.style_codes or [])]
    ]
    rows = session.execute(
        select(ObservationLatest, Store)
        .join(Store, Store.id == ObservationLatest.store_id)
        .where(
            ObservationLatest.owner_user_id == owner_user_id,
            ObservationLatest.style_id == style.id,
        )
        .order_by(ObservationLatest.checked_at.desc())
        .limit(4000)
    ).all()
    stores: dict[str, dict] = {}
    for latest, store in rows:
        entry = stores.setdefault(store.store_code, {"status": "未知", "price": None})
        if latest.availability == "IN_STOCK":
            entry["status"] = "有货"
            if latest.total_price is not None:
                price = float(latest.total_price)
                entry["price"] = price if entry["price"] is None else min(entry["price"], price)
        elif entry["status"] != "有货" and latest.availability in _AVAILABILITY_LABEL:
            entry["status"] = _AVAILABILITY_LABEL[latest.availability]
    header = code if not style.title or style.title == code else f"{code}（{style.title}）"
    lines = [header]
    if names:
        lines.append("在监控任务：" + "、".join(names[:5]))
    else:
        lines.append("还没有任务在监控它。")
    if not stores:
        lines.append("还没有门店检查记录。发「检查」跑一轮就有了。")
        return "\n".join(lines)
    counts: dict[str, int] = {}
    for entry in stores.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    order = ("有货", "无货", "未上架", "未知")
    summary = "、".join(f"{label} {counts[label]}" for label in order if label in counts)
    lines.append(f"门店 {len(stores)} 家检查过：{summary}。")
    in_stock = sorted(
        (code_, entry) for code_, entry in stores.items() if entry["status"] == "有货"
    )
    for store_code, entry in in_stock[:6]:
        price = f"¥{entry['price']:g}" if entry["price"] is not None else "待补价"
        lines.append(f"· {store_code} {price}")
    if len(in_stock) > 6:
        lines.append(f"…另有 {len(in_stock) - 6} 家有货")
    events = (
        session.execute(
            select(ChangeEvent)
            .where(
                ChangeEvent.owner_user_id == owner_user_id,
                ChangeEvent.style_id == style.id,
            )
            .order_by(ChangeEvent.detected_at.desc())
            .limit(3)
        )
        .scalars()
        .all()
    )
    if events:
        labels = {
            "RESTOCK": "补货",
            "SOLD_OUT": "售罄",
            "PRICE_CHANGE": "变价",
            "NEW_ARRIVAL_AT_STORE": "新上架",
        }
        parts = [
            f"{labels.get(event.event_type, event.event_type)} {event.size_label}"
            for event in events
        ]
        lines.append("最近变化：" + "；".join(parts))
    return "\n".join(lines)


def reply_for(session: Session, settings: Settings, link: DingTalkBotLink, text: str) -> str:
    """已绑定会话里的关键字路由（纯函数，方便单测）。"""
    command = text.strip()
    if "解绑" in command:
        unlink_conversation(session, settings, link.conversation_id)
        return UNLINKED_TEXT
    if command in {"帮助", "指令", "help", "?", "？"}:
        return HELP_TEXT
    disconnect = _disconnect_reply(session, settings, link.owner_user_id, command)
    if disconnect is not None:
        return disconnect
    arg = _command_arg(command, "立即检查", "检查")
    if arg is not None:
        return _trigger_check(session, settings, link.owner_user_id, arg)
    arg = _command_arg(command, "暂停")
    if arg is not None:
        return _toggle_task(session, link.owner_user_id, arg, active=False)
    arg = _command_arg(command, "启用")
    if arg is not None:
        return _toggle_task(session, link.owner_user_id, arg, active=True)
    if "额度" in command:
        return _quota_reply(session, link.owner_user_id)
    if "概览" in command:
        return _overview_reply(session, link.owner_user_id)
    arg = _command_arg(command, "新建")
    if arg is not None:
        return _create_task(session, link.owner_user_id, arg)
    arg = _command_arg(command, "加货号")
    if arg is not None:
        return _edit_codes(session, link.owner_user_id, arg, styles=True, add=True)
    arg = _command_arg(command, "删货号")
    if arg is not None:
        return _edit_codes(session, link.owner_user_id, arg, styles=True, add=False)
    arg = _command_arg(command, "加门店")
    if arg is not None:
        return _edit_codes(session, link.owner_user_id, arg, styles=False, add=True)
    arg = _command_arg(command, "删门店")
    if arg is not None:
        return _edit_codes(session, link.owner_user_id, arg, styles=False, add=False)
    arg = _command_arg(command, "频率")
    if arg is not None:
        return _set_cadence(session, link.owner_user_id, arg)
    arg = _command_arg(command, "盯")
    if arg is not None:
        return _set_stores(session, link.owner_user_id, arg)
    arg = _command_arg(command, "通知")
    if arg is not None:
        return _notify_reply(session, link.owner_user_id, arg)
    arg = _command_arg(command, "上次检查")
    if arg is not None:
        return _last_check_reply(session, link.owner_user_id, arg)
    arg = _command_arg(command, "重试失败")
    if arg is not None:
        return _retry_failed_reply(session, settings, link.owner_user_id, arg)
    arg = _command_arg(command, "失败门店")
    if arg is not None:
        return _failed_stores_reply(session, link.owner_user_id, arg)
    arg = _command_arg(command, "删除")
    if arg is not None:
        return _delete_task(session, link.owner_user_id, arg)
    if "任务" in command:
        return _tasks_reply(session, link.owner_user_id)
    if "门店" in command:
        return _stores_reply(session, link.owner_user_id)
    if "货号" in command:
        return _styles_reply(session, link.owner_user_id)
    arg = _command_arg(command, "详情")
    if arg is not None:
        return _style_detail_reply(session, link.owner_user_id, arg)
    if "变化" in command:
        return _changes_reply(session, link.owner_user_id)
    if "状态" in command:
        return _status_reply(session, link.owner_user_id)
    return HELP_TEXT


def handle_message(settings: Settings, data: dict) -> str | None:
    """消息入口（同步）；返回要回给会话的文字，None 表示静默。"""
    text = str((data.get("text") or {}).get("content") or "")
    conversation_id = str(data.get("conversationId") or "")
    if not (text and conversation_id):
        return None
    with SessionLocal() as session:
        existing = session.execute(
            select(DingTalkBotLink).where(DingTalkBotLink.conversation_id == conversation_id)
        ).scalar_one_or_none()
        explicit_bind = ("绑定" in text) or ("bind" in text.lower())
        matched = find_code_owner(session, text)
        if matched is not None and (explicit_bind or existing is None):
            code_row, user = matched
            if existing is not None and existing.owner_user_id != user.id:
                # 已绑定的会话不能被另一个账号的验证码顶掉。
                return "本会话已经绑定了其他账号。如需更换，请先在原账号里解绑。"
            link = link_conversation(session, settings, user.id, data)
            session.delete(code_row)
            session.commit()
            where = "单聊" if link.conversation_type == 1 else f"群「{link.conversation_title or '未命名'}」"
            return f"绑定成功：{where} 已关联到 {user.email}，提醒会发到这里。\n\n{HELP_TEXT}"
        if existing is None:
            return NOT_LINKED_TEXT
        reply = reply_for(session, settings, existing, text)
        session.commit()
        return reply


class BotHandler(CallbackHandler):
    """Stream 回调：绑定码认领、关键字应答，全部按绑定账号的数据回答。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    async def process(self, callback: CallbackMessage):  # type: ignore[override]
        data = dict(callback.data or {})
        try:
            reply = await asyncio.to_thread(handle_message, self.settings, data)
        except Exception:  # noqa: BLE001 - 机器人不能因为一次异常退出
            logger.exception("dingtalk bot failed to handle a message")
            reply = None
        if reply:
            webhook = str(data.get("sessionWebhook") or "")
            await asyncio.to_thread(_reply, webhook, reply)
        return AckMessage.STATUS_OK, "OK"


def _reply(webhook: str, text: str) -> None:
    if not webhook:
        return
    import httpx

    try:
        httpx.post(webhook, json={"msgtype": "text", "text": {"content": text}}, timeout=8)
    except Exception:  # noqa: BLE001 - 回复失败只记日志
        logger.warning("dingtalk bot reply failed", exc_info=True)


async def run_forever(settings: Settings) -> None:
    """保持平台机器人的 Stream 连接；没配置就什么都不做。"""
    configured = (
        settings.dingtalk_bot_enabled
        and settings.dingtalk_bot_client_id
        and settings.dingtalk_bot_client_secret
    )
    if not configured:
        logger.info("dingtalk bot disabled or not configured")
        return
    while True:
        try:
            client = DingTalkStreamClient(
                StreamCredential(
                    settings.dingtalk_bot_client_id, settings.dingtalk_bot_client_secret
                )
            )
            client.register_callback_handler(ChatbotMessage.TOPIC, BotHandler(settings))
            logger.info("dingtalk bot listening as %s", settings.dingtalk_bot_client_id)
            await client.start()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 断线重连，不能拖垮进程
            logger.exception("dingtalk bot connection failed; retrying")
        await asyncio.sleep(3)
