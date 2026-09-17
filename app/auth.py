"""Admin authentication via static API key (X-Admin-Key header)."""
import secrets

from fastapi import Header, HTTPException, status

from app.config import ADMIN_API_KEY


def require_admin(x_admin_key: str | None = Header(default=None)) -> str:
    """Dependency: validates the admin key, returns the actor identity.

    The key itself is not stored in the audit log; 'admin' is used as actor.
    """
    if not x_admin_key or not secrets.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing admin key")
    return "admin"