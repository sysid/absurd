# Python Examples for Absurd

This directory contains Python examples demonstrating how to use the Absurd SDK.

## Quick Start with Docker Compose

### 1. Start PostgreSQL

From the repository root:

```bash
# Start PostgreSQL in the background
docker-compose up -d

# Check it's running
docker-compose ps
```

The database will be automatically initialized with the Absurd schema from `sql/absurd.sql`.

### 2. Set Environment Variables

```bash
# Copy the example environment file
cp .env.example .env

# Or export directly
export ABSURD_DATABASE_URL="postgresql://absurd:absurd@localhost:5432/absurd"
```

### 3. Install Dependencies

```bash
# From the repository root, go to examples directory
cd examples

# Sync dependencies (creates .venv and installs absurd-sdk)
uv sync
```

### 4. Run the Example

```bash
# From the examples directory
uv run python python/simple_task.py
```

You should see output like:

```
Absurd Python Example
==================================================
✓ Queue 'examples' created (or already exists)
✓ Spawned task: 01234567-89ab-cdef-0123-456789abcdef
  Run ID: 01234567-89ab-cdef-0123-456789abcdef
  Attempt: 1

Starting worker... (Press Ctrl+C to stop)
--------------------------------------------------
Task started with params: {'name': 'Tom'}
Step 1 completed: {'message': 'Hello, Tom!', 'timestamp': '2025-01-01T00:00:00Z'}
Step 2 completed: {'greeting': 'HELLO, TOM!', 'length': 11}
Step 3 completed: {'status': 'success', 'greeting': 'HELLO, TOM!', 'char_count': 11}
```

Press Ctrl+C to stop the worker.

## Using absurdctl

You can also use `absurdctl` to spawn tasks separately:

```bash
# Spawn a task without starting a worker (from repo root)
./absurdctl spawn-task --queue examples hello-python -P name=Tom

# In another terminal, run just the worker (from examples directory)
cd examples
uv run python python/simple_task.py
```

## Event-Driven Workflows

Absurd supports event-driven workflows where tasks can wait for external events before continuing. This is perfect for coordinating with external systems, webhooks, or human approvals.

### Key Concepts

- **Events are race-free**: If an event is emitted before a task awaits it, the event is cached
- **Events are queue-scoped**: Events only wake tasks in the same queue
- **Event names can include identifiers**: Use patterns like `event:id` for per-task uniqueness
- **Events carry payloads**: Send JSON data with events

### Running the Event Demo

The `event_demo.py` example shows an order fulfillment workflow that waits for two external events.

#### Terminal 1: Spawn an order task

```bash
cd examples
uv run python python/event_demo.py spawn order-12345
```

Output:
```
✓ Spawned order fulfillment task:
  Order ID: order-12345
  Task ID: 01935bf7-8a25-7c92-b123-456789abcdef
```

#### Terminal 2: Start the worker

```bash
cd examples
uv run python python/event_demo.py
```

You'll see:
```
📦 Starting order fulfillment for order: order-12345
✓ Inventory reserved: {...}
⏳ Waiting for payment confirmation: payment.confirmed:order-12345
```

The task is now **suspended**, waiting for the payment event.

#### Terminal 3: Send the payment event

```bash
cd examples
uv run python python/send_event.py payment.confirmed:order-12345 \
  --payload '{"amount": 99.99, "method": "credit_card"}'
```

Output:
```
Emitting event to queue 'examples':
  Event name: payment.confirmed:order-12345
  Payload: {"amount": 99.99, "method": "credit_card"}
✓ Event emitted successfully!
```

Back in Terminal 2, the task will resume:
```
✓ Payment confirmed: {'amount': 99.99, 'method': 'credit_card'}
✓ Order shipped: {...}
⏳ Waiting for delivery: shipment.delivered:order-12345
```

#### Send the delivery event

```bash
cd examples
uv run python python/send_event.py shipment.delivered:order-12345 \
  --payload '{"timestamp": "2025-01-02T15:00:00Z", "location": "Customer doorstep"}'
```

The task completes:
```
✓ Delivery confirmed: {'timestamp': '2025-01-02T15:00:00Z', ...}
🎉 Order order-12345 completed successfully!
```

### Using Events in Your Code

```python
from absurd_sdk import Absurd

app = Absurd(conn_or_url=db_url, queue_name="myqueue")

@app.register_task("approval-workflow")
def approval_workflow(params, ctx):
    # Do some work
    doc = ctx.step("create-document", lambda: {"id": params["doc_id"]})

    # Wait for approval event (task suspends here)
    approval = ctx.await_event(f"document.approved:{doc['id']}")

    # Continue after event received
    ctx.step("finalize", lambda: {"status": "approved", **approval})

    return {"completed": True}

# Emit events from anywhere
app.emit_event("document.approved:doc-123", {"approver": "alice"})
```

### Event Patterns

**Per-task events**: Include a unique ID in the event name
```python
ctx.await_event(f"payment.confirmed:{order_id}")
```

**Broadcast events**: Use the same name for all tasks
```python
ctx.await_event("daily.report.ready")
```

**Event timeouts**: Events can timeout and raise `TimeoutError`
```python
try:
    result = ctx.await_event("user.responded", timeout=3600)  # 1 hour
except TimeoutError:
    # Handle timeout
    ctx.step("send-reminder", lambda: {...})
```

## Viewing Tasks in Habitat

Start the Habitat web UI to monitor your tasks:

```bash
cd habitat
make build
./bin/habitat run
```

Then open http://localhost:8080 in your browser.

## Cleanup

```bash
# Stop PostgreSQL
docker-compose down

# Remove data volumes (⚠️ this deletes all data)
docker-compose down -v
```

## Database Connection String Format

The connection string format is:

```
postgresql://[user]:[password]@[host]:[port]/[database]
```

Default values from docker-compose:
- User: `absurd`
- Password: `absurd`
- Host: `localhost`
- Port: `5432`
- Database: `absurd`

## Transaction Modes

The Python SDK supports both **autocommit** and **transaction mode**. Autocommit is the recommended default.

### Autocommit Mode (Default)

The SDK automatically uses `autocommit=True` when creating connections from connection strings:

```python
from absurd_sdk import Absurd

# Connection string - autocommit enabled automatically
client = Absurd("postgresql://...", queue_name="myqueue")

# Each operation commits immediately
client.spawn("task-1", {})  # Commits
client.spawn("task-2", {})  # Commits separately
```

**Benefits**: Simple, works immediately, no forgotten commits

### Transaction Mode (Advanced)

For batch atomic operations, pass your own connection with `autocommit=False`:

```python
import psycopg
from absurd_sdk import Absurd

# Transaction mode - you control commits
with psycopg.connect(dsn, autocommit=False) as conn:
    client = Absurd(conn, queue_name="myqueue")

    # Spawn multiple tasks in single transaction
    client.spawn("task-1", {"priority": "high"})
    client.spawn("task-2", {"priority": "high"})
    client.spawn("task-3", {"priority": "high"})

    conn.commit()  # All 3 tasks spawn atomically (all-or-nothing)
```

**Benefits**: Batch atomicity, more control over transaction boundaries

**Trade-off**: Must remember to commit, or changes will roll back

### Which Mode to Use?

- **Use autocommit (default)**: For most use cases, single operations
- **Use transaction mode**: When you need batch atomic operations (all-or-nothing semantics)
