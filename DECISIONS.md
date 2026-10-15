# DECISIONS.md

## 1. Indexes

Five indexes are created at startup, all idempotent (`create_index` is a no-op when the index already exists):

- **`employees.emp_code` (unique)** — enforces uniqueness at the database level so concurrent `POST /employees` requests with the same code yield exactly one 201 and the rest 409 via `DuplicateKeyError`.
- **`employees.department`** — speeds up the department filter on `GET /employees` and the `$lookup` join in analytics pipelines.
- **`attendance_logs.(emp_code, date)` (unique)** — the natural key of a record; enforces the one-record-per-employee-per-day constraint and makes all point lookups (`PATCH`, punch-out, monthly summary) instant.
- **`attendance_logs.(date DESC, emp_code ASC)`** — matches the sort order of `GET /attendance` exactly, so MongoDB can satisfy the query with an IXSCAN + no in-memory sort.
- **`attendance_logs.(date ASC, emp_code ASC)`** — used by the analytics pipelines that filter on a date range and then group or join.

Considered and rejected: a separate index on `attendance_logs.status` alone. Status has low cardinality (5 values) and is almost always combined with a date range, so the compound date indexes already cover status-filtered queries better than a standalone status index would.

## 2. Punch-in race condition

Two identical punch-in requests arrive at the same instant. Both reach the handler, both look up the employee (read-only, no conflict), both compute the same attendance date and derive the same document. Both call `insert_one`. MongoDB's unique compound index on `(emp_code, date)` ensures only one insert succeeds; the second raises a `DuplicateKeyError` which the handler catches and converts to a 409. There is no application-level check-then-insert window — the database enforces uniqueness atomically.

## 3. Ties at the leaderboard cutoff

If two employees share the same `total_late_minutes` and that rank equals `limit`, both are included in the response. The pipeline uses MongoDB's `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), then filters `rank <= limit`. So if `limit=3` and positions 2 and 3 are tied, both appear (rank 2 each), and the employee who would have been rank 4 is excluded. The result set can therefore contain more rows than `limit`, which matches the contract.

## 4. Employees with zero logs in department summary

The aggregation starts from the `employees` collection, not from `attendance_logs`. A `$group` on `employees` with `joined_on <= last_day_of_month` produces one row per department with the correct headcount regardless of whether those employees have any logs. Log statistics are then computed separately and left-joined to the headcount result in Python. An employee with no logs simply contributes 0 to every log metric but still increments `headcount`.

## 5. One change for 100× the data

At 10 million attendance records the analytics pipelines' `$lookup` from logs back to employees becomes the bottleneck. I would denormalize `department` and `shift_start`/`shift_end` directly onto each attendance log at write time (punch-in and regularize). This eliminates the cross-collection join from every read path and makes the aggregation pipelines operate on a single collection with a selective index scan on `(department, date)`. The trade-off is a slightly larger document and an extra update if an employee's department changes, but department changes are rare in HR systems and the read performance gain at scale is substantial.
