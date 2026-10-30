from socketio.exceptions import ConnectionRefusedError
from src.sockets.ws_chats import sio
from src.db import prisma
from src.utils.api_key import hash_api_key
from src.vector.retrieve import retrieve_relevant_chunks
from src.llm.llm import llm_api_call
from src.llm.prompt.system_prompt import system_prompt
import asyncio
from time import perf_counter
from src.sockets.limits import ChatLimits, LimitDenied, ip_hash, validate_message, REPLY_TIMEOUT_SECONDS
from shared.benchmark import benchmark_run


limits = ChatLimits(prisma)
_connection_tasks = {}


async def _renew_connection(sid):
    try:
        while True:
            await asyncio.sleep(30)
            if not await limits.renew(sid):
                break
    except asyncio.CancelledError:
        return
    except Exception:
        pass
    await sio.disconnect(sid)


async def reject(sid, error):
    await sio.emit("chat_error", error.payload(), to=sid)


@sio.event
async def connect(sid, environ, auth):
    address = ip_hash(environ)
    try:
        await limits.connection_attempt(address)
    except LimitDenied as error:
        raise ConnectionRefusedError(error.message, error.payload())
    except Exception:
        return False  # Fail closed if shared enforcement is unavailable.
    if not isinstance(auth, dict):
        return False
    api_key = auth.get("apiKey")
    bot_id = auth.get("botId")
    if not isinstance(api_key, str) or not isinstance(bot_id, str) or len(api_key) > 256 or len(bot_id) > 100 or not api_key or not bot_id:
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

    try:
        await limits.connect(sid, bot.id, api_key_obj.id, address)
    except LimitDenied as error:
        raise ConnectionRefusedError(error.message, error.payload())
    except Exception:
        return False
    # store context on socket
    await sio.save_session(sid, {
        "botId": bot.id,
        "apiKeyId": api_key_obj.id,
        "ipHash": address,
    })
    _connection_tasks[sid] = asyncio.create_task(_renew_connection(sid))

    print(f"Socket connected: {sid}")


@sio.event
async def start_chat(sid, data):
    try:
        session = await sio.get_session(sid)
        await limits.control(session)

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
    except LimitDenied as error:
        await reject(sid, error)
    except Exception as e:
        print(f"start_chat ERROR: {type(e).__name__}")
        await reject(sid, LimitDenied("unavailable", "Unable to start chat. Please try again.", 5))


@sio.event
async def resume_chat(sid, data):
    """Resume an existing chat session (when user clicks a conversation in sidebar)."""
    session = await sio.get_session(sid)
    try:
        await limits.control(session)
    except LimitDenied as error:
        await reject(sid, error)
        return
    except Exception:
        await reject(sid, LimitDenied("unavailable", "Chat is temporarily unavailable.", 5))
        return
    session_id = data.get("sessionId") if isinstance(data, dict) else None

    if not session_id:
        await sio.emit("error", {"message": "sessionId is required"}, to=sid)
        return

    # Verify the session exists and belongs to this bot
    chat_session = await prisma.chatsession.find_unique(
        where={"id": session_id}
    )

    if not chat_session or chat_session.botId != session["botId"] or chat_session.apiKeyId != session["apiKeyId"]:
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
    admission = None
    with benchmark_run("chat_turn") as benchmark:
        benchmark.started_at = perf_counter()
        try:
            validate_message(data)
            session = await sio.get_session(sid)
            if not session.get("sessionId") or not session.get("room"):
                raise LimitDenied("chat_required", "Please start a chat first.", 0)
            with benchmark.stage("admission"):
                admission = await limits.acquire(session)
            benchmark.metrics["reserved_output_tokens"] = admission.max_tokens
            async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
                await _respond_to_message(sid, data, benchmark, admission.max_tokens, session)
        except LimitDenied as error:
            benchmark.failed(error, outcome="rejected")
            benchmark.metrics["rejection_code"] = error.code
            await reject(sid, error)
        except Exception as error:
            benchmark.failed(error)
            await reject(sid, LimitDenied("unavailable", "Chat is temporarily unavailable. Please try again.", 5))
        finally:
            if admission is not None:
                calls = [call for call in benchmark.calls if call["provider"] == "sarvam"]
                # Missing final usage remains charged at the full reservation.
                reported = [call["usage"]["completion_tokens"] for call in calls]
                output = sum(reported) if all(value is not None for value in reported) else None
                try:
                    await asyncio.shield(limits.finish(admission, output))
                except Exception as error:
                    benchmark.metrics["quota_settlement_error"] = type(error).__name__


async def _respond_to_message(sid, data, benchmark, max_tokens, session):
    room = session["room"]
    bot_id = session["botId"]
    benchmark.identifiers.update(bot_id=bot_id, session_id=session["sessionId"])

    message = data.get("content")
    if not message:
        return

    with benchmark.stage("save_user_message"):
        user_record = await prisma.chatmessage.create(
            data={"sessionId": session["sessionId"], "role": "user", "content": message}
        )
    benchmark.identifiers["message_id"] = user_record.id
    partial = []
    try:
        with benchmark.stage("conversation_lookup"):
            previous = await prisma.chatmessage.find_many(
                where={"sessionId": session["sessionId"], "id": {"not": user_record.id},
                       "role": {"in": ["user", "assistant"]}},
                order={"createdAt": "desc"}, take=12,
            )
            history = [{"role": str(item.role.value if hasattr(item.role, "value") else item.role),
                        "content": item.content} for item in reversed(previous)]
            bot = await prisma.bot.find_unique(where={"id": bot_id})
        if not bot or not bot.isActive:
            raise ValueError("Assistant unavailable")
        # One embedding search avoids a slow generation call before the answer.
        # Prior user questions preserve the topic for short follow-up questions.
        prior_questions = [item["content"][:500] for item in history if item["role"] == "user"][-2:]
        search_question = "\n".join([*prior_questions, message])
        with benchmark.stage("retrieval"):
            context = await retrieve_relevant_chunks(collection_name=bot_id, question=search_question)
        streamed = bool(data.get("stream"))
        async def send_delta(content):
            partial.append(content)
            await sio.emit("assistant_delta", {
                "content": content, "messageId": user_record.id,
                "sessionId": session["sessionId"],
            }, to=room)
            if "server_ttft_ms" not in benchmark.metrics:
                benchmark.metrics["server_ttft_ms"] = round((perf_counter() - benchmark.started_at) * 1000, 3)
        with benchmark.stage("answer_generation"):
            options = dict(system_prompt=system_prompt, bot_prompt=bot.systemPrompt,
                           context=context, user_prompt=message, history=history, max_tokens=max_tokens)
            # Older integrations still receive the original complete-message event.
            if streamed:
                options["on_delta"] = send_delta
            response = await llm_api_call(**options)
        reply = response.get("choices", [{}])[0].get("message", {}).get("content")
        if not reply:
            benchmark.failed(ValueError("Empty assistant response"), outcome="fallback")
            reply = "Sorry, I couldn't respond just now. Please try again."
    except Exception as e:
        benchmark.failed(e, outcome="fallback")
        print(f"Chat response error: {type(e).__name__}")
        reply = "".join(partial) + "\n\nSorry, the response was interrupted. Please try again." if partial else "Sorry, I couldn't respond just now. Please try again."

    with benchmark.stage("save_and_emit_reply"):
        # Store assistant message in DB
        await prisma.chatmessage.create(
            data={
                "sessionId": session["sessionId"],
                "role": "assistant",
                "content": reply,
            }
        )

        payload = {"content": reply}
        if data.get("stream"):
            payload.update(messageId=user_record.id, sessionId=session["sessionId"], done=True)
        await sio.emit("assistant_message", payload, to=room)


@sio.event
async def disconnect(sid, reason=None):
    task = _connection_tasks.pop(sid, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    try:
        await limits.disconnect(sid)
    except Exception:
        pass  # Lease expires even if the process/database is unavailable.
