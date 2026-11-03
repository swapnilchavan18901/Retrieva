from fastapi import APIRouter, Depends, HTTPException
from src.db import prisma
from src.deps.auth import get_current_user
from src.utils.api_key import generate_api_key, hash_api_key
from src.env import env
from src.resource_limits import enforce_quota

router = APIRouter(tags=["api-keys"])

@router.post("/bots/{bot_id}/api-keys")
async def create_api_key(
    bot_id: str,
    user = Depends(get_current_user)
):
    bot = await prisma.bot.find_unique(where={"id": bot_id})
    if not bot or bot.userId != user.id:
        raise HTTPException(status_code=404, detail="Bot not found")

    raw_key = generate_api_key()
    hashed_key = hash_api_key(raw_key)
    async with prisma.tx() as tx:
        count = await enforce_quota(tx, "API key", bot_id, "apikey", {"botId": bot_id, "isActive": True}, env.MAX_API_KEYS_PER_BOT)
        name = f"ApiKey{count + 1}"
        key = await tx.apikey.create(data={"botId": bot_id, "keyHash": hashed_key, "name": name})
    return {"id": key.id, "apiKey": raw_key, "name": name}

@router.get("/bots/{bot_id}/api-keys")
async def list_api_keys(
    bot_id: str,
    user = Depends(get_current_user)
):
    bot = await prisma.bot.find_unique(where={"id": bot_id})

    if not bot or bot.userId != user.id:
        raise HTTPException(status_code=404, detail="Bot not found")

    keys = await prisma.apikey.find_many(
        where={"botId": bot_id},
    )

    return [{
        "id": k.id,
        "name": k.name,
        "isActive": k.isActive,
        "createdAt": k.createdAt,
        "lastUsedAt": k.lastUsedAt,
    } for k in keys]


@router.delete("/api-keys/{api_key_id}")
async def revoke_api_key(
    api_key_id: str,
    user = Depends(get_current_user)
):
    key = await prisma.apikey.find_unique(where={"id": api_key_id})

    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    bot = await prisma.bot.find_unique(where={"id": key.botId})
    if not bot or bot.userId != user.id:
        raise HTTPException(status_code=403)

    # Keep chat/session foreign keys intact while immediately denying new usage.
    await prisma.apikey.update(where={"id": api_key_id}, data={"isActive": False})

    return {"success": True}
