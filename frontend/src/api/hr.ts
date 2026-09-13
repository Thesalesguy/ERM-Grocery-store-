import { apiFetch, buildQuery } from './client'

export interface Department {
  id: number
  name: string
  is_active: boolean
}

export interface Position {
  id: number
  title: string
  department_id: number | null
  is_active: boolean
}

export interface Employee {
  id: number
  employee_number: string
  legal_name: string
  display_name: string | null
  hire_date: string
  user_id: number | null
}

export interface EmploymentStatusPeriod {
  id: number
  employee_id: number
  status: 'ACTIVE' | 'ON_LEAVE' | 'SUSPENDED' | 'TERMINATED'
  effective_from: string
  effective_to: string | null
  reason: string | null
  actor_id: number | null
}

export interface EmploymentAssignment {
  id: number
  employee_id: number
  store_id: number
  department_id: number | null
  position_id: number | null
  manager_employee_id: number | null
  effective_from: string
  effective_to: string | null
}

export interface CompensationPeriod {
  id: number
  employee_id: number
  pay_type: 'HOURLY' | 'SALARY'
  rate: string
  pay_frequency: string
  overtime_eligible: boolean
  currency: string
  effective_from: string
  effective_to: string | null
}

export interface EmployeeDetail {
  employee: Employee
  current_status: EmploymentStatusPeriod | null
  current_assignment: EmploymentAssignment | null
  current_compensation: CompensationPeriod | null
}

export interface AttendanceRecord {
  id: number
  employee_id: number
  store_id: number
  work_date: string
  clock_in_at: string
  clock_out_at: string | null
  source: 'CLOCK' | 'MANUAL'
  status: 'OPEN' | 'CLOSED' | 'VOIDED'
  correction_of_id: number | null
  correction_reason: string | null
  corrected_by: number | null
}

// --- Departments / Positions -------------------------------------------------

export function listDepartments(): Promise<Department[]> {
  return apiFetch<Department[]>('/api/v1/hr/departments')
}

export function createDepartment(name: string): Promise<Department> {
  return apiFetch<Department>('/api/v1/hr/departments', {
    method: 'POST',
    body: JSON.stringify({ name }),
  })
}

export function listPositions(): Promise<Position[]> {
  return apiFetch<Position[]>('/api/v1/hr/positions')
}

// --- Employees ---------------------------------------------------------------

export interface HireEmployeeInput {
  employee_number: string
  legal_name: string
  display_name?: string
  hire_date: string
  store_id: number
  department_id?: number
  position_id?: number
  manager_employee_id?: number
}

export function listEmployees(): Promise<Employee[]> {
  return apiFetch<Employee[]>('/api/v1/hr/employees')
}

export function hireEmployee(input: HireEmployeeInput): Promise<Employee> {
  return apiFetch<Employee>('/api/v1/hr/employees', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function getEmployeeDetail(employeeId: number): Promise<EmployeeDetail> {
  return apiFetch<EmployeeDetail>(`/api/v1/hr/employees/${employeeId}`)
}

export function changeEmploymentStatus(
  employeeId: number,
  input: { new_status: string; effective_from: string; reason?: string },
): Promise<EmploymentStatusPeriod> {
  return apiFetch<EmploymentStatusPeriod>(`/api/v1/hr/employees/${employeeId}/status`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

// --- Compensation (hr.compensation.write only) -------------------------

export function getCompensationHistory(employeeId: number): Promise<CompensationPeriod[]> {
  return apiFetch<CompensationPeriod[]>(`/api/v1/hr/employees/${employeeId}/compensation-history`)
}

export function changeCompensation(
  employeeId: number,
  input: {
    effective_from: string
    pay_type: string
    rate: string
    pay_frequency: string
    overtime_eligible?: boolean
    currency?: string
  },
): Promise<CompensationPeriod> {
  return apiFetch<CompensationPeriod>(`/api/v1/hr/employees/${employeeId}/compensation`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

// --- Attendance ----------------------------------------------------------

export function clockIn(input: {
  employee_id: number
  store_id: number
  clock_in_at: string
  source?: string
}): Promise<AttendanceRecord> {
  return apiFetch<AttendanceRecord>('/api/v1/hr/attendance/clock-in', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function clockOut(
  attendanceRecordId: number,
  clockOutAt: string,
): Promise<AttendanceRecord> {
  return apiFetch<AttendanceRecord>(`/api/v1/hr/attendance/${attendanceRecordId}/clock-out`, {
    method: 'POST',
    body: JSON.stringify({ clock_out_at: clockOutAt }),
  })
}

export function listAttendanceRecords(
  params: { employee_id?: number; include_voided?: boolean } = {},
): Promise<AttendanceRecord[]> {
  return apiFetch<AttendanceRecord[]>(`/api/v1/hr/attendance${buildQuery(params)}`)
}
