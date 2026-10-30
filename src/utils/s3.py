import boto3
from src.env import env
from botocore.config import Config

s3 = boto3.client(
    "s3",
    region_name=env.AWS_REGION,
    aws_access_key_id=env.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=env.AWS_SECRET_ACCESS_KEY,
    aws_session_token=env.AWS_SESSION_TOKEN,
    endpoint_url=f"https://s3.{env.AWS_REGION}.amazonaws.com",
    config=Config(signature_version="s3v4"),
)
BUCKET = env.AWS_S3_BUCKET

def generate_presigned_upload_url(s3_key: str, expires_in: int = 300):
    """
    Generate a presigned URL for uploading files to S3.
    IMPORTANT: The client MUST include 'Content-Type: application/pdf' header when uploading!
    """
    print(f"s3regionishere: {env.AWS_REGION}")
    print(f"s3_keyishere: {s3_key}")
    print(f"BUCKETishere: {BUCKET}")
    print(f"expires_inishere: {expires_in}")
    
    return s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": BUCKET,
            "Key": s3_key,
            "ContentType": "application/pdf",  # Enforce PDF content type
        },
        ExpiresIn=expires_in,
    )

