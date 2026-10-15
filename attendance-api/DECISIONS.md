# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicate guard, every lookup), `(department, emp_code)` (listing, trend), `(department, joined_on)` and `(joined_on)` (headcount filter). `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee queries, the join key) and `(date desc, emp_code)` (global listing sort, month scans, daily trend lookups). Rejected: an index on `status`. It is low-selectivity and costs every write; status filters run inside already-indexed scans.

2. **Punch-in race.** Both requests validate, then call `insert_one` with the same `(emp_code, date)`. The unique index lets exactly one write commit; the other gets `DuplicateKeyError`, which becomes 409. There is no read before the write to race against.

3. **Ties.** Window `$rank` over total late minutes gives 1, 2, 2, 4. I filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned (more than `limit` rows), ordered by minutes then `emp_code`.

4. **Headcount.** The summary pipeline starts from `employees` (joined by month end), `$lookup`s their logs and `$unwind`s with `preserveNullAndEmptyArrays`, so an employee with no logs survives as one row and is counted once.

5. **At 100x.** Keep per-day, per-department rollups updated on punch and correction, so analytics read small summary documents instead of scanning raw logs.
