from dotenv import load_dotenv
load_dotenv() 

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.routes.auth import router as auth_router
from src.db import prisma
from src.routes.bots import router as bots_router
from src.routes.api_key import router as api_key_router
from src.sockets.ws_chats import socket_app
from src.routes.documenation import router as documentation_router
from src.routes.testing import router as testing_router
from src.routes.chats import router as chats_router
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await prisma.connect()

@app.on_event("shutdown")
async def shutdown():
    await prisma.disconnect()
    
#sockets


@app.get("/")
async def status():
    return {"status": "ok", "service": "FileMind"}


@app.get("/health")
async def health():
    database_connected = prisma.is_connected()
    return JSONResponse(
        status_code=200 if database_connected else 503,
        content={
            "status": "ok" if database_connected else "unhealthy",
            "database": "connected" if database_connected else "disconnected",
        },
    )


app.include_router(auth_router)
app.include_router(bots_router)
app.include_router(api_key_router)
app.include_router(documentation_router)
app.include_router(testing_router)
app.include_router(chats_router)
app.mount("/", socket_app)
