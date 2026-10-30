"""Retired unmetered development endpoint; all paid chat calls use Socket.IO admission."""
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["testing"])


@router.post("/testing/{bot_id}")
async def testing(bot_id: str):
    raise HTTPException(status_code=410, detail="Use the authenticated chat connection.")
