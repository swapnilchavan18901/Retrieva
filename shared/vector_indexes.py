"""Payload indexes required for document/revision filters on Qdrant Cloud."""
from qdrant_client.models import PayloadSchemaType


def ensure_revision_indexes(client, collection_name):
    schema = client.get_collection(collection_name).payload_schema
    for field in ("doc_id", "user_id", "revisions"):
        if field not in schema:
            client.create_payload_index(collection_name, field_name=field,
                                        field_schema=PayloadSchemaType.KEYWORD, wait=True)
