"""Purchasing & goods receiving module (docs/TECHNICAL_BLUEPRINT.md
Sections C.3 and F).

M1 implements the full data model — suppliers, purchase orders, order
lines, goods receipts and their lines, and purchase returns — plus a real
transactional slice (`service.receive_goods`) that proves goods receiving
correctly triggers a Weighted Average Cost recompute through the inventory
module. A purchase order never touches inventory by itself; only an actual
goods receipt does (docs/M1_DATABASE_DESIGN.md).

The Purchasing/Goods Receiving screens and full purchase-order lifecycle
API are still scheduled for Milestone M3.
"""
