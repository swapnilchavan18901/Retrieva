"""
Utility script to clear all records from Qdrant collection.

Usage:
    python -m consumer.utils.clear_qdrant
"""

from consumer.vector.qdrantdb import qdrant_client
from consumer.vector.vector import ensure_collection
import asyncio

QDRANT_COLLECTION = "Retrieva"


def clear_collection():
    """Delete all points from the Qdrant collection"""
    try:
        print("=" * 60)
        print("🗑️  Qdrant Collection Cleanup")
        print("=" * 60)
        
        # Get collection info
        try:
            collection_info = qdrant_client.get_collection(QDRANT_COLLECTION)
            points_count = collection_info.points_count
            print(f"📊 Collection: {QDRANT_COLLECTION}")
            print(f"📈 Current points count: {points_count}")
        except Exception as e:
            print(f"⚠️  Could not get collection info: {e}")
            print(f"📊 Collection: {QDRANT_COLLECTION}")
            points_count = "unknown"
        
        # Ask for confirmation
        print("\n⚠️  WARNING: This will delete ALL records from the collection!")
        confirmation = input("Type 'YES' to confirm deletion: ")
        
        if confirmation != "YES":
            print("❌ Deletion cancelled.")
            return
        
        print(f"\n🔄 Deleting all points from '{QDRANT_COLLECTION}'...")
        
        # Delete the collection and recreate it
        try:
            qdrant_client.delete_collection(collection_name=QDRANT_COLLECTION)
            print(f"✅ Collection '{QDRANT_COLLECTION}' deleted")
        except Exception as e:
            print(f"⚠️  Error deleting collection: {e}")
        
        # Recreate the collection
        print(f"🔄 Recreating collection '{QDRANT_COLLECTION}'...")
        # asyncio.run(ensure_collection())
        
        # Verify it's empty
        collection_info = qdrant_client.get_collection(QDRANT_COLLECTION)
        new_count = collection_info.points_count
        
        print(f"✅ Collection recreated successfully")
        print(f"📊 New points count: {new_count}")
        print("\n" + "=" * 60)
        print("✨ Cleanup complete! Collection is now empty.")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Error during cleanup: {e}")
        import traceback
        traceback.print_exc()


def delete_by_bot_id(bot_id: str):
    """Delete all points for a specific bot_id"""
    try:
        print("=" * 60)
        print(f"🗑️  Deleting records for bot_id: {bot_id}")
        print("=" * 60)
        
        # Delete points with matching bot_id
        qdrant_client.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector={
                "filter": {
                    "must": [
                        {
                            "key": "bot_id",
                            "match": {
                                "value": bot_id
                            }
                        }
                    ]
                }
            }
        )
        
        print(f"✅ Deleted all records for bot_id: {bot_id}")
        print("=" * 60)
        
    except Exception as e:
        print(f"❌ Error deleting records: {e}")
        import traceback
        traceback.print_exc()


def delete_by_document_id(doc_id: str):
    """Delete all points for a specific document_id"""
    try:
        print("=" * 60)
        print(f"🗑️  Deleting records for document_id: {doc_id}")
        print("=" * 60)
        
        # Delete points with matching doc_id
        qdrant_client.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector={
                "filter": {
                    "must": [
                        {
                            "key": "doc_id",
                            "match": {
                                "value": doc_id
                            }
                        }
                    ]
                }
            }
        )
        
        print(f"✅ Deleted all records for document_id: {doc_id}")
        print("=" * 60)
        
    except Exception as e:
        print(f"❌ Error deleting records: {e}")
        import traceback
        traceback.print_exc()


def show_collection_stats():
    """Display collection statistics"""
    try:
        print("=" * 60)
        print("📊 Qdrant Collection Statistics")
        print("=" * 60)
        
        collection_info = qdrant_client.get_collection(QDRANT_COLLECTION)
        
        print(f"Collection Name: {QDRANT_COLLECTION}")
        print(f"Points Count: {collection_info.points_count}")
        print(f"Vectors Count: {collection_info.vectors_count}")
        print(f"Indexed Vectors: {collection_info.indexed_vectors_count}")
        print(f"Status: {collection_info.status}")
        
        print("=" * 60)
        
    except Exception as e:
        print(f"❌ Error getting collection stats: {e}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == "clear":
            clear_collection()
        elif command == "stats":
            show_collection_stats()
        elif command == "delete-bot" and len(sys.argv) > 2:
            bot_id = sys.argv[2]
            delete_by_bot_id(bot_id)
        elif command == "delete-doc" and len(sys.argv) > 2:
            doc_id = sys.argv[2]
            delete_by_document_id(doc_id)
        else:
            print("Usage:")
            print("  python -m consumer.utils.clear_qdrant clear          # Clear all records")
            print("  python -m consumer.utils.clear_qdrant stats          # Show collection stats")
            print("  python -m consumer.utils.clear_qdrant delete-bot <bot_id>")
            print("  python -m consumer.utils.clear_qdrant delete-doc <doc_id>")
    else:
        # Default: run clear_collection with confirmation
        clear_collection()
