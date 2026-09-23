"""Aggregates all versioned (/api/v1/...) endpoint routers.

Note: /health and /health/db are intentionally mounted at the application
root (see app/main.py), not under this versioned prefix, since infra tools
(Docker healthchecks, load balancers) expect them at a stable, unversioned
path.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    accounting,
    ap,
    audit,
    auth,
    hr,
    inventory,
    payroll,
    products,
    purchasing,
    replenishment,
    reports,
    sales,
    shifts,
    stores,
    transfers,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(products.router)
api_router.include_router(inventory.router)
api_router.include_router(sales.router)
api_router.include_router(shifts.router)
api_router.include_router(purchasing.router)
api_router.include_router(accounting.router)
api_router.include_router(ap.router)
api_router.include_router(transfers.router)
api_router.include_router(replenishment.router)
api_router.include_router(hr.router)
api_router.include_router(payroll.router)
api_router.include_router(reports.router)
api_router.include_router(audit.router)
api_router.include_router(stores.router)
