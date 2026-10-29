import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.llm.llm import llm_api_call, query_rewriter
from src.llm.prompt.system_prompt import system_prompt
from src.db import prisma
# from src.llm.llm_run import run_chat
from src.vector.retrieve import retrieve_relevant_chunks

router = APIRouter(tags=["testing"])

class TestingRequest(BaseModel):
    question: str

@router.post("/testing/{bot_id}")
async def testing(
    bot_id: str,
    request: TestingRequest,
):
   try:
        rewritten_query = await query_rewriter(request.question)
        print(f"rewritten_queryishere{rewritten_query}")
        return {
            "rewritten_query": rewritten_query,
        }
   except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))
