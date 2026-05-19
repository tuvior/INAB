from __future__ import annotations

import secrets
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .camt import CamtError, parse_upload
from .config import Settings
from .models import BankTransaction, ParseResult, normalize_whitespace, truncate
from .rules import RuleError, apply_rules, evaluate_transaction, validate_rule_input
from .store import AccountMapping, Store
from .transfers import detect_transfer_pairs
from .ynab_api import OfficialYnabGateway, YnabAccount, YnabCategory, YnabError, YnabGateway, YnabPayee

GatewayFactory = Callable[[Settings], YnabGateway]

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
templates.env.filters["money"] = lambda value: format_money(value)
templates.env.globals["category_choice"] = lambda category: _category_choice(category)


class LoginRequired(Exception):
    pass


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    gateway_factory: GatewayFactory | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = store or Store(settings.database_path)
    gateway_factory = gateway_factory or _default_gateway_factory

    app = FastAPI(title="INAB", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.gateway_factory = gateway_factory
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax")
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/") -> Any:
        return _render(request, "login.html", {"next": next})

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")) -> Any:
        if not settings.auth_configured:
            return _render(request, "login.html", {"error": "INAB_USERNAME and INAB_PASSWORD must be configured.", "next": next})
        username_ok = secrets.compare_digest(username, settings.username or "")
        password_ok = secrets.compare_digest(password, settings.password or "")
        if not (username_ok and password_ok):
            return _render(request, "login.html", {"error": "Invalid username or password.", "next": next}, status_code=401)
        request.session["authenticated"] = True
        return RedirectResponse(_safe_next(next), status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(_require_auth)])
    async def index(request: Request) -> Any:
        selected_plan_id, selected_plan_name = store.selected_plan()
        return _render(
            request,
            "index.html",
            {
                "selected_plan_id": selected_plan_id,
                "selected_plan_name": selected_plan_name,
                "mappings": store.list_mappings(),
                "observed_accounts": store.list_observed_accounts(),
                "ynab_configured": settings.ynab_configured,
            },
        )

    @get_or_post_setup(app)
    async def setup(request: Request) -> Any:
        _require_auth(request)
        post_error = None
        if request.method == "POST":
            form = await request.form()
            action = str(form.get("action") or "")
            try:
                if action == "plan":
                    _save_plan(store, gateway_factory(settings), str(form.get("plan_id") or ""))
                    return RedirectResponse("/setup", status_code=303)
                if action == "mapping":
                    _save_mapping(
                        store,
                        gateway_factory(settings),
                        iban=str(form.get("iban") or ""),
                        account_id=str(form.get("ynab_account_id") or ""),
                    )
                    return RedirectResponse("/setup", status_code=303)
                if action == "delete_mapping":
                    store.delete_mapping(str(form.get("iban") or ""))
                    return RedirectResponse("/setup", status_code=303)
                if action == "dismiss_observed_account":
                    store.dismiss_observed_account(str(form.get("iban") or ""))
                    return RedirectResponse("/setup", status_code=303)
                if action == "self_names":
                    _save_self_names(store, str(form.get("self_names") or ""))
                    return RedirectResponse("/setup", status_code=303)
                if action == "counterparty_mapping":
                    _save_counterparty_mapping(
                        store,
                        iban=str(form.get("iban") or ""),
                        label=str(form.get("label") or ""),
                    )
                    return RedirectResponse("/setup", status_code=303)
                if action == "delete_counterparty_mapping":
                    store.delete_counterparty_mapping(str(form.get("iban") or ""))
                    return RedirectResponse("/setup", status_code=303)
                if action == "dismiss_observed_counterparty":
                    store.dismiss_observed_counterparty_account(str(form.get("iban") or ""))
                    return RedirectResponse("/setup", status_code=303)
                if action == "csv_account":
                    _save_csv_account_iban(store, str(form.get("csv_account_iban") or ""))
                    return RedirectResponse("/setup", status_code=303)
            except YnabError as exc:
                post_error = str(exc)
            except HTTPException as exc:
                post_error = str(exc.detail)

        self_names = _effective_self_names(store, settings)
        mappings = store.list_mappings()
        counterparty_mappings = store.list_counterparty_mappings()
        mapped_ibans = {mapping.iban for mapping in mappings}
        mapped_counterparty_ibans = {mapping.iban for mapping in counterparty_mappings}
        dismissed_observed = store.dismissed_observed_account_ibans()
        dismissed_counterparties = store.dismissed_observed_counterparty_ibans()
        observed_accounts = store.list_observed_accounts()
        observed_counterparty_accounts = store.list_observed_counterparty_accounts()
        context: dict[str, Any] = {
            "plans": [],
            "accounts": [],
            "observed_accounts": observed_accounts,
            "observed_account_suggestions": [
                observed for observed in observed_accounts if observed["iban"] not in mapped_ibans and observed["iban"] not in dismissed_observed
            ],
            "observed_counterparty_accounts": observed_counterparty_accounts,
            "observed_counterparty_suggestions": [
                observed
                for observed in observed_counterparty_accounts
                if observed["iban"] not in mapped_counterparty_ibans and observed["iban"] not in dismissed_counterparties
            ],
            "mappings": mappings,
            "counterparty_mappings": counterparty_mappings,
            "selected_plan_id": store.selected_plan()[0],
            "selected_plan_name": store.selected_plan()[1],
            "self_names": ", ".join(self_names),
            "csv_account_iban": _effective_csv_account_iban(store, settings) or "",
            "ynab_configured": settings.ynab_configured,
            "error": post_error,
        }
        if settings.ynab_configured:
            try:
                gateway = gateway_factory(settings)
                context["plans"] = gateway.list_plans()
                if context["selected_plan_id"]:
                    context["accounts"] = _active_accounts(gateway.list_accounts(context["selected_plan_id"]))
            except YnabError as exc:
                context["error"] = str(exc)
        return _render(request, "setup.html", context)

    @app.post("/uploads", dependencies=[Depends(_require_auth)])
    async def upload(request: Request, file: UploadFile = File(...)) -> Any:
        filename = file.filename or "upload"
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload is too large.")
        try:
            parsed = parse_upload(
                filename,
                content,
                target_currency=settings.target_currency,
                csv_account_iban=_effective_csv_account_iban(store, settings),
            )
        except CamtError as exc:
            payload = {
                "filename": filename,
                "errors": [str(exc)],
                "missing_ibans": [],
                "statements": [],
                "rows": [],
                "transfers": [],
                "parse_result": None,
            }
            job_id = store.create_job(filename=filename, status="blocked", plan_id=store.selected_plan()[0], payload=payload)
            return RedirectResponse(f"/imports/{job_id}", status_code=303)

        for statement in parsed.statements:
            store.observe_account(
                iban=statement.iban,
                currency=statement.currency,
                owner_name=statement.owner_name,
                bank_name=statement.bank_name,
            )
        _observe_counterparty_accounts(store, parsed)
        _apply_counterparty_account_labels(parsed, store=store, settings=settings)
        apply_rules(parsed.transactions, store.list_rules(enabled_only=True))

        plan_id, plan_name = store.selected_plan()
        payload, status = _build_preview_payload(
            parsed,
            store=store,
            settings=settings,
            gateway_factory=gateway_factory,
            plan_id=plan_id,
            plan_name=plan_name,
            filename=filename,
        )
        job_id = store.create_job(filename=filename, status=status, plan_id=plan_id, payload=payload)
        return RedirectResponse(f"/imports/{job_id}", status_code=303)

    @app.get("/imports/{job_id}", response_class=HTMLResponse, dependencies=[Depends(_require_auth)])
    async def import_preview(request: Request, job_id: str) -> Any:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found.")
        return _render(request, "preview.html", {"job": job})

    @get_or_post_rules(app)
    async def rules(request: Request) -> Any:
        _require_auth(request)
        post_error = None
        if request.method == "POST":
            form = await request.form()
            action = str(form.get("action") or "")
            try:
                if action == "create_rule":
                    _create_rule_from_form(store, form)
                    return RedirectResponse("/rules", status_code=303)
                if action == "update_rule":
                    _update_rule_from_form(store, form)
                    return RedirectResponse("/rules", status_code=303)
                if action == "delete_rule":
                    store.delete_rule(str(form.get("rule_id") or ""))
                    return RedirectResponse("/rules", status_code=303)
                if action == "move_rule":
                    store.move_rule(str(form.get("rule_id") or ""), str(form.get("direction") or ""))
                    return RedirectResponse("/rules", status_code=303)
                if action == "test_rule":
                    context = _rules_context(
                        store=store,
                        settings=settings,
                        gateway_factory=gateway_factory,
                        error=None,
                        test_payee=str(form.get("test_payee") or ""),
                        test_memo=str(form.get("test_memo") or ""),
                    )
                    return _render(request, "rules.html", context)
            except (RuleError, HTTPException) as exc:
                post_error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)

        context = _rules_context(store=store, settings=settings, gateway_factory=gateway_factory, error=post_error)
        return _render(request, "rules.html", context)

    @app.post("/imports/{job_id}", dependencies=[Depends(_require_auth)])
    async def run_import(request: Request, job_id: str) -> Any:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found.")
        form = await request.form()
        accepted_transfer_ids = set(form.getlist("accepted_transfers"))
        result, status = _import_job(
            job,
            accepted_transfer_ids=accepted_transfer_ids,
            store=store,
            settings=settings,
            gateway_factory=gateway_factory,
        )
        store.update_job(job_id, status=status, result=result)
        return RedirectResponse(f"/imports/{job_id}", status_code=303)

    return app


def get_or_post_setup(app: FastAPI) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        app.get("/setup", response_class=HTMLResponse)(func)
        app.post("/setup", response_class=HTMLResponse)(func)
        return func

    return decorator


def get_or_post_rules(app: FastAPI) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        app.get("/rules", response_class=HTMLResponse)(func)
        app.post("/rules", response_class=HTMLResponse)(func)
        return func

    return decorator


def _default_gateway_factory(settings: Settings) -> YnabGateway:
    if not settings.ynab_access_token:
        raise YnabError("YNAB_ACCESS_TOKEN is not configured.")
    return OfficialYnabGateway(settings.ynab_access_token)


def format_money(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    sign = "-" if amount < 0 else ""
    absolute = abs(amount).quantize(Decimal("0.01"))
    whole, cents = f"{absolute:.2f}".split(".")
    groups: list[str] = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return f"{sign}{chr(39).join(reversed(groups))}.{cents}"


def _require_auth(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise LoginRequired()


def _render(request: Request, template: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    context = {"request": request, **context}
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _active_accounts(accounts: list[YnabAccount]) -> list[YnabAccount]:
    return [account for account in accounts if not account.closed and not account.deleted]


def _visible_categories(categories: list[YnabCategory]) -> list[YnabCategory]:
    return [category for category in categories if not category.hidden and not category.deleted]


def _active_payees(payees: list[YnabPayee]) -> list[YnabPayee]:
    return [payee for payee in payees if not payee.deleted]


def _save_plan(store: Store, gateway: YnabGateway, plan_id: str) -> None:
    plan_id = str(plan_id)
    if not plan_id:
        raise HTTPException(status_code=400, detail="No YNAB plan was selected.")
    plans = gateway.list_plans()
    selected = next((plan for plan in plans if str(plan.id) == plan_id), None)
    if selected is None:
        raise HTTPException(status_code=400, detail="Selected YNAB plan was not found.")
    store.save_selected_plan(str(selected.id), selected.name)


def _save_mapping(store: Store, gateway: YnabGateway, *, iban: str, account_id: str) -> None:
    plan_id, _ = store.selected_plan()
    if not plan_id:
        raise HTTPException(status_code=400, detail="Select a YNAB plan before mapping accounts.")
    iban = _normalize_iban(iban)
    if not iban:
        raise HTTPException(status_code=400, detail="IBAN is required.")
    account_id = str(account_id)
    account = next((item for item in gateway.list_accounts(plan_id) if str(item.id) == account_id), None)
    if account is None:
        raise HTTPException(status_code=400, detail="Selected YNAB account was not found.")
    store.upsert_mapping(
        iban=iban,
        ynab_account_id=account.id,
        ynab_account_name=account.name,
        transfer_payee_id=account.transfer_payee_id,
    )


def _save_self_names(store: Store, raw_names: str) -> None:
    names = [item.strip() for item in re.split(r"[\n,]+", raw_names) if item.strip()]
    store.save_self_names(names)


def _save_counterparty_mapping(store: Store, *, iban: str, label: str) -> None:
    iban = _normalize_iban(iban)
    label = normalize_whitespace(label)
    if not iban:
        raise HTTPException(status_code=400, detail="Counterparty IBAN is required.")
    if not label:
        raise HTTPException(status_code=400, detail="Counterparty account label is required.")
    store.upsert_counterparty_mapping(iban=iban, label=label[:120])


def _save_csv_account_iban(store: Store, value: str) -> None:
    normalized = _normalize_iban(value)
    if not normalized:
        raise HTTPException(status_code=400, detail="CSV account IBAN or account key is required.")
    store.set_config("csv_account_iban", normalized)


def _rules_context(
    *,
    store: Store,
    settings: Settings,
    gateway_factory: GatewayFactory,
    error: str | None,
    test_payee: str = "",
    test_memo: str = "",
) -> dict[str, Any]:
    plan_id, plan_name = store.selected_plan()
    categories: list[YnabCategory] = []
    payees: list[YnabPayee] = []
    category_error = None
    payee_error = None
    if not plan_id:
        category_error = "Select a YNAB plan before assigning categories."
        payee_error = "Select a YNAB plan before loading payees."
    elif not settings.ynab_configured:
        category_error = "YNAB_ACCESS_TOKEN is not configured; existing rules can still be managed."
        payee_error = "YNAB_ACCESS_TOKEN is not configured; payees cannot be loaded."
    else:
        try:
            gateway = gateway_factory(settings)
            categories = _visible_categories(gateway.list_categories(plan_id))
            payees = _active_payees(gateway.list_payees(plan_id))
        except YnabError as exc:
            category_error = str(exc)
            payee_error = str(exc)
    category_ids = {category.id for category in categories}
    rules = store.list_rules()
    enabled_rules = [rule for rule in rules if rule.enabled]
    test_result = evaluate_transaction(test_payee, test_memo, enabled_rules) if test_payee or test_memo else None
    return {
        "rules": rules,
        "rule_views": [
            {
                "rule": rule,
                "category_stale": bool(rule.category_id and rule.category_id not in category_ids),
            }
            for rule in rules
        ],
        "operators": [
            ("contains", "Contains"),
            ("exact", "Exact"),
            ("starts_with", "Starts with"),
            ("regex", "Regex"),
        ],
        "categories": categories,
        "payees": [
            {
                "payee": payee,
                "evaluation": evaluate_transaction(payee.name, None, enabled_rules),
                "is_transfer": payee.transfer_account_id is not None,
            }
            for payee in payees
        ],
        "selected_plan_name": plan_name,
        "category_error": category_error,
        "payee_error": payee_error,
        "test_payee": test_payee,
        "test_memo": test_memo,
        "test_result": test_result,
        "ynab_configured": settings.ynab_configured,
        "error": error,
    }


def _create_rule_from_form(store: Store, form: Any) -> None:
    rule_input = _rule_input_from_form(form)
    store.create_rule(
        name=rule_input.name,
        enabled=rule_input.enabled,
        operator=rule_input.operator,
        pattern=rule_input.pattern,
        replacement_payee=rule_input.replacement_payee,
        category_id=rule_input.category_id,
        category_name=rule_input.category_name,
    )


def _update_rule_from_form(store: Store, form: Any) -> None:
    rule_id = str(form.get("rule_id") or "")
    if not rule_id:
        raise HTTPException(status_code=400, detail="Rule ID is required.")
    rule_input = _rule_input_from_form(form)
    store.update_rule(
        rule_id,
        name=rule_input.name,
        enabled=rule_input.enabled,
        operator=rule_input.operator,
        pattern=rule_input.pattern,
        replacement_payee=rule_input.replacement_payee,
        category_id=rule_input.category_id,
        category_name=rule_input.category_name,
    )


def _rule_input_from_form(form: Any) -> Any:
    category_id, category_name = _parse_category_choice(str(form.get("category") or ""))
    return validate_rule_input(
        name=str(form.get("name") or ""),
        enabled=bool(form.get("enabled")),
        operator=str(form.get("operator") or ""),
        pattern=str(form.get("pattern") or ""),
        replacement_payee=str(form.get("replacement_payee") or ""),
        category_id=category_id,
        category_name=category_name,
    )


def _category_choice(category: YnabCategory) -> str:
    return f"{category.id}\t{_category_label(category)}"


def _category_label(category: YnabCategory) -> str:
    return f"{category.group_name}: {category.name}" if category.group_name else category.name


def _parse_category_choice(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    category_id, separator, category_name = value.partition("\t")
    if not separator:
        return None, None
    return category_id, category_name


def _normalize_iban(value: str) -> str:
    return "".join(value.upper().split())


def _observe_counterparty_accounts(store: Store, parsed: ParseResult) -> None:
    for tx in parsed.transactions:
        if tx.counterparty_iban:
            store.observe_counterparty_account(
                iban=tx.counterparty_iban,
                name=tx.counterparty_name,
                bank_name=tx.counterparty_bank,
            )


def _effective_self_names(store: Store, settings: Settings) -> list[str]:
    return store.self_names() or list(settings.self_names)


def _effective_csv_account_iban(store: Store, settings: Settings) -> str | None:
    return store.get_config("csv_account_iban") or settings.csv_account_iban


def _apply_counterparty_account_labels(parsed: ParseResult, *, store: Store, settings: Settings) -> None:
    self_names = _effective_self_names(store, settings)
    if not self_names:
        return
    counterparty_ibans = {_normalize_iban(tx.counterparty_iban) for tx in parsed.transactions if tx.counterparty_iban}
    mappings = store.counterparty_mappings_for(counterparty_ibans)
    if not mappings:
        return
    for tx in parsed.transactions:
        if not tx.counterparty_iban or not _matches_self_name(tx.counterparty_name, self_names):
            continue
        mapping = mappings.get(_normalize_iban(tx.counterparty_iban))
        if not mapping:
            continue
        direction = "to" if tx.amount < 0 else "from"
        original_payee = tx.payee
        tx.payee = f"Transfer {direction} {mapping.label}"[:200]
        memo_parts = [tx.memo] if tx.memo else []
        memo_parts.append(f"Self-transfer account: {mapping.label}")
        if original_payee and original_payee != tx.counterparty_name:
            memo_parts.append(f"Original payee: {original_payee}")
        tx.memo = truncate("\n".join(memo_parts), 500)


def _matches_self_name(value: str | None, self_names: list[str]) -> bool:
    candidate = _name_key(value)
    if not candidate:
        return False
    candidate_tokens = set(candidate.split())
    for name in self_names:
        expected = _name_key(name)
        if candidate == expected:
            return True
        expected_tokens = set(expected.split())
        if len(candidate_tokens) >= 2 and candidate_tokens == expected_tokens:
            return True
    return False


def _name_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", normalize_whitespace(value).casefold()).strip()


def _build_preview_payload(
    parsed: ParseResult,
    *,
    store: Store,
    settings: Settings,
    gateway_factory: GatewayFactory,
    plan_id: str | None,
    plan_name: str | None,
    filename: str,
) -> tuple[dict[str, Any], str]:
    errors: list[str] = []
    if not plan_id:
        errors.append("No YNAB plan is selected.")
    if not settings.ynab_configured:
        errors.append("YNAB_ACCESS_TOKEN is not configured.")

    transaction_ibans = {tx.iban for tx in parsed.transactions}
    mappings = store.mappings_for(transaction_ibans)
    missing_ibans = sorted(transaction_ibans - set(mappings))
    if missing_ibans:
        errors.append("Every IBAN in the upload must be mapped before import.")

    duplicate_keys: set[tuple[str, str]] = set()
    if not errors and plan_id:
        try:
            duplicate_keys = _existing_duplicate_keys(parsed.transactions, mappings=mappings, gateway=gateway_factory(settings), plan_id=plan_id)
        except YnabError as exc:
            errors.append(str(exc))

    transfer_pairs = []
    if not missing_ibans:
        transfer_pairs = detect_transfer_pairs(parsed.transactions)

    transfer_by_import_id: dict[str, tuple[str, str]] = {}
    for pair in transfer_pairs:
        debit_mapping = mappings.get(pair.source_iban)
        credit_mapping = mappings.get(pair.target_iban)
        if not debit_mapping or not credit_mapping:
            continue
        if (debit_mapping.ynab_account_id, pair.debit_import_id) in duplicate_keys:
            continue
        if (credit_mapping.ynab_account_id, pair.credit_import_id) in duplicate_keys:
            continue
        transfer_by_import_id[pair.debit_import_id] = (pair.id, "debit")
        transfer_by_import_id[pair.credit_import_id] = (pair.id, "credit")

    rows = []
    for tx in parsed.transactions:
        mapping = mappings.get(tx.iban)
        duplicate = bool(mapping and (mapping.ynab_account_id, tx.import_id) in duplicate_keys)
        transfer_marker = transfer_by_import_id.get(tx.import_id)
        rows.append(
            {
                "transaction": tx.to_dict(),
                "ynab_account_id": mapping.ynab_account_id if mapping else None,
                "ynab_account_name": mapping.ynab_account_name if mapping else None,
                "duplicate": duplicate,
                "transfer_id": transfer_marker[0] if transfer_marker else None,
                "transfer_role": transfer_marker[1] if transfer_marker else None,
                "status": _row_status(mapping=mapping, duplicate=duplicate, transfer_marker=transfer_marker),
            }
        )

    tx_by_import_id = {tx.import_id: tx for tx in parsed.transactions}
    transfers = []
    for pair in transfer_pairs:
        debit_tx = tx_by_import_id[pair.debit_import_id]
        credit_tx = tx_by_import_id[pair.credit_import_id]
        source_mapping = mappings.get(pair.source_iban)
        target_mapping = mappings.get(pair.target_iban)
        if not source_mapping or not target_mapping:
            continue
        debit_duplicate = (source_mapping.ynab_account_id, debit_tx.import_id) in duplicate_keys
        credit_duplicate = (target_mapping.ynab_account_id, credit_tx.import_id) in duplicate_keys
        if debit_duplicate or credit_duplicate:
            continue
        transfers.append(
            {
                **pair.to_dict(),
                "source_account_name": source_mapping.ynab_account_name,
                "target_account_name": target_mapping.ynab_account_name,
                "debit_payee": debit_tx.payee,
                "credit_payee": credit_tx.payee,
                "debit_date": debit_tx.booking_date.isoformat(),
                "credit_date": credit_tx.booking_date.isoformat(),
            }
        )

    statements = [statement.to_dict() for statement in parsed.statements]
    payload = {
        "filename": filename,
        "selected_plan_id": plan_id,
        "selected_plan_name": plan_name,
        "parse_result": parsed.to_dict(),
        "statements": statements,
        "rows": rows,
        "transfers": transfers,
        "missing_ibans": missing_ibans,
        "errors": errors,
        "duplicate_count": sum(1 for row in rows if row["duplicate"]),
        "ready_count": sum(1 for row in rows if row["status"] in {"ready", "transfer"}),
        "transaction_count": len(rows),
        "skipped_entries": parsed.skipped_entries,
    }
    return payload, "blocked" if errors else "preview"


def _row_status(
    *,
    mapping: AccountMapping | None,
    duplicate: bool,
    transfer_marker: tuple[str, str] | None,
) -> str:
    if not mapping:
        return "missing_mapping"
    if duplicate:
        return "duplicate"
    if transfer_marker:
        return "transfer"
    return "ready"


def _existing_duplicate_keys(
    transactions: list[BankTransaction],
    *,
    mappings: dict[str, AccountMapping],
    gateway: YnabGateway,
    plan_id: str,
) -> set[tuple[str, str]]:
    txs_by_account: dict[str, list[BankTransaction]] = defaultdict(list)
    for tx in transactions:
        mapping = mappings.get(tx.iban)
        if mapping:
            txs_by_account[mapping.ynab_account_id].append(tx)

    duplicates: set[tuple[str, str]] = set()
    for account_id, account_transactions in txs_by_account.items():
        since_date = min(tx.booking_date for tx in account_transactions)
        existing = gateway.existing_import_ids(plan_id, account_id, since_date)
        for tx in account_transactions:
            if tx.import_id in existing:
                duplicates.add((account_id, tx.import_id))
    return duplicates


def _import_job(
    job: dict[str, Any],
    *,
    accepted_transfer_ids: set[str],
    store: Store,
    settings: Settings,
    gateway_factory: GatewayFactory,
) -> tuple[dict[str, Any], str]:
    payload = job["payload"]
    if not payload.get("parse_result"):
        return {"errors": payload.get("errors") or ["No parsed transactions are available."], "created_count": 0}, "failed"
    parsed = ParseResult.from_dict(payload["parse_result"])
    plan_id = job.get("plan_id") or store.selected_plan()[0]
    if not plan_id:
        return {"errors": ["No YNAB plan is selected."], "created_count": 0}, "failed"

    transaction_ibans = {tx.iban for tx in parsed.transactions}
    mappings = store.mappings_for(transaction_ibans)
    missing_ibans = sorted(transaction_ibans - set(mappings))
    if missing_ibans:
        return {"errors": [f"Missing IBAN mappings: {', '.join(missing_ibans)}"], "created_count": 0}, "failed"

    try:
        gateway = gateway_factory(settings)
        duplicate_keys = _existing_duplicate_keys(parsed.transactions, mappings=mappings, gateway=gateway, plan_id=plan_id)
    except YnabError as exc:
        return {"errors": [str(exc)], "created_count": 0}, "failed"

    tx_by_import_id = {tx.import_id: tx for tx in parsed.transactions}
    transfer_payloads: list[dict[str, Any]] = []
    skipped_transfer_counterparts: list[str] = []
    skipped_transfer_duplicates: list[str] = []
    consumed_import_ids: set[str] = set()

    for transfer in payload.get("transfers", []):
        if transfer["id"] not in accepted_transfer_ids:
            continue
        debit = tx_by_import_id[transfer["debit_import_id"]]
        credit = tx_by_import_id[transfer["credit_import_id"]]
        source_mapping = mappings[debit.iban]
        target_mapping = mappings[credit.iban]
        if (source_mapping.ynab_account_id, debit.import_id) in duplicate_keys or (
            target_mapping.ynab_account_id,
            credit.import_id,
        ) in duplicate_keys:
            skipped_transfer_duplicates.extend([debit.import_id, credit.import_id])
            consumed_import_ids.update({debit.import_id, credit.import_id})
            continue
        if not target_mapping.transfer_payee_id:
            return {"errors": [f"YNAB account {target_mapping.ynab_account_name} has no transfer payee id."], "created_count": 0}, "failed"
        transfer_payloads.append(
            debit.to_ynab_payload(
                account_id=source_mapping.ynab_account_id,
                transfer_payee_id=target_mapping.transfer_payee_id,
            )
        )
        consumed_import_ids.update({debit.import_id, credit.import_id})
        skipped_transfer_counterparts.append(credit.import_id)

    normal_payloads: list[dict[str, Any]] = []
    skipped_duplicates: list[str] = []
    for tx in parsed.transactions:
        if tx.import_id in consumed_import_ids:
            continue
        mapping = mappings[tx.iban]
        if (mapping.ynab_account_id, tx.import_id) in duplicate_keys:
            skipped_duplicates.append(tx.import_id)
            continue
        normal_payloads.append(tx.to_ynab_payload(account_id=mapping.ynab_account_id))

    transactions_to_create = transfer_payloads + normal_payloads
    try:
        create_result = gateway.create_transactions(plan_id, transactions_to_create)
    except YnabError as exc:
        return {
            "errors": [str(exc)],
            "created_count": 0,
            "attempted_count": len(transactions_to_create),
            "skipped_duplicates": skipped_duplicates,
            "skipped_transfer_counterparts": skipped_transfer_counterparts,
        }, "failed"

    result = {
        "errors": [],
        "attempted_count": len(transactions_to_create),
        "created_count": len(create_result.transaction_ids),
        "transaction_ids": create_result.transaction_ids,
        "ynab_duplicate_import_ids": create_result.duplicate_import_ids,
        "skipped_duplicates": sorted(set(skipped_duplicates + skipped_transfer_duplicates)),
        "skipped_transfer_counterparts": skipped_transfer_counterparts,
        "accepted_transfers": sorted(accepted_transfer_ids),
        "normal_count": len(normal_payloads),
        "transfer_count": len(transfer_payloads),
    }
    return result, "imported"


def main() -> None:
    uvicorn.run("inab.web:create_app", factory=True, host="0.0.0.0", port=8000)
