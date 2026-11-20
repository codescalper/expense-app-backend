"""Limit/offset pagination for list endpoints.

Endpoint::

    @router.get("")
    async def list_things(principal: CurrentPrincipal, page: Pagination, ...) -> Page[ThingOut]:
        rows, total = await paginate(db, stmt.order_by(Thing.created_at.desc(), Thing.id), page)
        return Page.build([ThingOut.model_validate(r) for r in rows], total, page)

Always give ``stmt`` a deterministic ``ORDER BY`` (end with the primary key) so pages are stable.
Response JSON: ``{"items": [...], "total": 123, "limit": 50, "offset": 0, "has_more": true}``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class PageParams:
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size")] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


Pagination = Annotated[PageParams, Depends(page_params)]


class Page[ItemT](BaseModel):
    items: list[ItemT]
    total: int
    limit: int
    offset: int
    has_more: bool

    @classmethod
    def build(cls, items: Sequence[ItemT], total: int, params: PageParams) -> Page[ItemT]:
        return cls(
            items=list(items),
            total=total,
            limit=params.limit,
            offset=params.offset,
            has_more=params.offset + len(items) < total,
        )


async def paginate[RowT](
    session: AsyncSession, stmt: Select[RowT], params: PageParams
) -> tuple[list[RowT], int]:
    """Run ``stmt`` (a single-entity/column select) for one page and count all matches."""
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = list(await session.scalars(stmt.limit(params.limit).offset(params.offset)))
    return rows, total
