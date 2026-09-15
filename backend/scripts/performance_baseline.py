"""M12 Phase 16: performance and capacity baseline.

Standalone script (not a pytest test -- wall-clock timings are
inherently noisy and environment-dependent, so this is a measurement
tool to run and record, not a pass/fail gate; tests/test_reports_
performance.py already covers the thing that SHOULD be a permanent
regression gate -- query count, not wall-clock time).

Seeds a dedicated, disposable `erp_perf_test` database (created and
dropped by this script, never touching erp_dev/erp_test) with a
moderately realistic dataset, then times the operations that matter
most to a real POS deployment: login, barcode lookup, sale
finalization (checkout), receipt retrieval, and an analytics query over
the full dataset.

Run with: python -m scripts.performance_baseline
(from the backend/ directory, with the erp_user/postgres roles this
sandbox's other scripts already assume -- see test_backup_restore.py's
module docstring for the same sudo -u postgres pattern this reuses).

Do NOT read results from this script as a guarantee about production
hardware/network conditions -- it measures THIS sandbox, once, as a
documented baseline to compare future changes against, per the task's
explicit "do not prematurely optimize, document actual measurements."
"""

from __future__ import annotations

import statistics
import subprocess
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_DB_NAME = "erp_perf_test"
_OWNER_URL = f"postgresql+psycopg://erp_user:erp_password@localhost:5432/{_DB_NAME}"
_SUDO_POSTGRES = ["sudo", "-n", "-u", "postgres"]

_PRODUCT_COUNT = 300
_SALE_COUNT = 300
_LINES_PER_SALE = 3


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}\n{result.stdout}\n{result.stderr}")


def _recreate_database() -> None:
    _run([*_SUDO_POSTGRES, "dropdb", "--if-exists", "--force", _DB_NAME])
    _run([*_SUDO_POSTGRES, "createdb", "-O", "erp_user", _DB_NAME])
    _run(
        [
            *_SUDO_POSTGRES,
            "psql",
            "-d",
            _DB_NAME,
            "-f",
            "scripts/bootstrap_db_roles.sql",
        ]
    )
    _run(
        [
            *_SUDO_POSTGRES,
            "psql",
            "-d",
            _DB_NAME,
            "-c",
            "ALTER ROLE erp_app WITH PASSWORD 'erp_app_password'",
        ]
    )
    import os

    env = {**os.environ, "MIGRATIONS_DATABASE_URL": _OWNER_URL}
    result = subprocess.run(["alembic", "upgrade", "head"], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"migration failed:\n{result.stdout}\n{result.stderr}")


def _seed(db: Session) -> dict:
    from app.modules.auth.permissions import CASHIER
    from app.modules.sales import service as sales_service
    from app.modules.sales.service import PaymentInput, SaleLineInput
    from tests.factories import make_product, make_store, make_user_with_role

    store = make_store(db, name="Perf Test Store")
    cashier = make_user_with_role(db, store, CASHIER, username="perf_cashier")
    db.commit()

    products = [
        make_product(
            db,
            store,
            sku=f"PERF-SKU-{i:05d}",
            name=f"Perf Product {i}",
            current_price=Decimal("9.99"),
            current_cost=Decimal("5.00"),
            current_qty_on_hand=Decimal("100000"),
        )
        for i in range(_PRODUCT_COUNT)
    ]
    db.flush()

    from app.modules.products.models import ProductBarcode

    barcode_product = products[0]
    db.add(ProductBarcode(product_id=barcode_product.id, barcode="0000000000001"))
    db.commit()

    for i in range(_SALE_COUNT):
        lines = [
            SaleLineInput(product_id=products[(i + j) % len(products)].id, quantity=Decimal("1"))
            for j in range(_LINES_PER_SALE)
        ]
        total = Decimal("9.99") * _LINES_PER_SALE
        sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"perf-seed-{i}",
            caller_store_id=store.id,
            lines=lines,
            payments=[PaymentInput(payment_method="CASH", amount=total)],
        )
        db.commit()

    return {
        "store_id": store.id,
        "cashier_id": cashier.id,
        "cashier_username": "perf_cashier",
        "product_ids": [p.id for p in products],
        "barcode": "0000000000001",
    }


def _time_n(label: str, fn: Callable[[], Any], n: int) -> None:
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    mean_ms = statistics.mean(samples) * 1000
    p95_ms = sorted(samples)[int(len(samples) * 0.95) - 1] * 1000 if n > 1 else mean_ms
    print(f"{label}: mean={mean_ms:.2f}ms p95={p95_ms:.2f}ms (n={n})")


def main() -> None:
    print(f"Recreating dedicated database {_DB_NAME} ...")
    _recreate_database()
    engine = create_engine(_OWNER_URL)
    db = Session(bind=engine)
    print(f"Seeding {_PRODUCT_COUNT} products and {_SALE_COUNT} sales ...")
    seed = _seed(db)
    db.close()
    engine.dispose()

    app_engine = create_engine(
        f"postgresql+psycopg://erp_app:erp_app_password@localhost:5432/{_DB_NAME}"
    )
    db = Session(bind=app_engine)

    from app.modules.auth import service as auth_service
    from app.modules.products import service as products_service
    from app.modules.reports import service as reports_service
    from app.modules.sales import service as sales_service
    from app.modules.sales.service import PaymentInput, SaleLineInput

    print(
        "\n--- Performance baseline (erp_perf_test, "
        f"{_PRODUCT_COUNT} products, {_SALE_COUNT} sales) ---\n"
    )

    _time_n(
        "Login (password verify + token issuance)",
        lambda: auth_service.login(
            db, username=seed["cashier_username"], password="Test-Password-123!"
        ),
        n=20,
    )
    db.rollback()

    _time_n(
        "Barcode lookup",
        lambda: products_service.get_product_by_barcode(db, seed["barcode"]),
        n=50,
    )

    counter = {"i": 0}

    def _checkout() -> None:
        counter["i"] += 1
        pid = seed["product_ids"][counter["i"] % len(seed["product_ids"])]
        sales_service.finalize_sale(
            db,
            store_id=seed["store_id"],
            cashier_id=seed["cashier_id"],
            client_transaction_id=f"perf-timing-{counter['i']}",
            caller_store_id=seed["store_id"],
            lines=[SaleLineInput(product_id=pid, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("9.99"))],
        )
        db.commit()

    _time_n("Sale finalization (checkout, 1 line)", _checkout, n=30)

    _time_n(
        "Sales summary analytics (full dataset, all time)",
        lambda: reports_service.sales_summary(
            db, store_ids=None, date_from=date(1901, 1, 1), date_to=date(2099, 12, 31)
        ),
        n=10,
    )

    db.close()
    app_engine.dispose()

    print("\nCleaning up dedicated database ...")
    _run([*_SUDO_POSTGRES, "dropdb", "--if-exists", "--force", _DB_NAME])


if __name__ == "__main__":
    main()
