from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field
from src.db import prisma
from src.utils.password import hash_password, verify_password
from src.utils.jwt import create_access_token
from secrets import token_urlsafe


# A fixed identity keeps demo access separate from all registered accounts.
DEMO_USER_ID = "f64a43a4-3712-4eab-9a52-4ea4c63cbe15"
DEMO_USER_EMAIL = "demo@retrieva.invalid"

router = APIRouter(
    prefix="/auth",
    tags=["Auth"]
)

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/demo")
async def demo_login():
    user = await prisma.user.find_unique(where={"id": DEMO_USER_ID})
    if user is None:
        user = await prisma.user.upsert(
            where={"id": DEMO_USER_ID},
            data={
                "create": {
                    "id": DEMO_USER_ID,
                    "email": DEMO_USER_EMAIL,
                    "name": "Demo visitor",
                    "passwordHash": hash_password(token_urlsafe(32)),
                    "role": "user",
                },
                "update": {},
            },
        )

    # Fail closed if the reserved identity was repurposed by an operator.
    if user.email != DEMO_USER_EMAIL or user.role != "user":
        raise HTTPException(status_code=503, detail="Demo access is temporarily unavailable")

    return {"access_token": create_access_token({"sub": user.id})}


@router.post("/register")
async def register(data: RegisterRequest) -> dict:
    try:
        existing=await prisma.user.find_unique(where={"email":data.email});
        if existing :
            raise HTTPException(status_code= 400 , detail="user with this email already exists");
        
        user = await prisma.user.create(
            data={
                "email": data.email,
                "passwordHash": hash_password(data.password),
                "name": data.name,
            }
        )
        return {"message": "User registered successfully", "user": user}

    except Exception as e:
        raise HTTPException(status_code= 500 , detail=str(e))

    

@router.post("/login")
async def login(data: LoginRequest):
    user = await prisma.user.find_unique(
        where={"email": data.email}
    )
    if not user or not verify_password(data.password, user.passwordHash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.id})

    return {"access_token": token}
