"""FastAPI Dependencies - Auth, Tenant Context, DB access."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Will be expanded with:
# - get_current_user() - JWT validation
# - get_current_tenant() - tenant context from JWT
# - get_db_pool() - database connection
# - get_redis() - redis connection

security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Validate JWT token and return user data.

    TODO Phase 1: Implement JWT validation.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    # TODO: Decode and validate JWT
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Auth not yet implemented",
    )
