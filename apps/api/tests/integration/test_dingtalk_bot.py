"""钉钉机器人：绑定会话、关键字指令、自动配推送渠道（模拟回调，不连钉钉）。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    DingTalkBotLink,
    MonitorTask,
    NotificationChannel,
    ScanJob,
    ScanJobStore,
    Style,
)
from app.services import dingtalk_bot

from .helpers import make_store, make_style, make_tenant, make_user, store_credential


def _settings() -> Settings:
    return Settings(
        credential_key="test-key-for-unit-tests",
        dingtalk_bot_enabled=True,
        dingtalk_bot_client_id="ding_bot",
        dingtalk_bot_client_secret="secret_with_underscore",
    )


def _message(conversation_id: str, text: str, *, kind: int = 2) -> dict:
    return {
        "conversationId": conversation_id,
        "conversationType": kind,
        "conversationTitle": "测试群" if kind == 2 else "",
        "senderStaffId": "452523285939877041",
        "chatbotCorpId": "corp-1",
        "text": {"content": text},
    }


def _seed_user() -> tuple[str, str]:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant)
        make_store(db, owner=user, code="QA900")
        make_style(db, owner=user, code="DN9999-001")
        make_style(db, owner=user, code="DN3707-202")
        db.add(
            MonitorTask(
                tenant_id=tenant.id,
                name="钉钉任务",
                store_codes=[],
                style_codes=["DN3707-202"],
                all_stores=True,
                created_by=user.id,
            )
        )
        db.commit()
        return str(user.id), user.email


def test_bot_links_a_conversation_and_answers_keywords() -> None:
    settings = _settings()
    user_id, email = _seed_user()
    with SessionLocal() as db:
        code = dingtalk_bot.issue_code(db, settings, user_id).code
        db.commit()

    reply = dingtalk_bot.handle_message(settings, _message("cid-group", f"绑定 {code}"))
    assert reply is not None and "绑定成功" in reply and email in reply

    with SessionLocal() as db:
        link = db.execute(select(DingTalkBotLink)).scalar_one()
        assert str(link.owner_user_id) == user_id
        assert link.conversation_type == 2
        channel = db.execute(select(NotificationChannel)).scalar_one()
        assert channel.platform == "dingtalk_app"
        assert channel.owner_user_id == link.owner_user_id
        # 群会话渠道：目标里带 conversation，不带 dm
        assert "dm=1" not in channel.target_hint
        assert channel.target_hint.startswith("钉钉群 · ding_bot")

    assert "钉钉任务" in (dingtalk_bot.handle_message(settings, _message("cid-group", "任务")) or "")
    assert "最近 24 小时" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "变化")) or ""
    )
    assert "已登记 1 家门店" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "门店")) or ""
    )
    assert "DN9999-001" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "货号")) or ""
    )
    assert "Nike 连接" in (dingtalk_bot.handle_message(settings, _message("cid-group", "状态")) or "")
    assert "可用指令" in (dingtalk_bot.handle_message(settings, _message("cid-group", "随便")) or "")

    # 暂停 / 启用
    assert "已暂停" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "暂停 钉钉任务")) or ""
    )
    with SessionLocal() as db:
        assert db.execute(select(MonitorTask)).scalar_one().is_active is False
    assert "已启用" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "启用 钉钉任务")) or ""
    )
    with SessionLocal() as db:
        assert db.execute(select(MonitorTask)).scalar_one().is_active is True

    # 额度 / 概览
    assert "今日额度" in (dingtalk_bot.handle_message(settings, _message("cid-group", "额度")) or "")
    overview = dingtalk_bot.handle_message(settings, _message("cid-group", "概览")) or ""
    assert "监控任务 1 个启用" in overview
    assert "补货" in overview

    # 检查：未连接 Nike 时给出可操作的提示
    assert "连接 Nike 账号" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "检查 钉钉任务")) or ""
    )
    # 连接后再检查：真的排出一次检查任务
    store_credential(settings, UUID(user_id))
    assert "已开始检查" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "检查 钉钉任务")) or ""
    )
    with SessionLocal() as db:
        job = db.execute(select(ScanJob)).scalar_one()
        assert job.task_id is not None
        assert job.store_codes == ["QA900"]
        assert job.style_codes == ["DN3707-202"]

    # 新建：不写门店 = 全部门店；自动登记新货号
    assert "已新建" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "新建 聊天任务 DN1111-001")) or ""
    )
    with SessionLocal() as db:
        created = db.execute(
            select(MonitorTask).where(MonitorTask.name == "聊天任务")
        ).scalar_one()
        assert created.all_stores is True
        assert created.style_codes == ["DN1111-001"]
        assert db.execute(
            select(Style).where(Style.style_code == "DN1111-001")
        ).scalar_one() is not None
    # 指定门店 + 重名拒绝
    assert "已经有叫" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "新建 聊天任务 DN2222-001 QA900")) or ""
    )
    assert "已新建" in (
        dingtalk_bot.handle_message(
            settings, _message("cid-group", "新建 指定任务 DN2222-001 QA900")
        ) or ""
    )
    # 加/删货号
    assert "加了货号" in (
        dingtalk_bot.handle_message(
            settings, _message("cid-group", "加货号 DN3333-001 到 指定任务")
        ) or ""
    )
    assert "删了货号" in (
        dingtalk_bot.handle_message(
            settings, _message("cid-group", "删货号 DN2222-001 从 指定任务")
        ) or ""
    )
    with SessionLocal() as db:
        edited = db.execute(
            select(MonitorTask).where(MonitorTask.name == "指定任务")
        ).scalar_one()
        assert edited.style_codes == ["DN3333-001"]
    # 全部门店的任务不允许按门店加减
    assert "全部门店" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "加门店 QA900 到 聊天任务")) or ""
    )
    # 删到没有门店要拦住
    assert "就没有门店了" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "删门店 QA900 到 指定任务")) or ""
    )
    # 频率
    assert "改成每 10 分钟" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "频率 10分钟 到 指定任务")) or ""
    )
    assert "5 分钟到 24 小时" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "频率 1分钟 到 指定任务")) or ""
    )
    # 盯：换成指定门店 / 换回全部门店
    assert "改为盯 1 家门店" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "盯 QA900 到 指定任务")) or ""
    )
    assert "改为盯全部门店" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "盯 全部门店 到 指定任务")) or ""
    )
    assert "还没登记" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "盯 9999 到 指定任务")) or ""
    )
    # 通知：查看 + 修改开关
    assert "提醒设置：补货 开、售罄 开、变价 开" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "通知 指定任务")) or ""
    )
    assert "已更新：补货 关、变价 关" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "通知 指定任务 补货关 变价关")) or ""
    )
    with SessionLocal() as db:
        toggled = db.execute(
            select(MonitorTask).where(MonitorTask.name == "指定任务")
        ).scalar_one()
        assert toggled.notify_on_restock is False
        assert toggled.notify_on_sold_out is True
        assert toggled.notify_on_price_change is False
    # 详情：列任务归属与门店覆盖 / 未收录的货号
    detail = dingtalk_bot.handle_message(settings, _message("cid-group", "详情 DN3707-202")) or ""
    assert "在监控任务：钉钉任务" in detail
    assert "还没有门店检查记录" in detail
    assert "没有找到货号" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "详情 XX-999")) or ""
    )
    # 上次检查 / 重试失败（钉钉任务此时还在队列里）
    assert "上次检查：排队中" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "上次检查 钉钉任务")) or ""
    )
    assert "还在排队或检查中" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "重试失败 钉钉任务")) or ""
    )
    # 失败门店：先造一家失败（消息截断到 40 字）
    with SessionLocal() as db:
        row = db.execute(
            select(ScanJobStore).join(ScanJob, ScanJob.id == ScanJobStore.job_id)
        ).scalars().first()
        row.status = "FAILED"
        row.error_code = "TRANSPORT_ERROR"
        row.message = "上游连接超时"
        db.commit()
    failed = dingtalk_bot.handle_message(settings, _message("cid-group", "失败门店 钉钉任务")) or ""
    assert "1 家失败" in failed and "QA900" in failed and "重试失败" in failed
    # 注销：两步确认
    assert "确认注销" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "注销")) or ""
    )
    assert "已断开 Nike 连接" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "确认注销")) or ""
    )
    assert "Nike 连接：已断开" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "状态")) or ""
    )
    # 删除：必须是完整名字
    assert "是想删这些吗" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "删除 聊天")) or ""
    )
    assert "已删除" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "删除 聊天任务")) or ""
    )
    # 未登记门店的提示
    assert "还没登记" in (
        dingtalk_bot.handle_message(settings, _message("cid-group", "新建 杂项任务 DN4444-001 9999")) or ""
    )

    other = dingtalk_bot.handle_message(settings, _message("cid-unknown", "任务"))
    assert other is not None and "还没有绑定账号" in other

    unbound = dingtalk_bot.handle_message(settings, _message("cid-group", "解绑"))
    assert unbound is not None and "已解除" in unbound
    with SessionLocal() as db:
        assert db.execute(select(DingTalkBotLink)).scalars().all() == []
        assert db.execute(select(NotificationChannel)).scalars().all() == []


def test_bot_links_a_single_chat_and_replies_to_the_same_person() -> None:
    settings = _settings()
    user_id, _email = _seed_user()
    with SessionLocal() as db:
        code = dingtalk_bot.issue_code(db, settings, user_id).code
        db.commit()

    data = _message("cid-dm", f"绑定 {code}", kind=1)
    assert "绑定成功" in (dingtalk_bot.handle_message(settings, data) or "")

    with SessionLocal() as db:
        channel = db.execute(select(NotificationChannel)).scalar_one()
        assert channel.target_hint.startswith("钉钉单聊 · ding_bot")
        # 单聊目标按 userIds 发送：hint 里带的是钉钉用户 ID 尾号
        assert "7041" in channel.target_hint
