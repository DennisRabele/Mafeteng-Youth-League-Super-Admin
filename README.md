# Mafeteng Youth League Super Admin

This repository is the Super Admin-only deployment for Mafeteng Youth League.

It uses the same Supabase database and the same Supabase Storage buckets as the other app:

- `admin photos`
- `team logos`
- `player documents`
- `player photos`
- `player agreements`

## What is included

- Super Admin entrypoint only
- Shared database models and routes used by the Super Admin app
- Same SMTP configuration used in the original project
- Same Supabase connection and storage bucket names

## What is not included

- Team Admin deployment entrypoint
- Combined app entrypoint

## Required environment variables

Use the same values you already have for SMTP and Supabase, then set:

```text
APP_MODE=super_admin
APP_NAME=Mafeteng Youth Development League
SECRET_KEY=your-long-random-secret
DATABASE_URL=postgresql+psycopg://postgres.vwxcpsmtxjszjvrtkvww:YOUR_PASSWORD@aws-0-eu-west-3.pooler.supabase.com:6543/postgres?sslmode=require
SUPABASE_URL=https://vwxcpsmtxjszjvrtkvww.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-supabase-service-role-key
SUPABASE_ADMIN_PHOTOS_BUCKET=admin photos
SUPABASE_TEAM_LOGOS_BUCKET=team logos
SUPABASE_PLAYER_DOCUMENTS_BUCKET=player documents
SUPABASE_PLAYER_PHOTOS_BUCKET=player photos
SUPABASE_PLAYER_AGREEMENTS_BUCKET=player agreements
SUPER_ADMIN_NAME=League Super Admin
SUPER_ADMIN_EMAIL=admin@ydl.local
SUPER_ADMIN_PASSWORD=Admin123!
DEFAULT_SEASON_NAME=2026 Youth Development League
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=thecoderrabele@gmail.com
SMTP_FROM_EMAIL=thecoderrabele@gmail.com
SMTP_PASSWORD=your-email-app-password
SMTP_FROM_NAME=Mafeteng Youth League
EMAIL_CODE_MINUTES=15
LOGIN_CODE_MINUTES=10
UPLOAD_DIR=storage/uploads
```

## Local run

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.super_admin_main:app --reload --port 8001
```

Open:

```text
http://127.0.0.1:8001
```

## Vercel

Use this repo as the source for a new Vercel project and keep the production domain attached to this repository only.

The included `vercel.json` rewrites all requests to `api/super_admin.py`, which always boots `app.super_admin_main:app`.
