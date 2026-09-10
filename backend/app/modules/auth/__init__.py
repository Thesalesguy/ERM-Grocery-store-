"""Identity & access module.

Owns stores, users, roles, and permissions (docs/TECHNICAL_BLUEPRINT.md
Section C.1). Login, JWT issuance, password hashing, and RBAC enforcement
are deliberately NOT implemented in M0 — this module currently establishes
only the data model those features will be built on, per the M0 scope
(see docs/TECHNICAL_BLUEPRINT.md Section M and the M0 task instructions).
"""
