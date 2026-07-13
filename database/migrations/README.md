# database/migrations/

Versioned DDL migrations, applied in numeric order. Each file is frozen after
its milestone is approved. Never edit a migration that has been applied to any
environment — add a new file instead.

| File | Milestone | Contents |
|------|-----------|----------|
| `001_initial_schema.sql` | M1 | Schema `orderflow`, 7 tables, updated_at trigger function |
