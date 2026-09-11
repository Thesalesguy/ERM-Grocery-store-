"""POS / sales module (docs/TECHNICAL_BLUEPRINT.md Sections C.4 and E).

M1 implements the full sales data model (sale header, line items with
frozen historical price/cost/tax/discount, split-tender payments, and
sale returns) so it can support M2's real POS finalization service. No
sale-finalization endpoint exists yet — the invariants (grand_total =
subtotal - discount + tax; a sale's payments must sum to its total) are
proven directly against the schema in tests, per the M1 task's guidance
not to build the full sale service before it's needed.
"""
