#!/usr/bin/env python
"""
Helper script to emit events to Absurd tasks.

Usage:
    python send_event.py <event-name> [--payload <json>] [--queue <name>]

Examples:
    # Simple event without payload
    python send_event.py order.completed

    # Event with JSON payload
    python send_event.py payment.confirmed:12345 --payload '{"amount": 99.99}'

    # Event to specific queue
    python send_event.py shipment.delivered:12345 --queue orders --payload '{"tracking": "1Z999"}'
"""

from absurd_sdk import Absurd
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Emit events to Absurd tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s order.completed
  %(prog)s payment.confirmed:12345 --payload '{"amount": 99.99}'
  %(prog)s shipment.delivered:12345 --queue orders --payload '{"tracking": "1Z999"}'
        """
    )

    parser.add_argument(
        "event_name",
        help="Name of the event to emit (can include identifiers like 'event:id')"
    )

    parser.add_argument(
        "--payload", "-p",
        help="JSON payload to send with the event",
        default=None
    )

    parser.add_argument(
        "--queue", "-q",
        help="Queue name (default: examples)",
        default="examples"
    )

    args = parser.parse_args()

    # Get database URL from environment
    db_url = os.environ.get("ABSURD_DATABASE_URL") or os.environ.get("PGDATABASE")
    if not db_url:
        print("Error: Set ABSURD_DATABASE_URL or PGDATABASE environment variable")
        sys.exit(1)

    # Parse payload if provided
    payload = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON payload: {e}")
            sys.exit(1)

    # Connect and emit event
    try:
        client = Absurd(conn_or_url=db_url, queue_name=args.queue)

        print(f"Emitting event to queue '{args.queue}':")
        print(f"  Event name: {args.event_name}")
        if payload:
            print(f"  Payload: {json.dumps(payload, indent=2)}")
        else:
            print(f"  Payload: (none)")

        client.emit_event(args.event_name, payload)

        print("✓ Event emitted successfully!")
        print()
        print("If a task was waiting for this event, it will now resume.")

        client.close()

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
