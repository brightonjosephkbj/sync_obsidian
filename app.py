import hmac
"""b24-sync: tracks who needs which song, gets the mp3 fetched, and
reports what's ready. Downloading itself is done by Api-cache."""
import os, sqlite3, threading, time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
INTERNAL = os.environ.get("B24_INTERNAL_AUTH_TOKEN", "")
DOWNLOADER_URL = os.environ.get("DOWNLOADER_URL", "https://nrighton233j-api-cache.hf.space").strip().rstrip("/")
if not DOWNLOADER_URL.startswith(("http://", "https://")):
    DOWNLOADER_URL = "https://" + DOWNLOADER_URL
def _pick_db_path():
    p = os.environ.get("SYNC_DB_PATH")
    if p:
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            open(p, "a").close()
            return p
        except OSError as e:
            print("SYNC_DB_PATH not usable:", e, flush=True)
    for d in ("/data", "/tmp", "."):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return os.path.join(d, "sync.db")
    return "sync.db"


DB_PATH = _pick_db_path()
print("[sync] db path:", DB_PATH, flush=True)


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS songs(
            song_id TEXT PRIMARY KEY, title TEXT, artist TEXT, query TEXT,
            status TEXT DEFAULT 'pending', file_url TEXT,
            tries INTEGER DEFAULT 0, updated REAL);
        CREATE TABLE IF NOT EXISTS wants(
            user_id TEXT, song_id TEXT, delivered INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, song_id));
        """)


@app.before_request
def guard():
    if request.path == "/ping":
        return None
    if not INTERNAL:
        return jsonify({"error": "server auth not configured"}), 503
    sent = request.headers.get("X-Internal-Auth", "")
    if not hmac.compare_digest(sent.encode(), INTERNAL.encode()):
        return jsonify({"error": "unauthorized"}), 401


@app.get("/ping")
def ping():
    return jsonify({"service": "b24-sync", "status": "ready"})


@app.post("/request")
def req():
    """Body: {user_id, songs:[{song_id,title,artist,query?}]}"""
    d = request.get_json(silent=True) or {}
    uid, songs = d.get("user_id"), d.get("songs") or []
    if not uid or not songs:
        return jsonify({"error": "user_id and songs required"}), 400
    now = time.time()
    n = 0
    with db() as c:
        for s in songs:
            sid = s.get("song_id")
            if not sid:
                continue
            q = s.get("query") or f"{s.get('artist', '')} {s.get('title', '')}".strip()
            c.execute("INSERT OR IGNORE INTO songs(song_id,title,artist,query,updated) VALUES(?,?,?,?,?)",
                      (sid, s.get("title"), s.get("artist"), q, now))
            c.execute("INSERT OR IGNORE INTO wants(user_id,song_id) VALUES(?,?)", (uid, sid))
            n += 1
    return jsonify({"queued": n})


@app.post("/online")
def online():
    """Body: {user_id, have:[song_id,...]}. Marks confirmed songs as
    delivered, returns songs that are ready but not on the phone yet."""
    d = request.get_json(silent=True) or {}
    uid, have = d.get("user_id"), d.get("have") or []
    if not uid:
        return jsonify({"error": "user_id required"}), 400
    with db() as c:
        for sid in have:
            c.execute("UPDATE wants SET delivered=1 WHERE user_id=? AND song_id=?", (uid, sid))
        rows = c.execute(
            """SELECT s.song_id, s.title, s.artist, s.file_url
               FROM wants w JOIN songs s USING(song_id)
               WHERE w.user_id=? AND w.delivered=0 AND s.status='ready'""",
            (uid,)).fetchall()
    return jsonify({"ready": [dict(r) for r in rows]})


@app.get("/status")
def status():
    with db() as c:
        rows = c.execute("SELECT song_id,status,tries,file_url FROM songs").fetchall()
    return jsonify({"downloader": DOWNLOADER_URL, "songs": [dict(r) for r in rows]})


FILE_TTL = int(os.environ.get("SYNC_FILE_TTL_DAYS", "14")) * 86400


def download_song(song):
    r = requests.post(
        f"{DOWNLOADER_URL}/remote/fetch-by-query",
        json={
            "query": song["query"],
            "app_id": "b24music",
            "category": "music",
            "mode": "audio",
            "ref_id": song["song_id"],
            "expires_at": int(time.time()) + FILE_TTL,
        },
        headers={"X-Internal-Auth": INTERNAL, "X-Service-Name": "b24-sync"},
        timeout=600,
    )
    body = r.json()
    if not r.ok or not body.get("ok"):
        raise RuntimeError(body.get("error", f"HTTP {r.status_code}"))
    rec = body["data"]
    fid = rec.get("id") or rec.get("file_id")
    if fid is None:
        raise RuntimeError(f"no file id in record: {list(rec.keys())}")
    return f"/api/downloads/files/{fid}/view"


def worker():
    while True:
        with db() as c:
            row = c.execute(
                "SELECT * FROM songs WHERE status='pending' ORDER BY updated LIMIT 1").fetchone()
        if not row:
            time.sleep(3)
            continue
        print("[sync] fetching:", row["song_id"], "try", row["tries"] + 1, flush=True)
        try:
            file_url, status = download_song(dict(row)), "ready"
        except Exception as e:
            print("download failed:", row["song_id"], e, flush=True)
            file_url = None
            status = "failed" if row["tries"] + 1 >= 3 else "pending"
        with db() as c:
            c.execute("UPDATE songs SET status=?, file_url=?, tries=tries+1, updated=? WHERE song_id=?",
                      (status, file_url, time.time(), row["song_id"]))


init()
threading.Thread(target=worker, daemon=True).start()
print("[sync] worker started, downloader:", DOWNLOADER_URL, flush=True)

LAST_ERRORS = {}
_orig_download_song = download_song


def download_song(song):
    try:
        return _orig_download_song(song)
    except Exception as e:
        LAST_ERRORS[song["song_id"]] = f"{type(e).__name__}: {e}"[:600]
        raise


@app.get("/errors")
def errors():
    return jsonify(LAST_ERRORS)


@app.post("/retry")
def retry():
    with db() as c:
        n = c.execute("UPDATE songs SET status='pending', tries=0 WHERE status='failed'").rowcount
    return jsonify({"requeued": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
