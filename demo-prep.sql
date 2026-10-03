-- Bookly demo prep: run in Supabase SQL Editor before recording.
-- Safe to re-run: the table creation is IF NOT EXISTS and the reset is idempotent.

-- 1) Escalation work queue (insert-only; the agent opens tickets, never edits them)
create table if not exists escalations (
  id uuid primary key default gen_random_uuid(),
  ticket_ref text unique not null default ('ESC-' || floor(random()*9000+1000)::int::text),
  created_at timestamptz default now(),
  customer_email text not null,
  customer_name text,
  trigger text not null,
  priority text not null default 'medium',
  order_id text,
  amount numeric,
  summary text not null,
  context jsonb default '{}'::jsonb,
  status text not null default 'open'
);

-- 2) Reset Kim to a clean slate (zero orders, empty interaction history)
update customers
set details = '{"orders": [], "preferences": {"format": "paperback", "notifications": "email", "genres": ["fantasy", "adventure"]}, "interactions": []}'::jsonb
where email = 'kimjohnson3183@gmail.com';

-- 3) Optional: clear old escalation tickets from rehearsals
-- delete from escalations where customer_email = 'kimjohnson3183@gmail.com';
