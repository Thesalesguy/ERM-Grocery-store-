"""HR / workforce module (docs/M10_DESIGN.md).

Employee master data plus effective-dated employment history (status,
store/department/position assignment, compensation) and attendance.
Deliberately separate from `app.modules.auth`: `Employee` is related to
`User` by an optional, nullable, unique one-to-one foreign key
(`Employee.user_id`) — not every paid employee needs system access, and
not every system account is a paid employee. See M10_DESIGN.md Section 1
for the full reasoning (security, payroll, audit, termination, rehire).

Payroll itself (calculation, periods, GL posting) lives in
`app.modules.payroll`, which depends on this module for employee/
compensation/attendance data but not vice versa.
"""
