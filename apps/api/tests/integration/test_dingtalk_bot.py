"""钉钉机器人：客户自己填凭据，@ 机器人即自动绑定（模拟回调，不连钉钉）。"""

from __future__ import annotations

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import DingTalkBotLink, DingTalkRobot, MonitorTask, NotificationChannel
from app.services import dingtalk_bot, push

from .helpers import auth_headers, login, make_store, make_style, make_tenant, make_user


def _settings() -> Settings:
    return Settings(credential_key="test-key-for-unit-tests", dingtalk_bot_enabled=False)


def _message(conversation_id: str, text: str, *, kind: int = 2) -> dict:
    return {
        "conversationId": conversation_id,
        "conversationType": kind,
        "conversationTitle": "测试群" if kind == 2 else "",
        "senderStaffId": "452523285939877041",
        "chatbotCorpId": "corp-1",
        "text": {"content": text},
    }


def _seed_user(*, email: str | None = None, robot_code: str = "ding_app_key") -> tuple[str, str]:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=email)
        make_store(db, owner=user, code="QA900")
        make_style(db, owner=user, code="DN9999-001")
        db.add(
            MonitorTask(
                tenant_id=tenant.id,
                name="钉钉任务",
                store_codes=[],
                style_codes=["DN9999-001"],
                all_stores=True,
                created_by=user.id,
            )
        )
        dingtalk_bot.save_robot(db, user.id, robot_code, "secret_with_underscore")
        db.commit()
        return str(user.id), user.email


def test_first_message_binds_the_conversation_and_answers_keywords() -> None:
    settings = _settings()
    user_id, email = _seed_user()

    reply = dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "任务"))
    assert reply is not None and "绑定成功" in reply and email in reply

    with SessionLocal() as db:
        link = db.execute(select(DingTalkBotLink)).scalar_one()
        assert str(link.owner_user_id) == user_id
        assert link.conversation_type == 2
        channel = db.execute(select(NotificationChannel)).scalar_one()
        assert channel.platform == "dingtalk_app"
        assert channel.owner_user_id == link.owner_user_id
        # 渠道用的是客户自己的凭据；群会话目标不带 dm
        assert channel.target_hint.startswith("钉钉群 · ding_app_key")
        assert "dm=1" not in channel.target_hint

    # 绑定之后就是普通指令：绑定动作只发生一次
    assert "钉钉任务" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "任务")) or ""
    )
    assert "最近 24 小时" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "变化")) or ""
    )
    assert "已登记 1 家门店" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "门店")) or ""
    )
    assert "DN9999-001" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "货号")) or ""
    )
    assert "Nike 连接" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "状态")) or ""
    )
    assert "【管理】" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "帮助")) or ""
    )
    assert "已暂停" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "暂停 钉钉任务")) or ""
    )
    with SessionLocal() as db:
        assert db.execute(select(DingTalkBotLink)).scalar_one() is not None

    unbound = dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "解绑"))
    assert unbound is not None and "已解除" in unbound
    with SessionLocal() as db:
        assert db.execute(select(DingTalkBotLink)).scalars().all() == []
        assert db.execute(select(NotificationChannel)).scalars().all() == []


def test_single_chat_binds_by_the_sender_and_targets_the_user_id() -> None:
    settings = _settings()
    user_id, _email = _seed_user()
    assert "绑定成功" in (
        dingtalk_bot.handle_message(settings, user_id, _message("cid-dm", "你好", kind=1)) or ""
    )
    with SessionLocal() as db:
        channel = db.execute(select(NotificationChannel)).scalar_one()
        assert channel.target_hint.startswith("钉钉单聊 · ding_app_key")
        # 单聊按 userIds 发送：hint 里留的是发送人尾号
        assert "7041" in channel.target_hint


def test_a_conversation_keeps_its_owner_when_another_robot_messages() -> None:
    settings = _settings()
    first_id, _first = _seed_user(email="first@example.com")
    second_id, _second = _seed_user(email="second@example.com", robot_code="ding_other")
    dingtalk_bot.handle_message(settings, first_id, _message("cid-shared", "任务"))
    refused = dingtalk_bot.handle_message(settings, second_id, _message("cid-shared", "任务"))
    assert refused is not None and "已经绑定了其他账号" in refused
    with SessionLocal() as db:
        link = db.execute(select(DingTalkBotLink)).scalar_one()
        assert str(link.owner_user_id) == first_id


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_robot_api_validates_saves_and_rebuilds_channels(client, monkeypatch) -> None:
    settings = _settings()
    user_id, email = _seed_user(robot_code="old_key")
    csrf = login(client, email)
    monkeypatch.setattr(
        push.httpx, "post", lambda url, **kwargs: _Response(200, {"accessToken": "tok"})
    )

    # 先绑一个会话：渠道里写的是旧凭据
    dingtalk_bot.handle_message(settings, user_id, _message("cid-group", "任务"))

    saved = client.put(
        "/api/v1/me/dingtalk/robot",
        json={"robot_code": "new_key", "robot_secret": "new_secret"},
        headers=auth_headers(csrf),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"configured": True, "robot_code": "new_key"}
    with SessionLocal() as db:
        channels = db.execute(select(NotificationChannel)).scalars().all()
        assert len(channels) == 1
        assert channels[0].target_hint.startswith("钉钉群 · new_key")

    fetched = client.get("/api/v1/me/dingtalk/robot")
    assert fetched.json() == {"configured": True, "robot_code": "new_key"}

    # 钉钉拒绝这组凭据时不能落库，也不能覆盖已保存的那组
    monkeypatch.setattr(push.httpx, "post", lambda url, **kwargs: _Response(400, {}))
    rejected = client.put(
        "/api/v1/me/dingtalk/robot",
        json={"robot_code": "bad_key", "robot_secret": "bad_secret"},
        headers=auth_headers(csrf),
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == push.DINGTALK_BAD_CREDENTIALS_TEXT
    with SessionLocal() as db:
        assert db.execute(select(DingTalkRobot)).scalar_one().robot_code == "new_key"

    removed = client.delete("/api/v1/me/dingtalk/robot", headers=auth_headers(csrf))
    assert removed.status_code == 204
    with SessionLocal() as db:
        assert db.execute(select(DingTalkRobot)).scalars().all() == []
        assert db.execute(select(DingTalkBotLink)).scalars().all() == []
        assert db.execute(select(NotificationChannel)).scalars().all() == []
    assert client.get("/api/v1/me/dingtalk/robot").json() == {
        "configured": False,
        "robot_code": "",
    }
