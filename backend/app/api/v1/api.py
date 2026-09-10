"""Aggregates all versioned (/api/v1/...) endpoint routers.

Business modules add their routers here as they're implemented (e.g.
`api_router.include_router(products.router, prefix="/products", tags=["products"])`
in Milestone M1). No business routers are registered yet in M0.

Note: /health and /health/db are intentionally mounted at the application
root (see app/main.py), not under this versioned prefix, since infra tools
(Docker healthchecks, load balancers) expect them at a stable, unversioned
path.
"""

from fastapi import APIRouter

api_router = APIRouter()
