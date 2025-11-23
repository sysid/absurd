# Absurd SQL Architecture Research

**Date**: 2025-11-23
**Author**: Claude (Research Agent)
**Context**: Comprehensive analysis of sql/absurd.sql requested by Tom

## Executive Summary

Absurd is a PostgreSQL-native durable workflow execution system that moves the complexity of
durable task execution into the database layer via stored procedures. The entire system is defined
in a single SQL file (`sql/absurd.sql`, 1338 lines) that creates stored procedures handling task
lifecycle, checkpointing, event coordination, and retry logic. Client SDKs are intentionally
lightweight, acting as thin wrappers that call these stored procedures.

**Core Principle**: Handle tasks that may run for minutes, days, or years without losing state by
subdividing them into checkpointed steps that can survive crashes, restarts, and network failures.

---

## 1. Architecture Overview

### 1.1 Database-Centric Design

All business logic lives in PostgreSQL stored procedures. The SQL file creates:
- **Queue management**: create_queue, drop_queue, list_queues
- **Task lifecycle**: spawn_task, claim_task, complete_run, fail_run, cancel_task
- **Checkpointing**: set_task_checkpoint_state, get_task_checkpoint_states
- **Event coordination**: emit_event, await_event
- **Cleanup**: cleanup_tasks, cleanup_events
- **Time control**: fake_now() for testing

### 1.2 Queue-Based Table Structure

Each queue creates 5 dynamically-named tables (using `format()` and `execute`):

```sql
-- Tasks table: t_{queue_name}
CREATE TABLE t_examples (
    id UUID PRIMARY KEY,
    task_name TEXT NOT NULL,
    params JSONB,
    queue_name TEXT NOT NULL,
    state TEXT NOT NULL,  -- pending, running, sleeping, completed, failed, cancelled
    created_at TIMESTAMPTZ DEFAULT now(),
    max_delay INTERVAL,
    max_duration INTERVAL,
    ...
)

-- Runs table: r_{queue_name}
CREATE TABLE r_examples (
    id UUID PRIMARY KEY,
    task_id UUID REFERENCES t_examples(id),
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL,  -- running, completed, failed, cancelled
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result JSONB,
    error JSONB,
    ...
)

-- Checkpoints table: c_{queue_name}
CREATE TABLE c_examples (
    run_id UUID REFERENCES r_examples(id),
    step_name TEXT NOT NULL,
    checkpoint_data JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (run_id, step_name)
)

-- Events table: e_{queue_name}
CREATE TABLE e_examples (
    id BIGSERIAL PRIMARY KEY,
    event_name TEXT NOT NULL,
    payload JSONB,
    emitted_at TIMESTAMPTZ DEFAULT now(),
    claimed_by_run_id UUID,
    claimed_at TIMESTAMPTZ
)

-- Wait registrations table: w_{queue_name}
CREATE TABLE w_examples (
    run_id UUID REFERENCES r_examples(id),
    event_name TEXT NOT NULL,
    registered_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (run_id, event_name)
)
```

**Rationale**: Per-queue tables enable logical isolation, independent cleanup policies, and
queue-specific scaling. The `w_` table enables efficient event matching without scanning all tasks.

---

## 2. Key Patterns and Implementation Details

### 2.1 UUIDv7 Generation

Absurd uses time-ordered UUIDs (UUIDv7) for task and run IDs:

```sql
CREATE OR REPLACE FUNCTION absurd.portable_uuidv7() RETURNS uuid AS $$
BEGIN
    -- Try native gen_uuid_v7() for Postgres 18+
    RETURN gen_uuid_v7();
EXCEPTION WHEN undefined_function THEN
    -- Fallback implementation for older versions
    -- Custom bit manipulation to create RFC-9562 UUIDv7
    ...
END;
$$ LANGUAGE plpgsql VOLATILE;
```

**Benefits**:
- Time-ordered: Natural sorting by creation time
- Index-friendly: Better B-tree performance than UUIDv4
- Distributed: No coordination needed across workers
- Compatible: Fallback for pre-Postgres 18

### 2.2 Claim-Based Task Execution

Workers claim tasks with a timeout-based leasing mechanism:

```sql
-- Key part of claim_task()
UPDATE {tasks_table}
SET
    state = 'running',
    claimed_at = absurd.fake_now(),
    claim_expires_at = absurd.fake_now() + claim_timeout,
    worker_id = worker_id_param
WHERE id = (
    SELECT id FROM {tasks_table}
    WHERE state = 'pending'
      AND (scheduled_at IS NULL OR scheduled_at <= absurd.fake_now())
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED  -- Critical for concurrency
)
RETURNING *;
```

**Concurrency Control**:
- `FOR UPDATE SKIP LOCKED`: Prevents lock contention; workers skip locked rows
- `claim_expires_at`: Automatic cleanup if worker crashes
- Claim extension: Each checkpoint write extends the claim timeout

**Overlap Handling**: If a worker crashes without completing, claim expiry allows another worker to
pick up the task. This can lead to **concurrent execution** of the same task. Users must design
tasks to make observable progress before claim expiry.

### 2.3 Checkpointed Steps

Tasks are subdivided into named steps whose results are cached:

```sql
-- set_task_checkpoint_state()
INSERT INTO {checkpoints_table} (run_id, step_name, checkpoint_data)
VALUES (run_id_param, step_name_param, checkpoint_data_param)
ON CONFLICT (run_id, step_name) DO UPDATE
    SET checkpoint_data = EXCLUDED.checkpoint_data;

-- Also extends claim
UPDATE {tasks_table}
SET claim_expires_at = absurd.fake_now() + extend_claim_by_param
WHERE id = task_id_param;
```

**Behavior**:
- First execution: Step runs, result stored
- Retry: Checkpoint loaded from database, step skipped
- **Exactly-once semantics** (per step): Step code executes once per successful run
- Code outside steps may execute multiple times

**SDK Integration Example** (TypeScript):
```typescript
// SDK wraps stored procedures
async step<T>(name: string, fn: () => Promise<T>): Promise<T> {
  // Check if checkpoint exists
  const checkpoints = await this.getCheckpoints();
  if (checkpoints[name]) {
    return checkpoints[name] as T;
  }

  // Execute and store checkpoint
  const result = await fn();
  await this.setCheckpoint(name, result);
  return result;
}

private async setCheckpoint(name: string, data: any): Promise<void> {
  await this.client.query(
    'SELECT absurd.set_task_checkpoint_state($1, $2, $3, $4, $5)',
    [this.queueName, this.runId, name, JSON.stringify(data), this.claimTimeout]
  );
}
```

### 2.4 Race-Free Event System

Events can be emitted before or after a task awaits them, with no race conditions:

```sql
-- emit_event(): Cache event in e_{queue}
INSERT INTO {events_table} (event_name, payload)
VALUES (event_name_param, payload_param)
RETURNING *;

-- Atomically wake sleeping tasks waiting for this event
UPDATE {tasks_table}
SET state = 'pending', scheduled_at = absurd.fake_now()
WHERE id IN (
    SELECT task_id FROM {runs_table} r
    JOIN {wait_registrations_table} w ON w.run_id = r.id
    WHERE w.event_name = event_name_param
      AND r.state = 'running'
)
```

**await_event() Logic**:
1. Check if matching event already exists in `e_{queue}` (unclaimed)
2. If found: Claim it, return payload
3. If not found: Register wait in `w_{queue}`, suspend task (state = 'sleeping')

**Timeout Handling**:
```sql
-- Tasks with expired wait timeouts are failed
UPDATE {tasks_table}
SET state = 'failed'
WHERE state = 'sleeping'
  AND wait_timeout_at < absurd.fake_now();
```

**SDK Integration Example** (Python):
```python
# Python SDK
def await_event(self, event_name: str, timeout: Optional[int] = None) -> Any:
    result = self._conn.execute(
        "SELECT absurd.await_event(%s, %s, %s, %s, %s)",
        (self._queue_name, self._run_id, event_name, timeout, self._task_id)
    ).fetchone()

    if result[0] is None:
        # Event not found - task will suspend (handled by stored procedure)
        raise _TaskSuspended(event_name)

    return result[0]  # Event payload
```

### 2.5 Retry Strategies

Tasks can define retry behavior via `retry_strategy` JSONB:

```sql
retry_strategy JSONB DEFAULT '{"type": "exponential", "max_attempts": 5}'::jsonb

-- Supported strategies:
-- 1. Exponential backoff (default)
{
  "type": "exponential",
  "initial_delay": 1000,  -- milliseconds
  "max_delay": 300000,    -- 5 minutes
  "multiplier": 2,
  "max_attempts": 5
}

-- 2. Fixed delay
{
  "type": "fixed",
  "delay": 5000,
  "max_attempts": 3
}

-- 3. No retries
{
  "type": "none"
}
```

**fail_run() Implementation**:
```sql
-- Calculate next attempt delay based on strategy
next_delay := absurd.calculate_retry_delay(retry_strategy, current_attempt);

IF next_delay IS NOT NULL THEN
    -- Schedule retry
    UPDATE {tasks_table}
    SET
        state = 'pending',
        scheduled_at = absurd.fake_now() + next_delay,
        claim_expires_at = NULL
    WHERE id = task_id_param;
ELSE
    -- Max attempts reached
    UPDATE {tasks_table}
    SET state = 'failed'
    WHERE id = task_id_param;
END IF;
```

### 2.6 Cancellation Mechanism

Tasks can be cancelled with a custom SQLSTATE error code:

```sql
-- cancel_task()
UPDATE {tasks_table}
SET
    state = 'cancelled',
    cancelled_at = absurd.fake_now()
WHERE id = task_id_param
  AND state NOT IN ('completed', 'cancelled');

-- If task is currently running, raise error to worker
IF (SELECT state FROM {runs_table} WHERE id = current_run_id) = 'running' THEN
    RAISE EXCEPTION 'Task cancelled' USING ERRCODE = 'AB001';
END IF;
```

**SDK Handling**: SDKs catch SQLSTATE `AB001` and treat it as graceful cancellation:

```typescript
// TypeScript SDK
try {
  await this.processTask(task);
} catch (err) {
  if (err.code === 'AB001') {
    // Task was cancelled - exit gracefully
    return;
  }
  throw err;
}
```

### 2.7 Time Control for Testing

Absurd uses a session-scoped variable `absurd.fake_now` to control time in tests:

```sql
CREATE OR REPLACE FUNCTION absurd.fake_now() RETURNS timestamptz AS $$
DECLARE
    fake_time TEXT;
BEGIN
    fake_time := current_setting('absurd.fake_now', true);
    IF fake_time IS NULL OR fake_time = '' THEN
        RETURN now();
    ELSE
        RETURN fake_time::timestamptz;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE;
```

**Test Usage** (from Python tests):
```python
class AbsurdTestClient:
    def set_time(self, t: datetime):
        """Set fake time for testing."""
        self.conn.execute(f"SET absurd.fake_now = '{t.isoformat()}'")

    def advance_time(self, delta: timedelta):
        """Advance fake time by delta."""
        current = self.get_current_time()
        self.set_time(current + delta)

# Test example
def test_claim_expiry(db):
    client = AbsurdTestClient(db)
    client.set_time(datetime(2025, 1, 1, 10, 0, 0))

    # Spawn task with 30-second claim timeout
    task_id = client.spawn_task("test-task", {}, claim_timeout=timedelta(seconds=30))

    # Claim task
    claimed = client.claim_task()
    assert claimed['id'] == task_id

    # Advance time past claim expiry
    client.advance_time(timedelta(seconds=31))

    # Task should be available again
    reclaimed = client.claim_task()
    assert reclaimed['id'] == task_id
    assert reclaimed['attempt'] == 2
```

---

## 3. Testing Strategy

### 3.1 Test Infrastructure

**Testcontainers + PostgreSQL 16**:
```python
# tests/conftest.py
@pytest.fixture(scope="session")
def postgres_container():
    """Start PostgreSQL 16 in Docker container."""
    container = PostgresContainer("postgres:16")
    container.start()

    # Apply schema
    conn = psycopg.connect(container.get_connection_url())
    with open("sql/absurd.sql") as f:
        conn.execute(f.read())

    yield container
    container.stop()

@pytest.fixture
def db(postgres_container):
    """Fresh database connection for each test."""
    conn = psycopg.connect(
        postgres_container.get_connection_url(),
        autocommit=True  # Recommended for simplicity
    )
    yield conn
    conn.close()
```

**AbsurdTestClient Helper**:
```python
class AbsurdTestClient:
    """Test helper wrapping Absurd SDK with time control."""

    def __init__(self, conn: Connection):
        self.conn = conn
        self.sdk = Absurd(conn_or_url=conn, queue_name="test")

    def set_time(self, t: datetime):
        self.conn.execute(f"SET absurd.fake_now = '{t.isoformat()}'")

    def advance_time(self, delta: timedelta):
        current = self.get_current_time()
        self.set_time(current + delta)

    def spawn_task(self, task_name, params, **kwargs):
        return self.sdk.spawn(task_name, params, **kwargs)

    def get_task_state(self, task_id):
        """Direct table access for verification."""
        result = self.conn.execute(
            "SELECT state FROM t_test WHERE id = %s", (task_id,)
        ).fetchone()
        return result[0] if result else None
```

### 3.2 Test Coverage

**Core Functionality Tests**:
- `test_basic_task_lifecycle.py`: spawn → claim → complete → verify state
- `test_checkpointing.py`: step execution, checkpoint caching, retry recovery
- `test_retry_strategies.py`: exponential, fixed, none strategies
- `test_event_system.py`: emit_event, await_event, race conditions, timeouts
- `test_cancellation.py`: cancel_task, AB001 error handling
- `test_cleanup.py`: cleanup_tasks, cleanup_events TTL behavior

**Concurrency Tests**:
```python
def test_concurrent_claims(db):
    """Multiple workers claiming tasks simultaneously."""
    client1 = AbsurdTestClient(db)
    client2 = AbsurdTestClient(db)

    # Spawn 10 tasks
    for i in range(10):
        client1.spawn_task(f"task-{i}", {})

    # Both workers claim tasks concurrently
    # FOR UPDATE SKIP LOCKED ensures no conflicts
    claimed1 = [client1.claim_task() for _ in range(5)]
    claimed2 = [client2.claim_task() for _ in range(5)]

    # Verify no overlap
    task_ids = {t['id'] for t in claimed1 + claimed2}
    assert len(task_ids) == 10
```

**Time-Based Tests**:
```python
def test_scheduled_task_execution(db):
    """Tasks scheduled for future execution."""
    client = AbsurdTestClient(db)
    client.set_time(datetime(2025, 1, 1, 10, 0, 0))

    # Spawn task scheduled for 1 hour later
    task_id = client.spawn_task(
        "delayed-task", {},
        scheduled_at=datetime(2025, 1, 1, 11, 0, 0)
    )

    # Should not be claimable yet
    assert client.claim_task() is None

    # Advance time to scheduled time
    client.advance_time(timedelta(hours=1))

    # Now claimable
    claimed = client.claim_task()
    assert claimed['id'] == task_id
```

### 3.3 Direct Table Verification

Tests verify correctness by directly querying Postgres tables:

```python
def test_checkpoint_persistence(db):
    """Verify checkpoints are actually stored in database."""
    client = AbsurdTestClient(db)

    @client.sdk.register_task("checkpoint-test")
    def checkpoint_test(params, ctx):
        result1 = ctx.step("step-1", lambda: {"value": 42})
        result2 = ctx.step("step-2", lambda: {"value": result1["value"] * 2})
        return result2

    # Run task
    task_id = client.spawn_task("checkpoint-test", {})
    client.sdk.process_next_task()

    # Query checkpoints table directly
    checkpoints = db.execute("""
        SELECT step_name, checkpoint_data
        FROM c_test
        WHERE run_id = (SELECT id FROM r_test WHERE task_id = %s)
        ORDER BY step_name
    """, (task_id,)).fetchall()

    assert len(checkpoints) == 2
    assert checkpoints[0] == ("step-1", {"value": 42})
    assert checkpoints[1] == ("step-2", {"value": 84})
```

---

## 4. Correctness Verification

### 4.1 Transaction Boundaries

Stored procedures run in the **caller's transaction context**. They contain no explicit `BEGIN`/`COMMIT` statements.

**In autocommit mode** (psycopg3 default for Python SDK):
```sql
-- Each call is auto-committed
SELECT absurd.spawn_task('queue', 'task1', '{}');  -- Commits immediately
SELECT absurd.spawn_task('queue', 'task2', '{}');  -- Commits immediately (separate transaction)
```

**In transaction mode** (when user manages transactions):
```sql
BEGIN;
SELECT absurd.spawn_task('queue', 'task1', '{}');  -- In transaction
SELECT absurd.spawn_task('queue', 'task2', '{}');  -- In same transaction
COMMIT;  -- Both tasks spawn atomically
```

**SDK Implementation** (Python):
```python
class Absurd:
    def __init__(self, conn_or_url, queue_name):
        if isinstance(conn_or_url, str):
            # Use autocommit by default for connection strings
            self._conn = Connection.connect(conn_or_url, autocommit=True)
        else:
            # Use whatever mode the provided connection has
            self._conn = conn_or_url
```

**Why Autocommit is Recommended (But Not Required)**:

Autocommit is the **recommended default**, not a technical requirement:

**Benefits of Autocommit**:
- Each operation commits immediately (no forgotten commits)
- Matches TypeScript SDK behavior (`pg.Pool.query()` auto-commits)
- Matches all test patterns
- Simple mental model: each stored procedure call is atomic and durable

**Transaction Mode Still Works**:
- Stored procedures run in caller's transaction context
- Can use transaction mode by passing your own connection object
- Enables batch atomic operations (spawn multiple tasks atomically)

**Trade-off**:
- Autocommit (default): Simple, works immediately, each op commits separately
- Transaction mode: More control, batch atomicity, requires manual `commit()`

**Example - Batch Atomic Operations**:
```python
# Use transaction mode for batch atomicity
with psycopg.connect(db_url, autocommit=False) as conn:
    app = Absurd(conn, "queue")
    app.spawn("task-1", {})
    app.spawn("task-2", {})
    app.spawn("task-3", {})
    conn.commit()  # All 3 tasks spawn atomically (all-or-nothing)
```

### 4.2 Idempotency

**Step-Level Idempotency**:
```sql
-- ON CONFLICT ensures idempotent checkpoint writes
INSERT INTO {checkpoints_table} (run_id, step_name, checkpoint_data)
VALUES (run_id_param, step_name_param, checkpoint_data_param)
ON CONFLICT (run_id, step_name) DO UPDATE
    SET checkpoint_data = EXCLUDED.checkpoint_data;
```

**Event Idempotency**:
Events are not deduplicated. Multiple `emit_event()` calls create multiple events. This is intentional:
- Events represent occurrences, not state
- Deduplication should happen at application level if needed
- Workers can claim same event name multiple times (different event IDs)

**Recommended Pattern**:
```python
# Use task ID in event names for per-task uniqueness
ctx.await_event(f"payment.confirmed:{task_id}")

# Emit with matching name
app.emit_event(f"payment.confirmed:{task_id}", {"amount": 99.99})
```

### 4.3 Claim Timeout Safety

**Problem**: Worker crashes before completing task.

**Solution**: Automatic claim expiry and retry scheduling:
```sql
-- Background job or next claim_task() detects expired claims
UPDATE {tasks_table}
SET state = 'failed'
WHERE state = 'running'
  AND claim_expires_at < absurd.fake_now();

-- fail_run() schedules retry based on strategy
```

**Risk**: Overlapping execution if claim expires while worker is still running.

**Mitigation**:
1. Set generous claim timeouts (e.g., 5+ minutes for typical tasks)
2. Extend claim on each checkpoint write
3. Design tasks to make observable progress quickly
4. Use idempotent operations for external API calls

### 4.4 Cancellation Policies

Tasks can define automatic cancellation based on age or duration:

```sql
-- Check cancellation policies in claim_task()
IF (max_delay IS NOT NULL AND absurd.fake_now() > created_at + max_delay) THEN
    RAISE EXCEPTION 'Task exceeded max_delay' USING ERRCODE = 'AB001';
END IF;

IF (max_duration IS NOT NULL AND total_run_time > max_duration) THEN
    RAISE EXCEPTION 'Task exceeded max_duration' USING ERRCODE = 'AB001';
END IF;
```

**Example**:
```python
# Task must start within 1 hour of creation
# Total execution time cannot exceed 6 hours
client.spawn_task(
    "time-sensitive-task", {},
    max_delay=timedelta(hours=1),
    max_duration=timedelta(hours=6)
)
```

---

## 5. Concurrency Management

### 5.1 Lock-Free Task Claiming

**FOR UPDATE SKIP LOCKED** prevents lock contention:

```sql
-- Multiple workers execute this simultaneously
SELECT id FROM {tasks_table}
WHERE state = 'pending'
  AND (scheduled_at IS NULL OR scheduled_at <= absurd.fake_now())
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;  -- Skip rows locked by other workers
```

**Behavior**:
- Worker A locks task 1
- Worker B skips task 1, locks task 2
- No blocking, no deadlocks
- Maximum concurrency for task distribution

### 5.2 Event Wake-Up Atomicity

Event emission atomically wakes all waiting tasks using a single CTE (Common Table Expression):

```sql
-- Simplified from emit_event() stored procedure (lines 1047-1125)
execute format(
  'WITH expired_waits AS (
      DELETE FROM absurd.%1$I w
      WHERE w.event_name = $1 AND w.timeout_at <= $2
      RETURNING w.run_id
   ),
   affected AS (
      SELECT run_id, task_id, step_name
      FROM absurd.%1$I
      WHERE event_name = $1 AND (timeout_at IS NULL OR timeout_at > $2)
   ),
   updated_runs AS (
      UPDATE absurd.%2$I r
      SET state = ''pending'', available_at = $2, event_payload = $3
      WHERE r.run_id IN (SELECT run_id FROM affected) AND r.state = ''sleeping''
      RETURNING r.run_id, r.task_id
   ),
   checkpoint_upd AS (
      INSERT INTO absurd.%3$I (task_id, checkpoint_name, state, ...)
      SELECT a.task_id, a.step_name, $3, ...
      FROM affected a JOIN updated_runs ur ON ur.run_id = a.run_id
      ON CONFLICT (task_id, checkpoint_name) DO UPDATE ...
   ),
   updated_tasks AS (
      UPDATE absurd.%4$I t SET state = ''pending''
      WHERE t.task_id IN (SELECT task_id FROM updated_runs)
      RETURNING task_id
   )
   DELETE FROM absurd.%5$I w
   WHERE w.event_name = $1 AND w.run_id IN (SELECT run_id FROM updated_runs)',
  ...
) USING p_event_name, v_now, v_payload;
```

**Atomicity Mechanism**:

The atomicity comes from **single-statement execution**, not explicit transaction boundaries:

1. **No explicit BEGIN/COMMIT**: The stored procedure contains no transaction control statements
2. **Single EXECUTE statement**: The entire CTE runs as one SQL statement
3. **PostgreSQL guarantee**: A single SQL statement executes atomically within its transaction context

**What this means**:
- If called outside a transaction → runs in its own implicit transaction (auto-committed)
- If called within a transaction → participates in that transaction
- Either way, all operations (expire waits, update runs, create checkpoints, update tasks, clean up) happen atomically

**Guarantees**:
- Event emission and all task wake-ups complete or all fail together
- No lost wake-ups or partial states
- Multiple tasks can wait for same event name and all wake simultaneously
- Race-free: events cached in `e_{queue}` table, checked by `await_event` before sleeping

### 5.3 Checkpoint Write Concurrency

Checkpoint writes extend the claim timeout:

```sql
-- Atomic checkpoint + claim extension
WITH checkpoint_insert AS (
    INSERT INTO {checkpoints_table} (run_id, step_name, checkpoint_data)
    VALUES (run_id_param, step_name_param, checkpoint_data_param)
    ON CONFLICT (run_id, step_name) DO UPDATE
        SET checkpoint_data = EXCLUDED.checkpoint_data
    RETURNING *
)
UPDATE {tasks_table}
SET claim_expires_at = absurd.fake_now() + extend_claim_by_param
WHERE id = task_id_param;
```

**Race Condition Handling**:
- If task was cancelled during step execution, checkpoint write succeeds but subsequent operations will fail with AB001
- If claim expired, update affects 0 rows (no error, worker continues until next checkpoint or completion)

### 5.4 Cleanup Concurrency

Cleanup operations use row-level locking:

```sql
-- cleanup_tasks(): Delete old completed/failed/cancelled tasks
DELETE FROM {tasks_table}
WHERE state IN ('completed', 'failed', 'cancelled')
  AND (completed_at < absurd.fake_now() - ttl_param
       OR cancelled_at < absurd.fake_now() - ttl_param
       OR (state = 'failed' AND updated_at < absurd.fake_now() - ttl_param));
```

**Safe to run concurrently** with workers:
- Only touches terminal states (completed, failed, cancelled)
- Workers never claim tasks in terminal states
- CASCADE deletes clean up runs, checkpoints, wait registrations

---

## 6. SDK Integration Patterns

### 6.1 TypeScript SDK

**Architecture**: Wraps stored procedures with TypeScript type safety and async/await ergonomics.

```typescript
export class Absurd {
  private pool: pg.Pool;
  private queueName: string;

  constructor(options: AbsurdOptions) {
    this.pool = new pg.Pool({ connectionString: options.db });
    this.queueName = options.queueName;
  }

  // Thin wrapper over spawn_task stored procedure
  async spawn(taskName: string, params: object): Promise<string> {
    const result = await this.pool.query(
      'SELECT absurd.spawn_task($1, $2, $3) as task_id',
      [this.queueName, taskName, JSON.stringify(params)]
    );
    return result.rows[0].task_id;
  }

  // Worker loop
  async startWorker(workerId?: string): Promise<void> {
    while (true) {
      const task = await this.claimTask(workerId);
      if (!task) {
        await this.sleep(this.pollInterval);
        continue;
      }

      try {
        await this.executeTask(task);
      } catch (err) {
        if (err.code === 'AB001') {
          // Task cancelled - graceful exit
          continue;
        }
        await this.failTask(task.id, err);
      }
    }
  }

  private async claimTask(workerId?: string) {
    const result = await this.pool.query(
      'SELECT absurd.claim_task($1, $2, $3)',
      [this.queueName, workerId || 'default-worker', this.claimTimeout]
    );
    return result.rows[0]?.claim_task || null;
  }
}
```

**Task Execution Context**:
```typescript
class TaskContext {
  constructor(
    private client: Absurd,
    private taskId: string,
    private runId: string,
    private queueName: string
  ) {}

  async step<T>(name: string, fn: () => Promise<T>): Promise<T> {
    // Check cache first
    const cached = await this.getCheckpoint(name);
    if (cached !== null) return cached as T;

    // Execute and cache
    const result = await fn();
    await this.setCheckpoint(name, result);
    return result;
  }

  async awaitEvent(eventName: string, timeout?: number): Promise<any> {
    const result = await this.client.pool.query(
      'SELECT absurd.await_event($1, $2, $3, $4, $5)',
      [this.queueName, this.runId, eventName, timeout, this.taskId]
    );

    const event = result.rows[0];
    if (!event) {
      // Event not found - stored procedure suspends task
      throw new TaskSuspended(eventName);
    }

    return event.payload;
  }

  private async setCheckpoint(name: string, data: any): Promise<void> {
    await this.client.pool.query(
      'SELECT absurd.set_task_checkpoint_state($1, $2, $3, $4, $5)',
      [this.queueName, this.runId, name, JSON.stringify(data), this.claimTimeout]
    );
  }
}
```

### 6.2 Python SDK

**Architecture**: Nearly identical pattern to TypeScript, using psycopg3.

```python
class Absurd:
    def __init__(self, conn_or_url: Union[str, Connection], queue_name: str):
        if isinstance(conn_or_url, str):
            # Always use autocommit when creating connection
            self._conn = Connection.connect(conn_or_url, autocommit=True)
        else:
            self._conn = conn_or_url
        self._queue_name = queue_name

    def spawn(self, task_name: str, params: dict, **kwargs) -> str:
        result = self._conn.execute(
            "SELECT absurd.spawn_task(%s, %s, %s) as task_id",
            (self._queue_name, task_name, json.dumps(params))
        ).fetchone()
        return result[0]

    def start_worker(self, worker_id: str = "default-worker"):
        while True:
            task = self._claim_task(worker_id)
            if not task:
                time.sleep(self.poll_interval)
                continue

            try:
                self._execute_task(task)
            except psycopg.errors.lookup('AB001'):
                # Task cancelled
                continue
            except Exception as e:
                self._fail_task(task['id'], e)
```

**Task Execution Context**:
```python
class TaskContext:
    def __init__(self, conn, queue_name, task_id, run_id):
        self._conn = conn
        self._queue_name = queue_name
        self._task_id = task_id
        self._run_id = run_id
        self._checkpoints = None  # Lazy load

    def step(self, name: str, fn: Callable[[], T]) -> T:
        # Load checkpoints lazily
        if self._checkpoints is None:
            self._load_checkpoints()

        # Return cached result if exists
        if name in self._checkpoints:
            return self._checkpoints[name]

        # Execute and cache
        result = fn()
        self._set_checkpoint(name, result)
        self._checkpoints[name] = result
        return result

    def await_event(self, event_name: str, timeout: Optional[int] = None) -> Any:
        result = self._conn.execute(
            "SELECT absurd.await_event(%s, %s, %s, %s, %s)",
            (self._queue_name, self._run_id, event_name, timeout, self._task_id)
        ).fetchone()

        if result[0] is None:
            # Stored procedure suspended task
            raise _TaskSuspended(event_name)

        return result[0]

    def _set_checkpoint(self, name: str, data: Any):
        self._conn.execute(
            "SELECT absurd.set_task_checkpoint_state(%s, %s, %s, %s, %s)",
            (self._queue_name, self._run_id, name, json.dumps(data), self._claim_timeout)
        )
```

### 6.3 SDK Design Principles

Both SDKs follow these patterns:

1. **Transaction-agnostic**: Never manage transactions, rely on autocommit
2. **Thin wrappers**: Each SDK method calls exactly one stored procedure
3. **Type safety**: Convert between language types and JSONB
4. **Error handling**: Catch AB001 for cancellation, propagate other errors
5. **Lazy loading**: Checkpoints loaded once per run, cached in memory
6. **Claim extension**: Automatic on every checkpoint write

**No SDK Logic**: Business logic lives in SQL, not SDKs. This enables:
- Language-agnostic behavior
- Easy addition of new language SDKs
- Centralized testing of core logic
- Database-driven bug fixes (no SDK redeployment needed)

---

## 7. Migration Strategy

### 7.1 Version-Based Migrations

Absurd uses explicit migration files for version upgrades:

```
sql/
  absurd.sql                    # Current schema (v0.0.5)
  migrations/
    0.0.3-0.0.4.sql            # Upgrade from 0.0.3 to 0.0.4
    0.0.4-0.0.5.sql            # Upgrade from 0.0.4 to 0.0.5
```

**Migration File Structure**:
```sql
-- 0.0.4-0.0.5.sql
-- Migration from version 0.0.4 to 0.0.5
-- Adds: Event timeouts, cancellation policies

BEGIN;

-- Add wait_timeout_at column to tasks tables
-- Dynamically iterate over all queues
DO $$
DECLARE
    queue_record RECORD;
BEGIN
    FOR queue_record IN
        SELECT queue_name FROM absurd.queues
    LOOP
        EXECUTE format('ALTER TABLE t_%I ADD COLUMN wait_timeout_at TIMESTAMPTZ',
                       queue_record.queue_name);
    END LOOP;
END $$;

-- Add cancellation policy columns
ALTER TABLE absurd.queues
ADD COLUMN max_delay INTERVAL,
ADD COLUMN max_duration INTERVAL;

-- Update stored procedures with new logic
CREATE OR REPLACE FUNCTION absurd.claim_task(...) ...

COMMIT;
```

### 7.2 Automated Migration Generation

The repository includes a Claude Code command for migration creation:

**Command**: `/make-migrations`

**Process**:
1. Reads current `sql/absurd.sql`
2. Compares against last migration version
3. Generates incremental SQL migration
4. Validates migration syntax
5. Creates `sql/migrations/X.X.X-Y.Y.Y.sql`

**Developer Workflow**:
1. Modify `sql/absurd.sql` during development
2. Before release, run `/make-migrations`
3. Review generated migration for correctness
4. Test migration on copy of production database
5. Commit migration file with release

### 7.3 Migration Testing

Migrations are tested by:
1. Creating database with old schema version
2. Applying migration
3. Running full test suite against migrated schema
4. Verifying no data loss or corruption

**Example Test**:
```python
def test_migration_0_0_4_to_0_0_5(db):
    # Apply old schema
    db.execute(open("sql/old/absurd-0.0.4.sql").read())

    # Create some data
    db.execute("SELECT absurd.create_queue('test')")
    task_id = db.execute(
        "SELECT absurd.spawn_task('test', 'task', '{}')"
    ).fetchone()[0]

    # Apply migration
    db.execute(open("sql/migrations/0.0.4-0.0.5.sql").read())

    # Verify data intact
    task = db.execute(
        "SELECT * FROM t_test WHERE id = %s", (task_id,)
    ).fetchone()
    assert task is not None
    assert task['wait_timeout_at'] is None  # New column, default NULL
```

### 7.4 Production Migration Guidelines

**Safe Migration Practices**:
1. **Test on copy**: Always test on production copy first
2. **Read-only window**: Optionally stop workers during migration
3. **Backup first**: pg_dump before applying migration
4. **Rollback plan**: Keep rollback SQL ready (if applicable)
5. **Monitor post-migration**: Watch for errors after workers resume

**Backwards Compatibility**:
Migrations should maintain compatibility with running workers when possible:
- Additive changes (new columns, new procedures) are safe
- Destructive changes (dropping columns, changing types) require worker restarts
- Document any required deployment coordination in migration comments

---

## 8. Open Questions and Future Considerations

### 8.1 Identified Gaps

**No Observability Schema**:
- No built-in metrics tables (task duration, failure rates, etc.)
- Monitoring relies on external tools querying task/run tables
- Consider: Materialized views for common metrics queries

**No Partitioning Strategy**:
- All tables grow unbounded until manual cleanup
- High-volume queues may hit performance limits
- Consider: Time-based partitioning for task/run/event tables

**No Dead Letter Queue**:
- Failed tasks (max attempts exceeded) remain in failed state
- No automatic routing to DLQ for human intervention
- Consider: Separate queue for exhausted tasks

**Limited Priority Support**:
- Tasks claimed in FIFO order (created_at)
- No priority queue mechanism
- Consider: Priority column with ORDER BY priority DESC, created_at

### 8.2 Scalability Considerations

**Single Database Bottleneck**:
- All operations hit single Postgres instance
- No built-in sharding or horizontal scaling
- Mitigation: Use Postgres replicas for read-heavy operations (habitat UI)

**Event Table Growth**:
- Unclaimed events accumulate forever until cleanup
- High-frequency events can bloat table
- Mitigation: Aggressive cleanup policy, or TTL-based partitioning

**Checkpoint Storage**:
- Large step results stored as JSONB in database
- Memory-intensive tasks can create large checkpoints
- Consider: External blob storage for large results, with references in checkpoints

### 8.3 Potential Enhancements

**Batch Operations**:
- Spawn multiple tasks in single procedure call
- Claim multiple tasks at once (worker-level batching)

**Child Tasks**:
- Spawn sub-tasks from within parent task
- Await child task completion before proceeding
- Enables fan-out/fan-in patterns

**Saga Pattern**:
- Built-in compensation (rollback) steps
- Automatic rollback on failure
- Requires inverse operation registration

**Observability Events**:
- Emit structured events for task lifecycle (spawned, claimed, completed)
- Enable external monitoring systems to track progress
- Minimal overhead via LISTEN/NOTIFY

---

## 9. Conclusion

Absurd achieves its goal of "absurd simplicity" by:

1. **Centralizing complexity**: All logic in PostgreSQL stored procedures
2. **Thin SDKs**: Language bindings are just RPC wrappers
3. **Leveraging Postgres**: Row locks, JSONB, transactions, procedural language
4. **Testing discipline**: Comprehensive test coverage with time control
5. **Migration discipline**: Version-based migrations with automated generation

**Key Insight**: By treating PostgreSQL as a complete platform (not just storage), Absurd
eliminates entire classes of distributed systems problems:
- No message broker coordination
- No external state store
- No leader election
- No schema migrations across multiple services

**Trade-offs**:
- Single database is bottleneck (but acceptable for vast majority of use cases)
- Postgres expertise required for troubleshooting
- Limited by Postgres connection pool size

**Recommended Use Cases**:
- LLM-based agents (long-running, stateful workflows)
- Payment processing (durability, retries, idempotency)
- Order fulfillment (multi-stage, event-driven)
- Scheduled jobs (sleep, scheduled_at)
- Human-in-the-loop workflows (await_event for approvals)

**Not Recommended For**:
- Real-time event streaming (use Kafka/Pulsar)
- Sub-second latency requirements (use in-memory queues)
- Multi-datacenter coordination (use Temporal/Cadence)
- Unbounded horizontal scaling (database will be bottleneck)

---

## Appendix A: Stored Procedure Reference

### Core Procedures

| Procedure | Purpose | Key Parameters |
|-----------|---------|----------------|
| `absurd.create_queue(name)` | Create queue + 5 tables | queue_name |
| `absurd.drop_queue(name)` | Drop queue + all tables | queue_name |
| `absurd.spawn_task(...)` | Create new task | queue, task_name, params, retry_strategy |
| `absurd.claim_task(...)` | Worker claims next pending task | queue, worker_id, claim_timeout |
| `absurd.complete_run(...)` | Mark run as completed | queue, run_id, result |
| `absurd.fail_run(...)` | Mark run as failed, schedule retry | queue, run_id, error, retry_strategy |
| `absurd.cancel_task(...)` | Cancel task, raise AB001 if running | queue, task_id |
| `absurd.set_task_checkpoint_state(...)` | Store step result | queue, run_id, step_name, checkpoint_data |
| `absurd.get_task_checkpoint_states(...)` | Load all checkpoints for run | queue, run_id |
| `absurd.emit_event(...)` | Emit event, wake waiting tasks | queue, event_name, payload |
| `absurd.await_event(...)` | Claim event or register wait | queue, run_id, event_name, timeout |
| `absurd.cleanup_tasks(...)` | Delete old tasks/runs/checkpoints | queue, ttl |
| `absurd.cleanup_events(...)` | Delete old events | queue, ttl |

### Utility Functions

| Function | Purpose |
|----------|---------|
| `absurd.portable_uuidv7()` | Generate UUIDv7 (or fallback) |
| `absurd.fake_now()` | Return current time (or fake time for tests) |
| `absurd.calculate_retry_delay(...)` | Compute next retry delay based on strategy |

---

## Appendix B: Table Schema Reference

### Tasks Table: `t_{queue_name}`

```sql
CREATE TABLE t_examples (
    id UUID PRIMARY KEY DEFAULT absurd.portable_uuidv7(),
    task_name TEXT NOT NULL,
    params JSONB,
    queue_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'sleeping', 'completed', 'failed', 'cancelled')),
    created_at TIMESTAMPTZ DEFAULT absurd.fake_now(),
    scheduled_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    worker_id TEXT,
    retry_strategy JSONB DEFAULT '{"type": "exponential", "max_attempts": 5}'::jsonb,
    max_delay INTERVAL,
    max_duration INTERVAL,
    wait_timeout_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT absurd.fake_now()
);

CREATE INDEX idx_tasks_state_scheduled ON t_examples(state, scheduled_at);
CREATE INDEX idx_tasks_claim_expires ON t_examples(claim_expires_at);
```

### Runs Table: `r_{queue_name}`

```sql
CREATE TABLE r_examples (
    id UUID PRIMARY KEY DEFAULT absurd.portable_uuidv7(),
    task_id UUID NOT NULL REFERENCES t_examples(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed', 'cancelled')),
    started_at TIMESTAMPTZ DEFAULT absurd.fake_now(),
    completed_at TIMESTAMPTZ,
    result JSONB,
    error JSONB,
    worker_id TEXT
);

CREATE INDEX idx_runs_task_id ON r_examples(task_id);
CREATE INDEX idx_runs_state ON r_examples(state);
```

### Checkpoints Table: `c_{queue_name}`

```sql
CREATE TABLE c_examples (
    run_id UUID NOT NULL REFERENCES r_examples(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    checkpoint_data JSONB,
    created_at TIMESTAMPTZ DEFAULT absurd.fake_now(),
    PRIMARY KEY (run_id, step_name)
);

CREATE INDEX idx_checkpoints_run_id ON c_examples(run_id);
```

### Events Table: `e_{queue_name}`

```sql
CREATE TABLE e_examples (
    id BIGSERIAL PRIMARY KEY,
    event_name TEXT NOT NULL,
    payload JSONB,
    emitted_at TIMESTAMPTZ DEFAULT absurd.fake_now(),
    claimed_by_run_id UUID REFERENCES r_examples(id) ON DELETE SET NULL,
    claimed_at TIMESTAMPTZ
);

CREATE INDEX idx_events_name_claimed ON e_examples(event_name, claimed_by_run_id);
CREATE INDEX idx_events_emitted_at ON e_examples(emitted_at);
```

### Wait Registrations Table: `w_{queue_name}`

```sql
CREATE TABLE w_examples (
    run_id UUID NOT NULL REFERENCES r_examples(id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    registered_at TIMESTAMPTZ DEFAULT absurd.fake_now(),
    PRIMARY KEY (run_id, event_name)
);

CREATE INDEX idx_wait_registrations_event_name ON w_examples(event_name);
```

---

**End of Research Document**
