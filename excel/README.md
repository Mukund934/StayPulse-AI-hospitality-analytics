# Excel / Power Query workflow

The scenario this solves is the one that actually happens: **a new operational export
lands in a folder every morning**, and somebody rebuilds the same sheet by hand. The
answer is a folder-connector query that refreshes with one click — not a macro.

The workflow demonstrated here is:

```
  daily CSV exports in a folder
        ↓
  Power Query — combine, type, clean, flag defects
        ↓
  validated table loaded to the model
        ↓
  pivot / KPI sheet that refreshes with Data → Refresh All
```

Generate the input files first:

```bash
python scripts/export_excel_feed.py
```

That writes daily-partitioned CSVs to `excel/feed/` — deliberately partitioned, so
the folder connector has something real to combine rather than one tidy file that
would make the exercise pointless.

---

## Build it

### 1. Connect to the folder

**Data → Get Data → From File → From Folder** → select `excel/feed` →
**Transform Data** (not *Combine* — combining immediately hides the step where the
sample-file transform is defined, which is the part worth understanding).

### 2. Filter to the files you want

In the query editor, filter `Extension` to `.csv`. Without this, a stray
`.xlsx`, a `~$` lock file or a `.DS_Store` breaks the combine at the worst
possible moment — the morning it runs unattended.

### 3. Define the sample-file transform

Click the **Combine Files** icon on the `Content` column. Power Query creates a
`Transform Sample File` query. **Every step you add there applies to all files.**
That is the whole point of the pattern: the transform is written once and each new
day's file inherits it.

### 4. Type the columns explicitly

Set types by hand rather than accepting the auto-detected ones:

| Column | Type | Why explicitly |
|---|---|---|
| `stay_date` | Date | Auto-detection is locale-sensitive — `03/04` is 3 April or 4 March depending on the machine. A pipeline that changes answer by laptop is not a pipeline. |
| `property_code` | Text | |
| `rooms_available`, `rooms_sold` | Whole Number | |
| `room_revenue_net_inr` | Decimal Number | Not Currency — Currency rounds to 4 dp and silently loses precision on aggregation. |
| `occupancy_pct`, `adr_inr`, `revpar_inr` | Decimal Number | |

Delete the auto-generated `Changed Type` step and add your own so the locale
assumption is visible in the query rather than inherited.

### 5. Clean and flag — do not silently repair

Add these steps. The principle throughout: a defect gets **flagged and counted**, not
quietly fixed, because a silent repair destroys the evidence that the source is broken.

```
1  Trim + Clean on property_code           — strips whitespace and control chars
2  Uppercase property_code                  — 'blr-btm' and 'BLR-BTM' are one property
3  Remove blank rows
4  Custom column  is_valid_occupancy =
       [rooms_sold] <= [rooms_available]
5  Custom column  has_revenue_without_sales =
       [rooms_sold] = 0 and [room_revenue_net_inr] > 0
6  Custom column  recomputed_revpar =
       if [rooms_available] = 0 then null
       else Number.Round([room_revenue_net_inr] / [rooms_available], 2)
7  Custom column  revpar_matches =
       [recomputed_revpar] = Number.Round([revpar_inr], 2)
```

Steps 6–7 are the important pair: **Power Query independently recomputes RevPAR and
compares it to the exported value.** If they ever disagree, either the export or the
warehouse is wrong, and the sheet says so instead of averaging over it.

### 6. Remove the source-path column

Keep `Source.Name` if you want per-file traceability; delete the full folder path.
Shipping `C:\Users\<name>\...` inside a workbook you send to someone is a small leak
that costs nothing to avoid.

### 7. Load

**Close & Load To… → Only Create Connection** for the raw query, then load the cleaned
result as a **Table** on a `data` sheet. Loading a raw query straight onto a sheet is
what makes workbooks slow and hard to change.

---

## The KPI sheet

On a separate sheet, build a PivotTable from the loaded table:

- **Rows:** `property_code`
- **Values:** `rooms_available` (Sum), `rooms_sold` (Sum), `room_revenue_net_inr` (Sum)
- **Calculated fields** — computed from the sums, *never* averaged from the daily rates:

  ```
  Occupancy % = rooms_sold / rooms_available
  ADR         = room_revenue_net_inr / rooms_sold
  RevPAR      = room_revenue_net_inr / rooms_available
  ```

### The trap this avoids

`AVERAGE(adr_inr)` over daily rows is **not** ADR. ADR is
`SUM(revenue) / SUM(rooms_sold)` — a weighted figure. Averaging daily ADRs weights a
2-unit night the same as a 38-unit night. On this dataset the two differ by a visible
margin, and it is the single most common Excel error in hospitality reporting.

Add a validation block beside the pivot:

```
=SUMPRODUCT(--(tbl[revpar_matches]=FALSE))     → must be 0
=SUMPRODUCT(--(tbl[is_valid_occupancy]=FALSE)) → must be 0
=COUNTA(tbl[stay_date])                        → row count, reconcile to source
```

A number in a red cell beats a number nobody checked.

---

## Refreshing

Drop tomorrow's CSV into `excel/feed/` and hit **Data → Refresh All**. The folder
connector picks it up, the sample transform applies, the pivot and the validation
block update. Nothing is rebuilt by hand.

That is the whole claim: *nobody rebuilds the same sheet every morning.*

---

## Why Power Query and not a macro

- Refreshes without enabling macros, so the workbook stays shareable and does not trip
  security policy.
- Steps are declarative and inspectable — a reviewer reads the Applied Steps list
  instead of reverse-engineering VBA.
- The same M transformations lift directly into Power BI, which uses the same engine.
  Build once, reuse in both.
- **Merge → Left Anti Join** is the fastest reconciliation tool in Excel: point it at
  bookings and payments and it returns exactly the bookings with no matching payment.

---

## Status

The M steps, the transform order, the validation formulas and the anti-pattern above
are all specified. **The `.xlsx` is not committed** — a binary workbook in git is a
poor artifact (undiffable, and it grows the repo on every save), and the value here is
the reproducible method rather than one saved file. The build takes about 15 minutes
from the generated feed.
