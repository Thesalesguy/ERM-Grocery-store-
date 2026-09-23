"""M13 Phase 15: performance under the real production topology.

M12 Phase 16 (backend/scripts/performance_baseline.py) measured
login/barcode-lookup/checkout/analytics via DIRECT service-layer calls
-- no HTTP, no proxy, no TLS. This file repeats that measurement
discipline (a documented tool, not a pass/fail gate on wall-clock time
-- see that script's own docstring for why) through the REAL
production topology: real HTTPS requests through nginx to a real
uvicorn process, covering every operation the task names: login,
product search, barcode lookup, checkout/sale finalization, return,
receiving, AP payment, payroll posting, and analytics -- plus a
realistic-concurrency checkout burst and a snapshot of connection/
resource pressure while it runs.

Prerequisite data for each flow (suppliers, purchase orders, invoices,
payroll periods) is built via direct service-layer calls, matching
M12's own script -- setup speed isn't what's being measured. Only the
one HTTP call that IS the operation being measured goes through the
real nginx -> uvicorn round trip.

Thresholds below are deliberately generous (seconds, not milliseconds)
-- loose enough to never fail on ordinary variance, tight enough to
catch a genuine hang or a multi-second regression, per the task's own
"do not optimize without measurements" instruction: the printed
mean/p95 numbers are the actual measurement to read, not the assertion.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
from conftest import BACKEND_DIR, BASE_URL, DB_URL
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(BACKEND_DIR))

_GENEROUS_MAX_SECONDS = 5.0


# Paced well under every nginx rate-limit zone's SUSTAINED rate (M13
# Phase 4) -- not just kept under each zone's burst. Found the hard way
# during this phase's own development: shrinking sample counts to fit
# under a burst allowance still trips the limiter once several
# operations' request groups run back-to-back faster than the bucket
# refills (erp_general's 120r/m = 2/sec sustained, erp_reports' 30r/m,
# erp_login's 10r/m all apply on top of whatever burst a prior group
# already spent). Pacing every sample at this interval keeps the whole
# script's cumulative demand under the tightest zone's sustained rate
# regardless of how many groups precede it, which is also more
# representative of real traffic than firing dozens of identical
# requests with no gap between them at all.
_REQUEST_PACING_SECONDS = 0.6


def _time_n(label: str, fn: Callable[[], Any], n: int) -> list[float]:
    samples = []
    for i in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
        if i < n - 1:
            time.sleep(_REQUEST_PACING_SECONDS)
    mean_ms = statistics.mean(samples) * 1000
    p95_ms = sorted(samples)[max(int(len(samples) * 0.95) - 1, 0)] * 1000
    max_ms = max(samples) * 1000
    print(f"  {label}: mean={mean_ms:.1f}ms p95={p95_ms:.1f}ms max={max_ms:.1f}ms (n={n})")
    assert max(samples) < _GENEROUS_MAX_SECONDS, (
        f"{label}: slowest sample {max(samples):.2f}s exceeds the generous "
        f"{_GENEROUS_MAX_SECONDS}s sanity bound -- a real hang/regression, not noise"
    )
    return samples


def test_performance_baseline_through_the_real_production_topology(production_stack) -> None:
    from app.modules.ap import service as ap_service
    from app.modules.ap.service import PurchaseInvoiceLineInput
    from app.modules.auth.permissions import ADMIN
    from app.modules.hr import service as hr_service
    from app.modules.hr.service import EmployeeHireInput
    from app.modules.payroll import service as payroll_service
    from app.modules.payroll.service import PayrollPeriodInput
    from app.modules.purchasing import service as purchasing_service
    from app.modules.purchasing.models import PurchaseOrderItem
    from app.modules.purchasing.service import GoodsReceiptLineInput, PurchaseOrderItemInput
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_product,
        make_store,
        make_supplier,
        make_user_with_role,
        unique_suffix,
    )

    engine = create_engine(DB_URL)
    db = Session(engine)

    store = make_store(db, name=f"Perf Store {unique_suffix()}")
    supplier = make_supplier(db)
    username = f"m13_perf_admin_{unique_suffix()}"
    # Admin: unscoped and holds every permission, including PAYROLL_POST
    # (deliberately withheld from Manager -- M10 segregation of duties),
    # so one login covers every operation timed below.
    admin_user = make_user_with_role(db, None, ADMIN, username=username)
    products = [
        make_product(
            db,
            store,
            sku=f"PERF-{unique_suffix()}",
            name=f"Perf Product {i}",
            current_price=Decimal("9.99"),
            current_cost=Decimal("5.00"),
            current_qty_on_hand=Decimal("100000"),
        )
        for i in range(20)
    ]
    from app.modules.products.models import ProductBarcode

    barcode = f"9{unique_suffix()[:12]}"
    db.add(ProductBarcode(product_id=products[0].id, barcode=barcode))
    db.commit()

    login_resp = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        json={"username": username, "password": DEFAULT_TEST_PASSWORD},
    )
    assert login_resp.status_code == 200
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    print(f"\n--- M13 Phase 15 performance baseline (through {BASE_URL}) ---\n")

    # --- Login ----------------------------------------------------------
    # n kept small and deliberately: nginx's erp_login zone (M13 Phase 4)
    # allows only burst=5 rapid attempts before returning 429 -- correct
    # brute-force protection, and also realistic (a real user logs in
    # once per session, not repeatedly in a tight loop). Found by this
    # test itself hitting 429 at n=15 with the setup login already
    # having spent one slot of the burst.
    _time_n(
        "Login",
        lambda: httpx.post(
            f"{BASE_URL}/api/v1/auth/login",
            verify=False,
            json={"username": username, "password": DEFAULT_TEST_PASSWORD},
        ).raise_for_status(),
        n=3,
    )

    # --- Product search ---------------------------------------------------
    _time_n(
        "Product search",
        lambda: httpx.get(
            f"{BASE_URL}/api/v1/products",
            verify=False,
            params={"search": "Perf Product", "store_id": store.id},
            headers=headers,
        ).raise_for_status(),
        n=20,
    )

    # --- Barcode lookup -----------------------------------------------------
    _time_n(
        "Barcode lookup",
        lambda: httpx.get(
            f"{BASE_URL}/api/v1/products/barcode/{barcode}", verify=False, headers=headers
        ).raise_for_status(),
        n=20,
    )

    # --- Checkout / sale finalization --------------------------------------
    checkout_sale_ids: list[int] = []
    checkout_sale_item_ids: list[int] = []

    def _checkout() -> None:
        resp = httpx.post(
            f"{BASE_URL}/api/v1/sales",
            verify=False,
            headers=headers,
            json={
                "store_id": store.id,
                "client_transaction_id": f"perf-checkout-{unique_suffix()}",
                "lines": [{"product_id": products[0].id, "quantity": "1"}],
                "payments": [{"payment_method": "CASH", "amount": "9.99"}],
            },
        )
        resp.raise_for_status()
        body = resp.json()
        checkout_sale_ids.append(body["id"])
        checkout_sale_item_ids.append(body["items"][0]["id"])

    _time_n("Checkout (sale finalization, 1 line)", _checkout, n=15)

    # --- Return -------------------------------------------------------------
    return_iter = iter(zip(checkout_sale_ids, checkout_sale_item_ids, strict=True))

    def _return() -> None:
        sale_id, sale_item_id = next(return_iter)
        httpx.post(
            f"{BASE_URL}/api/v1/sales/{sale_id}/returns",
            verify=False,
            headers=headers,
            json={
                "store_id": store.id,
                "return_date": str(date.today()),
                "client_transaction_id": f"perf-return-{unique_suffix()}",
                "refund_method": "CASH",
                "lines": [{"sale_item_id": sale_item_id, "quantity": "1", "restock": True}],
            },
        ).raise_for_status()

    _time_n("Return", _return, n=10)

    # --- Receiving (goods receipt) -------------------------------------------
    # Prerequisite POs built via the real service layer (create -> submit,
    # not the test-factory shortcut) so each is in a legally receivable
    # state, prepared BEFORE timing so only the receive call itself is timed.
    receive_targets: list[tuple[int, int]] = []  # (purchase_order_id, item_id)
    for _ in range(10):
        po = purchasing_service.create_purchase_order(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            order_date=date.today(),
            client_transaction_id=f"perf-po-{unique_suffix()}",
            lines=[
                PurchaseOrderItemInput(
                    product_id=products[0].id,
                    quantity_ordered=Decimal("10"),
                    unit_cost=Decimal("5.00"),
                )
            ],
        )
        db.commit()
        purchasing_service.submit_purchase_order(db, po.id, actor_id=None)
        db.commit()
        item_id = (
            db.query(PurchaseOrderItem.id)
            .filter(PurchaseOrderItem.purchase_order_id == po.id)
            .scalar()
        )
        receive_targets.append((po.id, item_id))

    receive_iter = iter(receive_targets)

    def _receive() -> None:
        po_id, item_id = next(receive_iter)
        httpx.post(
            f"{BASE_URL}/api/v1/purchasing/purchase-orders/{po_id}/receive",
            verify=False,
            headers=headers,
            json={
                "received_date": str(date.today()),
                "client_transaction_id": f"perf-receive-{unique_suffix()}",
                "lines": [
                    {
                        "purchase_order_item_id": item_id,
                        "quantity_received": "10",
                        "unit_cost": "5.00",
                    }
                ],
            },
        ).raise_for_status()

    _time_n("Receiving (goods receipt)", _receive, n=10)

    # --- AP payment -----------------------------------------------------------
    # Prerequisite posted invoices, same "prepare then time" discipline.
    invoice_ids: list[int] = []
    for _ in range(10):
        po = purchasing_service.create_purchase_order(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            order_date=date.today(),
            client_transaction_id=f"perf-po-{unique_suffix()}",
            lines=[
                PurchaseOrderItemInput(
                    product_id=products[0].id,
                    quantity_ordered=Decimal("5"),
                    unit_cost=Decimal("5.00"),
                )
            ],
        )
        db.commit()
        purchasing_service.submit_purchase_order(db, po.id, actor_id=None)
        db.commit()
        item = (
            db.query(PurchaseOrderItem).filter(PurchaseOrderItem.purchase_order_id == po.id).one()
        )
        purchasing_service.receive_goods(
            db,
            purchase_order_id=po.id,
            received_date=date.today(),
            lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("5.00"))],
            client_transaction_id=f"perf-ap-receive-{unique_suffix()}",
            caller_store_id=None,
        )
        db.commit()
        invoice = ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"PERF-INV-{unique_suffix()}",
            invoice_date=date.today(),
            lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("5.00"))],
            client_transaction_id=f"perf-inv-{unique_suffix()}",
            caller_store_id=None,
        )
        ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
        db.commit()
        invoice_ids.append(invoice.id)

    invoice_iter = iter(invoice_ids)

    def _ap_payment() -> None:
        invoice_id = next(invoice_iter)
        httpx.post(
            f"{BASE_URL}/api/v1/ap/payments",
            verify=False,
            headers=headers,
            json={
                "store_id": store.id,
                "supplier_id": supplier.id,
                "payment_date": str(date.today()),
                "payment_method": "CASH",
                "amount": "25.00",
                "allocations": [{"purchase_invoice_id": invoice_id, "amount": "25.00"}],
                "client_transaction_id": f"perf-pay-{unique_suffix()}",
            },
        ).raise_for_status()

    _time_n("AP payment", _ap_payment, n=10)

    # --- Payroll posting --------------------------------------------------------
    # Fewer samples (n=5): each needs its own employee + compensation +
    # period taken all the way through OPEN -> CALCULATED -> APPROVED via
    # the real service layer first.
    approved_period_ids: list[int] = []
    for i in range(5):
        employee = hr_service.hire_employee(
            db,
            EmployeeHireInput(
                employee_number=f"PERF-EMP-{unique_suffix()}",
                legal_name=f"Perf Employee {i}",
                hire_date=date(2024, 1, 1),
                store_id=store.id,
            ),
            actor_id=None,
            caller_store_id=None,
        )
        hr_service.change_compensation(
            db,
            employee_id=employee.id,
            effective_from=date(2024, 1, 1),
            pay_type="SALARY",
            rate=Decimal("2000.00"),
            pay_frequency="MONTHLY",
            overtime_eligible=False,
            currency="USD",
            actor_id=None,
            caller_store_id=None,
        )
        db.commit()
        period = payroll_service.create_payroll_period(
            db,
            PayrollPeriodInput(
                store_id=store.id,
                period_start=date(2024, 1 + i, 1) if i < 11 else date(2025, 1, 1),
                period_end=date(2024, 1 + i, 15) if i < 11 else date(2025, 1, 15),
                pay_date=date(2024, 1 + i, 20) if i < 11 else date(2025, 1, 20),
            ),
            actor_id=None,
            caller_store_id=None,
        )
        payroll_service.open_payroll_period(
            db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )
        payroll_service.calculate_payroll_period(
            db, payroll_period_id=period.id, actor_id=admin_user.id, caller_store_id=None
        )
        payroll_service.approve_payroll_period(
            db, payroll_period_id=period.id, actor_id=admin_user.id, caller_store_id=None
        )
        db.commit()
        approved_period_ids.append(period.id)

    period_iter = iter(approved_period_ids)

    def _payroll_post() -> None:
        period_id = next(period_iter)
        httpx.post(
            f"{BASE_URL}/api/v1/payroll/periods/{period_id}/post",
            verify=False,
            headers=headers,
            json={"client_transaction_id": f"perf-post-{unique_suffix()}"},
        ).raise_for_status()

    _time_n("Payroll posting", _payroll_post, n=5)

    # --- Analytics ----------------------------------------------------------
    # n kept under erp_reports' burst=10 (M13 Phase 4) with margin, rather
    # than sitting exactly at the boundary where a flake would be timing-
    # dependent noise rather than a real signal.
    _time_n(
        "Analytics (sales summary)",
        lambda: httpx.get(
            f"{BASE_URL}/api/v1/reports/sales/summary",
            verify=False,
            params={
                "store_id": store.id,
                "date_from": "1901-01-01",
                "date_to": "2099-12-31",
            },
            headers=headers,
        ).raise_for_status(),
        n=8,
    )

    # --- Realistic concurrency: a burst of simultaneous checkouts -----------
    def _concurrent_checkout(i: int) -> int:
        resp = httpx.post(
            f"{BASE_URL}/api/v1/sales",
            verify=False,
            headers=headers,
            json={
                "store_id": store.id,
                "client_transaction_id": f"perf-concurrent-{unique_suffix()}",
                "lines": [{"product_id": products[1].id, "quantity": "1"}],
                "payments": [{"payment_method": "CASH", "amount": "9.99"}],
            },
        )
        return resp.status_code

    concurrency = 10
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        statuses = list(pool.map(_concurrent_checkout, range(concurrency)))
    elapsed = time.perf_counter() - start
    error_count = sum(1 for s in statuses if s not in (200, 201))
    print(
        f"  Concurrent checkout burst ({concurrency} simultaneous): "
        f"{elapsed * 1000:.1f}ms wall time, {error_count}/{concurrency} errors"
    )
    assert error_count == 0, f"statuses: {statuses}"

    # --- Resource usage snapshot ----------------------------------------------
    with Session(engine) as snapshot_db:
        conn_count = snapshot_db.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        ).scalar_one()
    ps_output = subprocess.run(
        ["pgrep", "-f", "uvicorn app.main:app.*--port 8000"], capture_output=True, text=True
    ).stdout.strip()
    backend_pid = ps_output.splitlines()[0] if ps_output else None
    rss_kb = None
    if backend_pid:
        rss_output = subprocess.run(
            ["ps", "-o", "rss=", "-p", backend_pid], capture_output=True, text=True
        ).stdout.strip()
        rss_kb = int(rss_output) if rss_output else None
    print(
        f"  Resource snapshot: {conn_count} DB connections to {DB_URL.rsplit('/', 1)[-1]!r}, "
        f"backend RSS={rss_kb / 1024:.1f}MB"
        if rss_kb
        else f"  DB connections: {conn_count}"
    )

    db.close()
