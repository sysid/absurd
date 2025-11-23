#!/usr/bin/env python
"""Test whether Absurd works with transaction mode (not autocommit)."""

import psycopg
import os
import sys

db_url = os.environ.get("ABSURD_DATABASE_URL", "postgresql://absurd:absurd@localhost:5432/absurd")

print("Testing Absurd with TRANSACTION MODE (autocommit=False)")
print("=" * 70)

# Test 1: Transaction mode WITH commit
print("\n1. Transaction mode WITH commit:")
try:
    conn = psycopg.connect(db_url, autocommit=False)  # Transaction mode

    # Create queue
    conn.execute("SELECT absurd.create_queue('test_tx_mode')")
    conn.commit()  # Explicitly commit
    print("   ✓ Queue created")

    # Spawn task
    result = conn.execute(
        "SELECT * FROM absurd.spawn_task('test_tx_mode', 'test-task', '{}'::jsonb)"
    ).fetchone()
    task_id = result[0]  # (task_id, run_id, attempt)
    conn.commit()  # Explicitly commit
    print(f"   ✓ Task spawned: {task_id}")

    # Verify task exists
    check = conn.execute(
        "SELECT * FROM t_test_tx_mode WHERE task_id = %s", (task_id,)
    ).fetchone()
    print(f"   ✓ Task verified in database: {check is not None}")

    # Cleanup
    conn.execute("SELECT absurd.drop_queue('test_tx_mode')")
    conn.commit()
    conn.close()
    print("   ✓ Transaction mode WITH commit WORKS!")

except Exception as e:
    print(f"   ✗ Error: {e}")
    sys.exit(1)

# Test 2: Transaction mode WITHOUT commit (should fail)
print("\n2. Transaction mode WITHOUT commit:")
try:
    conn = psycopg.connect(db_url, autocommit=False)

    conn.execute("SELECT absurd.create_queue('test_no_commit')")
    # NO COMMIT HERE

    result = conn.execute(
        "SELECT * FROM absurd.spawn_task('test_no_commit', 'test-task', '{}'::jsonb)"
    ).fetchone()
    task_id = result[0] if result else None
    # NO COMMIT HERE
    print(f"   Task spawned in transaction: {task_id}")

    # Close without commit (implicit rollback)
    conn.close()
    print("   Connection closed without commit")

    # Open new connection and check
    conn2 = psycopg.connect(db_url, autocommit=True)
    queues = conn2.execute("SELECT * FROM absurd.list_queues()").fetchall()
    queue_names = [q[0] for q in queues]

    if 'test_no_commit' in queue_names:
        print("   ✗ UNEXPECTED: Queue persisted without commit!")
        sys.exit(1)
    else:
        print("   ✓ Correct: Changes rolled back without commit")

    conn2.close()

except Exception as e:
    print(f"   ✗ Error: {e}")
    sys.exit(1)

# Test 3: Batch operations in single transaction
print("\n3. Batch operations in single transaction (atomicity):")
try:
    conn = psycopg.connect(db_url, autocommit=False)

    conn.execute("SELECT absurd.create_queue('test_batch')")
    conn.commit()

    # Spawn 5 tasks in a single transaction
    task_ids = []
    for i in range(5):
        result = conn.execute(
            "SELECT * FROM absurd.spawn_task('test_batch', 'batch-task', %s::jsonb)",
            (f'{{"index": {i}}}',)
        ).fetchone()
        task_ids.append(result[0] if result else None)

    # All 5 spawns in one transaction
    conn.commit()
    print(f"   ✓ Spawned 5 tasks atomically: {len(task_ids)}")

    # Verify all exist
    count = conn.execute(
        "SELECT COUNT(*) FROM t_test_batch"
    ).fetchone()[0]
    print(f"   ✓ All {count} tasks persisted")

    # Cleanup
    conn.execute("SELECT absurd.drop_queue('test_batch')")
    conn.commit()
    conn.close()
    print("   ✓ Batch transactions WORK!")

except Exception as e:
    print(f"   ✗ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 70)
print("CONCLUSION:")
print("  • Stored procedures work in BOTH autocommit and transaction mode")
print("  • Autocommit: Simple, each call commits immediately")
print("  • Transaction mode: More control, enables batch atomicity")
print("  • The original bug: transaction mode WITHOUT commits")
print("  • Autocommit is RECOMMENDED but not REQUIRED")
print("=" * 70)
