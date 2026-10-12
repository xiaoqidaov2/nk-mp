"""Operator CLI: bootstrap data and import credentials without the UI."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Credential, CredentialStatus, Role, Style, Tenant, User
from app.nike.client import NikeClient
from app.security import hash_password
from app.services.credentials import CredentialService
from app.services.stores import sync_stores


def _session():
    return SessionLocal()


def cmd_init_tenant(args: argparse.Namespace) -> int:
    with _session() as db:
        tenant = db.execute(
            select(Tenant).where(Tenant.slug == args.slug)
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                slug=args.slug,
                name=args.name,
                is_platform=args.platform,
                daily_review_budget=args.budget,
            )
            db.add(tenant)
            db.flush()
        user = db.execute(select(User).where(User.email == args.email.lower())).scalar_one_or_none()
        if user is None:
            user = User(
                tenant_id=tenant.id,
                email=args.email.lower(),
                display_name=args.display_name or args.email,
                password_hash=hash_password(args.password),
                role=Role.OPS_ADMIN.value if args.platform else Role.TENANT_ADMIN.value,
            )
            db.add(user)
        else:
            user.password_hash = hash_password(args.password)
            user.tenant_id = tenant.id
            user.role = Role.OPS_ADMIN.value if args.platform else Role.TENANT_ADMIN.value
        db.commit()
    print(f"tenant {args.slug} ready; user {args.email}")
    return 0


def cmd_import_credential(args: argparse.Namespace) -> int:
    settings = get_settings()
    access_token = _read(args.access_token_file, required=False)
    refresh_token = _read(args.refresh_token_file, required=False)
    if not access_token and not refresh_token:
        print(
            "no access or refresh token found "
            f"({args.access_token_file}, {args.refresh_token_file})",
            file=sys.stderr,
        )
        return 1
    with _session() as db:
        user = _require_user(db, args.user_id)
        binding = CredentialService(settings, user.id).store_tokens(
            access_token, refresh_token, client_id=settings.oauth_client_id
        )
        db.commit()
    print(f"credential stored for user {user.email} (generation {binding.generation})")
    return 0


def cmd_assign_credential(args: argparse.Namespace) -> int:
    """Attribute one legacy owner-less credential to an unbound user.

    One-shot operator tool for migration: no copy, no refresh, no secret is
    printed. Conflicts are refused instead of overwritten.
    """
    with _session() as db:
        user = _require_user(db, args.user_id)
        record = db.execute(
            select(Credential).where(Credential.id == args.credential_id)
        ).scalar_one_or_none()
        if record is None:
            print("credential not found", file=sys.stderr)
            return 1
        if record.owner_user_id is not None:
            print("credential already has an owner; refusing", file=sys.stderr)
            return 1
        existing = db.execute(
            select(Credential).where(Credential.owner_user_id == user.id)
        ).scalar_one_or_none()
        if existing is not None:
            print("user already has a credential binding; refusing", file=sys.stderr)
            return 1
        if record.phone_hash is not None:
            conflict = db.execute(
                select(Credential).where(
                    Credential.phone_hash == record.phone_hash,
                    Credential.id != record.id,
                    Credential.status != CredentialStatus.REVOKED.value,
                )
            ).scalar_one_or_none()
            if conflict is not None:
                print("phone number already bound to another credential; refusing", file=sys.stderr)
                return 1
        record.owner_user_id = user.id
        record.status = CredentialStatus.ACTIVE.value
        record.last_error_code = None
        record.updated_at = datetime.now(UTC)
        db.commit()
    print(f"credential assigned to user {user.email}")
    return 0


def cmd_sync_stores(args: argparse.Namespace) -> int:
    settings = get_settings()
    with _session() as db:
        user = _require_user(db, args.user_id)
        service = CredentialService(settings, user.id)
        binding = service.binding()
        token = service.access_token(binding=binding)
        with NikeClient(settings, token, token_provider=service.token_provider(binding)) as client:
            records = client.list_stores()
        touched = sync_stores(db, records, "cli", user.id)
        db.commit()
    print(f"synced {touched} stores")
    return 0


def _require_user(db, raw_user_id: str) -> User:
    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (TypeError, ValueError):
        raise SystemExit(f"invalid user id: {raw_user_id}") from None
    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise SystemExit(f"active user not found: {raw_user_id}")
    return user


def cmd_add_style(args: argparse.Namespace) -> int:
    with _session() as db:
        user = _require_user(db, args.user_id)
        for code in args.codes:
            normalized = code.strip().upper()
            if not normalized:
                continue
            existing = db.execute(
                select(Style).where(
                    Style.owner_user_id == user.id, Style.style_code == normalized
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(Style(style_code=normalized, title=normalized, owner_user_id=user.id))
        db.commit()
    print(f"registered {len(args.codes)} style code(s)")
    return 0


def cmd_add_monitor_user(args: argparse.Namespace) -> int:
    with _session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.slug)).scalar_one_or_none()
        if tenant is None:
            print(f"tenant {args.slug} not found", file=sys.stderr)
            return 1
        user = User(
            tenant_id=tenant.id,
            email=args.email.lower(),
            display_name=args.display_name or args.email,
            password_hash=hash_password(args.password),
            role=args.role,
        )
        db.add(user)
        db.commit()
    print(f"user {args.email} created in {args.slug} with role {args.role}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from app.bootstrap import create_schema, ensure_platform_tenant

    create_schema()
    ensure_platform_tenant(args.email, args.password, args.name)
    if args.demo:
        from app.bootstrap import seed_isolated_demo

        seed_isolated_demo()
        print("demo tenant seeded (login demo@example.com / demo-password)")
    print("schema ready; platform tenant available")
    return 0


def _read(path: str, required: bool = True) -> str:
    if not path or not os.path.exists(path):
        if required:
            raise SystemExit(f"required file not found: {path}")
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nike-mp", description="Nike 门店监控平台运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    tenant = sub.add_parser("init-tenant", help="创建或更新租户与管理员账号")
    tenant.add_argument("--slug", required=True)
    tenant.add_argument("--name", required=True)
    tenant.add_argument("--email", required=True)
    tenant.add_argument("--password", required=True)
    tenant.add_argument("--display-name", default="")
    tenant.add_argument("--platform", action="store_true", help="标记为服务方平台租户")
    tenant.add_argument("--budget", type=int, default=5000)
    tenant.set_defaults(func=cmd_init_tenant)

    bootstrap = sub.add_parser("bootstrap", help="建表并创建首个平台管理员")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--password", required=True)
    bootstrap.add_argument("--name", default="服务方")
    bootstrap.add_argument("--demo", action="store_true", help="额外写入离线演示数据")
    bootstrap.set_defaults(func=cmd_bootstrap)

    credential = sub.add_parser("import-credential", help="导入并加密存储访问凭证")
    credential.add_argument("--user-id", required=True, help="凭证归属的平台用户 UUID")
    credential.add_argument("--access-token-file", default="jwt_latest.txt")
    credential.add_argument("--refresh-token-file", default="mp_refresh.txt")
    credential.set_defaults(func=cmd_import_credential)

    assign = sub.add_parser("assign-credential", help="把无归属的历史凭证一次性指派给某用户")
    assign.add_argument("--credential-id", required=True, help="credential 行 UUID")
    assign.add_argument("--user-id", required=True, help="目标平台用户 UUID")
    assign.set_defaults(func=cmd_assign_credential)

    stores = sub.add_parser("sync-stores", help="从 Nike 接口同步门店清单")
    stores.add_argument("--user-id", required=True, help="使用该用户的绑定凭证")
    stores.set_defaults(func=cmd_sync_stores)

    styles = sub.add_parser("add-style", help="为某个用户登记待监控货号")
    styles.add_argument("--user-id", required=True, help="归属用户 id（目录按连接归属）")
    styles.add_argument("codes", nargs="+")
    styles.set_defaults(func=cmd_add_style)


    monitor_user = sub.add_parser("add-user", help="为租户新增用户")
    monitor_user.add_argument("--slug", required=True)
    monitor_user.add_argument("--email", required=True)
    monitor_user.add_argument("--password", required=True)
    monitor_user.add_argument("--role", default=Role.TENANT_VIEWER.value)
    monitor_user.add_argument("--display-name", default="")
    monitor_user.set_defaults(func=cmd_add_monitor_user)

    prune = sub.add_parser(
        "prune-observations",
        help="按保留策略清理观测历史（每个门店/货号/尺码只留最近 N 条）",
    )
    prune.add_argument("--keep", type=int, default=30, help="每个 key 保留条数（默认 30）")
    prune.add_argument("--batch", type=int, default=1000, help="每片处理多少个 key")
    prune.add_argument("--owner", default="", help="只清某个 owner 的 user id（默认全表）")
    prune.set_defaults(func=cmd_prune_observations)
    return parser


def cmd_prune_observations(args: argparse.Namespace) -> int:
    from app.services.retention import prune_all

    owner = None
    if args.owner:
        import uuid as _uuid

        owner = _uuid.UUID(args.owner)
    with _session() as db:
        removed = prune_all(db, keep=args.keep, owner_user_id=owner, keys=args.batch)
    print(f"pruned {removed} observation row(s) (keep={args.keep})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
