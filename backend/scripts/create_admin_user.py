#!/usr/bin/env python3
"""One-time bootstrap: create the first Admin user.

There is no hardcoded admin account and no public self-registration
endpoint (an internal POS/ERP has no legitimate "sign yourself up" use
case) — so creating the very first user is a deliberate, interactive,
operator-run step. Every subsequent user can be created by an Admin
through the application once one exists.

Usage (from backend/, with the virtualenv active and MIGRATIONS_DATABASE_URL
or DATABASE_URL pointed at a migrated database):

    python scripts/create_admin_user.py

Prompts for username, email, full name, store (optional), and password
(hidden input, confirmed). Refuses to run if the username/email already
exists, or if the "Admin" role hasn't been seeded yet (run migrations
first).
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.modules.auth.models import Role, Store, User, UserRole  # noqa: E402
from app.modules.auth.permissions import ADMIN  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        admin_role = db.execute(select(Role).where(Role.name == ADMIN)).scalar_one_or_none()
        if admin_role is None:
            print(
                "No 'Admin' role found — run `alembic upgrade head` first "
                "(it seeds roles/permissions).",
                file=sys.stderr,
            )
            return 1

        username = input("Username: ").strip()
        if not username:
            print("Username is required.", file=sys.stderr)
            return 1
        if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
            print(f"User {username!r} already exists.", file=sys.stderr)
            return 1

        email = input("Email: ").strip()
        if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
            print(f"A user with email {email!r} already exists.", file=sys.stderr)
            return 1

        full_name = input("Full name: ").strip() or username

        stores = db.execute(select(Store).where(Store.is_active)).scalars().all()
        store_id = None
        if stores:
            print("Stores:")
            for store in stores:
                print(f"  [{store.id}] {store.name}")
            raw = input("Store ID (blank for none / cross-store admin): ").strip()
            if raw:
                store_id = int(raw)

        password = getpass.getpass("Password: ")
        if len(password) < 12:
            print("Password must be at least 12 characters.", file=sys.stderr)
            return 1
        if password != getpass.getpass("Confirm password: "):
            print("Passwords did not match.", file=sys.stderr)
            return 1

        user = User(
            username=username,
            email=email,
            full_name=full_name,
            store_id=store_id,
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=admin_role.id))
        db.commit()
        print(f"Created Admin user {username!r} (id={user.id}).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
