"""Tag autocomplete (/api/v1/tags)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentPrincipal, DbSession
from app.schemas.categories import TagCountOut
from app.services.categories import TagService

router = APIRouter(prefix="/tags", tags=["tags"])


def get_tag_service(db: DbSession) -> TagService:
    return TagService(db)


TagSvc = Annotated[TagService, Depends(get_tag_service)]


@router.get("")
async def list_tags(
    principal: CurrentPrincipal,
    svc: TagSvc,
    family_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=40, description="Substring of the tag")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TagCountOut]:
    """Tags used on live transactions (my personal ones, or the family's), most used first."""
    return await svc.list_counts(principal.user, family_id, q, limit)
