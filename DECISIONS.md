# Design Decisions — Employee Attendance & Analytics API

## 1. Which indexes did you create, and why?
A unique index on `employees.emp_code` enforces the business identifier even under concurrent creates. A compound `department, emp_code` index supports department-filtered employee lists sorted by code. `department, joined_on` helps department headcount and trend queries. On attendance logs, unique `emp_code, date` prevents duplicate employee/date records. `date, emp_code` supports date-range reports, while `status, date` supports status/date filters. Explain plans must still be checked against the actual queries and realistic data.

## 2. What happens when two identical punch-ins arrive at the same instant?
Both requests can initially observe no record, so an application pre-check is not enough. The unique attendance index is the final concurrency guard. One insert succeeds with 201; the duplicate-key error from the other request is translated to 409. The database guarantees uniqueness.

## 3. What does the leaderboard do for a tie at the cutoff?
Ranking is based on total late minutes. Equal totals share a rank and the next rank is skipped, for example 1, 2, 2, 4. The limit is applied after ranking, so tied employees at the cutoff are all included. Rows with equal totals are ordered by `emp_code` for deterministic output.

## 4. How are employees with no logs counted in department summary?
The summary starts from employees who joined on or before the last day of the requested month, then joins any matching attendance records. Eligible employees with no logs remain in headcount, while employees who joined after the period are excluded.

## 5. What would change at 100 times the data?
I would benchmark the real aggregation pipelines with execution statistics, inspect index selectivity and memory use, and align indexes with filters and sort order. For expensive recurring reports, I would consider pre-aggregated daily summaries with a defined refresh strategy and correctness checks against source records. I would add repeatable load and concurrency tests before changing the architecture.
