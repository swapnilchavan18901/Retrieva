from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from starlette.concurrency import run_in_threadpool
from generated.prisma.errors import UniqueViolationError
from src.db import prisma
from src.utils.password import DUMMY_PASSWORD_HASH, MAX_PASSWORD_BYTES, hash_password, verify_password
from src.utils.jwt import create_access_token
from secrets import token_urlsafe
from typing import Final


DEMO_ENABLED: Final[bool] = True

# A fixed identity keeps demo access separate from all registered accounts.
DEMO_USER_ID = "f64a43a4-3712-4eab-9a52-4ea4c63cbe15"
DEMO_USER_EMAIL = "demo@retrieva.invalid"

router = APIRouter(
    prefix="/auth",
    tags=["Auth"]
)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value):
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must fit within {MAX_PASSWORD_BYTES} UTF-8 bytes")
        return value


class RegisterRequest(LoginRequest):
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)
    name: str | None = Field(default=None, max_length=100)


class PublicUser(BaseModel):
    id: str
    email: str
    name: str | None = None


class RegisterResponse(BaseModel):
    message: str
    user: PublicUser


@router.post("/demo")
async def demo_login():
    if not DEMO_ENABLED:
        raise HTTPException(status_code=404, detail="Demo access is disabled")
    user = await prisma.user.find_unique(where={"id": DEMO_USER_ID})
    if user is None:
        user = await prisma.user.upsert(
            where={"id": DEMO_USER_ID},
            data={
                "create": {
                    "id": DEMO_USER_ID,
                    "email": DEMO_USER_EMAIL,
                    "name": "Demo visitor",
                    "passwordHash": await run_in_threadpool(hash_password, token_urlsafe(32)),
                    "role": "user",
                },
                "update": {},
            },
        )

    # Fail closed if the reserved identity was repurposed by an operator.
    if user.email != DEMO_USER_EMAIL or user.role != "user":
        raise HTTPException(status_code=503, detail="Demo access is temporarily unavailable")

    return {"access_token": create_access_token({"sub": user.id})}


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(data: RegisterRequest) -> dict:
    existing = await prisma.user.find_unique(where={"email": data.email})
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    password_hash = await run_in_threadpool(hash_password, data.password)
    try:
        user = await prisma.user.create(
            data={
                "email": data.email,
                "passwordHash": password_hash,
                "name": data.name,
            }
        )
    except UniqueViolationError:
        # Concurrent registrations can both pass the initial lookup.
        raise HTTPException(status_code=409, detail="User with this email already exists") from None
    return {
        "message": "User registered successfully",
        "user": {"id": user.id, "email": user.email, "name": user.name},
    }

    

@router.post("/login")
async def login(data: LoginRequest):
    user = await prisma.user.find_unique(
        where={"email": data.email}
    )
    valid = await run_in_threadpool(
        verify_password, data.password, user.passwordHash if user else DUMMY_PASSWORD_HASH,
    )
    if not user or not valid:
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Bearer"})

    token = create_access_token({"sub": user.id})

    return {"access_token": token}
