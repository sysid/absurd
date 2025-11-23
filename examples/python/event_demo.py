"""
Event-driven workflow example with Absurd.

This demonstrates:
- Waiting for events within a task
- Emitting events from outside a task
- Event payloads and race-free caching
- Multi-step workflow with external coordination

Scenario: Order fulfillment workflow that waits for external events
"""

from absurd_sdk import Absurd
import os
import sys

# Get database URL from environment
db_url = os.environ.get("ABSURD_DATABASE_URL") or os.environ.get("PGDATABASE")
if not db_url:
    print("Error: Set ABSURD_DATABASE_URL or PGDATABASE environment variable")
    sys.exit(1)

# Create Absurd client (SDK now handles autocommit automatically)
app = Absurd(conn_or_url=db_url, queue_name="examples")


@app.register_task("order-fulfillment")
def order_fulfillment(params, ctx):
    """
    An order fulfillment workflow that coordinates with external systems via events.

    Flow:
    1. Reserve inventory
    2. Wait for payment confirmation event
    3. Ship the order
    4. Wait for delivery confirmation event
    5. Complete with final status
    """
    order_id = params["order_id"]
    print(f"📦 Starting order fulfillment for order: {order_id}")

    # Step 1: Reserve inventory
    inventory = ctx.step("reserve-inventory", lambda: {
        "items": params.get("items", []),
        "warehouse": "US-EAST-1",
        "reserved_at": "2025-01-01T10:00:00Z"
    })
    print(f"✓ Inventory reserved: {inventory}")

    # Step 2: Wait for payment confirmation
    # Events are race-free: if the event was already emitted, it's cached
    print(f"⏳ Waiting for payment confirmation: payment.confirmed:{order_id}")
    payment = ctx.await_event(f"payment.confirmed:{order_id}")
    print(f"✓ Payment confirmed: {payment}")

    # Step 3: Ship the order
    shipment = ctx.step("ship-order", lambda: {
        "tracking_number": f"1Z999{order_id}",
        "carrier": "UPS",
        "shipped_at": "2025-01-01T12:00:00Z"
    })
    print(f"✓ Order shipped: {shipment}")

    # Step 4: Wait for delivery confirmation
    print(f"⏳ Waiting for delivery: shipment.delivered:{order_id}")
    delivery = ctx.await_event(f"shipment.delivered:{order_id}")
    print(f"✓ Delivery confirmed: {delivery}")

    # Step 5: Complete
    result = ctx.step("finalize", lambda: {
        "status": "completed",
        "order_id": order_id,
        "total_amount": payment.get("amount", 0),
        "delivered_at": delivery.get("timestamp")
    })

    print(f"🎉 Order {order_id} completed successfully!")
    return result


if __name__ == "__main__":
    print("Absurd Event-Driven Workflow Demo")
    print("=" * 70)

    # Ensure queue exists
    try:
        app.create_queue("examples")
        print("✓ Queue 'examples' ready")
    except Exception as e:
        print(f"Note: {e}")

    print()
    print("This demo shows an order fulfillment workflow that waits for events.")
    print()

    # Check command line argument to either spawn or run worker
    if len(sys.argv) > 1 and sys.argv[1] == "spawn":
        # Spawn a new order task
        order_id = sys.argv[2] if len(sys.argv) > 2 else "12345"

        spawned = app.spawn("order-fulfillment", {
            "order_id": order_id,
            "items": ["widget-A", "gadget-B"],
            "customer": "tom@example.com"
        })

        print(f"✓ Spawned order fulfillment task:")
        print(f"  Order ID: {order_id}")
        print(f"  Task ID: {spawned['task_id']}")
        print(f"  Run ID: {spawned['run_id']}")
        print()
        print("Next steps:")
        print(f"  1. Start the worker:    uv run python python/event_demo.py")
        print(f"  2. Send payment event:  uv run python python/send_event.py payment.confirmed:{order_id} --payload '{{\"amount\": 99.99, \"method\": \"credit_card\"}}'")
        print(f"  3. Send delivery event: uv run python python/send_event.py shipment.delivered:{order_id} --payload '{{\"timestamp\": \"2025-01-02T15:00:00Z\"}}'")

    else:
        # Run worker
        print("Starting worker to process orders...")
        print("The worker will pause when waiting for events.")
        print()
        print("To send events, run in another terminal:")
        print("  uv run python python/send_event.py <event-name> --payload '<json>'")
        print()
        print("-" * 70)

        try:
            app.start_worker(worker_id="order-worker")
        except KeyboardInterrupt:
            print("\n" + "=" * 70)
            print("Worker stopped")
            app.close()
