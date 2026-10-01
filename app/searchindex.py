"""Interruptible search index over rule-version documents.

The index is built in small committed batches. State lives in ``index_state``:

  status        idle | building | ready
  cursor        next rule_versions.id to process
  total/done    bookkeeping for progress
  generation    bumped on each fresh build; an interrupted generation is
                abandoned by a fresh build but RESUMABLE until then
  built_at      completion timestamp

A crash/interruption mid-batch loses only that batch (it is committed only at
the end). Search keeps working against whatever was previously committed and a
runtime LIKE fallback, so index updates never take the encyclopedia offline.
"""
from __future__ import annotations

import re
import time

TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)

# English stopwords; CJK characters are indexed per character.
STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "is", "with"}


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok not in STOPWORDS and len(tok) > 1:
            tokens.append(tok)
    # CJK: index individual characters
    for ch in text:
        if "一" <= ch <= "鿿":
            tokens.append(ch)
    return tokens


def _get_state(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM index_state").fetchall()
    state = {r["key"]: r["value"] for r in rows}
    return {
        "status": state.get("status", "idle"),
        "cursor": int(state.get("cursor", "0")),
        "done": int(state.get("done", "0")),
        "total": int(state.get("total", "0")),
        "generation": int(state.get("generation", "0")),
        "built_at": state.get("built_at"),
        "message": state.get("message", ""),
    }


def _set(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO index_state(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))


def status(conn) -> dict:
    state = _get_state(conn)
    state["progress"] = round(state["done"] / state["total"], 3) if state["total"] else None
    return state


def start_build(conn, resume: bool = True) -> dict:
    st = _get_state(conn)
    total = conn.execute("SELECT COUNT(*) c FROM rule_versions").fetchone()["c"]
    if resume and st["status"] == "building":
        return {"started": False, "resumed": True, **st}
    gen = st["generation"] + 1
    conn.execute("DELETE FROM index_tokens")
    _set(conn, "status", "building")
    _set(conn, "cursor", 0)
    _set(conn, "done", 0)
    _set(conn, "total", total)
    _set(conn, "generation", gen)
    _set(conn, "message", "" if resume else "fresh build")
    conn.commit()
    return {"started": True, "resumed": False, **_get_state(conn)}


def build_batch(conn, limit: int = 5) -> dict:
    """Index up to ``limit`` versions in one committed batch."""
    st = _get_state(conn)
    if st["status"] != "building":
        return {"advanced": False, "reason": "not building", **st}
    rows = conn.execute(
        "SELECT rv.id, rv.version_label, rv.body, rv.params_json, rv.status, "
        "       r.id AS rule_id, r.kind, r.name, r.summary, "
        "       rv.effective_start_seq, rv.effective_end_seq "
        "FROM rule_versions rv JOIN rules r ON r.id=rv.rule_id "
        "WHERE rv.id > ? AND rv.status!='draft' ORDER BY rv.id LIMIT ?",
        (st["cursor"], limit)).fetchall()
    indexed = 0
    last_id = st["cursor"]
    for r in rows:
        last_id = r["id"]
        text = " ".join([r["rule_id"], r["name"], r["summary"], r["body"],
                         r["version_label"], r["kind"]])
        tf: dict[str, int] = {}
        for tok in tokenize(text):
            tf[tok] = tf.get(tok, 0) + 1
        for tok, n in tf.items():
            conn.execute(
                "INSERT OR REPLACE INTO index_tokens(token, rule_version_id, tf) VALUES (?,?,?)",
                (tok, r["id"], n))
        indexed += 1
    _set(conn, "cursor", last_id)
    _set(conn, "done", st["done"] + indexed)
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM rule_versions WHERE id > ? AND status!='draft'",
        (last_id,)).fetchone()["c"]
    if remaining == 0 and indexed >= 0:
        finished = conn.execute(
            "SELECT COUNT(*) c FROM rule_versions WHERE status!='draft'").fetchone()["c"]
        done = conn.execute("SELECT COUNT(DISTINCT rule_version_id) c FROM index_tokens").fetchone()["c"]
        if done >= finished:
            _set(conn, "status", "ready")
            _set(conn, "built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
            _set(conn, "message", "")
    conn.commit()
    return {"advanced": True, "indexed": indexed, **_get_state(conn)}


def interrupt(conn) -> dict:
    """Simulate an interrupted update: status left as 'building' mid-way."""
    st = _get_state(conn)
    if st["status"] != "building":
        return {"interrupted": False, "reason": "not building", **st}
    _set(conn, "message", "update interrupted (simulated crash)")
    conn.commit()
    return {"interrupted": True, **_get_state(conn)}


def search(conn, query: str, anchor_patch: str | None = None, day_slot: int | None = None,
           kind: str | None = None, current_only: bool = True, limit: int = 20) -> dict:
    """Default scope = current version at the newest patch; history filter opt-in.

    current_only=True and no anchor: only versions effective at the newest
    timeline coordinate.
    anchor given: restrict to the version effective at that anchor (history).
    """
    max_seq_row = conn.execute("SELECT MAX(seq) m FROM patches").fetchone()
    max_coord = max_seq_row["m"] * 10
    coord = max_coord
    if anchor_patch:
        seq = conn.execute("SELECT seq FROM patches WHERE id=?", (anchor_patch,)).fetchone()
        if seq is None:
            return {"query": query, "results": [], "error": f"未知补丁 {anchor_patch}"}
        coord = seq["seq"] * 10 + (day_slot or 0)

    terms = tokenize(query)
    indexed_hits: dict[int, int] = {}
    if terms:
        marks = ",".join("?" * len(terms))
        rows = conn.execute(
            f"SELECT rule_version_id, SUM(tf) s FROM index_tokens "
            f"WHERE token IN ({marks}) GROUP BY rule_version_id ORDER BY s DESC",
            terms).fetchall()
        indexed_hits = {r["rule_version_id"]: r["s"] for r in rows}

    # LIKE fallback guarantees search even with an interrupted/empty index
    like = "%" + (query or "").strip() + "%"
    rows = conn.execute(
        "SELECT rv.*, r.kind AS kind, r.name AS name, r.summary AS summary, "
        "       (SELECT COUNT(*) FROM rule_versions v2 "
        "        WHERE v2.rule_id=rv.rule_id AND v2.status!='draft') AS version_count "
        "FROM rule_versions rv JOIN rules r ON r.id=rv.rule_id "
        "WHERE rv.status!='draft' AND ("
        "  r.name LIKE ? OR r.summary LIKE ? OR rv.body LIKE ? OR r.id LIKE ?)",
        (like, like, like, like)).fetchall()

    results = []
    for r in rows:
        start = r["effective_start_seq"]
        end = r["effective_end_seq"] if r["effective_end_seq"] is not None else 10**9
        in_scope = start is not None and start <= coord < end
        if (current_only or anchor_patch) and not in_scope:
            continue
        if kind and r["kind"] != kind:
            continue
        score = indexed_hits.get(r["id"], 0)
        if not terms or score > 0 or like != "%%":
            results.append({
                "rule_id": r["rule_id"],
                "version_id": r["id"],
                "kind": r["kind"],
                "name": r["name"],
                "summary": r["summary"],
                "version_label": r["version_label"],
                "status": r["status"],
                "body": r["body"],
                "effective_at_anchor": in_scope,
                "interval": [r["effective_start_patch"], r["effective_end_patch"]],
                "interval_coord": [r["effective_start_seq"], r["effective_end_seq"]],
                "score": score,
            })
    # rank: text-index hits first, then version_id
    results.sort(key=lambda x: (-x["score"], x["rule_id"]))
    return {
        "query": query,
        "anchor_patch": anchor_patch,
        "anchor_coord": coord,
        "current_only": current_only and not anchor_patch,
        "terms": terms,
        "index_status": _get_state(conn)["status"],
        "results": results[:limit],
    }
