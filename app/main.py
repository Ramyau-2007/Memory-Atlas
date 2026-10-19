from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from time import perf_counter
from fastapi import FastAPI, Depends, HTTPException, Response, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .db import init_db, connect
from .auth import register, login, current_user, revoke
from .invitations import invitations_required
from . import memory
from .agent import ModelAdapter, ModelAdapterError, token_estimate
from .security import (
    DailyModelUsageGuard, ModelUsageLimitExceeded, RequestSecurityMiddleware,
    env_flag, limit_request, model_usage_guard,
)

@asynccontextmanager
async def lifespan(app):
    if (os.getenv("APP_ENV", "development").strip().lower() == "production"
            and env_flag("SESSION_COOKIE_AUTH") and not env_flag("SESSION_COOKIE_SECURE")):
        raise RuntimeError("SESSION_COOKIE_SECURE must be enabled when cookie authentication is used in production.")
    init_db()
    yield

_production = os.getenv("APP_ENV", "development").strip().lower() == "production"
app=FastAPI(title="Memory Atlas",version="0.2.0",lifespan=lifespan,
            docs_url=None if _production else "/docs",
            redoc_url=None if _production else "/redoc",
            openapi_url=None if _production else "/openapi.json")
app.add_middleware(RequestSecurityMiddleware)
app.state.model_usage_guard = model_usage_guard
app.mount("/static",StaticFiles(directory=Path(__file__).parent.parent/"static"),name="static")

@app.exception_handler(ModelAdapterError)
async def model_error_handler(request: Request, exc: ModelAdapterError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=64)
    password:str=Field(min_length=10,max_length=256)
    invitation_code:str|None=Field(default=None,max_length=160)
class MemoryInput(BaseModel): content:str=Field(min_length=1,max_length=2000); category:str=Field(default="fact",max_length=40); priority:int=Field(default=2,ge=1,le=5)
class PermanentDeleteInput(BaseModel): confirmation:str=Field(min_length=1,max_length=40)
class ChatInput(BaseModel): message:str=Field(min_length=1,max_length=4000); conversation_id:int|None=None
class MessageEditInput(BaseModel): message:str=Field(min_length=1,max_length=4000)
class ConversationCreate(BaseModel): title:str=Field(default="New chat",min_length=1,max_length=80)
class ConversationRename(BaseModel): title:str=Field(min_length=1,max_length=80)
def owner(u:int=Depends(current_user)): return u

def conversation_title(message:str)->str:
    """Create a short display title from the first user message."""
    text=" ".join(message.split())
    if len(text)<=48: return text
    shortened=text[:45].rsplit(" ",1)[0].rstrip(".,!?;:")
    return (shortened or text[:45]).rstrip()+"..."

def generate_chat_result(user:int,message:str,recent:list[dict],exclude_memory_ids:list[int]|None=None,
                         adapter=None,usage_reserved:bool=False)->dict:
    """Build selected context and generate one response without changing storage."""
    adapter=adapter or ModelAdapter()
    if adapter.provider=="groq" and not usage_reserved:
        app.state.model_usage_guard.reserve(user)
    analysis=None; analysis_status="not_needed"; analysis_ms=None
    decision=memory.classify(message)
    if adapter.provider=="groq" and memory.needs_memory_analysis(message):
        analysis_start=perf_counter()
        try:
            analysis=memory.validate_memory_analysis(adapter.analyze_memory_message(message))
            if analysis is None: analysis_status="invalid_response_fallback"
            else:
                decision=memory.decision_from_analysis(message,analysis)
                analysis_status="interpreted"
        except ModelAdapterError:
            analysis_status="provider_error_fallback"
        finally: analysis_ms=round((perf_counter()-analysis_start)*1000,3)
    retrieval_start=perf_counter()
    found=memory.retrieve(user,message,search_terms=analysis["search_terms"] if analysis else None,
                          exclude_ids=exclude_memory_ids)
    retrieval_ms=round((perf_counter()-retrieval_start)*1000,3)
    if adapter.provider=="ollama": supplied_memories=found; supplied_recent=recent
    elif adapter.provider=="groq": supplied_memories=found[:3]; supplied_recent=recent[-2:]
    else: supplied_memories=found[:3]; supplied_recent=[]
    provider_input=(adapter.ollama_prompt(message,supplied_memories,supplied_recent)
                    if adapter.provider=="ollama" else
                    adapter.groq_messages(message,supplied_memories,supplied_recent)
                    if adapter.provider=="groq" else None)
    generation_start=perf_counter()
    answer=adapter.answer(message,supplied_memories,supplied_recent)
    generation_ms=round((perf_counter()-generation_start)*1000,3)
    baseline=" ".join(m["content"] for m in recent)+" "+message
    proposed=(provider_input if isinstance(provider_input,str) else
              json.dumps(provider_input,ensure_ascii=False) if provider_input is not None else
              " ".join([message,*[m["content"] for m in supplied_memories]]))
    return {"answer":answer,"adapter":adapter,"decision":decision,"found":found,
            "analysis_status":analysis_status,"analysis_ms":analysis_ms,
            "supplied_memories":supplied_memories,"supplied_recent":supplied_recent,
            "provider_input":provider_input,"baseline_tokens_est":token_estimate(baseline),
            "proposed_tokens_est":token_estimate(proposed),"retrieval_ms":retrieval_ms,
            "generation_ms":generation_ms}

def chat_response_payload(result:dict,user_message:str,conversation_id:int,message_id:int|None=None)->dict:
    adapter=result["adapter"]
    provider_input=result["provider_input"]
    return {"conversation_id":conversation_id,"message_id":message_id,"answer":result["answer"],
            "provider":adapter.provider,"model":adapter.model_name,"retrieved":result["found"],
            "memory_decision":result["decision"],
            "memory_analysis":{"status":result["analysis_status"],"duration_ms":result["analysis_ms"],
                               "request_scope":"current_message_only" if result["analysis_ms"] is not None else None,
                               "note":"Structured interpretation runs only for likely personal facts/preferences or personal-memory questions. Its tokens are not included in the response-context estimate."},
            "context":{"baseline_tokens_est":result["baseline_tokens_est"],"proposed_tokens_est":result["proposed_tokens_est"],
                       "note":"Approximate character-based estimate; not model tokenizer measurements.",
                       "query":user_message,"memories_supplied":result["supplied_memories"],
                       "recent_context_supplied":result["supplied_recent"],
                       "ollama_prompt":provider_input if adapter.provider=="ollama" else None,
                       "adapter_input":provider_input,
                       "deterministic_note":"The development adapter is not an LLM; it uses the request and up to three retrieved memories and ignores recent conversation turns." if adapter.provider=="deterministic" else None},
            "timings":{"retrieval_ms":result["retrieval_ms"],"generation_ms":result["generation_ms"]}}

@app.get("/")
def home(): return FileResponse(Path(__file__).parent.parent/"static"/"index.html")
@app.get("/api/session-mode")
def api_session_mode():
    return {"cookie_auth":env_flag("SESSION_COOKIE_AUTH"),
            "invitation_required":invitations_required()}

@app.get("/api/session")
def api_session(user:int=Depends(owner)):
    return {"ok":True}

@app.post("/api/register",dependencies=[Depends(limit_request("register","REGISTER_RATE_LIMIT","REGISTER_RATE_WINDOW_SECONDS",5,3600))])
def api_register(body:Credentials):
    return {"user_id":register(body.username,body.password,invitation_code=body.invitation_code)}
@app.post("/api/login",dependencies=[Depends(limit_request("login","LOGIN_RATE_LIMIT","LOGIN_RATE_WINDOW_SECONDS",10,900))])
def api_login(body:Credentials,response:Response):
    token=login(body.username,body.password)
    if env_flag("SESSION_COOKIE_AUTH"):
        secure=env_flag("SESSION_COOKIE_SECURE",os.getenv("APP_ENV", "development").strip().lower()=="production")
        response.set_cookie("session",token,max_age=7*24*60*60,httponly=True,secure=secure,
                            samesite="lax",path="/")
        return {"ok":True}
    return {"ok":True,"token":token}
@app.post("/api/logout")
def api_logout(request:Request,response:Response,user:int=Depends(owner)):
    token=request.headers.get("authorization","")
    if token.lower().startswith("bearer "): token=token[7:].strip()
    else: token=request.cookies.get("session","")
    revoke(token)
    response.delete_cookie("session",path="/",httponly=True,
                           secure=env_flag("SESSION_COOKIE_SECURE",os.getenv("APP_ENV", "development").strip().lower()=="production"),samesite="lax")
    return {"ok":True}
@app.get("/api/conversations")
def api_conversations(user:int=Depends(owner)):
    with connect() as db:
        return [dict(row) for row in db.execute("""
            SELECT c.id,c.title,c.created_at,MAX(m.created_at) AS last_message_at,
                   COUNT(m.id) AS message_count
            FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
            WHERE c.user_id=? GROUP BY c.id
            ORDER BY COALESCE(MAX(m.id),0) DESC,c.id DESC
        """,(user,)).fetchall()]
@app.post("/api/conversations")
def api_create_conversation(body:ConversationCreate,user:int=Depends(owner)):
    title=body.title.strip()
    if not title: raise HTTPException(422,"Conversation title cannot be empty.")
    with connect() as db:
        row=db.execute("INSERT INTO conversations(user_id,title) VALUES(?,?)",(user,title))
        return {"id":row.lastrowid,"title":title}
@app.get("/api/conversations/{cid}/messages")
def api_conversation_messages(cid:int,user:int=Depends(owner)):
    with connect() as db:
        if not db.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",(cid,user)).fetchone():
            raise HTTPException(404,"Conversation not found.")
        return [dict(row) for row in db.execute(
            "SELECT id,role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id ASC",(cid,)
        ).fetchall()]
@app.patch("/api/conversations/{cid}/messages/{mid}",dependencies=[Depends(limit_request("chat","CHAT_RATE_LIMIT","CHAT_RATE_WINDOW_SECONDS",20,60))])
def api_edit_message(cid:int,mid:int,body:MessageEditInput,user:int=Depends(owner)):
    revised=body.message.strip()
    if not revised: raise HTTPException(422,"Message cannot be empty.")
    with connect() as db:
        conversation=db.execute("SELECT id,summary FROM conversations WHERE id=? AND user_id=?",(cid,user)).fetchone()
        target=db.execute("SELECT id,role FROM messages WHERE id=? AND conversation_id=?",(mid,cid)).fetchone()
        if not conversation or not target: raise HTTPException(404,"Conversation message not found.")
        if target["role"]!="user": raise HTTPException(400,"Only user messages can be edited.")
        branch_rows=db.execute("SELECT id FROM messages WHERE conversation_id=? AND id>=? ORDER BY id",(cid,mid)).fetchall()
        source_message_ids=[row["id"] for row in branch_rows]
        linked=db.execute(
            "SELECT id FROM memories WHERE user_id=? AND source_message_id IN ("+
            ",".join("?" for _ in source_message_ids)+")",(user,*source_message_ids)
        ).fetchall() if source_message_ids else []
        excluded_memory_ids=[row["id"] for row in linked]
        previous=[dict(row) for row in db.execute(
            "SELECT role,content FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT 4",(cid,mid)
        ).fetchall()][::-1]
        earlier_users=[row[0] for row in db.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 8",(cid,mid)
        ).fetchall()][::-1]
    summary=("Extractive conversation summary (recent user turns): "+" | ".join(text[:180] for text in earlier_users))[:1600] if len(earlier_users)>=8 else ""
    context=[{"role":"system","content":summary},*previous[-2:]] if summary else previous
    # Generate before mutating the stored branch. Provider errors therefore leave
    # the original message, later turns, and linked memories intact for retry.
    try:
        result=generate_chat_result(user,revised,context,excluded_memory_ids)
    except ModelUsageLimitExceeded as exc:
        raise HTTPException(429,str(exc),headers={"Retry-After":str(exc.retry_after)}) from exc
    decision=result["decision"]
    with connect() as db:
        deactivated,preserved=memory.deactivate_unedited_source_memories(db,user,source_message_ids)
        db.execute("UPDATE messages SET content=? WHERE id=? AND conversation_id=?",(revised,mid,cid))
        removed=db.execute("DELETE FROM messages WHERE conversation_id=? AND id>?",(cid,mid)).rowcount
        db.execute("UPDATE conversations SET summary=? WHERE id=? AND user_id=?",(summary,cid,user))
        db.execute("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)",(cid,"assistant",result["answer"]))
        if decision["retain"]:
            memory.add_memory_record(db,user,decision["content"],decision["category"],decision["priority"],
                                     decision["reason"],source="conversation",source_message_id=mid)
    if not decision["retain"]: memory.add_temporary(user,revised)
    payload=chat_response_payload(result,revised,cid,mid)
    payload.update({"edited":True,"removed_later_messages":removed,
                    "deactivated_memories":deactivated,"preserved_user_edited_memories":preserved})
    return payload
@app.patch("/api/conversations/{cid}")
def api_rename_conversation(cid:int,body:ConversationRename,user:int=Depends(owner)):
    title=body.title.strip()
    if not title: raise HTTPException(422,"Conversation title cannot be empty.")
    with connect() as db:
        changed=db.execute("UPDATE conversations SET title=? WHERE id=? AND user_id=?",(title,cid,user)).rowcount
        if not changed: raise HTTPException(404,"Conversation not found.")
    return {"ok":True,"id":cid,"title":title}
@app.delete("/api/conversations/{cid}")
def api_delete_conversation(cid:int,user:int=Depends(owner)):
    with connect() as db:
        changed=db.execute("DELETE FROM conversations WHERE id=? AND user_id=?",(cid,user)).rowcount
        if not changed: raise HTTPException(404,"Conversation not found.")
    return {"ok":True,"deleted":"conversation","note":"Conversation messages were deleted. Saved long-term memories were not changed."}
@app.get("/api/memories")
def api_memories(q:str="",user:int=Depends(owner)): return memory.list_memories(user,q)
@app.get("/api/memories/deleted")
def api_deleted_memories(q:str="",user:int=Depends(owner)): return memory.list_deleted(user,q)
@app.get("/api/timeline")
def api_timeline(limit:int=100,user:int=Depends(owner)): return memory.timeline(user,limit)
@app.get("/api/analytics")
def api_analytics(user:int=Depends(owner)): return memory.analytics(user)
@app.post("/api/memories")
def api_add(body:MemoryInput,user:int=Depends(owner)):
    return {"id":memory.add_memory(user,body.content,body.category,body.priority,"Added by user","user_panel")}
@app.patch("/api/memories/{mid}")
def api_update(mid:int,body:MemoryInput,user:int=Depends(owner)):
    if not memory.update_memory(user,mid,body.content,body.category,body.priority): raise HTTPException(404,"Memory not found.")
    return {"ok":True}
@app.delete("/api/memories/{mid}")
def api_delete(mid:int,user:int=Depends(owner)):
    if not memory.soft_delete(user,mid): raise HTTPException(404,"Memory not found.")
    return {"ok":True,"deleted":"soft"}
@app.post("/api/memories/{mid}/recover")
def api_recover(mid:int,user:int=Depends(owner)):
    if not memory.recover_deleted(user,mid): raise HTTPException(404,"Deleted memory not found.")
    return {"ok":True,"recovered":True}
@app.delete("/api/memories/{mid}/permanent")
def api_permanent_delete(mid:int,body:PermanentDeleteInput,user:int=Depends(owner)):
    if body.confirmation!="PERMANENTLY DELETE": raise HTTPException(400,"Type PERMANENTLY DELETE exactly to confirm.")
    if not memory.permanently_delete(user,mid): raise HTTPException(404,"Deleted memory not found.")
    return {"ok":True,"deleted":"permanent","note":"Memory and its versions were removed from this SQLite database. Backups or external copies are not affected."}
@app.get("/api/memories/{mid}/versions")
def api_versions(mid:int,user:int=Depends(owner)):
    if not memory.versions(user,mid): raise HTTPException(404,"Memory or versions not found.")
    return memory.versions(user,mid)
@app.post("/api/memories/{mid}/restore/{version}")
def api_restore(mid:int,version:int,user:int=Depends(owner)):
    if not memory.restore_version(user,mid,version): raise HTTPException(404,"Memory version not found.")
    return {"ok":True,"restored_as_new_version":True}
@app.post("/api/chat",dependencies=[Depends(limit_request("chat","CHAT_RATE_LIMIT","CHAT_RATE_WINDOW_SECONDS",20,60))])
def api_chat(body:ChatInput,user:int=Depends(owner)):
    adapter=ModelAdapter()
    if body.conversation_id is not None:
        with connect() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",(body.conversation_id,user)).fetchone():
                raise HTTPException(404,"Conversation not found.")
    usage_reserved=adapter.provider=="groq"
    if usage_reserved:
        try: app.state.model_usage_guard.reserve(user)
        except ModelUsageLimitExceeded as exc:
            raise HTTPException(429,str(exc),headers={"Retry-After":str(exc.retry_after)}) from exc
    with connect() as db:
        if body.conversation_id is not None:
            conv=db.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",(body.conversation_id,user)).fetchone()
            if not conv: raise HTTPException(404,"Conversation not found.")
            cid=body.conversation_id
        else:
            cid=db.execute("INSERT INTO conversations(user_id,title) VALUES(?,?)",(user,conversation_title(body.message))).lastrowid
        title_row=db.execute("SELECT title FROM conversations WHERE id=? AND user_id=?",(cid,user)).fetchone()
        if title_row and title_row["title"]=="New chat" and not db.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1",(cid,)
        ).fetchone():
            db.execute("UPDATE conversations SET title=? WHERE id=? AND user_id=?",(conversation_title(body.message),cid,user))
        recent=[dict(x) for x in db.execute("SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 4",(cid,)).fetchall()][::-1]
        message_id=db.execute("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)",(cid,"user",body.message)).lastrowid
        message_count=db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",(cid,)).fetchone()[0]
        summary_row=db.execute("SELECT summary FROM conversations WHERE id=?",(cid,)).fetchone()
        summary=summary_row["summary"] if summary_row else ""
        if message_count >= 8:
            user_turns=[x[0] for x in db.execute("SELECT content FROM messages WHERE conversation_id=? AND role='user' ORDER BY id DESC LIMIT 8",(cid,)).fetchall()][::-1]
            summary="Extractive conversation summary (recent user turns): " + " | ".join(x[:180] for x in user_turns)
            db.execute("UPDATE conversations SET summary=? WHERE id=?",(summary[:1600],cid))
        if summary: recent=[{"role":"system","content":summary},*recent[-2:]]
    result=generate_chat_result(user,body.message,recent,adapter=adapter,usage_reserved=usage_reserved)
    decision=result["decision"]
    with connect() as db: db.execute("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)",(cid,"assistant",result["answer"]))
    if decision["retain"]: memory.add_memory(user,decision["content"],decision["category"],decision["priority"],decision["reason"],source_message_id=message_id)
    else: memory.add_temporary(user,body.message)
    return chat_response_payload(result,body.message,cid,int(message_id))
@app.get("/api/health")
def health():
    adapter=ModelAdapter()
    return {"status":"ok","provider":adapter.provider,"model":adapter.model_name}
