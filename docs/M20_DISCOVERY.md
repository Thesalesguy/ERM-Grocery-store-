# M20 Discovery — Tax & Fiscal Compliance Architecture and Integration Readiness

Phase 0 of M20. Per this milestone's explicit instruction, this document
maps what actually exists in the repository today (code read directly,
not inferred from filenames/comments/docs) and establishes the
jurisdiction status before any design or implementation work begins.

## 1. Jurisdiction evidence matrix

| Source | What it says | Classification |
| --- | --- | --- |
| `docs/TECHNICAL_BLUEPRINT.md` line 15 (Ambiguity #3) | "No authority named. A pluggable tax-adapter interface is designed... Given likely deployment region (East Africa, TZS context from prior conversations), TRA's EFD/VFD model is the most probable future target, but nothing here hardcodes that — confirm official docs before building the adapter." | DOCUMENTED REQUIREMENT: jurisdiction is explicitly **unconfirmed**, only a probabilistic guess flagged as needing external confirmation before use |
| `docs/M10_DESIGN.md` line 101 | "No jurisdiction, country, or statutory tax authority is named" | FACT (restates the same unconfirmed status at M10) |
| `docs/M2_AUTH_AND_POS.md` line 624 | "Multi-currency / multi-tax-jurisdiction beyond a single [store]" listed as out of scope | IMPLEMENTATION DECISION (pre-M20, unchanged) |
| `docs/M11_DESIGN.md` lines 415/425/688 | Payroll explicitly disclaims any "jurisdiction-specific tax/social-security formula" | IMPLEMENTATION DECISION (payroll domain, consistent precedent) |
| `docs/M16_DESIGN.md` lines 252/254/286 | "No tax, currency, fiscal, payroll-jurisdiction, or hardware configuration" added by M16; "No fiscal/EFD/TRA integration... remaining [gaps]" | FACT — as of M16 (most recent milestone before M20 to touch this question), still zero jurisdiction configuration exists |
| `backend/app/modules/auth/models.py::Store` | No `country`, `currency`, `tax_registration_number`, `legal_name`, or jurisdiction column of any kind on `Store` | FACT (verified by reading the model directly) |
| `backend/app/core/config.py` | No `TAX_AUTHORITY`, `TAX_JURISDICTION`, or fiscal-provider setting of any kind | FACT |
| Any `.env.example` / `docker-compose*.yml` | No fiscal/tax-authority environment variable | FACT (grepped; none found) |

**Conclusion, per the milestone's Critical Jurisdiction Rule:**

> Target tax jurisdiction is not established in the current project source.

The one candidate ("TRA's EFD/VFD model... most probable future target")
is explicitly self-flagged in its own source document as an inference
from prior conversation context, not a project requirement, and the
document instructs that it must be confirmed against official authority
documentation before being used to build anything. No later milestone
(M2 through M19) ever established it as confirmed, and several
(`M2`, `M10`, `M11`, `M16`) independently re-confirm the same "not
established" status. M20 must therefore proceed as **jurisdiction-neutral
infrastructure** per the milestone's own instructions, with no
authority-specific payload, signing, or numbering scheme invented.

## 2. Source-of-truth capability matrix

| Requirement | Existing implementation | Source files | Tests | Gap | Risk |
| --- | --- | --- | --- | --- | --- |
| Product tax configuration | `Product.tax_rate_id` (nullable FK to `tax_rates`); no tax category, no exemption flag distinct from "no rate assigned" | `products/models.py:87`, `products/schemas.py:27,49,72` | `test_tax.py`, `test_tax_boundaries.py` | No explicit `EXEMPT`/`ZERO_RATED` distinction from "untaxed"; no per-product multi-tax | Low — current single-tax model matches every confirmed requirement; a real jurisdiction's category scheme is unknown, so building one now would be speculative |
| Tax rates | `TaxRate(name, rate_percent NUMERIC(6,3), is_active, effective_from, effective_to)`, `ck_tax_rates_rate_bounds` (0–100), `ck_tax_rates_effective_range` | `tax/models.py` | `test_tax.py` (bounds, range), `test_tax_boundaries.py` (7 boundary-date cases) | No jurisdiction/authority reference column (correctly absent — none exists to reference) | None |
| Tax-inclusive vs. exclusive pricing | Always exclusive: `_resolve_tax` computes `tax_amount = round((line_subtotal - discount) * rate / 100)` on top of `unit_price_at_sale` | `sales/service.py:123-158,264-300` | Implicit in every sales test asserting `tax_total` | No inclusive-pricing mode exists | Low — no confirmed requirement for inclusive pricing; do not invent one |
| Sale tax calculation | Per-line, single rate, `ROUND_HALF_UP` via `_round_money`, `Decimal` throughout, `NUMERIC(12,2)` storage | `sales/service.py:74-75,123-158,264-300` | `test_tax.py`, `test_tax_boundaries.py` | None found | None |
| Sale lines | `SaleItem.tax_rate_id` + `SaleItem.tax_amount` frozen at sale time; `ck_sale_items_line_total_consistent` ties `line_total` to the row's own arithmetic | `sales/models.py:99-142` | `test_tax.py::test_sale_item_keeps_its_historical_tax_rate_after_the_rate_changes` | None | None |
| Returns/tax reversal | `SaleReturnItem.tax_refunded` — proportional share of the original line's frozen `tax_amount`, never recomputed from current rate | `sales/models.py:250-286`, `sales/service.py:474-564` | `test_sales_returns_*` suite (M5) | None | None |
| Purchase-side tax | `PurchaseInvoice.tax_total` / `PurchaseInvoiceLine.tax_amount` — raw stored amounts from the supplier's own invoice, no FK to `tax_rates` (input tax is not computed from the internal rate table, matching a supplier-invoice-driven design) | `ap/models.py:86-168` | `test_ap_*` suite | None (input-tax non-recoverability is a documented M6 design decision, not a defect — `docs/M6_AP_VENDOR_ACCOUNTING.md` §8/§11) | None |
| Supplier invoices | AP tax posts to `ACCOUNT_PURCHASE_TAX_EXPENSE` (5200), expensed outright, never reclaimed | `accounting/service.py:488-599`, `accounting/constants.py:109-120` | `test_ap_*`, `test_accounting_api.py` | Documented, intentional non-goal (no recoverable-input-VAT workflow) | None — out of scope unless a jurisdiction confirms input-tax recovery |
| Accounting journal entries | `Account`/`JournalEntry`/`JournalLine`, double-entry, `ACCOUNT_TAX_PAYABLE` (2100, liability) credited on every sale with `tax_total > 0`, debited on returns | `accounting/service.py:264-356`, `accounting/constants.py:62` | `test_accounting_api.py`, `test_sales_returns_reconciliation.py` | None | None |
| AR/AP | AP fully built (M6/M7/M17/M19); no AR module exists (grocery POS is cash/card at point of sale, no customer credit) | `ap/*` | `test_ap_*` | No AR — consistent with a retail-POS business model, not a gap | None |
| General ledger | `Account` chart of accounts, `JournalEntry`/`JournalLine`, trial-balance/P&L reporting in `reports/service.py` | `accounting/*`, `reports/service.py` | `test_reports_financial.py` | None relevant to tax | None |
| Store identity | `Store(id, name, address, timezone, is_active, attendance_day_boundary_hour, return_approval_threshold_amount)` — **no** legal name, tax registration number, currency, or country field | `auth/models.py:27-70` | — | No legal/business identity infrastructure exists at all | Medium — any fiscal document needs a seller identity; must be added as a nullable, optional configuration boundary, not invented per a guessed jurisdiction |
| Legal/business identity | None exists anywhere in the schema | — | — | Confirmed absent | Medium (see above) |
| Invoice/receipt numbering | `Sale.sale_number`, `PurchaseOrder.purchase_number`, `SaleReturn.return_number` — each a unique, app-generated string (`_generate_sale_number`, etc.), not a gapless/fiscal sequence | `sales/service.py:118,503` | Uniqueness enforced by DB constraint | No fiscal (gapless, authority-mandated) numbering scheme — correctly absent, since no authority has specified one | Low — do not invent a numbering scheme; any real requirement is jurisdiction-specific |
| Receipt generation | Frontend-only, on-screen `Receipt` React component: sale number, line items, subtotal/discount/tax/grand totals, payments, change. No store legal identity, no per-line tax breakdown, no fiscal identifier, no print/PDF output, no `window.print()` call | `frontend/src/pages/PosPage.tsx:403-460` | `PosPage.test.tsx` (checkout flow, not receipt content) | Minimal by design (`docs/M2_AUTH_AND_POS.md` §11: "No receipt printer / fiscal device integration — deliberately deferred") | Low today; would need real fields once a jurisdiction is confirmed |
| Payment records | `Payment(sale_id, payment_method, amount, reference)` — split tender supported | `sales/models.py:145-165` | full sales suite | None relevant to tax | None |
| Sale finalization | `finalize_sale`/`create_sale` — single DB transaction: inventory movement + accounting post + sale row, all-or-nothing | `sales/service.py` | `test_sale_finalization_failure_injection.py` | No fiscalization step exists in this transaction today (nothing to remove/preserve) | N/A |
| Voids | Modeled as a full `SaleReturn` of every line (no separate status) | `sales/models.py:168-181` | `test_return_void_approval.py` | Tax reversal already correct (see returns row above) | None |
| Refunds | `SaleReturn.refund_amount`/`refund_method`, tied 1:1 to `tax_refunded` on each line | `sales/models.py` | `test_sales_returns_*` | None | None |
| Credit notes | `SupplierCreditNote` (AP side only — no customer-facing credit note concept, consistent with no-AR) | `ap/models.py` | `test_ap_payments.py`, `test_ap_reversal.py` | None relevant | None |
| Audit events | Append-only `AuditLog(user_id, action, entity_type, entity_id, before_state, after_state, ip_address, user_agent)`; DB-level UPDATE/DELETE revoked | `audit/models.py`, `audit/service.py` | `test_audit_hardening.py`, `test_audit_log_endpoint.py` | None — directly reusable for fiscal-config change auditing | None |
| Timestamps | `TimestampMixin` (`created_at`/`updated_at`, `timestamptz`, UTC convention throughout — see `test_tax_boundaries.py` docstring) | `db/base_class.py` | pervasive | None | None |
| Currency | No currency column/type anywhere in the schema (`docs/TECHNICAL_BLUEPRINT.md` assumption #2: single implicit local currency) | — | — | Confirmed absent, matches the blueprint's own explicit assumption | Low — do not invent multi-currency; out of scope until required |
| Rounding | `ROUND_HALF_UP`, `Decimal`, `NUMERIC(12,2)` for money / `NUMERIC(14,3)` quantities / `NUMERIC(14,6)` unit cost, applied consistently at every money computation site (`_round_money` in sales, equivalents in AP/purchasing) | `sales/service.py:74-75`, `purchasing/service.py`, `ap/service.py` | `test_tax_boundaries.py`, WAC fractional tests (M19) | None | None |
| Decimal/NUMERIC handling | No floating-point money anywhere (verified: no `float()` cast on any money field across the modules read) | pervasive | pervasive | None | None |
| Existing tax/fiscal abstractions | `TaxRate` model only. **No `TaxProvider` interface, no `tax_submissions` table, no fiscal-document model exist in code** — `docs/TECHNICAL_BLUEPRINT.md` Section J describes them but that document is explicitly headed "Analysis phase — no application code written" (line 3) and predates every milestone | `tax/models.py`, `tax/__init__.py` (empty) | `test_tax.py` | The entire fiscal-submission/provider-adapter layer described in the blueprint was never built | This is M20's actual scope |
| Existing external-integration abstractions | **None.** No HTTP client library used anywhere in `backend/app` (`httpx`/`requests` appear only in `deploy/tests/`, testing the app's own endpoints, never calling out); no outbound API client of any kind | grepped across `backend/app` | — | Confirmed absent | High — M20 must design this from zero, which is exactly what "integration readiness" (not integration) means |
| Authentication/credentials/secrets handling | `core/config.py` `Settings` with a documented `_forbid_insecure_production_config` gate; `core/logging.py` strips `secret`/`secret_key`/`token`/etc. keys from structured logs (M12) | `core/config.py`, `core/logging.py` | `test_production_secret_management.py`, `test_auth_hardening.py` | No secret-reference pattern exists yet for a *third-party* credential (only the app's own JWT secret/DB password) | Medium — any fiscal credential must follow the same never-in-logs/never-in-GET-response discipline, extended to a new kind of secret |
| Retry/idempotency infrastructure | Mature, proven pattern: `client_transaction_id` UNIQUE column + fast-path lookup + `IntegrityError` recovery, used by `Sale`, `SaleReturn`, `GoodsReceipt`, `PurchaseOrder`, `PurchaseReturn`, `PurchaseInvoice`, `SupplierPayment`, `SupplierCreditNote` (M2–M19) | pervasive, see `docs/M19_HARDENING_AUDIT.md` for the fullest description | dozens of idempotency/concurrency test files | No retry-with-backoff pattern exists (every existing use is synchronous request-time dedup, not an async retry queue) | Medium — a fiscal submission needs the same dedup key PLUS an async retry/backoff loop this codebase has never needed before |
| Background jobs | **None exist.** No worker process, no scheduler, no queue (`docs/TECHNICAL_BLUEPRINT.md` Section L describes a `worker` container that was never built) | — | — | Confirmed absent | High — a durable fiscal retry loop needs some execution model; must be designed to fit this app's actual (synchronous, single-process-per-request) architecture, not the blueprint's aspirational one |
| Outbox/event infrastructure | None | — | — | Confirmed absent | This is the core of what Phase 2/3 must design |
| API clients | None (frontend `apiFetch` wrapper calls this app's own backend only) | `frontend/src/api/client.ts` | — | N/A | N/A |
| Webhooks | No inbound webhook endpoint exists anywhere in `app/api/v1/endpoints/` | grepped | — | Confirmed absent | Medium — if a future authority pushes async acknowledgements, an inbound endpoint and its authentication scheme are still undesigned |
| Configuration management | `Settings` (env-based), plus the one precedent for per-store config living as typed columns directly on `Store` (`attendance_day_boundary_hour`, `return_approval_threshold_amount` — M10/M14design precedent, explicitly NOT a generic settings table) | `core/config.py`, `auth/models.py` | — | No fiscal-config precedent yet; the two established patterns (env var for a global secret-shaped value, typed `Store` column for per-store business config) both apply to different parts of what fiscal config needs | None — precedent exists to follow |
| Multi-store separation | Enforced pervasively at the service layer (every query scoped by `store_id`, verified via dozens of `test_*_isolation` / `test_store_isolation.py` tests across every M2–M19 domain) | pervasive | `test_store_isolation.py` + per-domain isolation tests | None found | None — must extend the identical pattern to fiscal config/submissions |

## 3. Fiscal-integration-boundary matrix (what M20 must design, since nothing exists)

Because the entire integration layer described in `TECHNICAL_BLUEPRINT.md`
Section J was never built, this is a from-zero design, not a gap-filling
exercise like M19. The blueprint's own sketch (`TaxProvider` interface,
opaque-JSONB `tax_submissions` table, `sale_id`-keyed idempotency,
never-block-checkout-on-the-network principle, retry-queue polling) is a
reasonable **starting point** — it is 5+ years of this project's own
prior analysis, not an invented requirement — but every authority-specific
detail in it (TRA/EFD/VFD, signing, endpoint shape) remains explicitly
unconfirmed and must not be built.

## 4. FACT / DOCUMENTED REQUIREMENT / IMPLEMENTATION DECISION / ASSUMPTION / OPEN QUESTION

**FACT** (verified by reading code):
- No jurisdiction, tax authority, fiscal device, legal identity, currency, or external-integration infrastructure exists anywhere in the current codebase.
- The tax domain model that does exist (`TaxRate`, per-line `tax_rate_id`/`tax_amount` freezing, `ROUND_HALF_UP` Decimal arithmetic, `ACCOUNT_TAX_PAYABLE` GL posting) is correct and already satisfies every M20 domain requirement that doesn't depend on a specific jurisdiction (historical immutability, exact arithmetic, accounting reconciliation).
- `client_transaction_id`-based idempotency is a proven, repeatedly-audited pattern in this codebase and is the correct primitive to extend for fiscal-submission dedup.

**DOCUMENTED REQUIREMENT** (from the project's own prior specs):
- The original mission's Phase 3 ("Web Tax Integration & APIs") calls for connecting sales to tax authority endpoints, sending secure payloads, and supporting fiscal-device/signature workflows "where the applicable authority requires them" — i.e., conditionally, not unconditionally.
- `TECHNICAL_BLUEPRINT.md` Section J requires: local-only tax calculation (never blocking checkout on network availability), a provider-agnostic interface, opaque-payload storage, `sale_id`-keyed idempotency, and authority-specific auth/signing "confirmed from that authority's official integration documentation before implementation."

**IMPLEMENTATION DECISION** (M20, to be finalized in `M20_DESIGN.md`):
- Build the integration boundary (provider interface, durable submission ledger, retry/state machine, fiscal-config data model) as real, tested code.
- Do not build any authority-specific adapter; ship a deterministic fake/null adapter only.
- Fiscalization is OFF by default and a pure no-op end-to-end until a jurisdiction is configured (which this milestone will not do).

**ASSUMPTION**: none permitted to become code per the Critical Jurisdiction Rule. Where the design needs a placeholder value (e.g., an example rate in a test), it must be visibly synthetic, exactly as `TaxRate`'s own docstring already establishes for the 18% example figure.

**OPEN QUESTION** (explicitly deferred, not to be guessed):
- Which authority, what payload shape, what signing mechanism, what numbering rule, what submission endpoint — all unknowable until a real jurisdiction is named.
- Whether store-level or company-level fiscal identity applies — deferred to the config boundary (support both via a nullable per-store override of a facility-level default, matching the existing `Store`-column precedent) rather than guessed.

## 5. Confirmed requirements

1. Tax calculation must remain exact Decimal/NUMERIC arithmetic (already true).
2. Historical tax treatment must never change retroactively (already true).
3. A fiscal submission must never be duplicated by a retry (extend the proven idempotency pattern).
4. Checkout must never block on an external network call (already true by omission — must remain true once fiscalization exists).
5. Multi-store isolation must extend to any new fiscal config/data (established pattern to reuse).
6. Fiscal credentials must never appear in logs, GET responses, or committed config (extend the existing secret-handling discipline).

## 6. Unknown requirements

Every jurisdiction-specific item listed in Section 4's OPEN QUESTION list.

## 7. Assumptions

None taken as implementation input. (The single documented, pre-existing
assumption — East Africa/TZS being "the most probable future target" —
is explicitly not adopted; see Section 1.)

## 8. Out-of-scope items (this milestone)

- Any real tax-authority adapter (TRA, KRA, ZRA, HMRC, IRS, or otherwise).
- Cryptographic signing of any kind (nothing to sign against without a real spec).
- Multi-currency support.
- A background-worker/queue process (out of proportion to this app's
  actual single-process deployment target — see Section 9 risk below;
  the retry mechanism will be designed to fit the existing
  request-time/synchronous architecture, e.g. lazy retry-on-next-relevant-
  request plus an operator-triggered retry endpoint, not a new daemon).
- Tax-inclusive pricing, multi-tax-per-line, or tax-category/exemption
  taxonomy beyond what already exists — no confirmed requirement calls
  for these, and inventing them would itself be speculative,
  jurisdiction-shaped functionality.
- Real receipt printing/PDF generation.

## 9. Implementation risks

1. **Scope creep into invented jurisdiction rules** — mitigated by Section 1's hard stop and the fake-adapter-only implementation rule.
2. **Building a background-worker system this deployment was never designed to run** (`docs/TECHNICAL_BLUEPRINT.md`'s own `worker` container was never built through M19) — mitigated by designing retry as synchronous, request-triggered reconciliation rather than a new long-running process, consistent with this codebase's actual architecture.
3. **A half-built integration boundary that can't actually be swapped in later without a rewrite** — mitigated by keeping the adapter interface narrow and the submission ledger's payload/response columns opaque (JSONB), exactly as the blueprint originally specified.
4. **Fiscal config accidentally becoming reachable/writable cross-store** — mitigated by reusing the same store-scoping + permission pattern audited in every prior milestone.
5. **Secrets leaking through audit logs, error responses, or the admin config GET endpoint** — mitigated by never returning credential material from any read endpoint (write-only reference, matching common secrets-manager UX) and reusing `core/logging.py`'s existing redaction list, extended with the new field names.

## Next step

Phase 1 (`M20_DESIGN.md`): design the jurisdiction-neutral fiscal
integration boundary — data model, state machine, provider interface,
fake adapter, admin configuration surface — scoped strictly to what
Sections 5–9 above support, with no jurisdiction-specific content.
