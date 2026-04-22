# Migrations

Alembic is wired against the same `DATABASE_URL` the app reads. To start using it on an existing database (where `Base.metadata.create_all()` has already created the tables):

```bash
cd backend

# 1. Mark the current schema as the baseline (no SQL runs)
alembic stamp head

# 2. From now on, when you change a model:
alembic revision --autogenerate -m "add events.is_published"
alembic upgrade head
```

`Base.metadata.create_all()` in `main.py` is still safe to leave in for fresh installs — Alembic owns the upgrade path for existing databases. The old `_ensure_column()` micro-migration in `main.py` should be retired once the equivalent Alembic revision is applied in every environment.
