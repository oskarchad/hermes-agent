"""Phase-owned skill projection; task skills remain implementation-owned."""
import json


def _skills(value):
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or "," in item for item in value
    ):
        raise ValueError("malformed phase skill list")
    return value


def handoff_skills(conn, task_id, author_skills, implementer, reviewer, explicit):
    from hermes_cli import kanban_db as kb

    author_skills = _skills(author_skills)
    if explicit is not None:
        _skills(explicit)
        return kb._normalize_task_skills(explicit)
    previous = kb._latest_event(conn, task_id, "review_requested")
    payload = kb._json_dict(kb._row_get(previous, "payload"))
    if payload.get("reviewer") == reviewer and "review_skills" in payload:
        return _skills(payload["review_skills"])
    if reviewer is None or reviewer == implementer:
        return author_skills
    if author_skills:
        raise ValueError("cross-profile review requires explicit review_skills; author skills are not reviewer requirements")
    return None


def review_skills(conn, task_id):
    """Resolve before claim AND capacity reservation; never guess legacy ownership."""
    from hermes_cli import kanban_db as kb

    row = conn.execute("SELECT skills, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("task not found")
    author = _skills(json.loads(row["skills"]) if row["skills"] is not None else None)
    event = kb._latest_event(conn, task_id, "review_requested")
    payload = kb._json_dict(kb._row_get(event, "payload"))
    if "review_skills" in payload:
        if (not payload.get("review_skills_explicit") and author
                and row["assignee"] != payload.get("implementer")):
            raise ValueError("reassigned review requires explicit review_skills; reopen-review then request-review")
        skills = _skills(payload["review_skills"])
    elif author and payload.get("implementer") != row["assignee"]:
        raise ValueError("legacy review has no explicit review_skills; reopen-review then request-review with review skills")
    else:
        skills = author
    return list(dict.fromkeys([*(skills or []), "sdlc-review"]))


def review_dispatchable(conn, task_id):
    try:
        review_skills(conn, task_id)
    except (ValueError, TypeError):
        return False
    return True
