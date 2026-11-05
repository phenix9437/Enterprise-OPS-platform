"""MCP server exposing the company's business database as a small set of SAFE, fixed tools.

Design choice: no "run any SQL" tool. Every tool is a parameterised query with validated inputs,
so an LLM mistake (or a prompt injection) can never become arbitrary database access.

Run standalone (stdio):  python -m app.mcp_servers.business_db
"""
import asyncio
from datetime import date, timedelta
from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.mcp_servers.business_models import (
    BusinessBase, Customer, Invoice, InvoiceReminder, PurchaseRequest, Quotation, Vendor, seed_demo_data,
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(  # changes our own DB only: reversible-ish and not external -> medium risk
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


def money(cents: int) -> float:
    return round(cents / 100, 2)


def create_server(session_factory: async_sessionmaker, today_fn: Callable[[], date] = date.today) -> MCPServer:
    srv = MCPServer(
        "business_db",
        instructions="Read and update business records: customers, invoices, purchase requests, quotations.",
    )

    @srv.tool(annotations=READ)
    async def list_overdue_invoices(
        min_days_overdue: Annotated[int, Field(ge=0, le=3650, description="Only invoices at least this many days past due")] = 1,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List unpaid or partly paid invoices past their due date, oldest first, with customer contact details."""
        today = today_fn()
        cutoff = today - timedelta(days=min_days_overdue)
        async with session_factory() as db:
            rows = (await db.execute(
                select(Invoice, Customer).join(Customer, Customer.id == Invoice.customer_id)
                .where(Invoice.status != "paid", Invoice.due_date <= cutoff)
                .order_by(Invoice.due_date.asc(), Invoice.id.asc()).limit(limit)
            )).all()
            ids = [inv.id for inv, _ in rows]
            stats = {}
            if ids:
                res = await db.execute(
                    select(InvoiceReminder.invoice_id, func.count(), func.max(InvoiceReminder.created_at))
                    .where(InvoiceReminder.invoice_id.in_(ids)).group_by(InvoiceReminder.invoice_id)
                )
                stats = {i: (n, last) for i, n, last in res.all()}

        items = []
        for inv, cust in rows:
            n, last = stats.get(inv.id, (0, None))
            items.append({
                "invoice_id": inv.id, "number": inv.number, "customer_id": cust.id,
                "customer_name": cust.name, "customer_email": cust.email, "currency": inv.currency,
                "amount": money(inv.amount_cents), "amount_paid": money(inv.amount_paid_cents),
                "outstanding": money(inv.amount_cents - inv.amount_paid_cents),
                "due_date": inv.due_date.isoformat(), "days_overdue": (today - inv.due_date).days,
                "status": inv.status, "reminders_sent": n,
                "last_reminder_at": last.isoformat() if last else None,
            })
        return {"as_of": today.isoformat(), "count": len(items), "invoices": items}

    @srv.tool(annotations=READ)
    async def get_invoice(invoice_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """Get one invoice with its customer and the reminders already logged for it."""
        today = today_fn()
        async with session_factory() as db:
            row = (await db.execute(
                select(Invoice, Customer).join(Customer, Customer.id == Invoice.customer_id)
                .where(Invoice.id == invoice_id)
            )).first()
            if row is None:
                raise ToolError(f"Invoice {invoice_id} not found")
            inv, cust = row
            reminders = (await db.scalars(
                select(InvoiceReminder).where(InvoiceReminder.invoice_id == invoice_id)
                .order_by(InvoiceReminder.created_at)
            )).all()
        return {
            "invoice_id": inv.id, "number": inv.number, "status": inv.status, "currency": inv.currency,
            "amount": money(inv.amount_cents), "amount_paid": money(inv.amount_paid_cents),
            "outstanding": money(inv.amount_cents - inv.amount_paid_cents),
            "issue_date": inv.issue_date.isoformat(), "due_date": inv.due_date.isoformat(),
            "days_overdue": max(0, (today - inv.due_date).days) if inv.status != "paid" else 0,
            "customer": {"id": cust.id, "name": cust.name, "email": cust.email, "company": cust.company},
            "reminders": [{"channel": r.channel, "note": r.note, "at": r.created_at.isoformat()} for r in reminders],
        }

    @srv.tool(annotations=READ)
    async def get_customer(customer_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """Get one customer with a summary of their open invoices."""
        async with session_factory() as db:
            cust = await db.get(Customer, customer_id)
            if cust is None:
                raise ToolError(f"Customer {customer_id} not found")
            open_invoices = (await db.scalars(
                select(Invoice).where(Invoice.customer_id == customer_id, Invoice.status != "paid")
            )).all()
        return {
            "id": cust.id, "name": cust.name, "email": cust.email, "phone": cust.phone, "company": cust.company,
            "open_invoice_count": len(open_invoices),
            "total_outstanding": money(sum(i.amount_cents - i.amount_paid_cents for i in open_invoices)),
        }

    @srv.tool(annotations=READ)
    async def search_customers(
        query: Annotated[str, Field(min_length=2, max_length=100)],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Find customers whose name, email or company contains the text."""
        q = query.lower()
        async with session_factory() as db:
            rows = (await db.scalars(
                select(Customer).where(
                    func.lower(Customer.name).contains(q, autoescape=True)
                    | func.lower(Customer.email).contains(q, autoescape=True)
                    | func.lower(func.coalesce(Customer.company, "")).contains(q, autoescape=True)
                ).order_by(Customer.name).limit(limit)
            )).all()
        return {"count": len(rows), "customers": [
            {"id": c.id, "name": c.name, "email": c.email, "company": c.company} for c in rows]}

    @srv.tool(annotations=READ)
    async def list_purchase_requests(
        status: Literal["pending", "approved", "rejected", "ordered"] | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List purchase requests, optionally filtered by status, newest first."""
        async with session_factory() as db:
            stmt = select(PurchaseRequest).order_by(PurchaseRequest.created_on.desc(), PurchaseRequest.id.desc()).limit(limit)
            if status:
                stmt = stmt.where(PurchaseRequest.status == status)
            rows = (await db.scalars(stmt)).all()
        return {"count": len(rows), "purchase_requests": [{
            "id": r.id, "requester": r.requester, "item": r.item, "quantity": r.quantity,
            "estimated_cost": money(r.estimated_cost_cents), "currency": r.currency,
            "status": r.status, "created_on": r.created_on.isoformat()} for r in rows]}

    @srv.tool(annotations=READ)
    async def list_quotations(purchase_request_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """List vendor quotations for a purchase request, cheapest first."""
        today = today_fn()
        async with session_factory() as db:
            pr = await db.get(PurchaseRequest, purchase_request_id)
            if pr is None:
                raise ToolError(f"Purchase request {purchase_request_id} not found")
            rows = (await db.execute(
                select(Quotation, Vendor).join(Vendor, Vendor.id == Quotation.vendor_id)
                .where(Quotation.purchase_request_id == purchase_request_id)
                .order_by(Quotation.amount_cents.asc())
            )).all()
        return {
            "purchase_request": {"id": pr.id, "item": pr.item, "quantity": pr.quantity,
                                 "estimated_cost": money(pr.estimated_cost_cents), "status": pr.status},
            "count": len(rows),
            "quotations": [{
                "quotation_id": q.id, "vendor_id": v.id, "vendor": v.name, "vendor_email": v.email,
                "amount": money(q.amount_cents), "currency": q.currency,
                "valid_until": q.valid_until.isoformat(), "expired": q.valid_until < today, "notes": q.notes,
            } for q, v in rows],
        }

    @srv.tool(annotations=LOCAL_WRITE)
    async def log_invoice_reminder(
        invoice_id: Annotated[int, Field(ge=1)],
        channel: Literal["email", "phone", "other"],
        note: Annotated[str, Field(min_length=1, max_length=500, description="What was sent / said")],
    ) -> dict[str, Any]:
        """Record that a payment reminder was sent for an invoice (writes one row to invoice_reminders)."""
        async with session_factory() as db:
            inv = await db.get(Invoice, invoice_id)
            if inv is None:
                raise ToolError(f"Invoice {invoice_id} not found")
            if inv.status == "paid":
                raise ToolError(f"Invoice {inv.number} is already paid; no reminder needed")
            rem = InvoiceReminder(invoice_id=invoice_id, channel=channel, note=note)
            db.add(rem)
            await db.commit()
            total = await db.scalar(select(func.count()).select_from(InvoiceReminder).where(InvoiceReminder.invoice_id == invoice_id))
        return {"reminder_id": rem.id, "invoice": inv.number, "total_reminders": total}

    @srv.tool(annotations=LOCAL_WRITE)
    async def update_purchase_request_status(
        request_id: Annotated[int, Field(ge=1)],
        new_status: Literal["approved", "rejected", "ordered"],
    ) -> dict[str, Any]:
        """Move a purchase request forward: pending -> approved/rejected, approved -> ordered."""
        allowed = {"pending": {"approved", "rejected"}, "approved": {"ordered"}}
        async with session_factory() as db:
            pr = await db.get(PurchaseRequest, request_id)
            if pr is None:
                raise ToolError(f"Purchase request {request_id} not found")
            if new_status not in allowed.get(pr.status, set()):
                raise ToolError(f"Cannot change status from '{pr.status}' to '{new_status}'")
            old = pr.status
            pr.status = new_status
            await db.commit()
        return {"id": request_id, "from": old, "to": new_status}

    return srv


def main() -> None:
    from app.core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.business_database_url or settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(BusinessBase.metadata.create_all)
        if settings.seed_demo_data:
            async with maker() as db:
                await seed_demo_data(db, date.today())
        await engine.dispose()  # the server below runs in a NEW event loop; connections can't be shared

    asyncio.run(prepare())
    create_server(maker).run("stdio")


if __name__ == "__main__":
    main()
