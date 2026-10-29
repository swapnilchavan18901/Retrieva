from src.sockets.ws_chats import sio
from src.db import prisma
from src.utils.api_key import hash_api_key
from src.vector.retrieve import retrieve_relevant_chunks
from src.llm.llm import llm_api_call, query_rewriter
from src.llm.prompt.system_prompt import system_prompt
import asyncio


@sio.event
async def connect(sid, environ, auth):
    print(f"connect: {sid}")
    print(f"auth: {auth}")
    api_key = auth.get("apiKey")
    bot_id = auth.get("botId")
    if not api_key or not bot_id:
        print("api_key or bot_id is not found")
        return False  # reject connection

    hashed = hash_api_key(api_key)

    api_key_obj = await prisma.apikey.find_unique(
        where={"keyHash": hashed}
    )

    if not api_key_obj or not api_key_obj.isActive:
        return False

    if api_key_obj.botId != bot_id:
        return False

    bot = await prisma.bot.find_unique(where={"id": bot_id})
    if not bot or not bot.isActive:
        return False

    # store context on socket
    await sio.save_session(sid, {
        "botId": bot.id,
        "apiKeyId": api_key_obj.id,
    })

    print(f"Socket connected: {sid}")


@sio.event
async def start_chat(sid, data):
    try:
        session = await sio.get_session(sid)
        print(f"start_chat: {sid}, session: {session}")

        # 0️⃣ Leave old room if any
        old_room = session.get("room")
        if old_room:
            await sio.leave_room(sid, old_room)

        # 1️⃣ Create chat session
        chat_session = await prisma.chatsession.create(
            data={
                "botId": session["botId"],
                "apiKeyId": session["apiKeyId"],
            }
        )

        # 2️⃣ Fetch bot to get system prompt
        bot = await prisma.bot.find_unique(
            where={"id": session["botId"]}
        )

        # 3️⃣ Insert SYSTEM message FIRST
        if bot and bot.systemPrompt:
            await prisma.chatmessage.create(
                data={
                    "sessionId": chat_session.id,
                    "role": "system",
                    "content": bot.systemPrompt,
                }
            )

        # 4️⃣ Join room (session == room)
        room = f"chat:{chat_session.id}"
        await sio.enter_room(sid, room)

        # 5️⃣ Save session context
        await sio.save_session(sid, {
            **session,
            "sessionId": chat_session.id,
            "room": room,
        })

        # 6️⃣ Notify client
        await sio.emit(
            "chat_started",
            {"sessionId": chat_session.id},
            to=sid,
        )
        print(f"chat_started emitted for session: {chat_session.id}")
    except Exception as e:
        print(f"start_chat ERROR: {e}")
        import traceback
        traceback.print_exc()
        await sio.emit("error", {"message": str(e)}, to=sid)


@sio.event
async def resume_chat(sid, data):
    """Resume an existing chat session (when user clicks a conversation in sidebar)."""
    session = await sio.get_session(sid)
    session_id = data.get("sessionId")

    if not session_id:
        await sio.emit("error", {"message": "sessionId is required"}, to=sid)
        return

    # Verify the session exists and belongs to this bot
    chat_session = await prisma.chatsession.find_unique(
        where={"id": session_id}
    )

    if not chat_session or chat_session.botId != session["botId"]:
        await sio.emit("error", {"message": "Invalid session"}, to=sid)
        return

    # Leave old room if any
    old_room = session.get("room")
    if old_room:
        await sio.leave_room(sid, old_room)

    # Join new room
    room = f"chat:{chat_session.id}"
    await sio.enter_room(sid, room)

    # Save session context
    await sio.save_session(sid, {
        **session,
        "sessionId": chat_session.id,
        "room": room,
    })

    # Notify client
    await sio.emit(
        "chat_resumed",
        {"sessionId": chat_session.id},
        to=sid,
    )


@sio.event
async def user_message(sid, data):
    session = await sio.get_session(sid)
    room = session["room"]
    bot_id = session["botId"]

    message = data.get("content")
    if not message:
        return

    # Store user message in DB
    await prisma.chatmessage.create(
        data={
            "sessionId": session["sessionId"],
            "role": "user",
            "content": message,
        }
    )
         # Get bot for its system prompt
    bot = await prisma.bot.find_unique(where={"id": bot_id})
    bot_data = bot.model_dump()
    if not bot_data:
        return
    query_rewriter_response = await query_rewriter(message)
    llm_context = []
    for query in query_rewriter_response["new_queries"]:
        context = await retrieve_relevant_chunks(
            collection_name=bot_id,
            question=query,
        )
        llm_context.append(context)
    # Retrieve relevant document chunks from vector store
    try:
        # Call the LLM
        response = await llm_api_call(
            system_prompt=system_prompt,
            bot_prompt=bot_data["systemPrompt"],
            context=context,
            user_prompt=message,
        )

        reply = response.get("choices", [{}])[0].get("message", {}).get("content", "Sorry, I could not generate a response.")
    except Exception as e:
        print(f"LLM error: {e}")
        reply = "Sorry, I encountered an error processing your question. Please try again."

    # Store assistant message in DB
    await prisma.chatmessage.create(
        data={
            "sessionId": session["sessionId"],
            "role": "assistant",
            "content": reply,
        }
    )

    await sio.emit("assistant_message", {
        "content": reply,
    }, to=room)


@sio.event
async def disconnect(sid):
    print(f"Socket disconnected: {sid}")
