BEGIN;

CREATE TABLE IF NOT EXISTS schedules (
    id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    owner_id UUID NOT NULL REFERENCES users(id),
    workflow VARCHAR(60) NOT NULL,
    params JSON NOT NULL,
    cron VARCHAR(100) NOT NULL,
    timezone VARCHAR(60) NOT NULL,
    enabled BOOLEAN NOT NULL,
    auto_execute BOOLEAN NOT NULL,
    overlap_policy VARCHAR(10) NOT NULL,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    last_task_id UUID,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS workflow VARCHAR(60),
    ADD COLUMN IF NOT EXISTS schedule_id UUID,
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pending_dispatch JSON,
    ADD COLUMN IF NOT EXISTS lock_owner VARCHAR(100),
    ADD COLUMN IF NOT EXISTS lock_expires_at TIMESTAMPTZ;

ALTER TABLE tasks
    ALTER COLUMN conversation_id DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tasks_schedule_id_fkey'
          AND conrelid = 'tasks'::regclass
    ) THEN
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_schedule_id_fkey
            FOREIGN KEY (schedule_id) REFERENCES schedules(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_task_schedule_slot'
          AND conrelid = 'tasks'::regclass
    ) THEN
        ALTER TABLE tasks
            ADD CONSTRAINT uq_task_schedule_slot
            UNIQUE (schedule_id, scheduled_for);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_tasks_workflow ON tasks(workflow);
CREATE INDEX IF NOT EXISTS ix_tasks_schedule_id ON tasks(schedule_id);
CREATE INDEX IF NOT EXISTS ix_tasks_conversation_id ON tasks(conversation_id);

CREATE INDEX IF NOT EXISTS ix_schedules_owner_id ON schedules(owner_id);
CREATE INDEX IF NOT EXISTS ix_schedules_enabled ON schedules(enabled);
CREATE INDEX IF NOT EXISTS ix_schedules_next_run_at ON schedules(next_run_at);

COMMIT;
