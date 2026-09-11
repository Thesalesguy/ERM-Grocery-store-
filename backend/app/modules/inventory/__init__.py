"""Inventory & Weighted Average Cost module (docs/TECHNICAL_BLUEPRINT.md
Sections C.2 and D).

This is the foundational piece of Milestone M1: `inventory_movements` is
the append-only ledger and single source of truth for stock quantity and
cost. `products.current_qty_on_hand`/`current_cost` are a maintained
*cache* for fast reads, never the authority — see
docs/M1_DATABASE_DESIGN.md for the reconciliation invariant and its test.

`service.py` provides the pure WAC formula and the transactional
`record_movement` helper (row-locked, ledger-first) that the purchasing
module's goods-receiving slice uses. The full Inventory Service described
in the blueprint (adjustment API, movement-ledger UI) is still scheduled
for Milestone M2.
"""
