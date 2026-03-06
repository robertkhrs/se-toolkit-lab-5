"""Router for analytics endpoints.

Each endpoint performs SQL aggregation queries on the interaction data
populated by the ETL pipeline. All endpoints require a `lab` query
parameter to filter results by lab (e.g., "lab-01").
"""

from fastapi import APIRouter, Depends, Query
from sqlmodel import select, func, case
from sqlmodel.ext.asyncio.session import AsyncSession
from app.database import get_session
from app.models.item import ItemRecord
from app.models.interaction import InteractionLog
from app.models.learner import Learner

router = APIRouter()


async def get_lab_id(lab: str, session: AsyncSession) -> int | None:
    """Helper to find the parent lab ID based on the query parameter."""
    search_term = lab.replace("-", " ").title()  # e.g., "lab-04" -> "Lab 04"
    stmt = (
        select(ItemRecord.id)
        .where(ItemRecord.type == "lab")
        .where(ItemRecord.title.contains(search_term))
    )
    result = await session.exec(stmt)
    return result.first()


@router.get("/scores")
async def get_scores(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Score distribution histogram for a given lab."""
    lab_id = await get_lab_id(lab, session)
    
    # Base buckets structure (ensures all 4 are returned even if count is 0)
    buckets = {
        "0-25": 0,
        "26-50": 0,
        "51-75": 0,
        "76-100": 0,
    }

    if lab_id:
        stmt = (
            select(
                case(
                    (InteractionLog.score <= 25, "0-25"),  # type: ignore
                    (InteractionLog.score <= 50, "26-50"),  # type: ignore
                    (InteractionLog.score <= 75, "51-75"),  # type: ignore
                    else_="76-100"
                ).label("bucket"),
                func.count(InteractionLog.id).label("count")  # type: ignore
            )
            .join(ItemRecord, InteractionLog.item_id == ItemRecord.id)  # type: ignore
            .where(ItemRecord.parent_id == lab_id)
            .where(InteractionLog.score != None)
            .group_by("bucket")
        )
        
        result = await session.exec(stmt)
        
        for row in result.all():
            if row[0] in buckets:
                buckets[row[0]] = row[1]

    return [{"bucket": k, "count": v} for k, v in buckets.items()]


@router.get("/pass-rates")
async def get_pass_rates(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Per-task pass rates for a given lab."""
    lab_id = await get_lab_id(lab, session)
    if not lab_id:
        return []

    stmt = (
        select(
            ItemRecord.title.label("task"),
            func.round(func.avg(InteractionLog.score), 1).label("avg_score"),
            func.count(InteractionLog.id).label("attempts")
        )
        .join(InteractionLog, InteractionLog.item_id == ItemRecord.id)
        .where(ItemRecord.parent_id == lab_id)
        .group_by(ItemRecord.title)
        .order_by(ItemRecord.title)
    )
    
    result = await session.exec(stmt)
    
    return [
        {
            "task": row[0],
            "avg_score": float(row[1]) if row[1] is not None else 0.0,
            "attempts": row[2]
        }
        for row in result.all()
    ]


@router.get("/timeline")
async def get_timeline(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Submissions per day for a given lab."""
    lab_id = await get_lab_id(lab, session)
    if not lab_id:
        return []

    stmt = (
        select(
            func.date(InteractionLog.created_at).label("date"),
            func.count(InteractionLog.id).label("submissions")
        )
        .join(ItemRecord, InteractionLog.item_id == ItemRecord.id)
        .where(ItemRecord.parent_id == lab_id)
        .group_by("date")
        .order_by("date")
    )
    
    result = await session.exec(stmt)
    
    return [
        {"date": str(row[0]), "submissions": row[1]}
        for row in result.all()
    ]


@router.get("/groups")
async def get_groups(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Per-group performance for a given lab."""
    lab_id = await get_lab_id(lab, session)
    if not lab_id:
        return []

    stmt = (
        select(
            Learner.student_group.label("group"),
            func.round(func.avg(InteractionLog.score), 1).label("avg_score"),
            func.count(func.distinct(Learner.id)).label("students")
        )
        .select_from(InteractionLog)
        .join(ItemRecord, InteractionLog.item_id == ItemRecord.id)
        .join(Learner, InteractionLog.learner_id == Learner.id)
        .where(ItemRecord.parent_id == lab_id)
        .group_by(Learner.student_group)
        .order_by(Learner.student_group)
    )
    
    result = await session.exec(stmt)
    
    return [
        {
            "group": row[0],
            "avg_score": float(row[1]) if row[1] is not None else 0.0,
            "students": row[2]
        }
        for row in result.all()
    ]