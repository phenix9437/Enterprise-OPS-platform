from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DB

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: DB):
    """Liveness + DB connectivity check (used by Docker/AWS/Railway)."""
    await db.execute(text("SELECT 1"))
    return {"status": "ok"}
