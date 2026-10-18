"""Accounting-entity module (M25 Phase 1, docs/M24D_TECHNICAL_CONTRACT.md
Sections 3-4): the organizational layer distinguishing corporate-division
stores (one shared accounting entity) from financially independent/
franchise stores (separate accounting entities). This phase is foundation
only -- no API, no permission, and no behavior change to any existing
accounting/inventory flow. Later M25 phases (not this one) read
`AccountingEntity.entity_type` to decide intra-entity vs. inter-entity
transfer treatment.
"""
