CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         text NOT NULL UNIQUE,
    password_hash text
);

CREATE TABLE tasks (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id),
    title      text NOT NULL CHECK (length(btrim(title)) > 0),
    status     text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'done')),
    position   integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_user_status_position ON tasks (user_id, status, position);

CREATE FUNCTION tasks_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION tasks_touch_updated_at();

COMMENT ON COLUMN tasks.position   IS 'Thu tu thu cong trong (user_id, status); nho hon = truoc';
COMMENT ON COLUMN tasks.created_at IS 'Thoi diem tao (UTC)';
COMMENT ON COLUMN tasks.updated_at IS 'Thoi diem cap nhat cuoi (trigger)';
