from fastapi import APIRouter, Depends, HTTPException
from src.db import prisma
from src.schemas.bots import BotCreateRequest, BotUpdateRequest
from src.deps.auth import get_current_user
from src.env import env
from src.resource_limits import enforce_quota

router = APIRouter(prefix="/bots", tags=["bots"])

@router.post("")
async def create_bot(
    data: BotCreateRequest,
    user = Depends(get_current_user)
):
    async with prisma.tx() as tx:
        await enforce_quota(tx, "Assistant", user.id, "bot", {"userId": user.id}, env.MAX_BOTS_PER_USER)
        bot = await tx.bot.create(
            data={
                "name": data.name,
                "description": data.description,
                "systemPrompt": data.systemPrompt,
                "userId": user.id,
            }
        )

    return {
        "id": bot.id,
        "name": bot.name,
        "createdAt": bot.createdAt,
    }


@router.get("")
async def list_bots(user = Depends(get_current_user)):
    bots = await prisma.bot.find_many(
        where={"userId": user.id},
        order={"createdAt": "desc"},
    )

    return bots


@router.put("/{bot_id}")
async def update_bot(
    bot_id: str,
    data: BotUpdateRequest,
    user = Depends(get_current_user),
):
    bot = await prisma.bot.find_unique(where={"id": bot_id})

    if not bot or bot.userId != user.id:
        raise HTTPException(status_code=404, detail="Bot not found")

    updated = await prisma.bot.update(
        where={"id": bot_id},
        data=data.model_dump(exclude_unset=True),
    )

    return updated


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: str,
    user = Depends(get_current_user),
):
    bot = await prisma.bot.find_unique(where={"id": bot_id})

    if not bot or bot.userId != user.id:
        raise HTTPException(status_code=404, detail="Bot not found")

    async with prisma.tx() as tx:
        # These history relations do not cascade from Bot in the Prisma schema.
        await tx.chatrequest.delete_many(where={"botId": bot_id})
        await tx.chatsession.delete_many(where={"botId": bot_id})
        await tx.bot.delete(where={"id": bot_id})

    return {"success": True}
