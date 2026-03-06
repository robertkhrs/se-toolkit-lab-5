"""ETL pipeline: fetch data from the autochecker API and load it into the database.

The autochecker dashboard API provides two endpoints:
- GET /api/items — lab/task catalog
- GET /api/logs  — anonymized check results (supports ?since= and ?limit= params)

Both require HTTP Basic Auth (email + password from settings).
"""

from datetime import datetime
from typing import Any

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.settings import settings


# Type aliases for API responses
ItemDict = dict[str, Any]
LogDict = dict[str, Any]


# ---------------------------------------------------------------------------
# Extract — fetch data from the autochecker API
# ---------------------------------------------------------------------------


async def fetch_items() -> list[ItemDict]:
    """Fetch the lab/task catalog from the autochecker API.

    - Uses httpx.AsyncClient to GET {settings.autochecker_api_url}/api/items
    - Passes HTTP Basic Auth using settings.autochecker_email and
      settings.autochecker_password
    - The response is a JSON array of objects with keys:
      lab (str), task (str | null), title (str), type ("lab" | "task")
    - Returns the parsed list of dicts
    - Raises an exception if the response status is not 200
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.autochecker_api_url}/api/items",
            auth=(settings.autochecker_email, settings.autochecker_password),
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]


async def fetch_logs(since: datetime | None = None) -> list[LogDict]:
    """Fetch check results from the autochecker API.

    - Uses httpx.AsyncClient to GET {settings.autochecker_api_url}/api/logs
    - Passes HTTP Basic Auth using settings.autochecker_email and
      settings.autochecker_password
    - Query parameters:
      - limit=500 (fetch in batches)
      - since={iso timestamp} if provided (for incremental sync)
    - The response JSON has shape:
      {"logs": [...], "count": int, "has_more": bool}
    - Handles pagination: keeps fetching while has_more is True
      - Uses the submitted_at of the last log as the new "since" value
    - Returns the combined list of all log dicts from all pages
    """
    all_logs: list[LogDict] = []
    current_since = since

    async with httpx.AsyncClient() as client:
        while True:
            params: dict[str, str | int] = {"limit": 500}
            if current_since is not None:
                params["since"] = current_since.isoformat()

            response = await client.get(
                f"{settings.autochecker_api_url}/api/logs",
                params=params,
                auth=(settings.autochecker_email, settings.autochecker_password),
            )
            response.raise_for_status()
            data = response.json()

            logs = data.get("logs", [])
            all_logs.extend(logs)

            if not data.get("has_more", False):
                break

            # Use the last log's submitted_at as the new "since" for pagination
            current_since = datetime.fromisoformat(logs[-1]["submitted_at"])

    return all_logs


# ---------------------------------------------------------------------------
# Load — insert fetched data into the local database
# ---------------------------------------------------------------------------


async def load_items(items: list[ItemDict], session: AsyncSession) -> int:
    """Load items (labs and tasks) into the database.

    - Imports ItemRecord from app.models.item
    - Processes labs first (items where type="lab"):
      - For each lab, checks if an item with type="lab" and matching title
        already exists (SELECT)
      - If not, INSERTs a new ItemRecord(type="lab", title=lab_title)
      - Builds a dict mapping the lab's short ID (the "lab" field, e.g.
        "lab-01") to the lab's database record, so you can look up
        parent IDs when processing tasks
    - Then processes tasks (items where type="task"):
      - Finds the parent lab item using the task's "lab" field (e.g.
        "lab-01") as the key into the dict built above
      - Checks if a task with this title and parent_id already exists
      - If not, INSERTs a new ItemRecord(type="task", title=task_title,
        parent_id=lab_item.id)
    - Commits after all inserts
    - Returns the number of newly created items
    """
    from sqlmodel import select
    from app.models.item import ItemRecord

    new_items_count = 0
    lab_id_map: dict[str, ItemRecord] = {}  # Maps short lab ID (e.g. "lab-01") to ItemRecord

    # Process labs first
    for item in items:
        if item.get("type") != "lab":
            continue

        lab_title = item["title"]
        lab_short_id = item["lab"]

        # Check if lab already exists
        existing = await session.exec(
            select(ItemRecord).where(ItemRecord.type == "lab").where(ItemRecord.title == lab_title)
        )
        lab_record = existing.first()

        if lab_record is None:
            # Create new lab record
            lab_record = ItemRecord(type="lab", title=lab_title)
            session.add(lab_record)
            await session.flush()  # Flush to get the ID
            new_items_count += 1

        # Map short ID to record for later lookup
        lab_id_map[lab_short_id] = lab_record

    # Process tasks
    for item in items:
        if item.get("type") != "task":
            continue

        task_title = item["title"]
        lab_short_id = item["lab"]

        # Get parent lab from map
        parent_lab = lab_id_map.get(lab_short_id)
        if parent_lab is None:
            # Parent lab not found, skip this task
            continue

        # Check if task already exists
        existing = await session.exec(
            select(ItemRecord)
            .where(ItemRecord.type == "task")
            .where(ItemRecord.title == task_title)
            .where(ItemRecord.parent_id == parent_lab.id)
        )
        task_record = existing.first()

        if task_record is None:
            # Create new task record
            task_record = ItemRecord(type="task", title=task_title, parent_id=parent_lab.id)
            session.add(task_record)
            await session.flush()
            new_items_count += 1

    await session.commit()
    return new_items_count


async def load_logs(
    logs: list[LogDict], items_catalog: list[ItemDict], session: AsyncSession
) -> int:
    """Load interaction logs into the database.

    Args:
        logs: Raw log dicts from the API (each has lab, task, student_id, etc.)
        items_catalog: Raw item dicts from fetch_items() — needed to map
            short IDs (e.g. "lab-01", "setup") to item titles stored in the DB.
        session: Database session.

    - Imports Learner from app.models.learner
    - Imports InteractionLog from app.models.interaction
    - Imports ItemRecord from app.models.item
    - Builds a lookup from (lab_short_id, task_short_id) to item title
      using items_catalog. For labs, the key is (lab, None). For tasks,
      the key is (lab, task). The value is the item's title.
    - For each log dict:
      1. Finds or creates a Learner by external_id (log["student_id"])
         - If creating, sets student_group from log["group"]
      2. Finds the matching item in the database:
         - Uses the lookup to get the title for (log["lab"], log["task"])
         - Queries the DB for an ItemRecord with that title
         - Skips this log if no matching item is found
      3. Checks if an InteractionLog with this external_id already exists
         (for idempotent upsert — skips if it does)
      4. Creates InteractionLog with:
         - external_id = log["id"]
         - learner_id = learner.id
         - item_id = item.id
         - kind = "attempt"
         - score = log["score"]
         - checks_passed = log["passed"]
         - checks_total = log["total"]
         - created_at = parsed log["submitted_at"]
    - Commits after all inserts
    - Returns the number of newly created interactions
    """
    from datetime import datetime

    from sqlmodel import select

    from app.models.interaction import InteractionLog
    from app.models.item import ItemRecord
    from app.models.learner import Learner

    # Build lookup: (lab_short_id, task_short_id) -> title
    item_title_lookup: dict[tuple[str, str | None], str] = {}
    for item in items_catalog:
        lab_short_id = item["lab"]
        task_short_id = item.get("task")
        title = item["title"]
        item_title_lookup[(lab_short_id, task_short_id)] = title

    # Build lookup: lab_short_id -> lab_title (for finding parent lab by short ID)
    lab_title_lookup: dict[str, str] = {}
    for item in items_catalog:
        if item.get("type") == "lab":
            lab_title_lookup[item["lab"]] = item["title"]

    new_interactions_count = 0

    for log in logs:
        # 1. Find or create Learner by external_id
        student_id = log["student_id"]
        student_group = log.get("group", "")

        # Query by external_id (not primary key id)
        existing_learner = await session.exec(
            select(Learner).where(Learner.external_id == student_id)
        )
        learner = existing_learner.first()

        if learner is None:
            learner = Learner(external_id=student_id, student_group=student_group)
            session.add(learner)
            await session.flush()

        # 2. Find matching item
        lab_short_id = log["lab"]
        task_short_id = log.get("task")

        # Determine if this is a lab or a task
        if task_short_id is None:
            # This is a lab log - find the lab item directly by title
            item_title = lab_title_lookup.get(lab_short_id)
            if item_title is None:
                continue  # No matching lab found

            existing_item = await session.exec(
                select(ItemRecord).where(ItemRecord.title == item_title)
            )
            item = existing_item.first()

            if item is None:
                continue  # Still no matching item, skip
        else:
            # This is a task log - need to find parent lab first, then the task
            # Step 2a: Get the lab title from the lookup
            lab_title = lab_title_lookup.get(lab_short_id)
            if lab_title is None:
                continue  # No matching lab found

            # Step 2b: Query DB for the parent lab to get its ID
            existing_parent = await session.exec(
                select(ItemRecord).where(ItemRecord.title == lab_title)
            )
            parent_lab = existing_parent.first()

            if parent_lab is None:
                continue  # Parent lab not found in DB

            # Step 2c: Get the task title from the lookup
            item_title = item_title_lookup.get((lab_short_id, task_short_id))
            if item_title is None:
                continue  # No matching task title found

            # Step 2d: Query DB for the task using BOTH title AND parent_id
            existing_item = await session.exec(
                select(ItemRecord)
                .where(ItemRecord.title == item_title)
                .where(ItemRecord.parent_id == parent_lab.id)  # type: ignore[arg-type]
            )
            item = existing_item.first()

            if item is None:
                continue  # Task not found with this title and parent, skip

        # 3. Check if InteractionLog with this external_id already exists
        existing_interaction = await session.exec(
            select(InteractionLog).where(InteractionLog.external_id == int(log["id"]))  # type: ignore[arg-type]
        )
        if existing_interaction.first() is not None:
            # Already exists, skip for idempotency
            continue

        # 4. Create InteractionLog
        # Assert that learner and item have IDs (they should after flush/commit)
        assert learner.id is not None, "Learner ID should not be None"
        assert item.id is not None, "Item ID should not be None"

        interaction = InteractionLog(
            external_id=int(log["id"]),  # Convert to int for DB storage
            learner_id=learner.id,
            item_id=item.id,
            kind="attempt",
            score=log.get("score"),  # type: ignore[arg-type]
            checks_passed=log.get("passed"),  # type: ignore[arg-type]
            checks_total=log.get("total"),  # type: ignore[arg-type]
            created_at=datetime.fromisoformat(log["submitted_at"]),
        )
        session.add(interaction)
        new_interactions_count += 1

    await session.commit()
    return new_interactions_count


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def sync(session: AsyncSession) -> dict[str, int]:
    """Run the full ETL pipeline.

    - Step 1: Fetch items from the API (keep the raw list) and load them
      into the database
    - Step 2: Determine the last synced timestamp
      - Query the most recent created_at from InteractionLog
      - If no records exist, since=None (fetch everything)
    - Step 3: Fetch logs since that timestamp and load them
      - Pass the raw items list to load_logs so it can map short IDs
        to titles
    - Returns a dict: {"new_records": <number of new interactions>,
                       "total_records": <total interactions in DB>}
    """
    from sqlmodel import func, select

    from app.models.interaction import InteractionLog

    # Step 1: Fetch and load items
    items = await fetch_items()
    await load_items(items, session)

    # Step 2: Determine the last synced timestamp
    # Query the most recent created_at from InteractionLog
    result = await session.exec(
        select(func.max(InteractionLog.created_at))
    )
    last_synced = result.first()

    since = last_synced if last_synced else None

    # Step 3: Fetch logs since that timestamp and load them
    logs = await fetch_logs(since=since)
    new_records = await load_logs(logs, items, session)

    # Count total interactions in DB
    total_result = await session.exec(
        select(func.count(InteractionLog.id).label("total"))  # type: ignore[arg-type]
    )
    total_records: int = total_result.one()  # type: ignore[assignment]

    return {"new_records": new_records, "total_records": total_records}
