from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.tools.react_agent import ReactAgent
from db.knowledge_repository import KnowledgeRepository
from db.session_repository import SessionPersistenceError
from rag.vector_store import KnowledgeVectorStore
from utils.config_handler import chroma_conf


app = FastAPI(title="全屋家具智能管家 API")

cors_origins_str = os.getenv("CORS_ORIGINS", "http://localhost:8501,http://localhost:8008")
cors_origins = [origin.strip() for origin in cors_origins_str.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = ReactAgent()
VALID_API_KEY = os.getenv("API_KEY", "")


def verify_api_key(x_api_key: str = Header(...)):
    if not VALID_API_KEY or x_api_key != VALID_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return x_api_key


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_uuid: Optional[str] = None
    request_id: Optional[str] = None


@app.post("/chat", dependencies=[Depends(verify_api_key)])
async def chat_endpoint(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    user_uuid = req.user_uuid or str(uuid.uuid4())
    request_id = req.request_id or str(uuid.uuid4())

    async def event_generator():
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def sync_gen():
            def push_event(event_type: str, payload: str | None) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, (event_type, payload))

            try:
                for chunk in agent.execute_stream(
                    query=req.message,
                    session_id=session_id,
                    user_uuid=user_uuid,
                    request_id=request_id,
                ):
                    for char in chunk:
                        push_event("content", char)
                push_event("done", None)
            except SessionPersistenceError as exc:
                push_event("error", str(exc))
            except Exception as exc:
                push_event("error", f"生成回答失败: {exc}")
            finally:
                push_event("end", None)

        loop.run_in_executor(None, sync_gen)

        while True:
            event_type, payload = await queue.get()
            if event_type == "end":
                break
            if event_type == "content":
                yield f"data: {json.dumps({'content': payload}, ensure_ascii=False)}\n\n"
            elif event_type == "done":
                yield f"data: {json.dumps({'done': True, 'session_id': session_id, 'user_uuid': user_uuid, 'request_id': request_id}, ensure_ascii=False)}\n\n"
            elif event_type == "error":
                yield f"data: {json.dumps({'error': payload, 'session_id': session_id, 'user_uuid': user_uuid, 'request_id': request_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/health")
def health():
    return {"status": "ok", "rag": _rag_readiness()}


@app.get("/live")
def live():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    rag_health = _rag_readiness()
    if rag_health["status"] != "ready":
        raise HTTPException(status_code=503, detail={"rag": rag_health})
    return {"status": "ready", "rag": rag_health}


def _rag_readiness():
    try:
        repository = KnowledgeRepository()
        rag_health = repository.get_health(chroma_conf["collection_name"])
        vector_store = KnowledgeVectorStore(
            collection_name=chroma_conf["collection_name"],
            require_persistent=True,
        )
        vector_health = vector_store.healthcheck()
        rag_health["vector_chunks"] = vector_health["vector_count"]
        rag_health["index_consistent"] = (
            rag_health["active_chunks"] == vector_health["vector_count"]
        )
        if not rag_health["index_consistent"]:
            rag_health["status"] = "degraded"
            rag_health["error"] = "mysql_chroma_chunk_count_mismatch"
    except Exception as exc:
        rag_health = {
            "status": "degraded",
            "active_documents": 0,
            "active_chunks": 0,
            "pending_tasks": 0,
            "dead_tasks": 0,
            "error": str(exc),
        }
    return rag_health


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8008, reload=True)
