import re
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from .db import connect

RETRIEVAL_STOP_WORDS={
    "the","user","which","what","who","where","when","why","how","does","do","did",
    "is","are","was","were","am","be","been","being","and","or","but","not","with",
    "from","into","about","this","that","these","those","have","has","had","can","could",
    "would","should","will","your","you","my","mine","our","ours","their","they","them",
    "for","of","to","in","on","at","by","as","it","its","a","an","i","we","me","us",
    "please","tell","show","give","find","know","remember","recall","like","prefer",
}

def classify(text: str) -> dict:
    clean = " ".join(text.split())[:2000]
    lower = clean.lower()
    if not clean:
        return {"retain": False, "reason": "Empty text is not useful memory."}
    if re.search(r"\b(password|api key|secret|credit card|ssn)\b", lower):
        return {"retain": False, "reason": "Potentially sensitive information is excluded by default."}
    if "?" in lower or re.match(r"\s*(?:what|which|who|where|when|how|do you|can you|u remember)\b", lower):
        return {"retain": False,"content":clean,"category":"fact","priority":1,"temporary":True,
                "reason": "Questions are not stored as personal facts; held as temporary context."}
    if (re.match(r"\s*(?:if|suppose|imagine|hypothetically)\b", lower)
        or re.search(r"\b(?:i am not sure|i'm not sure|maybe i|i guess|just kidding|jk|hypothetically)\b", lower)
        or re.search(r"\b(?:lol|lmao|haha|just joking|kidding)\b", lower)):
        return {"retain": False,"content":clean,"category":"fact","priority":1,"temporary":True,
                "reason": "Hypothetical or uncertain text is not retained as a durable memory."}
    preference = bool(re.search(
        r"\b(?:i prefer|i (?:really )?like|i lyk|i love|i hate|i dislike|i (?:do not|don't|dont) like|"
        r"my (?:fav|favorite|favourite)|please remember|my goal|i work as|i am a|i'm a|"
        r"i am vegetarian|i'm vegetarian|i am vegan|i'm vegan|i am allergic|i'm allergic)\b", lower
    ))
    return {"retain": preference, "content": clean, "category": "preference" if preference else "fact",
            "priority": 3 if preference else 1, "temporary": not preference,
            "reason": "Likely durable preference or goal; retained for future requests." if preference else "No durable preference detected; held as temporary context."}


def needs_memory_analysis(text: str) -> bool:
    """Cheap broad gate for likely personal assertions and memory-recall questions."""
    clean=" ".join(text.split()).lower()
    if not clean or re.search(r"\b(password|api key|secret|credit card|ssn)\b",clean):
        return False
    personal=bool(re.search(r"\b(?:i|im|i'm|my|mine|we|our)\b",clean))
    memory_cue=bool(re.search(
        r"\b(?:am|is|are|was|were|have|has|work|live|from|goal|name|prefer|like|lyk|love|hate|dislike|"
        r"fav|favorite|favourite|prog|programming|lang|language|allerg\w*|vegetarian|vegan|study|major|"
        r"speak|code|use|enjoy|based|remember|recall)\w*\b",clean
    ))
    personal_question="?" in clean and personal and memory_cue
    recall_question=bool(re.search(r"\b(?:remember|recall)\b",clean) and re.search(r"\b(?:what|which|where|when|who|how|i|my|u|you)\b",clean))
    return (personal and memory_cue) or personal_question or recall_question


def validate_memory_analysis(value: object) -> dict | None:
    """Validate model-produced analysis before any memory decision or retrieval use."""
    if not isinstance(value,dict) or set(value)!={"message_type","about_user","confidence","candidate","search_terms"}:
        return None
    message_type=value["message_type"]
    if not isinstance(message_type,str) or message_type not in {"assertion","question","hypothetical","joke","uncertain","other"}:
        return None
    if not isinstance(value["about_user"],bool): return None
    confidence=value["confidence"]
    if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not 0<=confidence<=1:
        return None
    terms=value["search_terms"]
    if not isinstance(terms,list) or len(terms)>12 or any(
        not isinstance(term,str) or not term.strip() or len(term.strip())>40
        or not re.fullmatch(r"[\w -]+",term.strip(),flags=re.UNICODE) for term in terms
    ):
        return None
    candidate=value["candidate"]
    if candidate is not None:
        if not isinstance(candidate,dict) or set(candidate)!={"content","category","polarity"}:
            return None
        content=candidate["content"]
        category=candidate["category"]
        polarity=candidate["polarity"]
        if not isinstance(content,str) or not content.strip() or len(content.strip())>500:
            return None
        if (not isinstance(category,str) or category not in {"fact","preference","goal","project"}
            or not isinstance(polarity,str) or polarity not in {"positive","negative","neutral"}):
            return None
        if classify(content)["reason"]=="Potentially sensitive information is excluded by default.":
            return None
        candidate={"content":content.strip(),"category":category,"polarity":polarity}
    return {"message_type":message_type,"about_user":value["about_user"],
            "confidence":float(confidence),"candidate":candidate,
            "search_terms":[term.strip().lower() for term in terms]}


def decision_from_analysis(text: str, analysis: dict) -> dict:
    """Retain only explicit, high-confidence, user-owned assertions."""
    candidate=analysis["candidate"]
    local=classify(text)
    eligible=(analysis["message_type"]=="assertion" and analysis["about_user"]
              and analysis["confidence"]>=0.85 and candidate is not None)
    locally_blocked=local["reason"].startswith(("Questions are not stored", "Hypothetical or uncertain text"))
    if candidate and candidate["category"]=="preference":
        lower=text.lower()
        negative=bool(re.search(r"\b(?:i (?:do not|don't|dont) like|i dislike|i hate|not a fan of|can't stand)\b",lower))
        positive=bool(re.search(r"\b(?:i (?:really )?like|i lyk|i love|my (?:fav|favorite|favourite))\b",lower))
        polarity_conflict=(negative and candidate["polarity"]!="negative") or (positive and not negative and candidate["polarity"]!="positive")
    else:
        polarity_conflict=False
    if (eligible and not locally_blocked and not polarity_conflict
        and local["reason"]!="Potentially sensitive information is excluded by default."):
        return {"retain":True,"content":candidate["content"],"category":candidate["category"],
                "priority":3 if candidate["category"] in {"preference","goal"} else 2,
                "temporary":False,"reason":f"Explicit personal assertion normalized from the message (confidence {analysis['confidence']:.2f}; {candidate['polarity']} polarity)."}
    label=analysis["message_type"]
    why=("local safety checks rejected the message or its polarity" if locally_blocked or polarity_conflict
         else f"analysis classified this as {label}, not a sufficiently clear personal assertion")
    return {"retain":False,"content":text,"category":"fact","priority":1,"temporary":True,
            "reason":f"Not retained as durable memory: {why}."}

def add_memory_record(db, user_id: int, content: str, category: str, priority: int,
                      reason: str, source: str = "conversation", temporary: bool = False,
                      source_message_id: int | None = None) -> int:
    """Insert a memory and its initial version into an existing transaction."""
    bounded_priority=max(1,min(priority,5))
    cur = db.execute(
        "INSERT INTO memories(user_id,content,category,priority,source,reason,temporary,source_message_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user_id,content[:2000],category,bounded_priority,source,reason[:500],int(temporary),source_message_id),
    )
    mid = int(cur.lastrowid)
    db.execute("INSERT INTO memory_versions(memory_id,version,content,category,priority,reason) VALUES(?,1,?,?,?,?)",
               (mid,content[:2000],category,bounded_priority,reason[:500]))
    return mid

def add_memory(user_id: int, content: str, category: str, priority: int, reason: str,
               source: str = "conversation", temporary: bool = False,
               db_path: str | None = None, source_message_id: int | None = None) -> int:
    with connect(db_path) as db:
        return add_memory_record(db,user_id,content,category,priority,reason,source,temporary,source_message_id)

def deactivate_unedited_source_memories(db, user_id: int, source_message_ids: list[int]) -> tuple[list[int], list[int]]:
    """Soft-delete untouched chat-extracted memories; preserve user-versioned edits.

    Must be called inside the same transaction that prunes the message branch.
    Returns (deactivated_ids, preserved_ids).
    """
    ids=sorted({int(value) for value in source_message_ids})
    if not ids: return [],[]
    marks=','.join('?' for _ in ids)
    rows=db.execute(
        f"SELECT m.id,m.source,m.deleted_at,COUNT(v.id) AS version_count FROM memories m "
        f"LEFT JOIN memory_versions v ON v.memory_id=m.id "
        f"WHERE m.user_id=? AND m.source_message_id IN ({marks}) GROUP BY m.id",
        (user_id,*ids),
    ).fetchall()
    deactivated=[]; preserved=[]
    for row in rows:
        if row['deleted_at'] is not None: continue
        if row['source']=='conversation' and row['version_count']==1:
            db.execute("UPDATE memories SET deleted_at=? WHERE id=? AND user_id=? AND deleted_at IS NULL",
                       (datetime.now(timezone.utc).isoformat(),row['id'],user_id))
            db.execute("INSERT INTO memory_events(user_id,memory_id,event_type) VALUES(?,?,?)",
                       (user_id,row['id'],'deleted'))
            deactivated.append(row['id'])
        else:
            preserved.append(row['id'])
    return deactivated,preserved

def update_memory(user_id: int, memory_id: int, content: str, category: str, priority: int, reason: str = "Edited by user", db_path: str | None = None) -> bool:
    with connect(db_path) as db:
        row = db.execute("SELECT * FROM memories WHERE id=? AND user_id=? AND deleted_at IS NULL",(memory_id,user_id)).fetchone()
        if not row: return False
        version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM memory_versions WHERE memory_id=?",(memory_id,)).fetchone()[0]
        db.execute("INSERT INTO memory_versions(memory_id,version,content,category,priority,reason) VALUES(?,?,?,?,?,?)",(memory_id,version,content[:2000],category,max(1,min(priority,5)),reason[:500]))
        db.execute("UPDATE memories SET content=?,category=?,priority=?,reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(content[:2000],category,max(1,min(priority,5)),reason[:500],memory_id,user_id))
        if reason.startswith("Restored version"):
            db.execute("INSERT INTO memory_events(user_id,memory_id,event_type) VALUES(?,?,?)",(user_id,memory_id,"restored"))
        return True

def list_memories(user_id: int, query: str = "", include_deleted: bool = False, db_path: str | None = None) -> list[dict]:
    sql="SELECT * FROM memories WHERE user_id=?" + ("" if include_deleted else " AND deleted_at IS NULL")
    params=[user_id]
    if query:
        sql += " AND (content LIKE ? OR category LIKE ?)"; params += [f"%{query[:100]}%"]*2
    sql += " ORDER BY priority DESC,updated_at DESC"
    with connect(db_path) as db: return [dict(x) for x in db.execute(sql,params).fetchall()]

def retrieve(user_id: int, query: str, limit: int = 5, db_path: str | None = None,
             search_terms: list[str] | None = None, exclude_ids: set[int] | list[int] | None = None) -> list[dict]:
    terms=set(re.findall(r"[a-z0-9]{3,}",query.lower()))
    for expanded in (search_terms or [])[:12]:
        terms.update(re.findall(r"[a-z0-9]{3,}",expanded.lower()))
    terms.difference_update(RETRIEVAL_STOP_WORDS)
    if not terms or limit<=0: return []
    candidates=[]
    # FTS5 expands the candidate set across tokenized query terms and ranks
    # longer, rarer matches. Owner and lifecycle filters remain SQL-enforced.
    fts_expression=" OR ".join(f'"{term}"' for term in sorted(terms))
    try:
        with connect(db_path) as db:
            candidates=[dict(row) for row in db.execute(
                "SELECT m.*,bm25(memory_search_fts,5.0,1.0) AS fts_rank "
                "FROM memory_search_fts JOIN memories m ON m.id=memory_search_fts.rowid "
                "WHERE memory_search_fts MATCH ? AND memory_search_fts.owner_id=? "
                "AND m.user_id=? AND m.deleted_at IS NULL AND m.temporary=0 "
                "ORDER BY fts_rank ASC LIMIT ?",
                (fts_expression,user_id,user_id,max(50,limit*10)),
            ).fetchall()]
    except sqlite3.OperationalError as exc:
        # Keep compatibility with SQLite builds without FTS5 or databases
        # initialized before the optional index was added.
        message=str(exc).lower()
        if "no such module: fts5" not in message and "no such table: memory_search_fts" not in message: raise
    if not candidates:
        candidates=list_memories(user_id,db_path=db_path)
    candidates=[item for item in candidates if not item["temporary"] and item['id'] not in (exclude_ids or ())]
    fts_order={item['id']:index for index,item in enumerate(candidates) if 'fts_rank' in item}
    ranked=[]
    for item in candidates:
        words=set(re.findall(r"[a-z0-9]{3,}",(item["content"]+" "+item["category"]).lower()))
        overlap=len(terms & words)
        fts_index=fts_order.get(item['id'])
        if overlap or fts_index is not None:
            lexical_score=overlap/max(1,math.sqrt(len(terms)))
            fts_score=(1-fts_index/max(1,len(fts_order))) if fts_index is not None else 0
            score=lexical_score+0.35*fts_score+item["priority"]*0.025
            reason=(f"Shares {overlap} query keyword(s); "
                    +(f"FTS5 result rank {fts_index+1}; " if fts_index is not None else "lexical match; ")
                    +f"priority {item['priority']}/5.")
            ranked.append({**item,"score":round(score,3),"retrieval_reason":reason})
    return sorted(ranked,key=lambda x:(x["score"],x["updated_at"]),reverse=True)[:limit]

def soft_delete(user_id: int, memory_id: int, db_path: str | None = None) -> bool:
    with connect(db_path) as db:
        cur=db.execute("UPDATE memories SET deleted_at=? WHERE id=? AND user_id=? AND deleted_at IS NULL",(datetime.now(timezone.utc).isoformat(),memory_id,user_id))
        if cur.rowcount != 1: return False
        db.execute("INSERT INTO memory_events(user_id,memory_id,event_type) VALUES(?,?,?)",(user_id,memory_id,"deleted"))
        return True

def recover_deleted(user_id: int, memory_id: int, db_path: str | None = None) -> bool:
    with connect(db_path) as db:
        cur=db.execute("UPDATE memories SET deleted_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND deleted_at IS NOT NULL",(memory_id,user_id))
        if cur.rowcount != 1: return False
        db.execute("INSERT INTO memory_events(user_id,memory_id,event_type) VALUES(?,?,?)",(user_id,memory_id,"recovered"))
        return True

def permanently_delete(user_id: int, memory_id: int, db_path: str | None = None) -> bool:
    """Permanently remove a previously soft-deleted memory and cascading versions."""
    with connect(db_path) as db:
        existing=db.execute("SELECT id FROM memories WHERE id=? AND user_id=? AND deleted_at IS NOT NULL",(memory_id,user_id)).fetchone()
        if not existing: return False
        # Events intentionally have no memory foreign key so the deletion audit
        # record survives the cascade that removes the memory and its versions.
        db.execute("INSERT INTO memory_events(user_id,memory_id,event_type) VALUES(?,?,?)",(user_id,memory_id,"permanently_deleted"))
        cur=db.execute("DELETE FROM memories WHERE id=? AND user_id=? AND deleted_at IS NOT NULL",(memory_id,user_id))
        return cur.rowcount==1

def list_deleted(user_id: int, query: str = "", db_path: str | None = None) -> list[dict]:
    sql="SELECT * FROM memories WHERE user_id=? AND deleted_at IS NOT NULL"
    params=[user_id]
    if query:
        sql += " AND (content LIKE ? OR category LIKE ?)"; params += [f"%{query[:100]}%"]*2
    sql += " ORDER BY deleted_at DESC"
    with connect(db_path) as db: return [dict(x) for x in db.execute(sql,params).fetchall()]

def restore_version(user_id: int, memory_id: int, version: int, db_path: str | None = None) -> bool:
    with connect(db_path) as db:
        row=db.execute("SELECT v.* FROM memory_versions v JOIN memories m ON m.id=v.memory_id WHERE m.id=? AND m.user_id=? AND v.version=? AND m.deleted_at IS NULL",(memory_id,user_id,version)).fetchone()
    return update_memory(user_id,memory_id,row["content"],row["category"],row["priority"],f"Restored version {version}",db_path) if row else False

def versions(user_id: int, memory_id: int, db_path: str | None = None) -> list[dict]:
    with connect(db_path) as db:
        return [dict(x) for x in db.execute("SELECT v.* FROM memory_versions v JOIN memories m ON m.id=v.memory_id WHERE m.id=? AND m.user_id=? ORDER BY version DESC",(memory_id,user_id)).fetchall()]

def add_temporary(user_id: int, content: str, ttl_hours: int = 24) -> None:
    with connect() as db:
        db.execute("DELETE FROM temporary_context WHERE expires_at < ?",(datetime.now(timezone.utc).isoformat(),))
        db.execute("INSERT INTO temporary_context(user_id,content,expires_at) VALUES(?,?,?)",(user_id,content[:2000],(datetime.now(timezone.utc)+timedelta(hours=ttl_hours)).isoformat()))

def timeline(user_id: int, limit: int = 100, db_path: str | None = None) -> list[dict]:
    """Return genuine version and recorded lifecycle events for one account."""
    bounded_limit=max(1,min(limit,250))
    with connect(db_path) as db:
        versions=[dict(row) for row in db.execute(
            "SELECT v.id, v.memory_id, v.version, v.content, v.reason, v.created_at, "
            "m.category, CASE WHEN v.version=1 THEN 'created' "
            "WHEN v.reason LIKE 'Restored version%' THEN 'restored' ELSE 'updated' END AS event_type "
            "FROM memory_versions v JOIN memories m ON m.id=v.memory_id "
            "WHERE m.user_id=? ORDER BY v.created_at DESC, v.id DESC LIMIT ?",
            (user_id,bounded_limit)).fetchall()]
        events=[dict(row) for row in db.execute(
            "SELECT e.id, e.memory_id, e.event_type, e.created_at, m.category, m.content "
            "FROM memory_events e LEFT JOIN memories m ON m.id=e.memory_id AND m.user_id=e.user_id "
            "WHERE e.user_id=? AND e.event_type IN ('deleted','recovered','permanently_deleted') "
            "ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
            (user_id,bounded_limit)).fetchall()]
    combined=versions+events
    def event_time(item: dict) -> tuple[float, int]:
        value=str(item.get("created_at", ""))
        try:
            parsed=datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp(),int(item.get("id",0))
        except (ValueError, OverflowError):
            return 0.0,int(item.get("id",0))
    combined.sort(key=event_time,reverse=True)
    return combined[:bounded_limit]

def analytics(user_id: int, db_path: str | None = None) -> dict:
    """Compute account-scoped counts from persisted records only."""
    with connect(db_path) as db:
        counts=db.execute(
            "SELECT SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) active, "
            "SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) deleted "
            "FROM memories WHERE user_id=?",(user_id,)).fetchone()
        categories=[dict(row) for row in db.execute(
            "SELECT category, COUNT(*) AS count FROM memories WHERE user_id=? AND deleted_at IS NULL "
            "GROUP BY category ORDER BY count DESC, category",(user_id,)).fetchall()]
        event_count=db.execute("SELECT COUNT(*) FROM memory_events WHERE user_id=?",(user_id,)).fetchone()[0]
        version_count=db.execute(
            "SELECT COUNT(*) FROM memory_versions v JOIN memories m ON m.id=v.memory_id WHERE m.user_id=?",
            (user_id,)).fetchone()[0]
    return {"active_memories":counts["active"] or 0,"deleted_memories":counts["deleted"] or 0,
            "categories":categories,"recorded_lifecycle_events":event_count,"memory_versions":version_count}
