"""
tracker_railway.py — Tracker email autonome pour déploiement Railway.

Version adaptée de tracker.py qui fonctionne avec sa propre SQLite
(pas besoin de crm.db). Le pipeline local sync les ouvertures/clics
via l'endpoint /api/events.

Endpoints :
- GET  /t/{email_id}.png     → pixel 1x1 transparent (track open)
- GET  /c/{email_id}/{link}  → redirect + track click
- GET  /api/events           → liste les events récents (pour sync locale)
- GET  /api/events?since=ID  → events depuis un ID donné
- GET  /stats                → dashboard JSON des stats
- GET  /dashboard            → mini dashboard HTML
- GET  /health               → healthcheck Railway

Déploiement Railway :
    railway init
    railway up
"""

import os
import sqlite3
import base64
from datetime import datetime
from fastapi import FastAPI, Request, Query
from fastapi.responses import Response, RedirectResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ───────────────────────────────────────────────────────────────────
SECRET = os.getenv("TRACKER_SECRET", "youngs_tracker_2026")
DB_PATH = os.getenv("TRACKER_DB_PATH", "/data/tracker.db")

# 1x1 transparent PNG
PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)

app = FastAPI(title="Youngs Job Email Tracker", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Database ─────────────────────────────────────────────────────────────────
def _get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            url TEXT,
            ip TEXT,
            user_agent TEXT,
            timestamp TEXT NOT NULL,
            synced INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_meta (
            email_id INTEGER PRIMARY KEY,
            opened INTEGER DEFAULT 0,
            clicked INTEGER DEFAULT 0,
            first_open TEXT,
            last_open TEXT,
            open_count INTEGER DEFAULT 0,
            click_count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


_ensure_tables()


# ── Tracking endpoints ───────────────────────────────────────────────────────
@app.get("/t/{email_id}.png")
async def track_open(email_id: int, request: Request):
    """Pixel de tracking d'ouverture."""
    try:
        conn = _get_db()
        now = datetime.now().isoformat()

        conn.execute(
            "INSERT INTO events (email_id, event_type, ip, user_agent, timestamp) VALUES (?, 'open', ?, ?, ?)",
            (email_id, request.client.host, request.headers.get("user-agent", ""), now)
        )

        # Upsert email_meta
        existing = conn.execute("SELECT * FROM email_meta WHERE email_id = ?", (email_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE email_meta SET opened = 1, last_open = ?, open_count = open_count + 1 WHERE email_id = ?",
                (now, email_id)
            )
        else:
            conn.execute(
                "INSERT INTO email_meta (email_id, opened, first_open, last_open, open_count) VALUES (?, 1, ?, ?, 1)",
                (email_id, now, now)
            )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[TRACKER] Erreur open: {e}")

    return Response(
        content=PIXEL_PNG,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.get("/c/{email_id}/{encoded_url}")
async def track_click(email_id: int, encoded_url: str, request: Request):
    """Redirect de tracking de clic."""
    try:
        url = base64.urlsafe_b64decode(encoded_url).decode("utf-8")
    except Exception:
        url = "https://youngs-job.fr"

    try:
        conn = _get_db()
        now = datetime.now().isoformat()

        conn.execute(
            "INSERT INTO events (email_id, event_type, url, ip, user_agent, timestamp) VALUES (?, 'click', ?, ?, ?, ?)",
            (email_id, url, request.client.host, request.headers.get("user-agent", ""), now)
        )

        existing = conn.execute("SELECT * FROM email_meta WHERE email_id = ?", (email_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE email_meta SET clicked = 1, click_count = click_count + 1 WHERE email_id = ?",
                (email_id,)
            )
        else:
            conn.execute(
                "INSERT INTO email_meta (email_id, clicked, click_count) VALUES (?, 1, 1)",
                (email_id,)
            )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[TRACKER] Erreur click: {e}")

    return RedirectResponse(url=url, status_code=302)


# ── API sync (pour que le pipeline local récupère les events) ────────────────
@app.get("/api/events")
async def get_events(since: int = Query(0), secret: str = Query("")):
    """Retourne les events depuis un ID donné (pour sync avec crm.db local)."""
    if secret != SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    conn = _get_db()
    events = conn.execute(
        "SELECT id, email_id, event_type, url, timestamp FROM events WHERE id > ? ORDER BY id LIMIT 500",
        (since,)
    ).fetchall()
    conn.close()

    return JSONResponse({
        "events": [dict(e) for e in events],
        "count": len(events),
    })


# ── Stats ────────────────────────────────────────────────────────────────────
@app.get("/stats")
async def stats():
    conn = _get_db()

    total_tracked = conn.execute("SELECT COUNT(DISTINCT email_id) FROM email_meta").fetchone()[0]
    opened = conn.execute("SELECT COUNT(*) FROM email_meta WHERE opened = 1").fetchone()[0]
    clicked = conn.execute("SELECT COUNT(*) FROM email_meta WHERE clicked = 1").fetchone()[0]

    daily = conn.execute("""
        SELECT DATE(timestamp) as day, event_type, COUNT(*) as cnt
        FROM events
        GROUP BY day, event_type
        ORDER BY day DESC
        LIMIT 60
    """).fetchall()

    conn.close()

    return JSONResponse({
        "total_tracked": total_tracked,
        "opened": opened,
        "clicked": clicked,
        "open_rate": f"{(opened/total_tracked*100):.1f}%" if total_tracked > 0 else "0%",
        "click_rate": f"{(clicked/total_tracked*100):.1f}%" if total_tracked > 0 else "0%",
        "daily": [{"day": d[0], "event": d[1], "count": d[2]} for d in daily],
    })


# ── Dashboard HTML ───────────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    conn = _get_db()

    total_tracked = conn.execute("SELECT COUNT(DISTINCT email_id) FROM email_meta").fetchone()[0]
    opened = conn.execute("SELECT COUNT(*) FROM email_meta WHERE opened = 1").fetchone()[0]
    clicked = conn.execute("SELECT COUNT(*) FROM email_meta WHERE clicked = 1").fetchone()[0]

    recent = conn.execute("""
        SELECT timestamp, email_id, event_type, ip
        FROM events
        ORDER BY id DESC
        LIMIT 30
    """).fetchall()

    conn.close()

    open_rate = f"{(opened/total_tracked*100):.1f}" if total_tracked > 0 else "0"

    rows_html = ""
    for r in recent:
        emoji = "📖" if r[2] == "open" else "🔗"
        rows_html += f"<tr><td>{r[0][:16]}</td><td>{emoji} {r[2]}</td><td>Email #{r[1]}</td><td>{r[3]}</td></tr>\n"

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Youngs Job — Email Tracking</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }}
        .cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 32px; }}
        .card {{ background: white; border-radius: 12px; padding: 20px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .card .num {{ font-size: 36px; font-weight: 700; color: #3B5BDB; }}
        .card .label {{ font-size: 13px; color: #666; margin-top: 4px; }}
        .card .rate {{ font-size: 14px; color: #22c55e; font-weight: 600; margin-top: 4px; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        th {{ background: #3B5BDB; color: white; padding: 12px; text-align: left; font-size: 13px; }}
        td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 13px; }}
        h1 {{ color: #1a1a2e; }}
        h2 {{ color: #333; margin-top: 32px; }}
    </style>
    <meta http-equiv="refresh" content="30">
</head>
<body>
    <h1>Email Tracking — Youngs Job</h1>
    <div class="cards">
        <div class="card"><div class="num">{total_tracked}</div><div class="label">Emails trackés</div></div>
        <div class="card"><div class="num">{opened}</div><div class="label">Ouvertures</div><div class="rate">{open_rate}%</div></div>
        <div class="card"><div class="num">{clicked}</div><div class="label">Clics</div></div>
    </div>

    <h2>Activité récente</h2>
    <table>
        <tr><th>Date</th><th>Event</th><th>Email</th><th>IP</th></tr>
        {rows_html if rows_html else "<tr><td colspan='4' style='text-align:center;color:#999;padding:20px;'>Aucun event pour le moment</td></tr>"}
    </table>

    <p style="color:#999;font-size:12px;margin-top:20px;">Auto-refresh toutes les 30s | Tracker Youngs Job v2</p>
</body>
</html>"""


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("TRACKER_PORT", "8099")))
    print(f"[TRACKER] Démarrage sur http://0.0.0.0:{port}")
    print(f"[TRACKER] Dashboard: http://localhost:{port}/dashboard")
    uvicorn.run(app, host="0.0.0.0", port=port)
