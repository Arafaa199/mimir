"""Recall — fan out over the estates a session holds, merge spine + floor, prove no leak.

Spec 07. Three properties, in priority order:

1. **It never leaks.** Every passage's estate is checked against the scope before it is
   returned, whatever the store said. A stray dataset raises EstateLeak and the WHOLE
   recall fails closed — never "return what we have". A missing answer is a bug someone
   notices; a leaked answer is a bug nobody notices.

2. **It never calls an LLM.** `only_context=True` returns graph facts and passages without
   a completion, so recall is local, costs $0, and ships work/confidential context to NO
   provider. §4 names the inference provider as a sink; this closes it at read time, not
   just at cognify time. Retrieval here, judgment in the caller (design law 6).

3. **It degrades instead of dying.** The spine (cognee/kuzu) holds an exclusive file lock
   on whatever estate is being cognified, so a backfill or a nightly shadow-write makes
   that estate temporarily unreadable. That is precisely what the pgvector FLOOR is for
   (spec 05 §2: the floor is the insurance). A spine failure degrades to the floor and says
   so. An EstateLeak is NOT degradable — it is the one error that must stop everything.
"""
import asyncio
from typing import List, Tuple

from ingress import CONFIDENTIAL
from passage import Passage
from scope import Scope


class EstateLeak(Exception):
    """A store returned content from outside the scope. Fail closed, loudly, always."""


def _unwrap(result, expected: str) -> List[str]:
    """Flatten cognee's envelope, asserting every item came from the dataset we asked for."""
    out: List[str] = []
    for item in result or []:
        if isinstance(item, dict) and "search_result" in item:
            got = item.get("dataset_name")
            if got not in (None, expected):
                raise EstateLeak(
                    f"cognee returned dataset {got!r} for a query scoped to {expected!r}. "
                    f"ACL isolation is not holding — refusing to return anything."
                )
            out.append(str(item["search_result"]))
        else:
            out.append(str(item))
    return [t for t in out if t and t.strip()]


async def _spine_dataset(query: str, dataset: str, k: int) -> List[Passage]:
    import cognee
    from cognee.modules.search.types import SearchType

    result = await cognee.search(
        query_text=query,
        query_type=SearchType.GRAPH_COMPLETION,
        datasets=[dataset],
        top_k=k,
        only_context=True,          # retrieval, not completion. No LLM. No provider.
    )
    return [Passage(estate=dataset, text=t, origin="spine")
            for t in _unwrap(result, dataset)]


async def _spine_confidential(query: str, k: int) -> List[Passage]:
    """Pointers, never content. These units were never cognified — their bodies have never
    reached any LLM — so there is nothing to retrieve and nothing we are willing to return.
    "This exists, go read the source" is useful and leaks nothing."""
    import os

    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )
    try:
        with conn, conn.cursor() as cur:
            # Trigram match on the TITLE only. The body is not in this table, and must
            # never be joined in from anywhere else.
            cur.execute(
                "SELECT title, source FROM mimir.provenance "
                "WHERE dataset = %s AND pointer_only AND title %% %s "
                "ORDER BY similarity(title, %s) DESC LIMIT %s",
                (CONFIDENTIAL, query, query, k),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        Passage(estate=CONFIDENTIAL, origin="spine", pointer_only=True,
                title=title, source=source,
                text=f"[CONFIDENTIAL — pointer only, content withheld] {title}. "
                     f"Retrieve the source directly: {source}")
        for title, source in rows
    ]


_existing: set = set()


async def _existing_datasets() -> set:
    """Which estates actually EXIST in cognee yet.

    `shared` is deliberately small and curated (writing to it is declassification), so for a
    long time it will not exist at all — and asking cognee for a dataset that has never been
    created raises DatasetNotFoundError for the WHOLE query. Left unfiltered, that made every
    default recall (scope = personal + shared) fail on the spine and answer from the floor
    alone: the graph we spent days building was silently not being consulted. A missing
    dataset is "nothing there", not "the spine is broken".
    """
    global _existing
    if not _existing:
        from cognee.modules.data.methods import get_datasets
        from cognee.modules.users.methods import get_default_user
        user = await get_default_user()
        _existing = {d.name for d in await get_datasets(user.id)}
    return _existing


async def _spine(query: str, scope: Scope, k: int) -> List[Passage]:
    live = await _existing_datasets()
    tasks = []
    for ds in sorted(scope.datasets):
        if ds == CONFIDENTIAL:
            tasks.append(_spine_confidential(query, k))   # ledger-backed, always available
        elif ds in live:
            tasks.append(_spine_dataset(query, ds, k))
    if not tasks:
        return []
    groups = await asyncio.gather(*tasks)       # an EstateLeak propagates: fail closed
    return [p for g in groups for p in g]


async def recall(query: str, scope: Scope, k: int = 5) -> Tuple[List[Passage], dict]:
    """Returns (passages, health). Never widens scope; never leaks; degrades to the floor."""
    if not query or not query.strip():
        return [], {"spine": "empty_query", "floor": "empty_query"}

    health = {"spine": "ok", "floor": "ok"}
    merged: List[Passage] = []

    spine_task = asyncio.create_task(_spine(query, scope, k))
    floor_task = asyncio.create_task(asyncio.to_thread(_floor_sync, query, scope, k))
    results = await asyncio.gather(spine_task, floor_task, return_exceptions=True)

    for name, res in zip(("spine", "floor"), results):
        if isinstance(res, EstateLeak):
            # The one error that is never degradable.
            raise res
        if isinstance(res, BaseException):
            health[name] = f"{type(res).__name__}: {str(res)[:120]}"
            continue
        merged.extend(res)

    if health["spine"] != "ok" and health["floor"] != "ok":
        raise RuntimeError(f"both stores failed: spine={health['spine']} floor={health['floor']}")

    # Belt and braces: whatever the stores did, nothing outside the scope leaves this function.
    for p in merged:
        if p.estate not in scope.datasets:
            raise EstateLeak(f"passage from {p.estate!r} outside scope {sorted(scope.datasets)}")

    return merged, health


def _floor_sync(query: str, scope: Scope, k: int) -> List[Passage]:
    """floor.recall_floor is sync (psycopg2 + requests); run it off the event loop."""
    import floor as _f
    return _f.recall_floor_sync(query, scope, k)
