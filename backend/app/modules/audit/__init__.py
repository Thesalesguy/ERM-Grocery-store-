"""Cross-cutting audit logging module.

Owns the append-only audit_logs table (docs/TECHNICAL_BLUEPRINT.md Section
C.6). A reusable logging helper (called from every privileged mutation in
later milestones) will live here; only the data model is established in M0.
"""
