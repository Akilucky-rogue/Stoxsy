# Database migrations

`infra/init.sql` is the base schema; it runs automatically the first time the TimescaleDB container starts on an empty volume. Every later change is a file `infra/migrations/NNN_*.sql` (currently 001 to 065; the next free number is 066). Migrations are additive and idempotent (`IF NOT EXISTS`, dynamic constraint checks), so running them again is safe.

## Apply them
From `backend/`, with the project venv (the database settings come from `.env`):

```powershell
..\..\.venv\Scripts\python.exe scripts\apply_migrations.py            # applies every migration in order
..\..\.venv\Scripts\python.exe scripts\apply_migrations.py --list     # lists them without applying
```
There is no tracking table: the runner applies every file each time, relying on idempotence.

## Rules
- Never edit a migration that has been applied to the live database; add a new one (065 fixed 064 this way).
- Ask the owner before applying a new migration to the live database; CI applies all of them to an empty database on every push.
- Keep a migration small, with a comment saying why it exists.
