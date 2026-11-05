"""Gmail and Drive backends.

Each MCP server talks to a *backend* object. Two implementations per service:
  - Mock*   : in-memory, deterministic demo data. Default. Needs no credentials.
  - Google* : real Gmail / Drive APIs. Needs `pip install -r requirements-google.txt`
              and an authorized-user credentials JSON (GOOGLE_CREDENTIALS_FILE).

NOTE: the Google* classes have not been run against live Google accounts yet.
"""
import asyncio
import base64
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from typing import Protocol


def _iso(day: date, hour: int = 10) -> str:
    return datetime.combine(day, time(hour, 0), tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------- Gmail ----
class GmailBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[dict]: ...
    async def get_thread(self, thread_id: str) -> dict | None: ...
    async def create_draft(self, to: str, subject: str, body: str) -> dict: ...
    async def send(self, to: str, subject: str, body: str) -> dict: ...


class MockGmailBackend:
    ME = "finance@mycompany.example"

    def __init__(self, today: date | None = None):
        t = today or date.today()
        d = lambda off: _iso(t + timedelta(days=off))  # noqa: E731
        self._messages: list[dict] = [
            # Cedar Logistics: promised to pay INV-1003
            dict(id="m-1", thread_id="t-100", frm="ap@cedarlogistics.example", to=self.ME,
                 subject="Re: Invoice INV-1003", date=d(-10),
                 body="Hello, we have received INV-1003 for INR 92,500. Our accounts team has scheduled "
                      "the payment for next Friday's payment run."),
            dict(id="m-2", thread_id="t-100", frm=self.ME, to="ap@cedarlogistics.example",
                 subject="Re: Invoice INV-1003", date=d(-9),
                 body="Thanks for confirming, we will look out for the payment."),
            # Asha Traders: disputes INV-1001 (do NOT send a harsh reminder!)
            dict(id="m-3", thread_id="t-101", frm="accounts@ashatraders.example", to=self.ME,
                 subject="Query on INV-1001", date=d(-50),
                 body="We were billed for 500 units but received only 420. Please issue a corrected invoice "
                      "before we can release payment for INV-1001."),
            dict(id="m-4", thread_id="t-101", frm=self.ME, to="accounts@ashatraders.example",
                 subject="Re: Query on INV-1001", date=d(-48),
                 body="Looking into this with the warehouse team and will revert shortly."),
            # Bright Foods: partial payment made
            dict(id="m-5", thread_id="t-102", frm="finance@brightfoods.example", to=self.ME,
                 subject="Part payment against INV-1002", date=d(-30),
                 body="We have paid INR 20,000 against INV-1002 as a first instalment. Balance by month end."),
            # Vendor quotation
            dict(id="m-6", thread_id="t-103", frm="sales@globalpackaging.example", to=self.ME,
                 subject="Quotation for corrugated boxes", date=d(-5),
                 body="Please find our quotation for 2000 corrugated boxes at INR 142,000, valid for 14 days."),
        ]
        self._drafts: list[dict] = []
        self._next = 100

    @staticmethod
    def _summary(m: dict) -> dict:
        return {"message_id": m["id"], "thread_id": m["thread_id"], "from": m["frm"], "to": m["to"],
                "subject": m["subject"], "date": m["date"], "snippet": m["body"][:120]}

    @staticmethod
    def _matches(m: dict, query: str) -> bool:
        for token in query.lower().split():
            if token.startswith("from:"):
                ok = token[5:] in m["frm"].lower()
            elif token.startswith("to:"):
                ok = token[3:] in m["to"].lower()
            elif token.startswith("subject:"):
                ok = token[8:] in m["subject"].lower()
            else:
                ok = token in f'{m["subject"]} {m["body"]} {m["frm"]} {m["to"]}'.lower()
            if not ok:
                return False
        return True

    async def search(self, query: str, max_results: int) -> list[dict]:
        hits = [m for m in self._messages if self._matches(m, query)]
        hits.sort(key=lambda m: m["date"], reverse=True)
        return [self._summary(m) for m in hits[:max_results]]

    async def get_thread(self, thread_id: str) -> dict | None:
        msgs = sorted((m for m in self._messages if m["thread_id"] == thread_id), key=lambda m: m["date"])
        if not msgs:
            return None
        return {"thread_id": thread_id, "subject": msgs[0]["subject"], "messages": [
            {"message_id": m["id"], "from": m["frm"], "to": m["to"], "date": m["date"], "body": m["body"]}
            for m in msgs]}

    async def create_draft(self, to: str, subject: str, body: str) -> dict:
        self._next += 1
        draft = {"draft_id": f"d-{self._next}", "to": to, "subject": subject}
        self._drafts.append({**draft, "body": body})
        return draft

    async def send(self, to: str, subject: str, body: str) -> dict:
        self._next += 1
        mid = f"m-{self._next}"
        self._messages.append(dict(
            id=mid, thread_id=f"t-{self._next}", frm=self.ME, to=to, subject=subject,
            date=datetime.now(timezone.utc).isoformat(), body=body))
        return {"message_id": mid, "thread_id": f"t-{self._next}", "to": to, "subject": subject, "status": "sent"}


def _raw_email(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _gmail_body(payload: dict) -> str:
    """Depth-first search for the first text/plain part."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        found = _gmail_body(part)
        if found:
            return found
    return ""


class GoogleGmailBackend:
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    def __init__(self, credentials_file: str):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(credentials_file, self.SCOPES)
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    async def search(self, query: str, max_results: int) -> list[dict]:
        def run():
            listing = self._svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            out = []
            for ref in listing.get("messages", []):
                m = self._svc.users().messages().get(
                    userId="me", id=ref["id"], format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"]).execute()
                h = {x["name"].lower(): x["value"] for x in m["payload"]["headers"]}
                out.append({"message_id": m["id"], "thread_id": m["threadId"], "from": h.get("from", ""),
                            "to": h.get("to", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
                            "snippet": m.get("snippet", "")})
            return out
        return await asyncio.to_thread(run)

    async def get_thread(self, thread_id: str) -> dict | None:
        def run():
            t = self._svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
            msgs = []
            for m in t.get("messages", []):
                h = {x["name"].lower(): x["value"] for x in m["payload"]["headers"]}
                msgs.append({"message_id": m["id"], "from": h.get("from", ""), "to": h.get("to", ""),
                             "date": h.get("date", ""), "body": _gmail_body(m["payload"])})
            subject = next((m for m in t.get("messages", [])), None)
            hdrs = {x["name"].lower(): x["value"] for x in subject["payload"]["headers"]} if subject else {}
            return {"thread_id": thread_id, "subject": hdrs.get("subject", ""), "messages": msgs}
        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # googleapiclient.errors.HttpError 404 -> not found
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise

    async def create_draft(self, to: str, subject: str, body: str) -> dict:
        def run():
            d = self._svc.users().drafts().create(
                userId="me", body={"message": {"raw": _raw_email(to, subject, body)}}).execute()
            return {"draft_id": d["id"], "to": to, "subject": subject}
        return await asyncio.to_thread(run)

    async def send(self, to: str, subject: str, body: str) -> dict:
        def run():
            m = self._svc.users().messages().send(userId="me", body={"raw": _raw_email(to, subject, body)}).execute()
            return {"message_id": m["id"], "thread_id": m.get("threadId"), "to": to, "subject": subject, "status": "sent"}
        return await asyncio.to_thread(run)


# ---------------------------------------------------------------- Drive ----
class DriveBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[dict]: ...
    async def read(self, file_id: str) -> dict | None: ...   # {file_id, name, mime_type, content}


class MockDriveBackend:
    def __init__(self, today: date | None = None):
        t = today or date.today()
        mod = lambda off: _iso(t + timedelta(days=off))  # noqa: E731
        self._files = [
            dict(file_id="f-1", name="Payment Terms Policy", mime_type="application/vnd.google-apps.document",
                 modified=mod(-200),
                 content="PAYMENT TERMS POLICY\nStandard terms: net 30 days from invoice date.\n"
                         "Reminder schedule: friendly reminder at 7 days overdue, firm reminder at 30 days, "
                         "final notice at 60 days. Escalate to the finance manager after 60 days.\n"
                         "Never send a payment reminder for an invoice that is under dispute."),
            dict(file_id="f-2", name="Collections Playbook", mime_type="application/vnd.google-apps.document",
                 modified=mod(-90),
                 content="COLLECTIONS PLAYBOOK\nTone: polite, specific, brief. Always quote the invoice number, "
                         "amount outstanding and original due date. Offer a payment link or bank details. "
                         "If the customer has promised a payment date, acknowledge it instead of chasing."),
            dict(file_id="f-3", name="Q3 Sales Report", mime_type="application/vnd.google-apps.spreadsheet",
                 modified=mod(-14),
                 content="Region,Revenue (INR),Growth\nNorth,4200000,8%\nWest,3900000,5%\nSouth,2800000,12%\nEast,1900000,-2%"),
            dict(file_id="f-4", name="Vendor Onboarding Checklist", mime_type="application/vnd.google-apps.document",
                 modified=mod(-120),
                 content="VENDOR ONBOARDING\n1. Collect GST certificate\n2. Bank verification\n3. Sign NDA\n4. Add to approved vendor list"),
        ]

    async def search(self, query: str, max_results: int) -> list[dict]:
        tokens = query.lower().split()
        hits = [f for f in self._files if all(t in f"{f['name']} {f['content']}".lower() for t in tokens)]
        hits.sort(key=lambda f: f["modified"], reverse=True)
        return [{"file_id": f["file_id"], "name": f["name"], "mime_type": f["mime_type"],
                 "modified": f["modified"]} for f in hits[:max_results]]

    async def read(self, file_id: str) -> dict | None:
        for f in self._files:
            if f["file_id"] == file_id:
                return {k: f[k] for k in ("file_id", "name", "mime_type", "content")}
        return None


class GoogleDriveBackend:
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

    def __init__(self, credentials_file: str):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(credentials_file, self.SCOPES)
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    async def search(self, query: str, max_results: int) -> list[dict]:
        safe = query.replace("\\", "\\\\").replace("'", "\\'")

        def run():
            res = self._svc.files().list(
                q=f"fullText contains '{safe}' and trashed = false", pageSize=max_results,
                orderBy="modifiedTime desc", fields="files(id,name,mimeType,modifiedTime)").execute()
            return [{"file_id": f["id"], "name": f["name"], "mime_type": f["mimeType"],
                     "modified": f.get("modifiedTime", "")} for f in res.get("files", [])]
        return await asyncio.to_thread(run)

    async def read(self, file_id: str) -> dict | None:
        def run():
            meta = self._svc.files().get(fileId=file_id, fields="id,name,mimeType").execute()
            mime = meta["mimeType"]
            if mime == "application/vnd.google-apps.document":
                data = self._svc.files().export(fileId=file_id, mimeType="text/plain").execute()
            elif mime == "application/vnd.google-apps.spreadsheet":
                data = self._svc.files().export(fileId=file_id, mimeType="text/csv").execute()
            elif mime.startswith("text/"):
                data = self._svc.files().get_media(fileId=file_id).execute()
            else:
                raise ValueError(f"Unsupported file type: {mime}")
            text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
            return {"file_id": meta["id"], "name": meta["name"], "mime_type": mime, "content": text}
        try:
            return await asyncio.to_thread(run)
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise
