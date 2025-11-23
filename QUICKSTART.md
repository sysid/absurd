# Quick Start Guide

This guide will get you up and running with Absurd in 5 minutes.

## 1. Start PostgreSQL

```bash
# Start the database
docker-compose up -d

# Verify it's running
docker-compose ps
```

The database will be automatically initialized with the Absurd schema.

## 2. Set Environment Variables

```bash
export ABSURD_DATABASE_URL="postgresql://absurd:absurd@localhost:5432/absurd"
```

Or copy the example file:
```bash
cp .env.example .env
source .env
```

## 3. Install Dependencies

```bash
cd examples
uv sync
```

This creates a virtual environment and installs the Absurd Python SDK in editable mode.

## 4. Run the Example

```bash
# From the examples directory
uv run python python/simple_task.py
```

Expected output:
```
Absurd Python Example
==================================================
✓ Queue 'examples' created (or already exists)
✓ Spawned task: 01935bf7-8a25-7c92-b123-456789abcdef
  Run ID: 01935bf7-8a26-7c92-b456-789abcdef012
  Attempt: 1

Starting worker... (Press Ctrl+C to stop)
--------------------------------------------------
Task started with params: {'name': 'Tom'}
Step 1 completed: {'message': 'Hello, Tom!', 'timestamp': '2025-01-01T00:00:00Z'}
Step 2 completed: {'greeting': 'HELLO, TOM!', 'length': 11}
Step 3 completed: {'status': 'success', 'greeting': 'HELLO, TOM!', 'char_count': 11}
```

## 5. View Tasks in Habitat (Optional)

Build and run the web UI:

```bash
cd habitat
make build
./bin/habitat run
```

Open http://localhost:8080 in your browser to see your tasks.

## Database Connection Details

- **Host**: localhost:5432
- **Database**: absurd
- **User**: absurd
- **Password**: absurd
- **Connection String**: `postgresql://absurd:absurd@localhost:5432/absurd`

## Useful Commands

```bash
# Stop database
docker-compose down

# Stop and remove all data
docker-compose down -v

# View logs
docker-compose logs -f

# Access PostgreSQL directly
docker exec -it absurd-postgres psql -U absurd -d absurd
```

## Next Steps

- Read the [Python examples README](examples/python/README.md) for more details
- Check out [AGENTS.md](AGENTS.md) for architecture information
- See [README.md](README.md) for comprehensive documentation
