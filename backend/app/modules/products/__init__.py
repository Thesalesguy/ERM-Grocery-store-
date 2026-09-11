"""Product catalog module (docs/TECHNICAL_BLUEPRINT.md Section C.2).

M1 implements the full catalog data model (categories, products, multiple
barcodes per product) plus a minimal read/create API proving the
model/schema/service/route separation described in the M1 task. The
Products screen's remaining CRUD and barcode-management UI are still
scheduled for the blueprint's original M1 roadmap step, now folded into
later frontend work.

Weighed goods: `Product.is_weighed` and `Product.unit_of_measure` exist so
weight-based pricing can be added later, but no weighed-item barcode
parser or scale integration is implemented in M1 (see
docs/M1_DATABASE_DESIGN.md for the explicit scope decision).
"""
