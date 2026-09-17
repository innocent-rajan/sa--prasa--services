"""Knowledge base: public article reads + FTS5 search + admin CRUD."""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_admin
from app.db import get_db
from app.schemas.models import KbArticleCreate, KbArticleOut, KbArticleUpdate
from app.services.audit import write_audit

router = APIRouter(tags=["knowledge"])

PUBLISHED_FILTER = "is_published = 1"


def _row_to_out(row: sqlite3.Row) -> KbArticleOut:
    return KbArticleOut(
        id=row["id"], slug=row["slug"], category=row["category"],
        language=row["language"], title=row["title"], body=row["body"],
        is_published=bool(row["is_published"]), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/kb/articles", response_model=list[KbArticleOut],
            summary="List or search published articles",
            description="""With `q`: full-text search (FTS5) over title+body, ranked.
Without `q`: all published articles for the language, optionally by category.""",
            responses={200: {"content": {"application/json": {"example": [{
                "id": 1, "slug": "buy-ticket", "category": "tickets",
                "language": "en", "title": "How to buy a ticket",
                "body": "Buy at the station or in the app.",
                "is_published": True,
                "created_at": "2026-09-15 09:00:00",
                "updated_at": "2026-09-15 09:00:00"}]}}}})
def list_articles(category: str | None = Query(default=None,
                  description="Filter by category"),
                  language: str = Query(default="en", description="ISO language code"),
                  q: str | None = Query(default=None, max_length=100,
                  description="Full-text search, e.g. 'ticket' or 'luggage'"),
                  conn: sqlite3.Connection = Depends(get_db)):
    if q:
        # FTS5 search restricted to published articles in the requested language
        rows = conn.execute(
            """SELECT k.* FROM kb_articles k
               JOIN kb_articles_fts f ON f.rowid = k.id
               WHERE kb_articles_fts MATCH ?
                 AND k.is_published = 1 AND k.language = ?
               ORDER BY rank LIMIT 50""",
            (q, language),
        ).fetchall()
        return [_row_to_out(r) for r in rows]
    where = [PUBLISHED_FILTER, "language = ?"]
    params: list = [language]
    if category:
        where.append("category = ?")
        params.append(category)
    rows = conn.execute(
        f"SELECT * FROM kb_articles WHERE {' AND '.join(where)} ORDER BY category, title",
        params,
    ).fetchall()
    return [_row_to_out(r) for r in rows]


@router.get("/kb/articles/{slug}", response_model=KbArticleOut,
            summary="Get one published article by slug",
            description="If no article matches, the client should offer escalation "
                        "to an inquiry (SOW requirement; inquiry module is a later phase).",
            responses={404: {"description": "Not found or not published"}})
def get_article(slug: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute(
        f"SELECT * FROM kb_articles WHERE slug = ? AND {PUBLISHED_FILTER}", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return _row_to_out(row)


@router.post("/kb/articles", response_model=KbArticleOut, status_code=201,
             summary="Create an article (admin)",
             responses={201: {"description": "Created"},
                        401: {"description": "Missing/wrong admin key"},
                        409: {"description": "Slug already exists"},
                        422: {"description": "Validation error"}})
def create_article(body: KbArticleCreate, actor: str = Depends(require_admin),
                   conn: sqlite3.Connection = Depends(get_db)):
    exists = conn.execute("SELECT 1 FROM kb_articles WHERE slug = ?", (body.slug,)).fetchone()
    if exists:
        raise HTTPException(status_code=409, detail=f"Slug '{body.slug}' already exists")
    cur = conn.execute(
        """INSERT INTO kb_articles (slug, category, language, title, body, is_published)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (body.slug, body.category, body.language, body.title, body.body,
         1 if body.is_published else 0),
    )
    new_id = cur.lastrowid
    write_audit(conn, actor, "create", "kb_article", new_id, body.model_dump(mode="json"))
    row = conn.execute("SELECT * FROM kb_articles WHERE id = ?", (new_id,)).fetchone()
    return _row_to_out(row)


@router.patch("/kb/articles/{article_id}", response_model=KbArticleOut,
              summary="Update an article (admin)",
              responses={401: {"description": "Missing/wrong admin key"},
                         404: {"description": "Not found"},
                         422: {"description": "Validation error or no fields to update"}})
def update_article(article_id: int, body: KbArticleUpdate,
                   actor: str = Depends(require_admin),
                   conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update")
    fields, params = [], []
    for key, value in changes.items():
        fields.append(f"{key} = ?")
        params.append(1 if value is True else 0 if value is False else value)
    fields.append("updated_at = datetime('now')")
    params.append(article_id)
    conn.execute(f"UPDATE kb_articles SET {', '.join(fields)} WHERE id = ?", params)
    write_audit(conn, actor, "update", "kb_article", article_id, changes)
    row = conn.execute("SELECT * FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
    return _row_to_out(row)


@router.delete("/kb/articles/{article_id}", status_code=204,
               summary="Delete an article (admin)",
               responses={204: {"description": "Deleted"},
                          401: {"description": "Missing/wrong admin key"},
                          404: {"description": "Not found"}})
def delete_article(article_id: int, actor: str = Depends(require_admin),
                   conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT id FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    conn.execute("DELETE FROM kb_articles WHERE id = ?", (article_id,))
    write_audit(conn, actor, "delete", "kb_article", article_id)