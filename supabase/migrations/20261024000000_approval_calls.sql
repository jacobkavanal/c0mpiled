-- Phone-call approvals: who can approve, the held requests, and the call audit trail.

create type public.approval_status as enum
    ('pending', 'calling', 'approved', 'denied', 'expired', 'failed');

create type public.risk_level as enum ('low', 'medium', 'high', 'critical');

create table public.approvers (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   text not null,
    name        text not null,
    phone_e164  text not null check (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    -- scrypt hash of a keypad PIN; when set, nothing about the request is
    -- spoken until the PIN is entered.
    pin_hash    text,
    created_at  timestamptz not null default now()
);

create index approvers_tenant_idx on public.approvers (tenant_id);

create table public.approval_requests (
    id               uuid primary key default gen_random_uuid(),
    tenant_id        text not null,
    approver_id      uuid not null references public.approvers (id),
    status           public.approval_status not null default 'pending',
    risk_level       public.risk_level not null,
    summary          text not null,
    -- Established by the gateway (tool call, arguments, checks it ran).
    facts            jsonb not null,
    -- Stated by the agent; unverified.
    claims           jsonb not null,
    call_sid         text unique,
    decision_reason  text,
    decided_at       timestamptz,
    expires_at       timestamptz not null,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create index approval_requests_open_idx on public.approval_requests (expires_at)
    where status in ('pending', 'calling');
create index approval_requests_tenant_idx on public.approval_requests (tenant_id, created_at desc);

-- Transcript and audit trail: approver and assistant turns, tool calls,
-- PIN results (never the digits), and Twilio call status changes.
create table public.call_events (
    id           bigint generated always as identity primary key,
    approval_id  uuid not null references public.approval_requests (id) on delete cascade,
    kind         text not null,
    content      jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

create index call_events_approval_idx on public.call_events (approval_id, id);

create function public.touch_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create trigger approval_requests_touch
    before update on public.approval_requests
    for each row execute function public.touch_updated_at();

-- Wakes every gateway instance waiting on a request as soon as it is resolved,
-- whichever instance (or the dashboard) resolved it.
create function public.notify_approval_resolved() returns trigger
language plpgsql as $$
begin
    if new.status in ('approved', 'denied', 'expired', 'failed')
       and new.status is distinct from old.status then
        perform pg_notify('approval_resolved', new.id::text);
    end if;
    return null;
end;
$$;

create trigger approval_requests_notify
    after update of status on public.approval_requests
    for each row execute function public.notify_approval_resolved();

-- The gateway connects as the database owner and bypasses RLS. With no
-- policies defined, the anon and authenticated roles can read nothing; add
-- tenant-scoped policies when the dashboard gets Supabase Auth.
alter table public.approvers enable row level security;
alter table public.approval_requests enable row level security;
alter table public.call_events enable row level security;
