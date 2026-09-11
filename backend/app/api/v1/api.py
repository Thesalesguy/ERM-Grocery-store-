"""Aggregates all versioned (/api/v1/...) endpoint routers.

Note: /health and /health/db are intentionally mounted at the application
root (see app/main.py), not under this versioned prefix, since infra tools
(Docker healthchecks, load balancers) expect them at a stable, unversioned
path.

M1 registers a deliberately minimal set of routers (products, inventory
read) — just enough to prove the model/schema/service/route separation.
Full CRUD for every module arrives with the milestone that needs it.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import inventory, products

api_router = APIRouter()
api_router.include_router(products.router)
api_router.include_router(inventory.router)
