"""Aggregates all versioned (/api/v1/...) endpoint routers.

Note: /health and /health/db are intentionally mounted at the application
root (see app/main.py), not under this versioned prefix, since infra tools
(Docker healthchecks, load balancers) expect them at a stable, unversioned
path.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, inventory, products, sales

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(products.router)
api_router.include_router(inventory.router)
api_router.include_router(sales.router)
