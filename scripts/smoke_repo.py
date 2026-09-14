"""Manual smoke test for schema + repository (run with: uv run python scripts/smoke_repo.py)."""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pgserver
import psycopg

from db import apply_migrations
import preprocess_store as store
import annotation_repository as repo

pg = pgserver.get_server(".pgdata/smoke")
uri = pg.get_uri()
os.environ["ANNOTATION_DB_DSN"] = uri

# Fresh DB
with psycopg.connect(uri, autocommit=True) as c:
    c.execute("DROP DATABASE IF EXISTS smoke_db")
    c.execute("CREATE DATABASE smoke_db")
    os.environ["ANNOTATION_DB_DSN"] = pg.get_uri(database="smoke_db")

from db import db_conn  # re-read env
with db_conn() as conn:
    print("migrations:", apply_migrations(conn))

# Two preprocessed tasks
def seg(i, s, e, asr="asr"):
    return {"id": i, "start": s, "end": e, "duration": e - s,
            "asr_text": asr, "text": "", "exclude_from_training": False}

with db_conn() as conn:
    for n in ("a.wav", "b.wav"):
        r = store.store_preprocessed_task(
            conn, rel_path=n, filename=n, folder="", duration=12.5,
            segments=[seg(1, 0.0, 5.0), seg(2, 5.0, 12.5)],
            waveform_payload=bytes(range(256)) * 4,
            skip_if_human_modified=True,
        )
        print("store:", n, r["action"], r["task_id"])

# Login alice
_sid = str(uuid.uuid4())
user = repo.login("alice", _sid, 1800)
uid = user["id"]
print("user:", user)

# claim
asg = repo.claim(uid)
print("claim:", asg["task_id"], asg["filename"], "rev", asg["revision"],
      "segs", len(asg["segments"]), "wf", bool(asg["waveform_b64"]))

# idempotent claim
asg2 = repo.claim(uid)
assert asg2["task_id"] == asg["task_id"]
print("claim idempotent OK")

# autosave dirty segments
full = [{"id": s["id"], "start": s["start"], "end": s["end"],
         "duration": s["duration"], "text": f"النص {s['id']}",
         "exclude_from_training": False} for s in asg["segments"]]
save_op = str(uuid.uuid4())
r = repo.save_draft(uid, asg["lease_token"], asg["revision"], full,
                    save_op, "save-hash")
print("save rev ->", r["revision"])

# stale revision must conflict
try:
    repo.save_draft(uid, asg["lease_token"], asg["revision"], full,
                    str(uuid.uuid4()), "stale-hash")
    print("ERROR: stale save accepted")
except repo.RevisionConflict as e:
    print("stale save 409 OK, current:", e.current_revision)

# bad lease token
try:
    repo.save_draft(uid, str(uuid.uuid4()), r["revision"], [],
                    str(uuid.uuid4()), "bad-token-hash")
    print("ERROR: bad token accepted")
except repo.ForbiddenError:
    print("bad lease token 403 OK")

# incomplete validation: mark one segment empty and try complete
bad = [dict(s, text="") for s in full[:1]] + full[1:]
try:
    repo.complete(uid, asg["lease_token"], r["revision"], "annotated", [],
                  bad, str(uuid.uuid4()), "h")
    print("ERROR: empty text accepted")
except repo.ValidationError as e:
    print("empty-text rejected OK:", str(e)[:60])

# proper complete
op = str(uuid.uuid4())
res = repo.complete(uid, asg["lease_token"], r["revision"], "annotated", [],
                    full, op, "h2")
print("complete:", res)
res2 = repo.complete(uid, asg["lease_token"], r["revision"], "annotated", [],
                     full, op, "h2")
assert res2.get("idempotent_replay"), res2
print("complete idempotent OK")

# history
h = repo.history_recent(uid)
print("history:", [(i["filename"], i["status"]) for i in h["items"]])

# completed list + detail + reopen
cl = repo.completed_list(uid)
print("completed summary:", cl["summary"], "items", len(cl["items"]))
d = repo.completed_detail(uid, asg["task_id"])
print("detail segs:", [(s["id"], s["text"]) for s in d["segments"]])
rp = repo.reopen_completed(uid, asg["task_id"], str(uuid.uuid4()))
print("reopen:", rp["mode"])

# logout keeps assignment
repo.logout("alice", _sid)
asg3 = repo.get_assignment(uid)
assert asg3 and asg3["mode"] == "revision"
print("assignment survives logout OK")

# dashboard
dash = repo.dashboard()
print("stats:", dash["stats"], "lb:", dash["leaderboard"])

# pool state + next claim by bob
print("pool:", repo.pool_state())
bob = repo.login("bob", str(uuid.uuid4()), 1800)
b = repo.claim(bob["id"])
print("bob claim:", b["filename"])
b_again = repo.claim(bob["id"])
assert b_again["task_id"] == b["task_id"]
print("bob claim idempotent OK")

# bob abandon -> task back to pool
repo.abandon(bob["id"], b["lease_token"], str(uuid.uuid4()), confirm=True)
print("after abandon pool:", repo.pool_state())
b2 = repo.claim(bob["id"])
print("bob reclaim:", b2["filename"], "draft text kept:",
      [s["text"] for s in b2["segments"]])

print("\nALL SMOKE CHECKS PASSED")
