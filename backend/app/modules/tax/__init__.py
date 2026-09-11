"""Tax integration module (docs/TECHNICAL_BLUEPRINT.md Section J).

M1 adds only the foundational `tax_rates` table (versioned, effective-dated
rates that sale line items reference historically) so historical sales
remain reproducible even after rates change. The provider-agnostic
`TaxProvider` interface and fiscal-invoice submission are still scheduled
for Milestone M7 — no tax-authority integration exists here.
"""
