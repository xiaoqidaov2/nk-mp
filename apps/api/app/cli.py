"""Operator CLI: bootstrap data and import credentials without the UI."""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Role, Style, Tenant, User
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
        CredentialService(settings, db).store_tokens(
            access_token, refresh_token, client_id=settings.oauth_client_id
        )
        db.commit()
    print("credential stored (encrypted)")
    return 0


def cmd_sync_stores(_args: argparse.Namespace) -> int:
    settings = get_settings()
    with _session() as db:
        service = CredentialService(settings, db)
        token = service.access_token()
        with NikeClient(settings, token, token_provider=service.token_provider()) as client:
            records = client.list_stores()
        touched = sync_stores(db, records, "cli")
        db.commit()
    print(f"synced {touched} stores")
    return 0


def cmd_add_style(args: argparse.Namespace) -> int:
    with _session() as db:
        for code in args.codes:
            normalized = code.strip().upper()
            if not normalized:
                continue
            existing = db.execute(
                select(Style).where(Style.style_code == normalized)
            ).scalar_one_or_none()
            if existing is None:
                db.add(Style(style_code=normalized, title=normalized))
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
    credential.add_argument("--access-token-file", default="jwt_latest.txt")
    credential.add_argument("--refresh-token-file", default="mp_refresh.txt")
    credential.set_defaults(func=cmd_import_credential)

    stores = sub.add_parser("sync-stores", help="从 Nike 接口同步门店清单")
    stores.set_defaults(func=cmd_sync_stores)

    styles = sub.add_parser("add-style", help="登记待监控货号")
    styles.add_argument("codes", nargs="+")
    styles.set_defaults(func=cmd_add_style)


    monitor_user = sub.add_parser("add-user", help="为租户新增用户")
    monitor_user.add_argument("--slug", required=True)
    monitor_user.add_argument("--email", required=True)
    monitor_user.add_argument("--password", required=True)
    monitor_user.add_argument("--role", default=Role.TENANT_VIEWER.value)
    monitor_user.add_argument("--display-name", default="")
    monitor_user.set_defaults(func=cmd_add_monitor_user)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
