"""Payroll module (docs/M10_DESIGN.md).

Deduction/contribution configuration, per-store payroll periods, and the
derived/posted payroll result (earnings, deductions, net pay), posted to
the existing `app.modules.accounting` journal — never a second ledger.
Depends on `app.modules.hr` for employee/compensation/attendance data,
never the other way around.
"""
