#!/usr/bin/env python
"""Quick test to verify database connection and absurd-sdk installation."""

import os
import psycopg
from absurd_sdk import Absurd

db_url = os.environ.get("ABSURD_DATABASE_URL", "postgresql://absurd:absurd@localhost:5432/absurd")

print(f"Connecting to: {db_url}")

try:
    conn = psycopg.connect(db_url, autocommit=True)
    client = Absurd(conn_or_url=conn, queue_name="test")
    print("✓ Connected successfully!")

    queues = client.list_queues()
    print(f"✓ Existing queues: {queues}")

    print("✓ Everything works!")
    client.close()
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
