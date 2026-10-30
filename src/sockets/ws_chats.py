import socketio

sio = socketio.AsyncServer(
    async_mode="asgi",
    max_http_buffer_size=32_768,
    cors_allowed_origins="*",
)

socket_app = socketio.ASGIApp(sio)

# 👇 IMPORT HANDLERS SO EVENTS REGISTER
import src.sockets.handlers  # noqa: F401, E402