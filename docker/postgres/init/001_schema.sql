create table if not exists storage_meta (
  key text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);
