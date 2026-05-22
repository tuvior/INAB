# Actual Budget Backend Analysis

Date: 2026-05-21

This document assesses whether INAB can support Actual Budget (AB) as an alternative import backend to YNAB using [`bvanelli/actualpy`](https://github.com/bvanelli/actualpy).

Assumption: INAB would talk to a hosted/self-hosted Actual Budget server. That is the natural deployment model for actualpy; it connects to the server, downloads or resumes a local copy of the selected budget, then syncs committed changes back to the server.

## Summary

Supporting Actual Budget is feasible, but it is not a drop-in replacement for the current YNAB client. The existing INAB workflow maps well to Actual Budget: parse bank exports locally, preview rows, apply local rules, map bank accounts to budget accounts, skip duplicates, import transactions, represent transfers, and undo an import. The main work is to introduce a backend-neutral budget gateway and move YNAB-specific naming out of storage, templates, and transaction payload generation.

The current operating model is:

- The active backend is selected in the Setup UI and persisted in `INAB_DATA_DIR/app.sqlite3`.
- Each backend gets separate local state by default, so YNAB and Actual rules, mappings, selected budget, import history, and undo IDs do not mix.
- Existing YNAB behavior remains the reference implementation.

Recommended verdict: implement an adapter layer first, then add Actual Budget behind the same gateway contract. Do not try to make Actual pretend to be YNAB internally.

A separate YNAB-to-Actual migration command is also feasible, but it should be treated as a migration product rather than a side effect of the importer. Category targets are the most delicate part: YNAB stores targets as structured category fields, while Actual Budget's nearest equivalent is the experimental budget-template feature stored as text lines in category notes.

## Current YNAB Coupling In INAB

The code already has a useful boundary in `src/inab/ynab_api.py`: `YnabGateway` exposes plans, accounts, categories, payees, existing transactions, create transactions, and delete transaction. Most web code uses this protocol rather than the official SDK directly.

However, YNAB names leak through the rest of the app:

- `Settings` only knows `ynab_access_token` and `ynab_configured`.
- `Store` persists `ynab_plan_id`, `ynab_plan_name`, `ynab_account_id`, `ynab_account_name`, and `transfer_payee_id`.
- `BankTransaction.to_ynab_payload()` emits YNAB-shaped transaction dictionaries.
- Templates and validation messages say YNAB throughout setup, rules, CSV upload, preview, history, and undo.
- Result payloads store fields such as `ynab_transactions`, `ynab_matched_count`, and `ynab_duplicate_import_ids`.

This means Actual Budget support should start with renaming the boundary conceptually:

- `BudgetBackend` or `BudgetGateway`, not `YnabGateway`.
- `BudgetPlan` or `BudgetFile`, not `YnabPlan`.
- `BudgetAccount`, `BudgetCategory`, `BudgetPayee`.
- `BudgetError`.
- Backend-specific transaction serialization inside adapters, not on `BankTransaction`.

## Actual Budget API Fit

Actual Budget's JavaScript API has direct equivalents for the INAB workflow:

- Transaction objects support `account`, `date`, `amount`, `payee_name`, `imported_payee`, `category`, `notes`, `imported_id`, and `transfer_id`.
- `imported_id` is the Actual equivalent of YNAB `import_id`; Actual documents it as the unique bank/import identifier used to avoid duplicates.
- `importTransactions(accountId, transactions, opts)` runs Actual's import path, including rules, duplicate reconciliation, and transfer creation when a transfer payee is used.
- `addTransactions` exists, but Actual explicitly says it does not avoid duplicates and is mainly for raw data.
- Payees include `transfer_acct`, which identifies transfer payees for account-to-account transfers.
- Categories and category groups are available, including hidden filtering.

Actual's own duplicate behavior is similar in spirit to YNAB's import behavior: it first checks the imported ID, then falls back to date/amount/payee matching. One important Actual API option is `reimportDeleted`; the API default is `true`, while the file import UI defaults to `false`. INAB should use `reimportDeleted=false` semantics for repeat bank imports so deleted transactions do not reappear unexpectedly.

## actualpy Fit

`actualpy` is a Python implementation for Actual server access. It is not just a thin REST client: it downloads the Actual budget into a local SQLite file, exposes SQLAlchemy models and query helpers, and syncs changes back to the server.

Because the target deployment includes an AB server, INAB does not need to import or manage standalone Actual backup files. It should use the server connection as the source of truth and treat `ACTUAL_DATA_DIR` only as actualpy's local cache for the selected remote budget.

Relevant actualpy capabilities:

- `Actual(base_url=..., password=..., file=..., data_dir=..., encryption_password=...)` opens a server budget.
- `download_budget()` downloads or resumes the local budget and applies sync changes.
- `commit()` sends pending local changes back to the Actual server. The docs call out that changes are only visible in the frontend after `commit()`, and that commit is not atomic if interrupted.
- `get_accounts`, `get_categories`, and `get_payees` provide lookup data for setup and rules.
- `get_transactions` can fetch existing transactions, filtered by date/account.
- `create_transaction` can create a transaction with `imported_id`, `cleared`, `category`, `notes`, and `imported_payee`.
- `reconcile_transaction` performs fuzzy matching against existing transactions and accepts `already_matched` to prevent duplicate matching within the same import batch.
- `create_transfer` creates linked transfer transactions directly.

actualpy stores Actual's imported ID in the transaction model field `financial_id` and the raw imported payee in `imported_description`. INAB can map:

| INAB / YNAB concept | Actual / actualpy equivalent |
| --- | --- |
| budget plan | Actual budget file |
| account id | Actual account id |
| category id | Actual category id |
| payee id | Actual payee id |
| transfer payee id | Actual payee with `transfer_acct`, or actualpy `create_transfer` |
| `import_id` | `imported_id` / `financial_id` |
| memo | `notes` |
| cleared `"cleared"` | `cleared=True` |
| approved false | no direct equivalent needed |
| created transaction id | Actual transaction id |
| delete transaction | mark transaction tombstoned and commit, or use a supported helper if actualpy adds one |

The biggest actualpy-specific design issue is sync lifecycle. A YNAB operation is an HTTP request. An Actual operation is: open/download local budget, query or modify local SQLite through actualpy, then `commit()` to sync changes. The adapter should open short-lived Actual sessions per web operation unless later profiling shows that session reuse is needed.

## Feature Parity

Expected parity is high for the user-facing workflow:

| INAB feature | Actual support | Notes |
| --- | --- | --- |
| CAMT/CSV parsing | Yes | Backend independent. |
| Preview before import | Yes | Existing INAB preview remains local. |
| Account mappings | Yes | Map IBAN/account key to Actual account IDs. |
| CSV target account selection | Yes | Select Actual account instead of YNAB account. |
| Local import rules | Yes | Keep INAB rules local and backend-specific. |
| Category assignment | Yes | Actual categories can be selected and assigned. |
| Payee preview | Yes | Actual payees can be listed. |
| Duplicate detection | Yes | Use `financial_id`/`imported_id`; keep INAB's explicit preview duplicate check. |
| Import IDs | Yes | INAB's `INAB:<ref-or-hash>` IDs can be sent as Actual `imported_id`. |
| Internal transfers | Yes | Either use transfer payees through Actual import behavior or actualpy `create_transfer`. |
| Undo import | Likely yes | Need implement delete/tombstone with actualpy and commit; verify in an integration test. |
| YNAB match counters | Partial | Actual can report added/updated via JS API, but actualpy reconciliation returns transaction objects; result fields should become generic. |

The main caveat is "the whole functionality" should mean the same INAB workflow, not identical YNAB result metadata. Some YNAB-specific diagnostics, such as matched transaction counts returned by YNAB's API response, should become backend-specific optional details.

## Configuration

Use the Setup UI to choose YNAB or Actual Budget. Backend-specific local state is stored under `INAB_DATA_DIR/<backend>/`.

YNAB settings stay separate:

```sh
YNAB_ACCESS_TOKEN="..."
```

Actual settings should be separate:

```sh
ACTUAL_BASE_URL="http://localhost:5006"
ACTUAL_PASSWORD="..."
ACTUAL_ENCRYPTION_PASSWORD=""   # only for encrypted budget files
ACTUAL_DATA_DIR="./data/actual-cache"
ACTUAL_VERIFY_SSL="true"
```

`ACTUAL_BASE_URL` points to the hosted AB server. `ACTUAL_DATA_DIR` is not INAB's rules/mappings database; it is the local actualpy cache of the remote Actual budget file.

Shared INAB settings:

```sh
INAB_USERNAME="inab"
INAB_PASSWORD="choose-a-password"
INAB_SESSION_SECRET="stable-cookie-signing-secret"
INAB_ROOT_PATH="/inab"
```

INAB accepts uploads up to 50 MiB and imports CHF statements. Own-name aliases are edited in Setup and stored with the active backend state.

For backend-local state, prefer separate SQLite databases:

```text
data/
  ynab/inab.sqlite3
  actual/inab.sqlite3
  actual-cache/
```

This is simpler and safer than adding a backend/profile column to every state table. It naturally keeps rules, mappings, selected budget, import jobs, undo IDs, observed accounts, dismissed suggestions, and counterparty labels separate. A future `INAB_PROFILE` could allow multiple Actual or YNAB configurations:

```text
data/{backend}-{profile}/inab.sqlite3
```

## Proposed Adapter Shape

Introduce a backend-neutral module, for example `src/inab/budget_api.py`:

```python
class BudgetGateway(Protocol):
    backend_name: str
    backend_label: str

    def list_budgets(self) -> list[BudgetRef]: ...
    def list_accounts(self, budget_id: str) -> list[BudgetAccount]: ...
    def list_categories(self, budget_id: str) -> list[BudgetCategory]: ...
    def list_payees(self, budget_id: str) -> list[BudgetPayee]: ...
    def existing_transactions(self, budget_id: str, account_id: str, since_date: date | None = None) -> list[ExistingTransaction]: ...
    def create_transactions(self, budget_id: str, transactions: list[ImportTransaction]) -> CreateTransactionsResult: ...
    def delete_transaction(self, budget_id: str, transaction_id: str) -> None: ...
```

Then:

- Move YNAB payload generation from `BankTransaction.to_ynab_payload()` into `OfficialYnabGateway`.
- Add an `ActualBudgetGateway` that converts `ImportTransaction` into actualpy calls.
- Keep parser, rules, preview, transfer detection, and reconciliation summaries independent of backend.
- Rename persisted store fields in code to generic names, or isolate old YNAB names behind store accessors if preserving the database schema short-term.

## YNAB Plan To Actual Migration

Actual already documents a native nYNAB migration path: export the full YNAB budget JSON, then import that JSON file in the Actual UI as `nYnab`. That should be the primary plan migration path unless it proves insufficient for the target/template use case. INAB should not start by reimplementing the entire historical migration stack.

The more useful INAB migration feature is a companion workflow:

- Help fetch the YNAB JSON from the API if the user wants.
- Analyze the YNAB JSON before import.
- Generate an explicit target-to-Actual-template conversion report.
- After the Actual native import, connect to the hosted Actual server and patch category notes with generated `#template` / `#goal` lines.
- Migrate INAB-specific local state, such as bank-account mappings and import rules, from the YNAB backend database to the Actual backend database.

A full API-driven YNAB-to-Actual migration remains feasible, but should be fallback work only if Actual's native import cannot preserve enough data.

### Wizard Flow

This migration should be exposed as a guided wizard in INAB rather than only as a CLI. The wizard can keep the high-risk write steps explicit while still making the migration approachable.

Recommended screens:

1. **Start**
   - Explain that INAB will export YNAB data, Actual's own importer will create the Actual budget, and INAB will then patch templates and local importer state.
   - Show prerequisites: `YNAB_ACCESS_TOKEN`, `ACTUAL_BASE_URL`, `ACTUAL_PASSWORD`, and an accessible Actual server.
   - Require a checkbox acknowledging that Actual Budget templates are experimental.

2. **Choose YNAB Plan**
   - List YNAB plans using the existing YNAB gateway.
   - Let the user pick the source plan.
   - Show plan name, first month, last month, and last modified date if available.

3. **Generate YNAB JSON**
   - Fetch the complete YNAB plan JSON from the YNAB API.
   - Store it temporarily in INAB's backend-specific migration workspace.
   - Show file size, category count, account count, transaction count, and detected target count.
   - Provide a download button for `ynab-export-<plan-name>-<date>.json`.
   - Tell the user this is the file Actual's native `nYnab` importer expects.

4. **Analyze Targets**
   - Parse YNAB category target fields from the JSON.
   - Show the proposed Actual note output for every category with a target.
   - Group rows by conversion confidence: exact, approximate, needs review, unsupported.
   - Let the user disable individual generated template lines or mark them as comment-only suggestions.
   - Surface review markers as explicit wizard feedback, not only as text hidden in generated notes.
   - Show counts for active templates, comment-only suggestions, unsupported targets, and unresolved review items.
   - Require a user decision for every `needs review` item: accept active template, keep as comment-only suggestion, edit generated template text, or skip.
   - Block the "continue" path only for parser errors, not for low-confidence conversions.

5. **Import In Actual**
   - Show concise step-by-step instructions based on Actual's documented flow:
     - Open Actual.
     - Close the current file if needed.
     - Select `Import file`.
     - Select `nYnab`.
     - Upload the JSON downloaded from INAB.
   - Include a button/link to the configured `ACTUAL_BASE_URL`.
   - Ask the user to return to INAB after the Actual import has completed.

6. **Connect To Imported Actual Budget**
   - Connect to the hosted Actual server through actualpy.
   - List Actual budget files.
   - Ask the user to select the newly imported budget.
   - Download/sync the selected budget into `ACTUAL_DATA_DIR`.
   - Show imported account/category counts and any Actual duplicate category/group renames detected.

7. **Match Categories**
   - Match YNAB categories to Actual categories.
   - Prefer stable source metadata if Actual's importer preserves it; otherwise match by `category group name + category name`.
   - Present ambiguous or missing matches for manual selection.
   - Save the category match map in the migration workspace so the wizard can be resumed.

8. **Patch Budget Templates**
   - Show a pre-apply review summary with all categories that still have review markers.
   - Require confirmation if any review marker will be written as comment-only text instead of an active template.
   - Append or replace an INAB-delimited block in each matched Actual category note.
   - Preserve existing notes.
   - Use active `#template` / `#goal` lines only for accepted conversions.
   - Use comment-only suggestions for uncertain conversions.
   - Commit changes to the Actual server.
   - Show a final list of patched categories.

9. **Migrate INAB State**
   - Copy INAB account mappings, import rules, self-name aliases, counterparty labels, and dismissed suggestions from the YNAB backend database to the Actual backend database.
   - Remap stored category IDs in rules using the category match map.
   - Remap stored account IDs in bank-account mappings using a user-confirmed account match map.
   - Do not copy import job history by default; offer it as read-only archival history if needed.

10. **Verify**
   - Ask the user to run `Check templates` in Actual's budget template menu.
   - Show post-apply feedback for review markers: which categories were patched with active templates, which were patched with comment-only suggestions, which were skipped, and which still need manual work in Actual.
   - Show cleanup reminders from Actual's nYNAB docs: credit-card debt, `To Budget` / future-month money, and duplicate categories/groups.
   - Offer to switch INAB startup configuration guidance from YNAB to Actual.

The wizard should be resumable. A migration state record should track:

- source YNAB plan ID and name
- JSON export path/checksum
- target conversion decisions
- unresolved/reviewed target markers
- selected Actual budget file
- category match map
- account match map
- note patch status
- INAB state migration status

Recommended shape:

```sh
uv run inab-migrate-ynab-to-actual analyze \
  --ynab-plan-id "..." \
  --output ynab-migration-report.md

uv run inab-migrate-ynab-to-actual apply-templates \
  --ynab-json budget.json \
  --actual-budget "Household"
```

The workflow should default to analysis only, produce a detailed report, then require an explicit apply step for Actual category-note updates. It should be idempotent by using delimited INAB blocks in category notes.

### Migration Scope

For the native-Actual-import-first path, INAB should handle:

1. YNAB target extraction from the JSON/API response.
2. Category matching between YNAB categories and Actual-imported categories.
3. Category-note patching with generated template lines.
4. Template validation/reporting.
5. INAB local state migration from `data/ynab/inab.sqlite3` to `data/actual/inab.sqlite3`.

Actual's own cleanup notes should still be surfaced in INAB's migration report: credit-card debt may need manual handling, YNAB's `Ready to Assign` and Actual's `To Budget` can differ for future-month money, and duplicate category/group names may be auto-renamed by Actual.

For a full custom migration fallback, the migration would copy, in this order:

1. Category groups and categories.
2. Category notes, with generated Actual budget templates appended.
3. Accounts, including closed/off-budget status where Actual has an equivalent.
4. Payees and transfer payees.
5. Historical transactions, preserving dates, amounts, payees, categories, memos/notes, cleared status, and transfer links.
6. Scheduled transactions, where Actual can represent them.
7. Historical monthly budget amounts, where desired, using actualpy budget helpers.
8. Importer state for INAB, such as bank-account mappings, if the user wants INAB to point at the new Actual budget after migration.

Actual budget creation through actualpy is documented as not recommended because migrations can include JavaScript logic that actualpy cannot perfectly reproduce. For the fallback custom path, create an empty Actual budget through the Actual UI first, then let INAB populate that existing budget on the hosted server.

### Target To Template Conversion

This needs careful handling. YNAB targets are structured fields on categories. Actual Budget's comparable feature is "Budget Templates", an experimental feature where each template is a single `#template` or `#goal` line in the category note. Actual documents that all template lines must be single lines, amounts must not include currency symbols or thousands separators, and decimal commas are not supported.

Actual also warns that templates are experimental and may require experimental/nightly features. So the migration should never silently discard YNAB target data after generating a note macro. It should append a migration block like this to each category note:

```text
YNAB target migrated by INAB:
#template 120

Original YNAB target:
goal_type=MF
goal_target=120000
goal_target_currency=120.00
goal_target_date=
goal_cadence=
goal_cadence_frequency=
goal_day=
goal_needs_whole_amount=
```

Use generated `#template`/`#goal` lines for Actual behavior, and keep the original YNAB fields for audit and manual correction.

Suggested conversion table:

| YNAB target | YNAB API signal | Actual note output | Confidence |
| --- | --- | --- | --- |
| Monthly set-aside / monthly savings builder | `goal_type=MF` | `#template <amount>` | High |
| Have a balance of amount, no date | `goal_type=TB` | `#goal <amount>` | Medium |
| Have a balance by date | `goal_type=TBD`, `goal_target_date` | `#template <amount> by <YYYY-MM>` plus optional `#goal <amount>` | Medium-high |
| Needed for spending, refill up to | `goal_type=NEED`, `goal_needs_whole_amount=false` | `#template up to <amount>` for monthly targets | Medium |
| Needed for spending, set aside another | `goal_type=NEED`, `goal_needs_whole_amount=true` | `#template <amount>` for monthly targets | Medium-high |
| Weekly recurring target | cadence/frequency/day fields | `#template <amount> repeat every week starting <YYYY-MM-DD>` | Medium |
| Every N weeks/months/years | cadence/frequency/date fields | `#template <amount> repeat every <N> <period> starting <YYYY-MM-DD>` or `#template <amount> by <YYYY-MM> repeat every <period>` | Medium |
| Spend-by date with allowed spending period | YNAB target date/cadence fields | `#template <amount> by <YYYY-MM> spend from <YYYY-MM>` only when period can be inferred | Low-medium |
| Credit card payoff targets | credit-card payment category target fields | Manual-review block; optionally generate `#template <amount>` or `#template <amount> by <YYYY-MM>` | Low |

The migration should prefer conservative conversions. When target fields cannot be mapped exactly, generate no active template or prefix it with a review marker rather than creating misleading automation. For example:

```text
YNAB target needs review:
; suggested template: #template 600 by 2026-12 repeat every year
Original YNAB target: ...
```

Actual template notes can contain ordinary explanatory text, but only `#template` and `#goal` lines drive automation. A commented suggestion convention such as `; suggested template:` keeps uncertain conversions visible without running them.

### Target Amount Conversion

YNAB stores money in milliunits. Actual template notes expect decimal currency text. The migration should convert:

```text
goal_target=123450 -> 123.45
goal_target=120000 -> 120
```

Formatting rules for generated template amounts:

- Use `.` as the decimal separator.
- Do not include a currency symbol.
- Do not include thousands separators.
- Drop trailing `.00` for whole amounts.
- Keep two decimals when cents are present.

### Due Dates And Repeats

Actual's `by` template accepts `YYYY-MM`, while periodic templates use `starting YYYY-MM-DD`. YNAB now exposes `goal_target_date`; older `goal_target_month` should be treated as a fallback only.

The migration should normalize dates as follows:

- `goal_target_date=2026-12-15` for a date-based savings target becomes `by 2026-12`.
- Monthly targets with a day-of-month can either ignore the day or use a periodic template starting on that day. The safer default is to preserve the day in the audit block and use monthly template behavior.
- Weekly targets need an actual start date, not just a weekday. Pick the first matching weekday on or after `goal_creation_month`, and record that derived date in the migration report.

### Category Notes

actualpy exposes writable category notes, so generated templates can be appended without replacing user notes. The migration should:

- Preserve existing Actual category notes when re-running.
- Preserve YNAB category notes if the YNAB API/export exposes them.
- Put generated templates in a delimited block so they can be updated idempotently.
- Never create more than one active `up to` template in a category, because Actual documents that only one `up to` template is allowed.

Example:

```text
Existing note text...

<!-- INAB:YNAB_TARGET_START -->
#template up to 500
#goal 500
Original YNAB target: goal_type=NEED, goal_target=500000, goal_needs_whole_amount=false
<!-- INAB:YNAB_TARGET_END -->
```

### Migration Validation

The dry-run report should include:

- Number of categories with exact, approximate, and manual-review target conversions.
- Every generated template line.
- Every category where no active template was generated.
- Categories with multiple `up to` candidates.
- Categories whose YNAB target depends on fields not available from the API response.
- A before/after sample budget month showing whether generated templates ask for roughly the expected amount.

After applying, the migration should call Actual's template syntax check if actualpy or the API exposes it. If not, it should at least run a local parser/validator for the generated syntax subset and tell the user to run "Check templates" in the Actual UI.

### Relationship To Actual's Native Import

Actual's documented nYNAB import is the lowest-risk way to move accounts, categories, transactions, budget history, and most core budget structure. INAB should initially avoid competing with that importer.

The first migration feature should therefore be:

1. Read the same YNAB JSON export that Actual imports.
2. Let the user run Actual's native import.
3. Connect to the imported Actual budget on the hosted AB server.
4. Match categories by stable data where possible; if Actual preserved YNAB IDs in import metadata, use those, otherwise match by group/category path and report ambiguities.
5. Patch category notes with target-derived templates.
6. Copy INAB runtime state into the Actual backend profile.

This keeps the difficult target/template conversion under INAB control without taking ownership of every edge case in historical budget migration.

## Actual Import Strategy

For normal transactions, the Actual adapter should initially prefer actualpy's `reconcile_transaction` over `create_transaction` because it matches the import semantics INAB needs. Pass:

- `date=booking_date`
- `account=<Actual account object>`
- `payee=payee`
- `notes=memo`
- `category=<Actual category object or id/name>`
- `amount=Decimal amount`
- `imported_id=INAB import_id`
- `cleared=True`
- `imported_payee=original imported/bank payee`
- `already_matched=<transactions added/updated earlier in this same batch>`

For duplicate preview, query existing Actual transactions with `get_transactions(start_date=since_date, account=account)` and compare `financial_id` against INAB's `import_id` and legacy import IDs. That preserves INAB's explicit preview behavior even if Actual's reconciliation would also skip duplicates.

For transfers, there are two viable paths:

1. Use actualpy `create_transfer(source_account, dest_account, amount=abs(amount), notes=...)` for accepted transfer pairs. This is direct and guarantees linked transactions, but INAB must set imported IDs on both created transaction rows for future duplicate detection.
2. Use Actual transfer payees and `reconcile_transaction`/import semantics. This is closer to the official Actual API behavior, but actualpy's transfer-payee path needs verification for imported IDs on both sides.

Recommended initial path: use `create_transfer`, then set `financial_id` on the two returned transactions to the debit and credit INAB import IDs, then commit. Add an integration test against a disposable Actual budget before relying on this in production.

## Implementation Plan

1. Create backend-neutral dataclasses and `BudgetGateway`.
2. Move `OfficialYnabGateway` behind that protocol with no behavior change.
3. Add backend-specific `Settings` fields.
4. Change store path selection to `INAB_DATA_DIR/<backend>/inab.sqlite3`.
5. Rename UI labels and error messages to use `gateway.backend_label` instead of hard-coded YNAB where possible.
6. Replace `BankTransaction.to_ynab_payload()` with backend-neutral import transaction construction.
7. Add `ActualBudgetGateway` using actualpy.
8. Add unit tests with a fake generic gateway, plus actualpy adapter tests isolated behind mocks.
9. Add an optional integration test that runs against a local Actual server and a disposable budget.

## Risks And Open Questions

- actualpy's local-sync workflow means failed imports can leave local changes committed but not synced, or synced partially if interrupted. The adapter should create all rows, `commit()` once, and surface failures clearly.
- YNAB-to-Actual migration needs extra safeguards. It should require a dry-run report before writing and should not run against a non-empty Actual budget unless the user explicitly allows merging.
- Undo needs concrete verification. If actualpy has no public delete helper, setting Actual transaction tombstones directly may be necessary and should be tested carefully.
- Actual's own rules can run during import depending on the path chosen. INAB should avoid double-rule behavior by either using local INAB rules only, or documenting that Actual rules may also apply when using an import/reconcile path.
- Category handling by ID should be verified. actualpy helpers can auto-create categories by name; INAB should avoid unintended category creation when a stored category ID is stale.
- Budget templates are experimental Actual functionality stored in category notes. Target migration should be version-gated and reviewable, not assumed to be perfect.
- Actual encrypted budgets require `ACTUAL_ENCRYPTION_PASSWORD`; without it, `download_budget()` fails.
- Long-lived Actual sessions may race with browser/frontend edits. Short-lived sessions that sync before each operation are safer initially.

## Sources

- actualpy project README: <https://github.com/bvanelli/actualpy>
- actualpy quickstart: <https://actualpy.readthedocs.io/en/latest/>
- actualpy `Actual` API reference: <https://actualpy.readthedocs.io/en/latest/API-reference/actual/>
- actualpy queries API reference: <https://actualpy.readthedocs.io/en/latest/API-reference/queries/>
- actualpy custom CSV import example: <https://actualpy.readthedocs.io/en/latest/examples/importing-a-custom-csv-file/>
- actualpy database models: <https://actualpy.readthedocs.io/en/stable/API-reference/models/>
- Actual Budget API reference: <https://actualbudget.org/docs/api/reference/>
- Actual Budget importing docs: <https://actualbudget.org/docs/transactions/importing/>
- Actual Budget transfers docs: <https://actualbudget.org/docs/transactions/transfers/>
- Actual Budget budget templates: <https://actualbudget.org/docs/experimental/goal-templates/>
- Actual Budget nYNAB migration: <https://actualbudget.org/docs/migration/nynab/>
- YNAB API changelog/category target fields: <https://api.ynab.com/>
