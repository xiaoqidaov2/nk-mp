"""Push targets, message aggregation and backoff parsing.

These tests never talk to a real notification platform: they pin the Apprise
URLs we hand over, the frozen message format and the error codes the API and
the worker rely on.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.models import ChangeEvent, Store, Style
from app.services import push


def _settings(**overrides) -> Settings:
    base = {"credential_key": "test-key-for-unit-tests"}
    base.update(overrides)
    return Settings(**base)


def _event(
    event_type: str,
    *,
    size: str = "42",
    style_code: str = "DN3707-202",
    store_code: str = "QA001",
    before=None,
    after=None,
) -> ChangeEvent:
    return ChangeEvent(
        event_type=event_type,
        size_label=size,
        store=Store(store_code=store_code),
        style=Style(style_code=style_code),
        before_total_price=before,
        after_total_price=after,
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
    assert excinfo.value.detail == "请填写Webhook access_token。"
    with pytest.raises(push.PushTargetError) as excinfo:
        push.build_target("mailto", {"smtp_host": "smtp.example.com"})
    assert excinfo.value.detail == "请填写邮箱账号。"


def test_target_hint_keeps_a_short_reminder_only() -> None:
    assert (
        push.target_hint("mailto", "mailtos://u:p@smtp.example.com:587?to=a%40y.com")
        == "a@y.com"
    )
    assert push.target_hint("custom", "json://127.0.0.1:19100/__push") == "••••push"


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
        "· DN3707-202 42 QA001 ¥699.00\n"
        "· DN3707-202 44 QA001 待补价\n"
        "售罄 1 条\n"
        "· DN3707-202 40 QA001\n"
        "变价 1 条\n"
        "· DN3707-202 42 QA001 ¥699.00 → ¥599.00\n"
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
