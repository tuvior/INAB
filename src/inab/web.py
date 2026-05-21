from __future__ import annotations

import secrets
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .actual_api import ActualBudgetGateway, ActualBudgetSettings
from .budget_api import (
    BudgetAccount,
    BudgetCategory,
    BudgetError,
    BudgetGateway,
    BudgetPayee,
    ImportTransaction,
)
from .camt import CamtError, parse_upload
from .config import Settings
from .models import BankTransaction, ParseResult, normalize_whitespace, truncate
from .rules import RuleError, apply_rules, evaluate_transaction, validate_rule_input
from .store import AccountMapping, Store
from .transfers import detect_transfer_pairs
from .ynab_api import OfficialYnabGateway

GatewayFactory = Callable[[Settings], BudgetGateway]
STALE_UNCOMMITTED_IMPORT_DAYS = 7
UNUSUAL_TRANSACTION_AMOUNT = Decimal("10000")

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
    if (
        settings.backend == "actual"
        and settings.actual_budget
        and not store.selected_plan()[0]
    ):
        store.save_selected_plan(settings.actual_budget, settings.actual_budget)
    gateway_factory = gateway_factory or _default_gateway_factory

    app = FastAPI(
        title="INAB", docs_url=None, redoc_url=None, root_path=settings.root_path
    )
    app.state.settings = settings
    app.state.store = store
    app.state.gateway_factory = gateway_factory
    app.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret, same_site="lax"
    )
    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static"
    )

    @app.exception_handler(LoginRequired)
    async def login_required_handler(
        request: Request, exc: LoginRequired
    ) -> RedirectResponse:
        return _redirect(request, f"/login?next={quote(request.url.path)}")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/") -> Any:
        return _render(request, "login.html", {"next": next})

    @app.post("/login", response_class=HTMLResponse)
    async def login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
    ) -> Any:
        if not settings.auth_configured:
            return _render(
                request,
                "login.html",
                {
                    "error": "INAB_USERNAME and INAB_PASSWORD must be configured.",
                    "next": next,
                },
            )
        username_ok = secrets.compare_digest(username, settings.username or "")
        password_ok = secrets.compare_digest(password, settings.password or "")
        if not (username_ok and password_ok):
            return _render(
                request,
                "login.html",
                {"error": "Invalid username or password.", "next": next},
                status_code=401,
            )
        request.session["authenticated"] = True
        return _redirect(request, _safe_next(next))

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        request.session.clear()
        return _redirect(request, "/login")

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(_require_auth)])
    async def index(request: Request) -> Any:
        store.prune_stale_uncommitted_jobs(
            older_than_days=STALE_UNCOMMITTED_IMPORT_DAYS
        )
        selected_plan_id, selected_plan_name = store.selected_plan()
        accounts: list[BudgetAccount] = []
        account_error = None
        if selected_plan_id and settings.backend_configured:
            try:
                accounts = _active_accounts(
                    gateway_factory(settings).list_accounts(selected_plan_id)
                )
            except BudgetError as exc:
                account_error = str(exc)
        return _render(
            request,
            "index.html",
            {
                "selected_plan_id": selected_plan_id,
                "selected_plan_name": selected_plan_name,
                "mappings": store.list_mappings(),
                "observed_accounts": store.list_observed_accounts(),
                "accounts": accounts,
                "account_error": account_error,
                "backend_configured": settings.backend_configured,
                "backend_label": settings.backend_label,
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
                    _save_plan(
                        store, gateway_factory(settings), str(form.get("plan_id") or "")
                    )
                    return _redirect(request, "/setup")
                if action == "mapping":
                    _save_mapping(
                        store,
                        gateway_factory(settings),
                        iban=str(form.get("iban") or ""),
                        account_id=str(form.get("ynab_account_id") or ""),
                    )
                    return _redirect(request, "/setup")
                if action == "delete_mapping":
                    store.delete_mapping(str(form.get("iban") or ""))
                    return _redirect(request, "/setup")
                if action == "dismiss_observed_account":
                    store.dismiss_observed_account(str(form.get("iban") or ""))
                    return _redirect(request, "/setup")
                if action == "self_names":
                    _save_self_names(
                        store, [str(item) for item in form.getlist("self_names")]
                    )
                    return _redirect(request, "/setup")
                if action == "counterparty_mapping":
                    _save_counterparty_mapping(
                        store,
                        iban=str(form.get("iban") or ""),
                        label=str(form.get("label") or ""),
                    )
                    return _redirect(request, "/setup")
                if action == "delete_counterparty_mapping":
                    store.delete_counterparty_mapping(str(form.get("iban") or ""))
                    return _redirect(request, "/setup")
                if action == "dismiss_observed_counterparty":
                    store.dismiss_observed_counterparty_account(
                        str(form.get("iban") or "")
                    )
                    return _redirect(request, "/setup")
            except BudgetError as exc:
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
                observed
                for observed in observed_accounts
                if observed["iban"] not in mapped_ibans
                and observed["iban"] not in dismissed_observed
            ],
            "observed_counterparty_accounts": observed_counterparty_accounts,
            "observed_counterparty_suggestions": [
                observed
                for observed in observed_counterparty_accounts
                if observed["iban"] not in mapped_counterparty_ibans
                and observed["iban"] not in dismissed_counterparties
            ],
            "mappings": mappings,
            "counterparty_mappings": counterparty_mappings,
            "selected_plan_id": store.selected_plan()[0],
            "selected_plan_name": store.selected_plan()[1],
            "self_names": self_names,
            "backend_configured": settings.backend_configured,
            "backend_label": settings.backend_label,
            "error": post_error,
        }
        if settings.backend_configured:
            try:
                gateway = gateway_factory(settings)
                context["plans"] = gateway.list_budgets()
                if context["selected_plan_id"]:
                    context["accounts"] = _active_accounts(
                        gateway.list_accounts(context["selected_plan_id"])
                    )
            except BudgetError as exc:
                context["error"] = str(exc)
        return _render(request, "setup.html", context)

    @app.post("/uploads", dependencies=[Depends(_require_auth)])
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        csv_ynab_account_id: str = Form(""),
    ) -> Any:
        filename = file.filename or "upload"
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload is too large.")
        csv_mapping, csv_error = _csv_mapping_for_upload(
            filename=filename,
            account_id=csv_ynab_account_id,
            store=store,
            settings=settings,
            gateway_factory=gateway_factory,
        )
        if csv_error:
            payload = {
                "filename": filename,
                "errors": [csv_error],
                "missing_ibans": [],
                "statements": [],
                "rows": [],
                "transfers": [],
                "parse_result": None,
                "account_overrides": {},
                "ignored_ibans": [],
                "ignored_transaction_count": 0,
            }
            job_id = store.create_job(
                filename=filename,
                status="blocked",
                plan_id=store.selected_plan()[0],
                payload=payload,
            )
            return _redirect(request, f"/imports/{job_id}")
        try:
            parsed = parse_upload(
                filename,
                content,
                target_currency=settings.target_currency,
                csv_account_key=csv_mapping.iban if csv_mapping else None,
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
                "account_overrides": {},
                "ignored_ibans": [],
                "ignored_transaction_count": 0,
            }
            job_id = store.create_job(
                filename=filename,
                status="blocked",
                plan_id=store.selected_plan()[0],
                payload=payload,
            )
            return _redirect(request, f"/imports/{job_id}")

        for statement in parsed.statements:
            if _is_csv_account_key(statement.iban):
                continue
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
            account_overrides={csv_mapping.iban: csv_mapping} if csv_mapping else None,
        )
        job_id = store.create_job(
            filename=filename, status=status, plan_id=plan_id, payload=payload
        )
        return _redirect(request, f"/imports/{job_id}")

    @app.get(
        "/imports", response_class=HTMLResponse, dependencies=[Depends(_require_auth)]
    )
    async def import_history(request: Request) -> Any:
        pruned_count = store.prune_stale_uncommitted_jobs(
            older_than_days=STALE_UNCOMMITTED_IMPORT_DAYS
        )
        jobs = [_history_job_view(job) for job in store.list_jobs()]
        return _render(
            request,
            "history.html",
            {
                "jobs": jobs,
                "pruned_count": pruned_count,
                "stale_days": STALE_UNCOMMITTED_IMPORT_DAYS,
            },
        )

    @app.get(
        "/imports/{job_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(_require_auth)],
    )
    async def import_preview(request: Request, job_id: str) -> Any:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found.")
        return _render(
            request, "preview.html", {"job": job, "can_undo": _can_undo_job(job)}
        )

    @app.post("/imports/{job_id}/ignored-ibans", dependencies=[Depends(_require_auth)])
    async def ignore_import_ibans(request: Request, job_id: str) -> Any:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found.")
        payload = job["payload"]
        if not payload.get("parse_result"):
            return _redirect(request, f"/imports/{job_id}")

        form = await request.form()
        missing_ibans = {
            _normalize_iban(iban) for iban in payload.get("missing_ibans", [])
        }
        requested_ibans = {
            _normalize_iban(str(iban))
            for iban in form.getlist("ignored_ibans")
            if _normalize_iban(str(iban)) in missing_ibans
        }
        if not requested_ibans:
            return _redirect(request, f"/imports/{job_id}")

        parsed = ParseResult.from_dict(payload["parse_result"])
        filtered, ignored_ibans, ignored_transaction_count = _without_ignored_ibans(
            parsed, requested_ibans
        )
        if not ignored_ibans:
            return _redirect(request, f"/imports/{job_id}")

        account_overrides = {
            iban: _account_mapping_from_dict(mapping)
            for iban, mapping in (payload.get("account_overrides") or {}).items()
        }
        plan_id = (
            job.get("plan_id")
            or payload.get("selected_plan_id")
            or store.selected_plan()[0]
        )
        plan_name = payload.get("selected_plan_name") or store.selected_plan()[1]
        combined_ignored_ibans = sorted(
            set(payload.get("ignored_ibans") or []) | ignored_ibans
        )
        total_ignored_transactions = (
            int(payload.get("ignored_transaction_count") or 0)
            + ignored_transaction_count
        )
        updated_payload, status = _build_preview_payload(
            filtered,
            store=store,
            settings=settings,
            gateway_factory=gateway_factory,
            plan_id=plan_id,
            plan_name=plan_name,
            filename=payload.get("filename") or job["filename"],
            account_overrides=account_overrides,
            ignored_ibans=combined_ignored_ibans,
            ignored_transaction_count=total_ignored_transactions,
        )
        store.update_job(job_id, status=status, payload=updated_payload)
        return _redirect(request, f"/imports/{job_id}")

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
                    return _redirect(request, "/rules")
                if action == "update_rule":
                    _update_rule_from_form(store, form)
                    return _redirect(request, "/rules")
                if action == "delete_rule":
                    store.delete_rule(str(form.get("rule_id") or ""))
                    return _redirect(request, "/rules")
                if action == "move_rule":
                    store.move_rule(
                        str(form.get("rule_id") or ""), str(form.get("direction") or "")
                    )
                    return _redirect(request, "/rules")
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
                post_error = (
                    str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                )

        context = _rules_context(
            store=store,
            settings=settings,
            gateway_factory=gateway_factory,
            error=post_error,
        )
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
        return _redirect(request, f"/imports/{job_id}")

    @app.post("/imports/{job_id}/undo", dependencies=[Depends(_require_auth)])
    async def undo_import(request: Request, job_id: str) -> Any:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found.")
        result, status = _undo_import_job(
            job, settings=settings, gateway_factory=gateway_factory
        )
        store.update_job(job_id, status=status, result=result)
        return _redirect(request, f"/imports/{job_id}")

    return app


def get_or_post_setup(
    app: FastAPI,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        app.get("/setup", response_class=HTMLResponse)(func)
        app.post("/setup", response_class=HTMLResponse)(func)
        return func

    return decorator


def get_or_post_rules(
    app: FastAPI,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        app.get("/rules", response_class=HTMLResponse)(func)
        app.post("/rules", response_class=HTMLResponse)(func)
        return func

    return decorator


def _default_gateway_factory(settings: Settings) -> BudgetGateway:
    if settings.backend == "ynab":
        if not settings.ynab_access_token:
            raise BudgetError("YNAB_ACCESS_TOKEN is not configured.")
        return OfficialYnabGateway(settings.ynab_access_token)
    if settings.backend == "actual":
        return ActualBudgetGateway(
            ActualBudgetSettings(
                base_url=settings.actual_base_url or "",
                password=settings.actual_password or "",
                budget=settings.actual_budget or "",
                encryption_password=settings.actual_encryption_password,
                data_dir=settings.actual_data_dir,
                verify_ssl=settings.actual_verify_ssl,
            )
        )
    raise BudgetError(f"Unsupported INAB_BACKEND {settings.backend!r}.")


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


def _render(
    request: Request, template: str, context: dict[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    settings = request.app.state.settings
    context = {
        "request": request,
        "url_path_for": lambda name, **params: _url_path_for(request, name, **params),
        "backend_label": settings.backend_label,
        "backend_configured": settings.backend_configured,
        **context,
    }
    return templates.TemplateResponse(
        request, template, context, status_code=status_code
    )


def _safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _redirect(
    request: Request, path: str, *, status_code: int = 303
) -> RedirectResponse:
    return RedirectResponse(
        _external_path(request.app.state.settings, path), status_code=status_code
    )


def _url_path_for(request: Request, name: str, **path_params: Any) -> str:
    return _external_path(
        request.app.state.settings, str(request.app.url_path_for(name, **path_params))
    )


def _external_path(settings: Settings, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    root_path = settings.root_path.rstrip("/")
    if not root_path:
        return path
    if (
        path == root_path
        or path.startswith(f"{root_path}/")
        or path.startswith(f"{root_path}?")
    ):
        return path
    return f"{root_path}{path}"


def _active_accounts(accounts: list[BudgetAccount]) -> list[BudgetAccount]:
    return [
        account for account in accounts if not account.closed and not account.deleted
    ]


def _visible_categories(categories: list[BudgetCategory]) -> list[BudgetCategory]:
    return [
        category
        for category in categories
        if not category.hidden and not category.deleted
    ]


def _active_payees(payees: list[BudgetPayee]) -> list[BudgetPayee]:
    return [payee for payee in payees if not payee.deleted]


def _save_plan(store: Store, gateway: BudgetGateway, plan_id: str) -> None:
    plan_id = str(plan_id)
    if not plan_id:
        raise HTTPException(
            status_code=400, detail=f"No {gateway.backend_label} budget was selected."
        )
    plans = gateway.list_budgets()
    selected = next((plan for plan in plans if str(plan.id) == plan_id), None)
    if selected is None:
        raise HTTPException(
            status_code=400,
            detail=f"Selected {gateway.backend_label} budget was not found.",
        )
    store.save_selected_plan(str(selected.id), selected.name)


def _save_mapping(
    store: Store, gateway: BudgetGateway, *, iban: str, account_id: str
) -> None:
    plan_id, _ = store.selected_plan()
    if not plan_id:
        raise HTTPException(
            status_code=400,
            detail=f"Select a {gateway.backend_label} budget before mapping accounts.",
        )
    iban = _normalize_iban(iban)
    if not iban:
        raise HTTPException(status_code=400, detail="IBAN is required.")
    account_id = str(account_id)
    account = next(
        (item for item in gateway.list_accounts(plan_id) if str(item.id) == account_id),
        None,
    )
    if account is None:
        raise HTTPException(
            status_code=400,
            detail=f"Selected {gateway.backend_label} account was not found.",
        )
    store.upsert_account_mapping(
        iban=iban,
        account_id=account.id,
        account_name=account.name,
        transfer_payee_id=account.transfer_payee_id,
    )


def _save_self_names(store: Store, raw_names: list[str]) -> None:
    names = [
        item.strip()
        for raw_name in raw_names
        for item in re.split(r"[\n,]+", raw_name)
        if item.strip()
    ]
    store.save_self_names(names)


def _save_counterparty_mapping(store: Store, *, iban: str, label: str) -> None:
    iban = _normalize_iban(iban)
    label = normalize_whitespace(label)
    if not iban:
        raise HTTPException(status_code=400, detail="Counterparty IBAN is required.")
    if not label:
        raise HTTPException(
            status_code=400, detail="Counterparty account label is required."
        )
    store.upsert_counterparty_mapping(iban=iban, label=label[:120])


def _csv_mapping_for_upload(
    *,
    filename: str,
    account_id: str,
    store: Store,
    settings: Settings,
    gateway_factory: GatewayFactory,
) -> tuple[AccountMapping | None, str | None]:
    if Path(filename).suffix.lower() != ".csv":
        return None, None
    if not account_id:
        return None, f"Select a {settings.backend_label} account for this CSV upload."
    plan_id, _ = store.selected_plan()
    if not plan_id:
        return (
            None,
            f"Select a {settings.backend_label} budget before uploading CSV files.",
        )
    if not settings.backend_configured:
        return None, f"{settings.backend_label} is not configured."
    try:
        account = next(
            (
                item
                for item in _active_accounts(
                    gateway_factory(settings).list_accounts(plan_id)
                )
                if str(item.id) == account_id
            ),
            None,
        )
    except BudgetError as exc:
        return None, str(exc)
    if account is None:
        return None, f"Selected CSV {settings.backend_label} account was not found."
    return (
        AccountMapping(
            iban=_csv_upload_account_key(str(account.id)),
            ynab_account_id=str(account.id),
            ynab_account_name=account.name,
            transfer_payee_id=account.transfer_payee_id,
            updated_at="",
        ),
        None,
    )


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
    categories: list[BudgetCategory] = []
    payees: list[BudgetPayee] = []
    category_error = None
    payee_error = None
    if not plan_id:
        category_error = (
            f"Select a {settings.backend_label} budget before assigning categories."
        )
        payee_error = f"Select a {settings.backend_label} budget before loading payees."
    elif not settings.backend_configured:
        category_error = f"{settings.backend_label} is not configured; existing rules can still be managed."
        payee_error = (
            f"{settings.backend_label} is not configured; payees cannot be loaded."
        )
    else:
        try:
            gateway = gateway_factory(settings)
            categories = _visible_categories(gateway.list_categories(plan_id))
            payees = _active_payees(gateway.list_payees(plan_id))
        except BudgetError as exc:
            category_error = str(exc)
            payee_error = str(exc)
    category_ids = {category.id for category in categories}
    rules = store.list_rules()
    enabled_rules = [rule for rule in rules if rule.enabled]
    test_result = (
        evaluate_transaction(test_payee, test_memo, enabled_rules)
        if test_payee or test_memo
        else None
    )
    return {
        "rules": rules,
        "rule_views": [
            {
                "rule": rule,
                "category_stale": bool(
                    rule.category_id and rule.category_id not in category_ids
                ),
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
        "backend_configured": settings.backend_configured,
        "backend_label": settings.backend_label,
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


def _category_choice(category: BudgetCategory) -> str:
    return f"{category.id}\t{_category_label(category)}"


def _category_label(category: BudgetCategory) -> str:
    return (
        f"{category.group_name}: {category.name}"
        if category.group_name
        else category.name
    )


def _parse_category_choice(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    category_id, separator, category_name = value.partition("\t")
    if not separator:
        return None, None
    return category_id, category_name


def _normalize_iban(value: str) -> str:
    return "".join(value.upper().split())


def _csv_upload_account_key(account_id: str) -> str:
    return _normalize_iban(f"CSV:{account_id}")


def _is_csv_account_key(value: str) -> bool:
    return _normalize_iban(value).startswith("CSV:")


def _account_mapping_to_dict(mapping: AccountMapping) -> dict[str, Any]:
    return {
        "iban": mapping.iban,
        "ynab_account_id": mapping.ynab_account_id,
        "ynab_account_name": mapping.ynab_account_name,
        "transfer_payee_id": mapping.transfer_payee_id,
        "updated_at": mapping.updated_at,
    }


def _account_mapping_from_dict(data: dict[str, Any]) -> AccountMapping:
    return AccountMapping(
        iban=str(data["iban"]),
        ynab_account_id=str(data["ynab_account_id"]),
        ynab_account_name=str(data["ynab_account_name"]),
        transfer_payee_id=data.get("transfer_payee_id"),
        updated_at=str(data.get("updated_at") or ""),
    )


def _mappings_with_overrides(
    store: Store, ibans: set[str], account_overrides: dict[str, AccountMapping] | None
) -> dict[str, AccountMapping]:
    mappings = store.mappings_for(ibans)
    if account_overrides:
        mappings.update(
            {
                iban: mapping
                for iban, mapping in account_overrides.items()
                if iban in ibans
            }
        )
    return mappings


def _without_ignored_ibans(
    parsed: ParseResult, ignored_ibans: set[str]
) -> tuple[ParseResult, set[str], int]:
    ignored_ibans = {
        _normalize_iban(iban) for iban in ignored_ibans if _normalize_iban(iban)
    }
    if not ignored_ibans:
        return parsed, set(), 0
    statements = []
    actual_ignored_ibans: set[str] = set()
    ignored_transaction_count = 0
    for statement in parsed.statements:
        if _normalize_iban(statement.iban) in ignored_ibans:
            actual_ignored_ibans.add(statement.iban)
            ignored_transaction_count += len(statement.transactions)
            continue
        statements.append(statement)
    return (
        ParseResult(statements=statements, skipped_entries=parsed.skipped_entries),
        actual_ignored_ibans,
        ignored_transaction_count,
    )


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


def _apply_counterparty_account_labels(
    parsed: ParseResult, *, store: Store, settings: Settings
) -> None:
    self_names = _effective_self_names(store, settings)
    if not self_names:
        return
    counterparty_ibans = {
        _normalize_iban(tx.counterparty_iban)
        for tx in parsed.transactions
        if tx.counterparty_iban
    }
    mappings = store.counterparty_mappings_for(counterparty_ibans)
    if not mappings:
        return
    for tx in parsed.transactions:
        if not tx.counterparty_iban or not _matches_self_name(
            tx.counterparty_name, self_names
        ):
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
    account_overrides: dict[str, AccountMapping] | None = None,
    ignored_ibans: list[str] | None = None,
    ignored_transaction_count: int = 0,
) -> tuple[dict[str, Any], str]:
    errors: list[str] = []
    if not plan_id:
        errors.append(f"No {settings.backend_label} budget is selected.")
    if not settings.backend_configured:
        errors.append(f"{settings.backend_label} is not configured.")

    transaction_ibans = {tx.iban for tx in parsed.transactions}
    mappings = _mappings_with_overrides(store, transaction_ibans, account_overrides)
    missing_ibans = sorted(transaction_ibans - set(mappings))
    if missing_ibans:
        errors.append("Every IBAN in the upload must be mapped before import.")

    duplicate_matches: dict[tuple[str, str], dict[str, Any]] = {}
    if plan_id and settings.backend_configured and mappings:
        try:
            duplicate_matches = _existing_duplicate_matches(
                parsed.transactions,
                mappings=mappings,
                gateway=gateway_factory(settings),
                plan_id=plan_id,
            )
        except BudgetError as exc:
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
        if (debit_mapping.account_id, pair.debit_import_id) in duplicate_matches:
            continue
        if (credit_mapping.account_id, pair.credit_import_id) in duplicate_matches:
            continue
        transfer_by_import_id[pair.debit_import_id] = (pair.id, "debit")
        transfer_by_import_id[pair.credit_import_id] = (pair.id, "credit")

    rows = []
    for tx in _preview_transactions(parsed.transactions):
        mapping = mappings.get(tx.iban)
        duplicate_match = (
            duplicate_matches.get((mapping.account_id, tx.import_id))
            if mapping
            else None
        )
        duplicate = bool(duplicate_match)
        transfer_marker = transfer_by_import_id.get(tx.import_id)
        rows.append(
            {
                "transaction": tx.to_dict(),
                "account_id": mapping.account_id if mapping else None,
                "account_name": mapping.account_name if mapping else None,
                "ynab_account_id": mapping.account_id if mapping else None,
                "ynab_account_name": mapping.account_name if mapping else None,
                "duplicate": duplicate,
                "duplicate_match": duplicate_match,
                "transfer_id": transfer_marker[0] if transfer_marker else None,
                "transfer_role": transfer_marker[1] if transfer_marker else None,
                "status": _row_status(
                    mapping=mapping,
                    duplicate=duplicate,
                    transfer_marker=transfer_marker,
                ),
                "search_text": _row_search_text(
                    tx,
                    mapping=mapping,
                    status=_row_status(
                        mapping=mapping,
                        duplicate=duplicate,
                        transfer_marker=transfer_marker,
                    ),
                ),
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
        debit_duplicate = (
            source_mapping.account_id,
            debit_tx.import_id,
        ) in duplicate_matches
        credit_duplicate = (
            target_mapping.account_id,
            credit_tx.import_id,
        ) in duplicate_matches
        if debit_duplicate or credit_duplicate:
            continue
        transfers.append(
            {
                **pair.to_dict(),
                "source_account_name": source_mapping.account_name,
                "target_account_name": target_mapping.account_name,
                "debit_payee": debit_tx.payee,
                "credit_payee": credit_tx.payee,
                "debit_date": debit_tx.booking_date.isoformat(),
                "credit_date": credit_tx.booking_date.isoformat(),
            }
        )

    statements = []
    for statement in parsed.statements:
        statement_mapping = mappings.get(statement.iban)
        statement_data = statement.to_dict()
        statement_data["account_name"] = (
            statement_mapping.account_name if statement_mapping else None
        )
        statement_data["ynab_account_name"] = (
            statement_mapping.account_name if statement_mapping else None
        )
        statement_data["reconciliation"] = _statement_reconciliation(statement)
        statements.append(statement_data)
    summary = _preview_summary(rows, transfers)
    warnings = _preview_warnings(
        rows=rows, statements=statements, missing_ibans=missing_ibans, summary=summary
    )
    payload = {
        "filename": filename,
        "selected_plan_id": plan_id,
        "selected_plan_name": plan_name,
        "parse_result": parsed.to_dict(),
        "account_overrides": {
            iban: _account_mapping_to_dict(mapping)
            for iban, mapping in (account_overrides or {}).items()
        },
        "ignored_ibans": sorted(ignored_ibans or []),
        "ignored_transaction_count": ignored_transaction_count,
        "statements": statements,
        "rows": rows,
        "transfers": transfers,
        "missing_ibans": missing_ibans,
        "errors": errors,
        "duplicate_count": sum(1 for row in rows if row["duplicate"]),
        "ready_count": sum(1 for row in rows if row["status"] in {"ready", "transfer"}),
        "transaction_count": len(rows),
        "summary": summary,
        "warnings": warnings,
        "skipped_entries": parsed.skipped_entries,
        "backend_label": settings.backend_label,
    }
    return payload, "blocked" if errors else "preview"


def _preview_transactions(transactions: list[BankTransaction]) -> list[BankTransaction]:
    return sorted(transactions, key=lambda tx: tx.booking_date)


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


def _row_search_text(
    tx: BankTransaction, *, mapping: AccountMapping | None, status: str
) -> str:
    parts = [
        tx.booking_date.isoformat(),
        mapping.account_name if mapping else tx.iban,
        tx.payee,
        tx.original_payee,
        tx.memo,
        tx.category_name,
        tx.applied_rule_name,
        tx.import_id,
        status,
    ]
    return " ".join(str(part).casefold() for part in parts if part)


def _statement_reconciliation(statement: Any) -> dict[str, Any]:
    opening = (
        statement.opening_balance.signed_amount if statement.opening_balance else None
    )
    actual = (
        statement.closing_balance.signed_amount if statement.closing_balance else None
    )
    movement = statement.movement_total
    expected = opening + movement if opening is not None else None
    delta = actual - expected if actual is not None and expected is not None else None
    return {
        "opening": _decimal_string(opening),
        "movement": _decimal_string(movement),
        "expected_closing": _decimal_string(expected),
        "actual_closing": _decimal_string(actual),
        "delta": _decimal_string(delta),
        "status": (
            "reconciled"
            if statement.balances_reconcile is True
            else "mismatch" if statement.balances_reconcile is False else "missing"
        ),
    }


def _preview_summary(
    rows: list[dict[str, Any]], transfers: list[dict[str, Any]]
) -> dict[str, Any]:
    importable_rows = [row for row in rows if row["status"] in {"ready", "transfer"}]
    amounts = [Decimal(str(row["transaction"]["amount"])) for row in importable_rows]
    inflow = sum((amount for amount in amounts if amount > 0), Decimal("0"))
    outflow = sum((amount for amount in amounts if amount < 0), Decimal("0"))
    net = sum(amounts, Decimal("0"))
    return {
        "account_count": len(
            {row["account_id"] for row in importable_rows if row["account_id"]}
        ),
        "ready_count": sum(1 for row in rows if row["status"] in {"ready", "transfer"}),
        "duplicate_count": sum(1 for row in rows if row["duplicate"]),
        "transfer_count": len(transfers),
        "assigned_category_count": sum(
            1 for row in importable_rows if row["transaction"].get("category_id")
        ),
        "inflow_total": _decimal_string(inflow),
        "outflow_total": _decimal_string(outflow),
        "net_total": _decimal_string(net),
    }


def _preview_warnings(
    *,
    rows: list[dict[str, Any]],
    statements: list[dict[str, Any]],
    missing_ibans: list[str],
    summary: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    for statement in statements:
        reconciliation = statement.get("reconciliation") or {}
        if reconciliation.get("status") == "mismatch":
            account = (
                statement.get("account_name")
                or statement.get("ynab_account_name")
                or statement.get("iban")
            )
            warnings.append(
                "Balance mismatch for "
                f"{account}: expected closing {format_money(reconciliation.get('expected_closing'))}, "
                f"actual {format_money(reconciliation.get('actual_closing'))}, "
                f"delta {format_money(reconciliation.get('delta'))}."
            )
    if missing_ibans:
        warnings.append(f"Missing mappings for {', '.join(missing_ibans)}.")
    if rows and int(summary["ready_count"]) == 0:
        warnings.append("This import has zero ready rows.")
    for row in rows:
        if row["status"] not in {"ready", "transfer"}:
            continue
        amount = Decimal(str(row["transaction"]["amount"]))
        if abs(amount) >= UNUSUAL_TRANSACTION_AMOUNT:
            warnings.append(
                f"Unusually large transaction: {row['transaction']['booking_date']} "
                f"{row['transaction']['payee']} {format_money(amount)}."
            )
    return warnings


def _decimal_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _milliunits_to_amount_string(value: int | None) -> str | None:
    if value is None:
        return None
    return str((Decimal(value) / Decimal("1000")).quantize(Decimal("0.001")))


def _existing_duplicate_matches(
    transactions: list[BankTransaction],
    *,
    mappings: dict[str, AccountMapping],
    gateway: BudgetGateway,
    plan_id: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    txs_by_account: dict[str, list[BankTransaction]] = defaultdict(list)
    for tx in transactions:
        mapping = mappings.get(tx.iban)
        if mapping:
            txs_by_account[mapping.account_id].append(tx)

    duplicates: dict[tuple[str, str], dict[str, Any]] = {}
    for account_id, account_transactions in txs_by_account.items():
        since_date = min(tx.booking_date for tx in account_transactions)
        existing_transactions = gateway.existing_transactions(
            plan_id, account_id, since_date
        )
        existing_by_import_id = {
            transaction.import_id: transaction for transaction in existing_transactions
        }
        exact_alias_counts = Counter(
            alias for tx in account_transactions for alias in tx.legacy_exact_import_ids
        )
        exact_alias_date_counts = Counter(
            (alias, tx.booking_date)
            for tx in account_transactions
            for alias in tx.legacy_exact_import_ids
        )
        exact_alias_amount_counts = Counter(
            (alias, tx.milliunits)
            for tx in account_transactions
            for alias in tx.legacy_exact_import_ids
        )
        for tx in account_transactions:
            existing = existing_by_import_id.get(tx.import_id)
            if existing:
                duplicates[(account_id, tx.import_id)] = _duplicate_match(
                    account_id=account_id,
                    uploaded_import_id=tx.import_id,
                    matched_import_id=tx.import_id,
                    match_type="import_id",
                    existing=existing,
                )
                continue
            legacy_import_id = next(
                (
                    import_id
                    for import_id in tx.legacy_import_ids
                    if import_id in existing_by_import_id
                ),
                None,
            )
            if legacy_import_id:
                duplicates[(account_id, tx.import_id)] = _duplicate_match(
                    account_id=account_id,
                    uploaded_import_id=tx.import_id,
                    matched_import_id=legacy_import_id,
                    match_type="legacy_import_id",
                    existing=existing_by_import_id[legacy_import_id],
                )
                continue
            for import_id in tx.legacy_exact_import_ids:
                existing = existing_by_import_id.get(import_id)
                if _legacy_exact_match(
                    existing,
                    tx,
                    import_id=import_id,
                    exact_alias_counts=exact_alias_counts,
                    exact_alias_date_counts=exact_alias_date_counts,
                    exact_alias_amount_counts=exact_alias_amount_counts,
                ):
                    duplicates[(account_id, tx.import_id)] = _duplicate_match(
                        account_id=account_id,
                        uploaded_import_id=tx.import_id,
                        matched_import_id=import_id,
                        match_type="legacy_exact_import_id",
                        existing=existing,
                    )
                    break
    return duplicates


def _duplicate_match(
    *,
    account_id: str,
    uploaded_import_id: str,
    matched_import_id: str,
    match_type: str,
    existing: Any,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "uploaded_import_id": uploaded_import_id,
        "matched_import_id": matched_import_id,
        "match_type": match_type,
        "existing_date": (
            existing.date.isoformat() if getattr(existing, "date", None) else None
        ),
        "existing_amount_milliunits": existing.amount,
        "existing_amount": _milliunits_to_amount_string(existing.amount),
    }


def _legacy_exact_match(
    existing: Any,
    tx: BankTransaction,
    *,
    import_id: str,
    exact_alias_counts: Counter[str],
    exact_alias_date_counts: Counter[tuple[str, date]],
    exact_alias_amount_counts: Counter[tuple[str, int]],
) -> bool:
    if not existing:
        return False
    date_matches = (
        existing.date == tx.booking_date if existing.date is not None else None
    )
    amount_matches = (
        existing.amount == tx.milliunits if existing.amount is not None else None
    )
    if date_matches is False or amount_matches is False:
        return False
    if date_matches is True and amount_matches is True:
        return True
    if date_matches is True:
        return exact_alias_date_counts[(import_id, tx.booking_date)] == 1
    if amount_matches is True:
        return exact_alias_amount_counts[(import_id, tx.milliunits)] == 1
    return exact_alias_counts[import_id] == 1


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
        return {
            "errors": payload.get("errors")
            or ["No parsed transactions are available."],
            "created_count": 0,
        }, "failed"
    parsed = ParseResult.from_dict(payload["parse_result"])
    plan_id = job.get("plan_id") or store.selected_plan()[0]
    if not plan_id:
        return {
            "errors": [f"No {settings.backend_label} budget is selected."],
            "created_count": 0,
        }, "failed"

    transaction_ibans = {tx.iban for tx in parsed.transactions}
    account_overrides = {
        iban: _account_mapping_from_dict(mapping)
        for iban, mapping in (payload.get("account_overrides") or {}).items()
    }
    mappings = _mappings_with_overrides(store, transaction_ibans, account_overrides)
    missing_ibans = sorted(transaction_ibans - set(mappings))
    if missing_ibans:
        return {
            "errors": [f"Missing IBAN mappings: {', '.join(missing_ibans)}"],
            "created_count": 0,
        }, "failed"

    try:
        gateway = gateway_factory(settings)
        duplicate_matches = _existing_duplicate_matches(
            parsed.transactions, mappings=mappings, gateway=gateway, plan_id=plan_id
        )
    except BudgetError as exc:
        return {"errors": [str(exc)], "created_count": 0}, "failed"

    tx_by_import_id = {tx.import_id: tx for tx in parsed.transactions}
    transfer_payloads: list[ImportTransaction] = []
    submitted_transactions: list[dict[str, Any]] = []
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
        if (source_mapping.account_id, debit.import_id) in duplicate_matches or (
            target_mapping.account_id,
            credit.import_id,
        ) in duplicate_matches:
            skipped_transfer_duplicates.extend([debit.import_id, credit.import_id])
            consumed_import_ids.update({debit.import_id, credit.import_id})
            continue
        if settings.backend == "ynab" and not target_mapping.transfer_payee_id:
            return {
                "errors": [
                    f"YNAB account {target_mapping.account_name} has no transfer payee id."
                ],
                "created_count": 0,
            }, "failed"
        transfer_payload = _import_transaction(
            debit,
            account_id=source_mapping.account_id,
            transfer_account_id=target_mapping.account_id,
            transfer_payee_id=target_mapping.transfer_payee_id,
            transfer_counterpart_import_id=credit.import_id,
        )
        transfer_payloads.append(transfer_payload)
        submitted_transactions.append(
            _submitted_transaction_summary(
                debit,
                account=source_mapping,
                kind="transfer",
                payee_name=f"Transfer to {target_mapping.account_name}",
                counterpart_import_id=credit.import_id,
                counterpart_account_name=target_mapping.account_name,
            )
        )
        consumed_import_ids.update({debit.import_id, credit.import_id})
        skipped_transfer_counterparts.append(credit.import_id)

    normal_payloads: list[ImportTransaction] = []
    skipped_duplicates: list[str] = []
    for tx in parsed.transactions:
        if tx.import_id in consumed_import_ids:
            continue
        mapping = mappings[tx.iban]
        if (mapping.account_id, tx.import_id) in duplicate_matches:
            skipped_duplicates.append(tx.import_id)
            continue
        normal_payloads.append(_import_transaction(tx, account_id=mapping.account_id))
        submitted_transactions.append(
            _submitted_transaction_summary(
                tx,
                account=mapping,
                kind="transaction",
                payee_name=tx.payee,
            )
        )

    transactions_to_create = transfer_payloads + normal_payloads
    try:
        create_result = gateway.create_transactions(plan_id, transactions_to_create)
    except BudgetError as exc:
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
        "submitted_transactions": submitted_transactions,
        "backend_transactions": create_result.transactions,
        "backend_matched_count": sum(
            1 for tx in create_result.transactions if tx.get("matched_transaction_id")
        ),
        "backend_transfer_counterpart_count": sum(
            1 for tx in create_result.transactions if tx.get("transfer_transaction_id")
        ),
        "backend_duplicate_import_ids": create_result.duplicate_import_ids,
        "backend_label": settings.backend_label,
        "ynab_transactions": create_result.transactions,
        "ynab_matched_count": sum(
            1 for tx in create_result.transactions if tx.get("matched_transaction_id")
        ),
        "ynab_transfer_counterpart_count": sum(
            1 for tx in create_result.transactions if tx.get("transfer_transaction_id")
        ),
        "ynab_duplicate_import_ids": create_result.duplicate_import_ids,
        "skipped_duplicates": sorted(
            set(skipped_duplicates + skipped_transfer_duplicates)
        ),
        "skipped_transfer_counterparts": skipped_transfer_counterparts,
        "accepted_transfers": sorted(accepted_transfer_ids),
        "normal_count": len(normal_payloads),
        "transfer_count": len(transfer_payloads),
    }
    return result, "imported"


def _import_transaction(
    tx: BankTransaction,
    *,
    account_id: str,
    transfer_account_id: str | None = None,
    transfer_payee_id: str | None = None,
    transfer_counterpart_import_id: str | None = None,
) -> ImportTransaction:
    return ImportTransaction(
        account_id=account_id,
        date=tx.booking_date,
        amount=tx.amount,
        amount_milliunits=tx.milliunits,
        payee_name=tx.payee[:200],
        memo=truncate(tx.memo, 500),
        import_id=tx.import_id,
        category_id=None if transfer_account_id else tx.category_id,
        transfer_account_id=transfer_account_id,
        transfer_payee_id=transfer_payee_id,
        transfer_counterpart_import_id=transfer_counterpart_import_id,
    )


def _submitted_transaction_summary(
    tx: BankTransaction,
    *,
    account: AccountMapping,
    kind: str,
    payee_name: str,
    counterpart_import_id: str | None = None,
    counterpart_account_name: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "date": tx.booking_date.isoformat(),
        "account_id": account.account_id,
        "account_name": account.account_name,
        "payee_name": payee_name,
        "amount": str(tx.amount),
        "import_id": tx.import_id,
        "category_name": tx.category_name,
        "counterpart_import_id": counterpart_import_id,
        "counterpart_account_name": counterpart_account_name,
    }


def _history_job_view(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") or {}
    result = job.get("result") or {}
    return {
        "id": job["id"],
        "filename": job["filename"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "transaction_count": payload.get("transaction_count") or 0,
        "ready_count": payload.get("ready_count") or 0,
        "duplicate_count": payload.get("duplicate_count") or 0,
        "transfer_count": len(payload.get("transfers") or []),
        "created_count": result.get("created_count")
        or len(result.get("transaction_ids") or []),
        "can_undo": _can_undo_job(job),
    }


def _can_undo_job(job: dict[str, Any]) -> bool:
    return job.get("status") == "imported" and bool(_job_created_transaction_ids(job))


def _job_created_transaction_ids(job: dict[str, Any]) -> list[str]:
    result = job.get("result") or {}
    transaction_ids = result.get("transaction_ids")
    if not isinstance(transaction_ids, list):
        return []
    return [
        str(transaction_id) for transaction_id in transaction_ids if str(transaction_id)
    ]


def _undo_import_job(
    job: dict[str, Any],
    *,
    settings: Settings,
    gateway_factory: GatewayFactory,
) -> tuple[dict[str, Any], str]:
    result = dict(job.get("result") or {})
    transaction_ids = _job_created_transaction_ids(job)
    if job.get("status") == "reverted":
        result["undo"] = {
            "attempted_transaction_ids": [],
            "deleted_transaction_ids": [],
            "errors": ["Import has already been reverted."],
        }
        return result, "reverted"
    if not transaction_ids:
        result["undo"] = {
            "attempted_transaction_ids": [],
            "deleted_transaction_ids": [],
            "errors": [
                f"No created {settings.backend_label} transaction IDs are available to undo."
            ],
        }
        return result, job.get("status") or "failed"
    plan_id = job.get("plan_id")
    if not plan_id:
        result["undo"] = {
            "attempted_transaction_ids": transaction_ids,
            "deleted_transaction_ids": [],
            "errors": [
                f"No {settings.backend_label} budget is associated with this import."
            ],
        }
        return result, "imported"

    deleted_transaction_ids: list[str] = []
    errors: list[str] = []
    try:
        gateway = gateway_factory(settings)
    except BudgetError as exc:
        result["undo"] = {
            "attempted_transaction_ids": transaction_ids,
            "deleted_transaction_ids": [],
            "errors": [str(exc)],
        }
        return result, "imported"

    for transaction_id in transaction_ids:
        try:
            gateway.delete_transaction(plan_id, transaction_id)
            deleted_transaction_ids.append(transaction_id)
        except BudgetError as exc:
            errors.append(f"{transaction_id}: {exc}")

    result["undo"] = {
        "attempted_transaction_ids": transaction_ids,
        "deleted_transaction_ids": deleted_transaction_ids,
        "errors": errors,
    }
    return result, "reverted" if not errors else "imported"


def main() -> None:
    uvicorn.run("inab.web:create_app", factory=True, host="0.0.0.0", port=8000)
