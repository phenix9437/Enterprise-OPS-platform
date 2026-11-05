"""Tables of the *customer's business database* (not the platform's own tables).

They live on a separate metadata object so the platform's create_all never touches them.
In a real deployment these already exist in the company's PostgreSQL and the MCP server is
pointed at them with a restricted DB role.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class BusinessBase(DeclarativeBase):
    pass


class Customer(BusinessBase):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    company: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Invoice(BusinessBase):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    amount_paid_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="unpaid")  # unpaid | partial | paid


class InvoiceReminder(BusinessBase):
    __tablename__ = "invoice_reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Vendor(BusinessBase):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254))


class PurchaseRequest(BusinessBase):
    __tablename__ = "purchase_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester: Mapped[str] = mapped_column(String(120))
    item: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    estimated_cost_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|ordered
    created_on: Mapped[date] = mapped_column(Date)


class Quotation(BusinessBase):
    __tablename__ = "quotations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purchase_request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id"), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    valid_until: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)


async def seed_demo_data(db: AsyncSession, today: date) -> bool:
    """Insert a small, deterministic dataset (dates relative to `today`). No-op if data exists."""
    if await db.scalar(select(Customer.id).limit(1)) is not None:
        return False

    d = lambda days: today + timedelta(days=days)  # noqa: E731
    customers = [
        Customer(name="Asha Traders", email="accounts@ashatraders.example", company="Asha Traders Pvt Ltd"),
        Customer(name="Bright Foods", email="finance@brightfoods.example", company="Bright Foods Ltd"),
        Customer(name="Cedar Logistics", email="ap@cedarlogistics.example", company="Cedar Logistics LLP"),
        Customer(name="Delta Pharma", email="payables@deltapharma.example", company="Delta Pharma Ltd"),
        Customer(name="Evergreen Textiles", email="billing@evergreentextiles.example", company="Evergreen Textiles"),
        Customer(name="Falcon Auto Parts", email="accounts@falconauto.example", company="Falcon Auto Parts"),
    ]
    db.add_all(customers)
    await db.flush()
    c = {i + 1: cust.id for i, cust in enumerate(customers)}

    # (number, customer, amount INR, paid INR, due offset, status)
    invoices = [
        ("INV-1001", 1, 125000, 0, -75, "unpaid"),
        ("INV-1002", 2, 48000, 20000, -45, "partial"),
        ("INV-1003", 3, 92500, 0, -20, "unpaid"),
        ("INV-1004", 4, 15000, 0, -5, "unpaid"),
        ("INV-1005", 5, 67000, 0, 10, "unpaid"),     # not yet due
        ("INV-1006", 1, 30000, 30000, -60, "paid"),  # settled
        ("INV-1007", 6, 210000, 0, -32, "unpaid"),
    ]
    for number, cust, amount, paid, due_off, status in invoices:
        db.add(Invoice(
            number=number, customer_id=c[cust], amount_cents=amount * 100, amount_paid_cents=paid * 100,
            issue_date=d(due_off - 30), due_date=d(due_off), status=status,
        ))

    vendors = [
        Vendor(name="Global Packaging", email="sales@globalpackaging.example"),
        Vendor(name="Metro Stationers", email="quotes@metrostationers.example"),
        Vendor(name="Apex Office Supplies", email="sales@apexoffice.example"),
    ]
    db.add_all(vendors)
    await db.flush()

    prs = [
        PurchaseRequest(requester="Ravi (Operations)", item="Corrugated shipping boxes", quantity=2000,
                        estimated_cost_cents=150000 * 100, status="pending", created_on=d(-6)),
        PurchaseRequest(requester="Meera (IT)", item="USB-C laptop docks", quantity=10,
                        estimated_cost_cents=60000 * 100, status="pending", created_on=d(-3)),
        PurchaseRequest(requester="Ravi (Operations)", item="Printer toner cartridges", quantity=12,
                        estimated_cost_cents=36000 * 100, status="approved", created_on=d(-20)),
    ]
    db.add_all(prs)
    await db.flush()

    for vendor, amount, notes in [(vendors[0], 142000, "Bulk discount over 1500 units"),
                                  (vendors[1], 155000, None),
                                  (vendors[2], 138500, "Delivery in 3 weeks")]:
        db.add(Quotation(purchase_request_id=prs[0].id, vendor_id=vendor.id, amount_cents=amount * 100,
                         valid_until=d(14), notes=notes))
    for vendor, amount in [(vendors[1], 58000), (vendors[2], 61500)]:
        db.add(Quotation(purchase_request_id=prs[1].id, vendor_id=vendor.id, amount_cents=amount * 100,
                         valid_until=d(10)))
    await db.commit()
    return True
