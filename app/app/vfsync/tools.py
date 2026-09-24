"""Operator CLI for VF-Sync (run inside the container).

    python -m app.vfsync.tools generate-key
    python -m app.vfsync.tools set-credentials --slug ohlstadt --username tech \
        --appkey <key> [--password-md5 <md5> | --password <plain>] [--cid 123]
    python -m app.vfsync.tools enable  --slug ohlstadt [--live]   # default dry_run
    python -m app.vfsync.tools disable --slug ohlstadt
    python -m app.vfsync.tools show    --slug ohlstadt

`enable` is deliberately not exposed through the web API (Konzept 8.6: no
self-activation); this tool is the operator's path. Secrets are read
from arguments or - preferred - from the environment variables
VF_PASSWORD_MD5 / VF_PASSWORD / VF_APPKEY so they do not end up in the
shell history.
"""

import argparse
import asyncio
import hashlib
import os
import sys

from app.db.connection import close_db, get_db, init_db
from app.vfsync import crypto


async def _airfield_id(slug: str):
    row = await get_db().fetchrow("SELECT id FROM airfields WHERE slug = $1", slug)
    if not row:
        sys.exit(f"airfield '{slug}' not found")
    return row["id"]


async def cmd_set_credentials(args) -> None:
    password_md5 = args.password_md5 or os.environ.get("VF_PASSWORD_MD5")
    plain = args.password or os.environ.get("VF_PASSWORD")
    if not password_md5 and plain:
        # VF wants md5(password); store only what we send (Kap. 8.2)
        password_md5 = hashlib.md5(plain.encode("utf-8")).hexdigest()
    appkey = args.appkey or os.environ.get("VF_APPKEY")
    if not (args.username and password_md5 and appkey):
        sys.exit("username, password(-md5) and appkey are required")

    airfield_id = await _airfield_id(args.slug)
    await get_db().execute(
        """
        INSERT INTO vf_sync_config (airfield_id, vf_username, vf_password_enc, vf_appkey_enc,
                                    vf_cid, vf_base_url, updated_at)
        VALUES ($1, $2, $3, $4, $5, COALESCE($6, 'https://www.vereinsflieger.de'), NOW())
        ON CONFLICT (airfield_id) DO UPDATE SET
            vf_username = EXCLUDED.vf_username,
            vf_password_enc = EXCLUDED.vf_password_enc,
            vf_appkey_enc = EXCLUDED.vf_appkey_enc,
            vf_cid = COALESCE(EXCLUDED.vf_cid, vf_sync_config.vf_cid),
            vf_base_url = COALESCE($6, vf_sync_config.vf_base_url),
            updated_at = NOW()
        """,
        airfield_id, args.username, crypto.encrypt(password_md5), crypto.encrypt(appkey),
        args.cid, args.base_url,
    )
    print(f"credentials stored for {args.slug} (encrypted)")


async def cmd_enable(args) -> None:
    airfield_id = await _airfield_id(args.slug)
    dry_run = not args.live
    result = await get_db().execute(
        """
        UPDATE vf_sync_config SET enabled = TRUE, dry_run = $2, updated_at = NOW()
        WHERE airfield_id = $1
        """,
        airfield_id, dry_run,
    )
    if result.endswith("0"):
        sys.exit("no vf_sync_config row - run set-credentials first")
    print(f"{args.slug}: enabled, dry_run={dry_run}")


async def cmd_disable(args) -> None:
    airfield_id = await _airfield_id(args.slug)
    await get_db().execute(
        "UPDATE vf_sync_config SET enabled = FALSE, updated_at = NOW() WHERE airfield_id = $1",
        airfield_id,
    )
    print(f"{args.slug}: disabled")


async def cmd_show(args) -> None:
    airfield_id = await _airfield_id(args.slug)
    row = await get_db().fetchrow(
        """
        SELECT enabled, dry_run, vf_base_url, vf_cid, vf_username,
               vf_password_enc IS NOT NULL AS has_password,
               vf_appkey_enc IS NOT NULL AS has_appkey,
               flags, daily_budget, updated_at
        FROM vf_sync_config WHERE airfield_id = $1
        """,
        airfield_id,
    )
    if not row:
        print(f"{args.slug}: not configured")
        return
    for k, v in dict(row).items():
        print(f"{k:14} {v}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.vfsync.tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("generate-key", help="print a new VFSYNC_CRED_KEY")

    s = sub.add_parser("set-credentials", help="store encrypted VF credentials")
    s.add_argument("--slug", required=True)
    s.add_argument("--username", required=True)
    s.add_argument("--password-md5", help="or env VF_PASSWORD_MD5")
    s.add_argument("--password", help="plaintext, hashed locally (or env VF_PASSWORD)")
    s.add_argument("--appkey", help="or env VF_APPKEY")
    s.add_argument("--cid", type=int)
    s.add_argument("--base-url")

    e = sub.add_parser("enable", help="enable tenant (dry_run unless --live)")
    e.add_argument("--slug", required=True)
    e.add_argument("--live", action="store_true", help="dry_run=false (after validation!)")

    d = sub.add_parser("disable")
    d.add_argument("--slug", required=True)

    sh = sub.add_parser("show")
    sh.add_argument("--slug", required=True)
    return p


async def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "generate-key":
        print(crypto.generate_key())
        return
    await init_db()
    try:
        await {
            "set-credentials": cmd_set_credentials,
            "enable": cmd_enable,
            "disable": cmd_disable,
            "show": cmd_show,
        }[args.cmd](args)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
