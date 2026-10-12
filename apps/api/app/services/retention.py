"""观测历史保留：每个 (owner, 门店, 货号, 尺码) 只保留最近 N 次。

监控间隔小（30 秒 ~ 10 分钟）、门店与货号多，`observation` 一天能涨几百万行
（2026-10-10 生产实测 752 万行 / 2.4 GB），而历史只用于回溯。

**为什么按 key 分片**：一条全局窗口删除要对整表重排（750 万行 → 每批几十秒，
一次清理要跑很久）；而索引 `ix_observation_owner_key_checked`
`(owner_user_id, store_id, style_id, size_label, checked_at DESC)` 让"单个 key 的第
N+1 条之后"变成索引上的顺序扫描。于是清理按 key 分片滚动推进：

* 每个作业收尾跑**一小片**（默认 200 个 key，约 2 万行），代价有界；
* 一次性大清理用 ``python -m app.cli prune-observations`` 一直推进到扫完一轮；
* 游标在进程内保存（重启后从头再来，无害）。

稳态：每小时新增 ~31 万行 ⇒ 目标水位 ~73k key × 30 ≈ 220 万行，滚动分片足够压住。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("nike_mp.retention")

#: 每片处理多少个 key（key 少 → 每片代价小；key 多 → 一轮更快）。
SLICE_KEYS = 200
#: CLI 一次性清理时的每片 key 数（大一些更快）。
CLI_SLICE_KEYS = 1000

_SLICE = text(
    """
    WITH keys AS (
        SELECT store_id, style_id, size_label
        FROM observation_latest
        WHERE (CAST(:owner AS uuid) IS NULL OR owner_user_id = CAST(:owner AS uuid))
          AND (CAST(:after_store AS uuid) IS NULL
               OR (store_id, style_id, size_label)
                  > (CAST(:after_store AS uuid), CAST(:after_style AS uuid), CAST(:after_size AS text)))
        ORDER BY store_id, style_id, size_label
        LIMIT :keys
    ), cutoffs AS (
        SELECT k.store_id, k.style_id, k.size_label,
               (SELECT o.checked_at FROM observation o
                 WHERE o.store_id = k.store_id
                   AND o.style_id = k.style_id
                   AND o.size_label = k.size_label
                 ORDER BY o.checked_at DESC, o.id DESC
                 OFFSET :keep - 1 LIMIT 1) AS cutoff
        FROM keys k
    )
    DELETE FROM observation o
    USING cutoffs c
    WHERE o.store_id = c.store_id
      AND o.style_id = c.style_id
      AND o.size_label = c.size_label
      AND c.cutoff IS NOT NULL
      AND o.checked_at < c.cutoff
    """
)


def prune_observation_slice(
    session: Session,
    *,
    keep: int,
    owner_user_id: UUID | None = None,
    keys: int = SLICE_KEYS,
    after: tuple | None = None,
) -> tuple[int, tuple | None]:
    """清理一小片 key 的历史，返回 ``(删除行数, 下一片游标)``。

    游标为 None 表示这一轮已经扫到底（调用方可选择从头再来）。
    """
    if keep <= 0:
        return 0, None
    after_store, after_style, after_size = after or (None, None, None)
    result = session.execute(
        _SLICE,
        {
            "owner": owner_user_id,
            "keep": keep,
            "keys": keys,
            "after_store": after_store,
            "after_style": after_style,
            "after_size": after_size,
        },
    )
    deleted = int(result.rowcount or 0)

    # 取这一片最后一个 key 作为下一片游标（同样从 observation_latest 取，代价恒定）。
    next_row = session.execute(
        text(
            """
            SELECT store_id, style_id, size_label
            FROM observation_latest
            WHERE (CAST(:owner AS uuid) IS NULL OR owner_user_id = CAST(:owner AS uuid))
              AND (CAST(:after_store AS uuid) IS NULL
                   OR (store_id, style_id, size_label)
                      > (CAST(:after_store AS uuid), CAST(:after_style AS uuid), CAST(:after_size AS text)))
            ORDER BY store_id, style_id, size_label
            OFFSET :offset LIMIT 1
            """
        ),
        {
            "owner": owner_user_id,
            "after_store": after_store,
            "after_style": after_style,
            "after_size": after_size,
            "offset": keys - 1,
        },
    ).first()
    next_cursor = (next_row[0], next_row[1], next_row[2]) if next_row is not None else None
    if deleted:
        logger.info(
            "pruned %s observation row(s) (keep=%s keys=%s owner=%s)",
            deleted,
            keep,
            keys,
            owner_user_id or "all",
        )
    return deleted, next_cursor


#: 进程内游标：作业收尾的滚动清理用它接着上次的位置继续。
_cursor: tuple | None = None


#: 单片的时间/锁上限：历史表很大时单片可能很慢，宁可跳过也不能拖慢其它工作。
SLICE_TIMEOUT_MS = 3000
LOCK_TIMEOUT_MS = 500


def prune_idle_slice(*, keep: int, owner_user_id: UUID | None = None) -> int:
    """worker 空闲时推进一小片——**用独立会话**，绝不参与作业事务。

    为什么必须独立会话：早期版本在作业收尾（``_finalize``）里调它，切片超时后
    ``session.rollback()`` 把整个作业事务回滚掉了（状态、门店结果、变化事件、
    待投递推送全丢），作业停在 RUNNING、调度器又不再派新作业——生产实测直接停摆
    近 24 小时。现在清理只在自己的连接里跑，失败/超时只影响这一片。
    """
    global _cursor
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError

    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            session.execute(text(f"SET LOCAL statement_timeout = '{SLICE_TIMEOUT_MS}ms'"))
            session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
            deleted, next_cursor = prune_observation_slice(
                session, keep=keep, owner_user_id=owner_user_id, after=_cursor
            )
            session.commit()
    except (DBAPIError, SQLAlchemyError) as exc:
        logger.debug("retention slice skipped: %s", exc.__class__.__name__)
        return 0
    _cursor = next_cursor
    return deleted


def prune_all(
    session: Session,
    *,
    keep: int,
    owner_user_id: UUID | None = None,
    keys: int = CLI_SLICE_KEYS,
) -> int:
    """一直推进直到扫完一轮（CLI 用）。"""
    cursor: tuple | None = None
    total = 0
    while True:
        deleted, cursor = prune_observation_slice(
            session, keep=keep, owner_user_id=owner_user_id, keys=keys, after=cursor
        )
        session.commit()
        total += deleted
        if cursor is None:
            return total
