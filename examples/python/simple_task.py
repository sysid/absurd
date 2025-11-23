"""
Simple Absurd task example in Python.

This demonstrates:
- Registering a task with checkpointed steps
- Spawning a task
- Running a worker to process tasks
"""

from absurd_sdk import Absurd
import os
import sys
import psycopg

# Get database URL from environment
db_url = os.environ.get("ABSURD_DATABASE_URL") or os.environ.get("PGDATABASE")
if not db_url:
    print("Error: Set ABSURD_DATABASE_URL or PGDATABASE environment variable")
    sys.exit(1)

# Create connection with autocommit enabled (required for psycopg3)
conn = psycopg.connect(db_url, autocommit=True)

# Create Absurd client with the connection
app = Absurd(conn_or_url=conn, queue_name="examples")


@app.register_task("hello-python")
def hello_python(params, ctx):
    """A simple task with checkpointed steps."""
    print(f"Task started with params: {params}")

    # Step 1: Process input (this step is checkpointed)
    result1 = ctx.step("process-input", lambda: {
        "message": f"Hello, {params.get('name', 'World')}!",
        "timestamp": "2025-01-01T00:00:00Z"
    })
    print(f"Step 1 completed: {result1}")

    # Step 2: Transform data (this step is also checkpointed)
    result2 = ctx.step("transform-data", lambda: {
        "greeting": result1["message"].upper(),
        "length": len(result1["message"])
    })
    print(f"Step 2 completed: {result2}")

    # Step 3: Final result
    final = ctx.step("finalize", lambda: {
        "status": "success",
        "greeting": result2["greeting"],
        "char_count": result2["length"]
    })
    print(f"Step 3 completed: {final}")

    return final


if __name__ == "__main__":
    print("Absurd Python Example")
    print("=" * 50)

    # Ensure queue exists
    try:
        app.create_queue("examples")
        print("✓ Queue 'examples' created (or already exists)")
    except Exception as e:
        print(f"Note: Queue creation failed (might already exist): {e}")

    # Spawn a task
    spawned = app.spawn("hello-python", {"name": "Tom"})
    print(f"✓ Spawned task: {spawned['task_id']}")
    print(f"  Run ID: {spawned['run_id']}")
    print(f"  Attempt: {spawned['attempt']}")
    print()

    # Start worker to process tasks
    print("Starting worker... (Press Ctrl+C to stop)")
    print("-" * 50)
    try:
        app.start_worker(worker_id="python-example-worker")
    except KeyboardInterrupt:
        print("\n" + "=" * 50)
        print("Worker stopped")
        app.close()
