"""Push targets, message aggregation and backoff parsing.

These tests never talk to a real notification platform: they pin the Apprise
URLs we hand over, the frozen message format and the error codes the API and
the worker rely on.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.models import ChangeEvent, MonitorTask, Store, Style
from app.services import push


def _settings(**overrides) -> Settings:
    base = {"credential_key": "test-key-for-unit-tests"}
    base.update(overrides)
    return Settings(**base)


_AFTER_STATUS = {
    "RESTOCK": "IN_STOCK",
    "NEW_ARRIVAL_AT_STORE": "IN_STOCK",
    "PRICE_CHANGE": "IN_STOCK",
    "SOLD_OUT": "OUT_OF_STOCK",
}


def _event(
    event_type: str,
    *,
    size: str = "42",
    style_code: str = "DN3707-202",
    store_code: str = "QA001",
    display_name: str = "",
    before=None,
    after=None,
) -> ChangeEvent:
    return ChangeEvent(
        event_type=event_type,
        size_label=size,
        store=Store(store_code=store_code, display_name=display_name),
        style=Style(style_code=style_code),
        before_total_price=before,
        after_total_price=after,
        after_status=_AFTER_STATUS.get(event_type),
    )


def test_build_target_covers_every_platform() -> None:
    assert (
        push.build_target("dingtalk", {"access_token": "abc"})
        == "dingtalk://abc/?format=markdown"
    )
    assert (
        push.build_target("dingtalk", {"access_token": "abc", "secret": "sec"})
        == "dingtalk://sec@abc/?format=markdown"
    )
    assert push.build_target("feishu", {"token": "abc"}) == "feishu://abc"
    assert (
        push.build_target(
            "wecombot",
            {"key_or_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K1-2_x"},
        )
        == "wecombot://K1-2_x"
    )
    assert push.build_target("wecombot", {"key_or_url": "K1-2_x"}) == "wecombot://K1-2_x"
    assert push.build_target("schan", {"sendkey": "SCT123"}) == "schan://SCT123/"
    assert push.build_target("bark", {"device_key": "dk"}) == "barks://api.day.app/dk"
    assert (
        push.build_target("bark", {"device_key": "dk", "server": "http://1.2.3.4:8080/x"})
        == "bark://1.2.3.4:8080/dk"
    )
    assert (
        push.build_target(
            "bark", {"device_key": "dk", "server": "https://bark.example.com"}
        )
        == "barks://bark.example.com/dk"
    )
    assert (
        push.build_target(
            "mailto",
            {
                "smtp_host": "smtp.example.com",
                "username": "u@x.com",
                "password": "p@ss",
                "from": "u@x.com",
                "to": "a@y.com",
            },
        )
        == "mailtos://u%40x.com:p%40ss@smtp.example.com:587?from=u%40x.com&to=a%40y.com"
    )
    assert (
        push.build_target("custom", {"url": "json://127.0.0.1:19100/__push"})
        == "json://127.0.0.1:19100/__push"
    )


def test_build_target_names_the_missing_field() -> None:
    with pytest.raises(push.PushTargetError) as excinfo:
        push.build_target("dingtalk", {})
    assert excinfo.value.code == push.PUSH_TARGET_INVALID
    assert excinfo.value.detail == "请填写Webhook 地址或 access_token。"
    with pytest.raises(push.PushTargetError) as excinfo:
        push.build_target("mailto", {"smtp_host": "smtp.example.com"})
    assert excinfo.value.detail == "请填写邮箱账号。"


def test_build_target_accepts_a_pasted_dingtalk_webhook_address() -> None:
    """客户把整段 Webhook 地址粘进来时自动取 access_token。"""
    assert (
        push.build_target(
            "dingtalk",
            {"access_token": "https://oapi.dingtalk.com/robot/send?access_token=abc123XYZ"},
        )
        == "dingtalk://abc123XYZ/?format=markdown"
    )
    assert (
        push.build_target(
            "dingtalk",
            {
                "access_token": "  https://oapi.dingtalk.com/robot/send?access_token=abc123XYZ  ",
                "secret": "sec",
            },
        )
        == "dingtalk://sec@abc123XYZ/?format=markdown"
    )


def test_build_target_rejects_a_dingtalk_address_without_a_token() -> None:
    with pytest.raises(push.PushTargetError) as excinfo:
        push.build_target("dingtalk", {"access_token": "https://oapi.dingtalk.com/robot/send"})
    assert excinfo.value.code == push.PUSH_TARGET_INVALID
    assert "access_token" in excinfo.value.detail


def test_build_target_accepts_a_pasted_feishu_webhook_address() -> None:
    assert (
        push.build_target(
            "feishu",
            {"token": "https://open.feishu.cn/open-apis/bot/v2/hook/feishu-token-1"},
        )
        == "feishu://feishu-token-1"
    )
    with pytest.raises(push.PushTargetError) as excinfo:
        push.build_target("feishu", {"token": "https://open.feishu.cn/open-apis/bot/v2/hook/"})
    assert excinfo.value.code == push.PUSH_TARGET_INVALID


def test_target_hint_keeps_a_short_reminder_only() -> None:
    assert (
        push.target_hint("mailto", "mailtos://u:p@smtp.example.com:587?to=a%40y.com")
        == "a@y.com"
    )
    assert push.target_hint("custom", "json://127.0.0.1:19100/__push") == "••••push"
    # 钉钉渠道提示用 token 结尾，而不是 Apprise URL 的固定后缀。
    assert push.target_hint("dingtalk", "dingtalk://abcdef123456/?format=markdown") == "••••3456"
    assert (
        push.target_hint("dingtalk", "dingtalk://sec@abcdef123456/?format=markdown")
        == "••••3456"
    )


def test_validate_target_rejects_unknown_schemes() -> None:
    with pytest.raises(push.PushTargetError) as excinfo:
        push.validate_target("ssh://host", _settings())
    assert excinfo.value.code == push.PUSH_TARGET_INVALID
    assert excinfo.value.detail.startswith("不支持的推送地址；支持 dingtalk://")


def test_validate_target_blocks_private_addresses_unless_allowed() -> None:
    with pytest.raises(push.PushTargetError) as excinfo:
        push.validate_target("json://127.0.0.1:19100/__push", _settings())
    assert excinfo.value.code == push.PUSH_TARGET_PRIVATE
    assert excinfo.value.detail == "不允许推送到内网地址；如确需，请联系管理员开启。"
    push.validate_target(
        "json://127.0.0.1:19100/__push", _settings(push_allow_private_targets=True)
    )


def test_validate_target_checks_the_apprise_contract() -> None:
    with pytest.raises(push.PushTargetError) as excinfo:
        push.validate_target("dingtalk://", _settings(push_allow_private_targets=True))
    assert excinfo.value.code == push.PUSH_TARGET_INVALID
    assert excinfo.value.detail == "渠道地址无法识别，请检查填写内容。"


def test_build_message_aggregates_categories_in_order() -> None:
    events = [
        _event("RESTOCK", size="42", after=699),
        _event("RESTOCK", size="44", after=None),
        _event("SOLD_OUT", size="40"),
        _event("PRICE_CHANGE", size="42", before=699, after=599),
    ]
    message = push.build_message("QA 冒烟任务", events, base_url="http://127.0.0.1:15173")
    assert message is not None
    title, body = message
    assert title == "【库存提醒】QA 冒烟任务"
    assert body == (
        "补货 2 条\n"
        "· DN3707-202 42 QA001 有货 ¥699.00\n"
        "· DN3707-202 44 QA001 有货 待补价\n"
        "售罄 1 条\n"
        "· DN3707-202 40 QA001 无货\n"
        "变价 1 条\n"
        "· DN3707-202 42 QA001 有货 ¥699.00 → ¥599.00\n"
        "查看：http://127.0.0.1:15173/changes"
    )


def test_build_message_needs_events_and_truncates_the_title() -> None:
    assert push.build_message("任务", [], base_url="http://example.test") is None
    title, _body = push.build_message("x" * 60, [_event("SOLD_OUT")], base_url="http://x")
    assert title == f"【库存提醒】{'x' * 40}"


def test_build_message_shows_five_lines_per_category_and_counts_the_rest() -> None:
    events = [_event("RESTOCK", size=str(40 + index), after=699) for index in range(7)]
    message = push.build_message("任务", events, base_url="http://127.0.0.1:15173")
    assert message is not None
    _title, body = message
    lines = body.splitlines()
    assert sum(1 for line in lines if line.startswith("· ")) == 5
    assert lines[-1] == "另有 2 条，查看：http://127.0.0.1:15173/changes"


def test_build_message_truncates_long_bodies_but_keeps_the_link() -> None:
    events = [
        _event("RESTOCK", size="42", style_code="S" * 300, after=699) for _ in range(5)
    ]
    message = push.build_message("任务", events, base_url="http://127.0.0.1:15173")
    assert message is not None
    _title, body = message
    assert len(body) <= push.BODY_LIMIT
    assert body.splitlines()[-1].endswith("查看：http://127.0.0.1:15173/changes")


def test_backoff_seconds_parsing_falls_back_to_one_minute() -> None:
    assert push._backoff_seconds(_settings(push_backoff_seconds="60,300,900")) == [
        60,
        300,
        900,
    ]
    assert push._backoff_seconds(_settings(push_backoff_seconds="bad")) == [60]
    assert push._backoff_seconds(_settings(push_backoff_seconds="")) == [60]


def test_build_target_supports_the_enterprise_robot() -> None:
    target = push.build_target(
        "dingtalk_app",
        {
            "robot_code": "dingw9i1o98w3eyn8qwu",
            "robot_secret": "s3cret_with_underscore",
            "conversation_id": "cid6KeBBLoveMJOGXoYKF5x7EeiodoA==",
        },
    )
    assert target == (
        "dingtalkapp://dingw9i1o98w3eyn8qwu"
        "?secret=s3cret_with_underscore"
        "&conversation=cid6KeBBLoveMJOGXoYKF5x7EeiodoA%3D%3D"
    )
    assert push.target_hint("dingtalk_app", target) == "钉钉群 · dingw9i1o98w3eyn8qwu → ••••oA=="


def test_bot_target_supports_group_and_single_chat() -> None:
    group = push.build_bot_target("ding", "s", conversation_id="cid")
    assert group == "dingtalkapp://ding?secret=s&conversation=cid"
    assert push.target_hint("dingtalk_app", group) == "钉钉群 · ding → ••••cid"
    dm = push.build_bot_target("ding", "s", user_id="452523285939877041")
    assert dm == "dingtalkapp://ding?secret=s&user=452523285939877041&dm=1"
    assert push.target_hint("dingtalk_app", dm) == "钉钉单聊 · ding → ••••7041"


def test_bot_target_needs_credentials_and_a_destination() -> None:
    push.validate_target("dingtalkapp://ding?secret=s&conversation=cid", _settings())
    push.validate_target("dingtalkapp://ding?secret=s&user=u1&dm=1", _settings())
    with pytest.raises(push.PushTargetError) as excinfo:
        push.validate_target("dingtalkapp://ding?secret=s", _settings())
    assert excinfo.value.code == push.PUSH_TARGET_INVALID
    with pytest.raises(push.PushTargetError) as excinfo:
        push.build_target("dingtalk_app", {"robot_code": "ding"})
    assert excinfo.value.detail == "请填写AppSecret。"


def test_fetch_dingtalk_token_maps_bad_credentials_and_network_errors(monkeypatch) -> None:
    class _Response:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}

        def json(self) -> dict:
            return self._payload

    monkeypatch.setattr(
        push.httpx, "post", lambda url, **kwargs: _Response(200, {"accessToken": "tok"})
    )
    assert push.fetch_dingtalk_token("k", "s", _settings()) == "tok"

    monkeypatch.setattr(push.httpx, "post", lambda url, **kwargs: _Response(400, {}))
    with pytest.raises(push.PushTargetError) as excinfo:
        push.fetch_dingtalk_token("k", "s", _settings())
    assert excinfo.value.detail == push.DINGTALK_BAD_CREDENTIALS_TEXT

    def boom(url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(push.httpx, "post", boom)
    with pytest.raises(push.PushTargetError) as excinfo:
        push.fetch_dingtalk_token("k", "s", _settings())
    assert excinfo.value.detail == push.DINGTALK_UNREACHABLE_TEXT


def test_enterprise_robot_send_mints_a_token_and_posts_the_group_message(monkeypatch) -> None:
    calls: list[dict] = []

    class _Response:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("http error")

        def json(self) -> dict:
            return self._payload

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url == push.DINGTALK_TOKEN_URL:
            return _Response(200, {"accessToken": "tok-123"})
        return _Response(200, {})

    monkeypatch.setattr(push.httpx, "post", fake_post)
    target = push.build_target(
        "dingtalk_app",
        {"robot_code": "ding", "robot_secret": "s", "conversation_id": "cid"},
    )
    ok, code = push.send_now(target, "标题", "正文", _settings(push_timeout_seconds=3))
    assert (ok, code) == (True, None)
    assert calls[0]["json"] == {"appKey": "ding", "appSecret": "s"}
    assert calls[1]["headers"] == {"x-acs-dingtalk-access-token": "tok-123"}
    assert calls[1]["json"] == {
        "robotCode": "ding",
        "msgKey": "sampleMarkdown",
        "msgParam": '{"title": "标题", "text": "正文"}',
        "openConversationId": "cid",
    }

    def failing_post(url, **kwargs):
        if url == push.DINGTALK_TOKEN_URL:
            return _Response(200, {"accessToken": "tok-123"})
        return _Response(400, {})

    monkeypatch.setattr(push.httpx, "post", failing_post)
    assert push.send_now(target, "标题", "正文", _settings(push_timeout_seconds=3)) == (
        False,
        push.PUSH_DELIVERY_FAILED,
    )


def test_first_time_arrival_rides_the_restock_switch(settings) -> None:
    """未铺货→有货（新上架）必须被推送，否则这类变化会被静默丢掉。"""
    on = MonitorTask(
        name="到货任务", store_codes=[], style_codes=[], all_stores=True,
        notify_on_restock=True, notify_on_sold_out=False, notify_on_price_change=False,
    )
    off = MonitorTask(
        name="关闭任务", store_codes=[], style_codes=[], all_stores=True,
        notify_on_restock=False, notify_on_sold_out=False, notify_on_price_change=False,
    )
    assert push._notified_types(on, settings) == ["RESTOCK", "NEW_ARRIVAL_AT_STORE"]
    assert push._notified_types(off, settings) == []


def test_message_keeps_arrival_rows_with_price() -> None:
    arrival = _event("NEW_ARRIVAL_AT_STORE", before=None, after=899.0)
    message = push.build_message("到货任务", [arrival], base_url="https://example.test")
    assert message is not None
    _title, body = message
    assert "新上架 1 条" in body
    assert "· DN3707-202 42 QA001 有货 ¥899.00" in body


def test_message_orders_arrival_before_sold_out() -> None:
    events = [
        _event("SOLD_OUT", after=None),
        _event("NEW_ARRIVAL_AT_STORE", before=None, after=899.0),
        _event("RESTOCK", after=899.0),
    ]
    _title, body = push.build_message("排序任务", events, base_url="https://example.test") or ("", "")
    assert body.index("补货") < body.index("新上架") < body.index("售罄")


def test_task_less_jobs_announce_the_default_categories(settings) -> None:
    """手动检查 / 任务已删的补跑没有任务开关可依：按平台默认分类通知。

    2026-10-15 客户要求「售空不要通知」后，售罄不再默认包含（其余三类照发）。
    """
    assert push._notified_types(None, settings) == [
        "RESTOCK",
        "NEW_ARRIVAL_AT_STORE",
        "PRICE_CHANGE",
    ]


def test_detail_line_carries_style_size_store_and_current_state() -> None:
    """客户按这行定位：货号 + 尺码 + 门店（编码·名称）+ 当前状态 + 价格。"""
    line = push._detail_line(
        _event("RESTOCK", size="42", store_code="3402", display_name="NIKE北京朝阳园", after=699)
    )
    assert line == "· DN3707-202 42 3402·NIKE北京朝阳园 有货 ¥699.00"


def test_detail_line_falls_back_to_the_store_code_and_keeps_long_names_bounded() -> None:
    assert push._detail_line(_event("SOLD_OUT", store_code="3402")) == "· DN3707-202 42 3402 无货"
    line = push._detail_line(_event("RESTOCK", store_code="3402", display_name="店" * 80, after=699))
    assert "店" * push.STORE_LABEL_LIMIT in line
    assert "店" * (push.STORE_LABEL_LIMIT + 1) not in line


def test_task_less_rounds_skip_sold_out_by_default(settings) -> None:
    """无任务作业（手动「立即检查」）默认不提醒售罄（客户要求「售空不要通知」）。

    补货/到店上新/变价照发；``push_sold_out_default=true`` 可改回全发。
    """
    assert push._notified_types(None, settings) == [
        "RESTOCK",
        "NEW_ARRIVAL_AT_STORE",
        "PRICE_CHANGE",
    ]
    with_sold_out = Settings(credential_key="k", push_sold_out_default=True)
    assert push._notified_types(None, with_sold_out) == [
        "RESTOCK",
        "NEW_ARRIVAL_AT_STORE",
        "SOLD_OUT",
        "PRICE_CHANGE",
    ]


def test_task_message_carries_a_link_to_the_task_page() -> None:
    """机器人消息除了"查看变化流水"，还要能一键跳到该任务的监控详情。

    客户反馈：点"查看"只能到流水，找不到是哪个任务在监控 → 任务作业追加一行
    ``任务：{base}/tasks/{id}``；无任务作业（手动检查）没有这一行。
    """
    events = [_event("NEW_ARRIVAL_AT_STORE", size="42", store_code="3410", after=466.95)]
    _title, body = push.build_message(
        "新建监控任务 2",
        events,
        base_url="https://example.test",
        task_id="11111111-2222-3333-4444-555555555555",
    ) or ("", "")
    lines = body.splitlines()
    assert lines[-2] == "查看：https://example.test/changes"
    assert lines[-1] == "任务：https://example.test/tasks/11111111-2222-3333-4444-555555555555"

    # 无任务作业：只有流水链接，不出现 任务： 行。
    _title, plain = push.build_message("门店检查", events, base_url="https://example.test") or ("", "")
    assert "任务：" not in plain
    assert plain.splitlines()[-1] == "查看：https://example.test/changes"


def test_dingtalk_markdown_body_doubles_single_newlines() -> None:
    """钉钉 markdown 会吃掉单换行：群机器人与企业机器人都必须补成两个换行。

    客户实测：库里正文是 `补货 1 条\n· 货号 …\n查看：…`，钉钉群里收到的是一整行。
    """
    body = "补货 1 条\n· DV5477-008 37.5 3999·门店 有货 ¥219.45\n查看：https://x/changes"
    assert push._markdown_body(body) == body.replace("\n", "\n\n")

    # 已经是空行（\n\n）的不再重复补；CRLF 先归一。
    assert push._markdown_body("a\n\nb\r\nc") == "a\n\nb\n\nc"
    # text 类渠道不变：custom/bark 仍按单换行发送。
    assert push._markdown_body("单行") == "单行"


def test_dingtalk_webhook_and_app_send_markdown_with_blank_lines(monkeypatch) -> None:
    """两条钉钉发送路径都使用补过空行的正文（群机器人走 apprise，企业机器人走 msgParam）。"""
    sent: list[dict] = []

    class _FakeApprise:
        def add(self, _target):  # noqa: ANN001, ANN202
            return True

        def notify(self, **kwargs):  # noqa: ANN003, ANN202
            sent.append(kwargs)
            return True

    monkeypatch.setattr(push.apprise, "Apprise", _FakeApprise)
    webhook = push.build_target("dingtalk", {"access_token": "tok"})
    assert push.send_now(webhook, "标题", "第一行\n第二行", _settings(push_timeout_seconds=3)) == (
        True,
        None,
    )
    assert sent[-1]["body"] == "第一行\n\n第二行"

    calls: list[dict] = []

    class _Resp:
        def __init__(self, status: int, payload: dict) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append({"url": url, **kwargs})
        if url == push.DINGTALK_TOKEN_URL:
            return _Resp(200, {"accessToken": "tok-123"})
        return _Resp(200, {})

    monkeypatch.setattr(push.httpx, "post", fake_post)
    app_target = push.build_target(
        "dingtalk_app",
        {"robot_code": "ding", "robot_secret": "s", "conversation_id": "cid"},
    )
    assert push.send_now(app_target, "标题", "第一行\n第二行", _settings()) == (True, None)
    assert '"text": "第一行\\n\\n第二行"' in calls[1]["json"]["msgParam"]
