# Event-Driven Workflow Demo

This guide walks through the event-driven workflow example, demonstrating how Absurd handles tasks that coordinate with external systems through events.

## Overview

The event demo (`event_demo.py`) implements an **order fulfillment workflow** that:
1. Reserves inventory (checkpointed step)
2. ⏳ **Waits for payment confirmation** (external event)
3. Ships the order (checkpointed step)
4. ⏳ **Waits for delivery confirmation** (external event)
5. Completes with final status

This pattern is perfect for:
- Payment processing workflows
- Approval workflows (human-in-the-loop)
- Multi-system coordination (webhooks, external APIs)
- Long-running processes that span days or weeks

## Files

### `event_demo.py`
The main workflow implementation with:
- Task registration (`@app.register_task`)
- Event waiting (`ctx.await_event()`)
- Checkpointed steps (`ctx.step()`)
- Dual mode: spawn tasks or run worker

### `send_event.py`
A helper script to emit events from the command line:
```bash
python send_event.py <event-name> --payload '<json>' --queue <queue-name>
```

## Quick Start

You'll need **3 terminal windows** to see the full workflow in action.

### Prerequisites

Make sure you have:
1. PostgreSQL running (via `docker-compose up -d` from repo root)
2. Dependencies installed (`uv sync` in examples directory)
3. Environment variable set:
   ```bash
   export ABSURD_DATABASE_URL="postgresql://absurd:absurd@localhost:5432/absurd"
   ```

---

## Step-by-Step Demo

### Terminal 1: Spawn an Order Task

```bash
cd examples
uv run python python/event_demo.py spawn order-12345
```

**Output:**
```
Absurd Event-Driven Workflow Demo
======================================================================
✓ Queue 'examples' ready

This demo shows an order fulfillment workflow that waits for events.

✓ Spawned order fulfillment task:
  Order ID: order-12345
  Task ID: 01935bf7-8a25-7c92-b123-456789abcdef
  Run ID: 01935bf7-8a26-7c92-b456-789abcdef012

Next steps:
  1. Start the worker:    uv run python python/event_demo.py
  2. Send payment event:  uv run python python/send_event.py payment.confirmed:order-12345 --payload '{"amount": 99.99, "method": "credit_card"}'
  3. Send delivery event: uv run python python/send_event.py shipment.delivered:order-12345 --payload '{"timestamp": "2025-01-02T15:00:00Z"}'
```

The task is now in the queue, ready to be processed.

---

### Terminal 2: Start the Worker

```bash
cd examples
uv run python python/event_demo.py
```

**Output:**
```
Absurd Event-Driven Workflow Demo
======================================================================
✓ Queue 'examples' ready

Starting worker to process orders...
The worker will pause when waiting for events.

To send events, run in another terminal:
  uv run python python/send_event.py <event-name> --payload '<json>'

----------------------------------------------------------------------
📦 Starting order fulfillment for order: order-12345
✓ Inventory reserved: {'items': ['widget-A', 'gadget-B'], 'warehouse': 'US-EAST-1', 'reserved_at': '2025-01-01T10:00:00Z'}
⏳ Waiting for payment confirmation: payment.confirmed:order-12345
```

**The worker is now suspended**, waiting for the payment event. The task state is saved in the database, and the worker can pick up other tasks if available.

---

### Terminal 3: Send Payment Event

```bash
cd examples
uv run python python/send_event.py payment.confirmed:order-12345 \
  --payload '{"amount": 99.99, "method": "credit_card"}'
```

**Output:**
```
Emitting event to queue 'examples':
  Event name: payment.confirmed:order-12345
  Payload: {
  "amount": 99.99,
  "method": "credit_card"
}
✓ Event emitted successfully!

If a task was waiting for this event, it will now resume.
```

**Back in Terminal 2**, the worker immediately resumes:
```
✓ Payment confirmed: {'amount': 99.99, 'method': 'credit_card'}
✓ Order shipped: {'tracking_number': '1Z999order-12345', 'carrier': 'UPS', 'shipped_at': '2025-01-01T12:00:00Z'}
⏳ Waiting for delivery: shipment.delivered:order-12345
```

Again, the worker is suspended, waiting for the delivery event.

---

### Terminal 3: Send Delivery Event

```bash
cd examples
uv run python python/send_event.py shipment.delivered:order-12345 \
  --payload '{"timestamp": "2025-01-02T15:00:00Z", "location": "Customer doorstep"}'
```

**Output:**
```
Emitting event to queue 'examples':
  Event name: shipment.delivered:order-12345
  Payload: {
  "timestamp": "2025-01-02T15:00:00Z",
  "location": "Customer doorstep"
}
✓ Event emitted successfully!
```

**Back in Terminal 2**, the workflow completes:
```
✓ Delivery confirmed: {'timestamp': '2025-01-02T15:00:00Z', 'location': 'Customer doorstep'}
🎉 Order order-12345 completed successfully!
```

---

## Key Features Demonstrated

### 1. Race-Free Events
Events are cached if emitted before `await_event()` is called. This means:
- You can emit events in any order
- No race conditions between event emission and task execution
- Events are stored until claimed by a waiting task

### 2. Event Payloads
Events can carry JSON data:
```python
# In the task
payment = ctx.await_event(f"payment.confirmed:{order_id}")
# payment = {'amount': 99.99, 'method': 'credit_card'}
```

### 3. Per-Task Event Names
Use identifiers in event names for per-task uniqueness:
```python
ctx.await_event(f"payment.confirmed:{order_id}")  # Specific to this order
```

This prevents one task from consuming another task's event.

### 4. Task Suspension
When a task waits for an event:
- Task state is persisted to the database
- Worker is freed to process other tasks
- When the event arrives, the task resumes automatically
- All previous step results are restored from checkpoints

### 5. Event Timeouts (Optional)
```python
try:
    result = ctx.await_event("user.responded", timeout=3600)  # 1 hour
except TimeoutError:
    # Handle timeout case
    ctx.step("send-reminder", lambda: send_reminder_email())
```

---

## Code Patterns

### Waiting for Events in Tasks

```python
@app.register_task("approval-workflow")
def approval_workflow(params, ctx):
    # Do some work
    doc = ctx.step("create-document", lambda: {"id": params["doc_id"]})

    # Wait for approval (task suspends here)
    approval = ctx.await_event(f"document.approved:{doc['id']}")

    # Continue after event received
    result = ctx.step("finalize", lambda: {
        "status": "approved",
        "approver": approval.get("approver"),
        "timestamp": approval.get("timestamp")
    })

    return result
```

### Emitting Events from Code

```python
# From within a task
ctx.emit_event("order.shipped", {"tracking": "1Z999"})

# From outside a task
app = Absurd(conn_or_url=db_url, queue_name="orders")
app.emit_event("order.shipped", {"tracking": "1Z999"})
```

### Emitting Events from CLI

```bash
# Simple event
python send_event.py order.completed

# Event with payload
python send_event.py payment.received:order-123 \
  --payload '{"amount": 99.99, "currency": "USD"}'

# Event to specific queue
python send_event.py daily.report.ready \
  --queue reports \
  --payload '{"date": "2025-01-01"}'
```

---

## Event Patterns

### Pattern 1: Per-Task Events
Include a unique identifier in the event name:
```python
order_id = params["order_id"]
ctx.await_event(f"payment.confirmed:{order_id}")
```

### Pattern 2: Broadcast Events
Use the same event name for all tasks:
```python
# Multiple tasks can wait for the same event
ctx.await_event("daily.report.ready")
```

### Pattern 3: Multi-Stage Workflows
Chain multiple events together:
```python
# Stage 1
ctx.await_event(f"stage1.complete:{workflow_id}")
ctx.step("process-stage1", lambda: {...})

# Stage 2
ctx.await_event(f"stage2.complete:{workflow_id}")
ctx.step("process-stage2", lambda: {...})
```

---

## Monitoring with Habitat

You can view suspended tasks in the Habitat web UI:

```bash
cd habitat
make build
./bin/habitat run
```

Open http://localhost:8080 to see:
- Tasks in "sleeping" state (waiting for events)
- Event wait registrations
- Task history and checkpoints

---

## Troubleshooting

### Task Not Resuming
- Check the event name matches exactly (case-sensitive)
- Verify you're emitting to the correct queue
- Check the worker is running
- Use Habitat to inspect the wait registrations table

### Events Lost
Events are **never lost**. They're cached in the database until claimed. If a task hasn't resumed:
- The event name might not match
- The task might not have reached the `await_event()` call yet
- The task might be waiting for a different event first

### Testing Events
Use `absurdctl` to inspect the events table:
```bash
./absurdctl -d "postgresql://absurd:absurd@localhost:5432/absurd" \
  list-events --queue examples
```

---

## Next Steps

Try modifying the demo:
1. Add a timeout to one of the events
2. Add a third event (e.g., "customer.feedback")
3. Implement a cancellation event pattern
4. Create a webhook endpoint that emits events

For more details, see the main [README.md](README.md).
