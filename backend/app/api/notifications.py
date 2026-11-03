"""Notification center API: badge, panel, mark-read."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db.deps import get_db
from ..db.models import Notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _item_html(n: Notification) -> str:
    kind_style = ("background:#da3633;color:#fff" if n.kind == "action"
                  else "background:rgba(79,140,255,.15);color:var(--accent)")
    unread = "" if n.read_at else "background:rgba(79,140,255,.06);"
    if n.link and n.link.startswith("http"):
        link_html = (f'<a href="{n.link}" target="_blank" rel="noopener" '
                     f'style="color:var(--accent)" onclick="fetch('
                     f'\'/api/notifications/{n.id}/read\',{{method:\'POST\'}})">'
                     f"{n.title} ↗</a>")
    elif n.link:
        link_html = (f'<a href="{n.link}" style="color:var(--accent)" onclick="fetch('
                     f'\'/api/notifications/{n.id}/read\',{{method:\'POST\'}})">{n.title}</a>')
    else:
        link_html = n.title
    return (
        f'<div style="padding:10px 16px;border-bottom:1px solid var(--border);{unread}">'
        f'<span class="badge" style="{kind_style}">{n.kind}</span> {link_html}'
        f'<div class="muted" style="margin-top:4px">{n.detail}</div></div>'
    )


@router.get("/badge")
def badge(db: Session = Depends(get_db)) -> HTMLResponse:
    n = db.query(Notification).filter(Notification.read_at.is_(None)).count()
    html = (f'<span class="badge" style="background:#da3633;color:#fff">{n}</span>'
            if n else "")
    return HTMLResponse(html)


@router.get("/panel")
def panel(db: Session = Depends(get_db)) -> HTMLResponse:
    items = (
        db.query(Notification).order_by(Notification.id.desc()).limit(50).all()
    )
    rows = "".join(_item_html(n) for n in items) or (
        '<div class="muted" style="padding:16px">No notifications yet — '
        "the agent will alert you here (and on your desktop) the moment it "
        "needs you, e.g. a CAPTCHA wall.</div>"
    )
    html = (
        '<div class="card" style="max-width:980px;margin:12px auto 0;padding:0;overflow:hidden">'
        '<div style="padding:12px 16px;border-bottom:1px solid var(--border);'
        'display:flex;justify-content:space-between;align-items:center">'
        "<strong>Notifications</strong>"
        '<button class="secondary" hx-post="/api/notifications/read-all" hx-swap="none" '
        'onclick="setTimeout(()=>{if(window.pollBadge)pollBadge()},300)">Mark all read</button>'
        "</div>"
        f"{rows}"
        "</div>"
    )
    return HTMLResponse(html)


@router.post("/read-all")
def read_all(db: Session = Depends(get_db)) -> dict:
    from datetime import datetime

    n = (
        db.query(Notification)
        .filter(Notification.read_at.is_(None))
        .update({Notification.read_at: datetime.utcnow()}, synchronize_session=False)
    )
    db.commit()
    return {"read": n}


@router.post("/{note_id}/read")
def read_one(note_id: int, db: Session = Depends(get_db)) -> dict:
    from datetime import datetime

    note = db.get(Notification, note_id)
    if note is None:
        return {"error": "not found"}
    if note.read_at is None:
        note.read_at = datetime.utcnow()
        db.commit()
    return {"read": note_id}
