## Deploy To Render

This app is now set up for a public deployment on Render.

## Security First

People who open your deployed site do **not** automatically get your source code.

Your code is exposed only if:

- your GitHub repo is public, or
- you explicitly give someone repo access

To keep testing private:

- keep the GitHub repo **private**
- set `SITE_ACCESS_USERNAME`
- set `SITE_ACCESS_PASSWORD`

With those two environment variables set, the whole site is protected by a login prompt.
Only `/healthz` stays public so Render can monitor the service.

### What you need

- A GitHub repo that contains this project
- A Render account
- Your `FOOTBALL_DATA_API_KEY`

### Option 1: Use the included blueprint

1. Push this project to GitHub.
2. In Render, choose `New` -> `Blueprint`.
3. Connect the repository.
4. Render will detect [render.yaml](C:/summas-bet/render.yaml).
5. Set the missing secret:
   - `FOOTBALL_DATA_API_KEY`
   - `SITE_ACCESS_USERNAME`
   - `SITE_ACCESS_PASSWORD`
6. Deploy.

This blueprint creates:
- one Python web service
- one managed Postgres database

The app health check is:
- `/healthz`

### Option 2: Create the service manually

If you prefer not to use the blueprint:

1. Create a new `Web Service`.
2. Connect the repo.
3. Use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Create a Render Postgres database.
5. Set environment variables:
   - `DATABASE_URL` = your Render Postgres connection string
   - `FOOTBALL_DATA_API_KEY` = your football-data token
   - `SITE_ACCESS_USERNAME` = your private test username
   - `SITE_ACCESS_PASSWORD` = your private test password

### Important notes

- Do not deploy the local SQLite file. The repo now ignores `football.db`.
- In production, use Postgres through `DATABASE_URL`.
- The app already creates tables on startup.
- Do not put secrets in the repo. Set them only in Render.

### After deploy

Open:
- `/`
- `/docs`
- `/status`

Then run a date:
- `POST /run?source=hybrid&date=YYYY-MM-DD`

### Best testing flow for your next month

1. Keep the repo private.
2. Deploy to Render.
3. Set:
   - `SITE_ACCESS_USERNAME`
   - `SITE_ACCESS_PASSWORD`
4. Share those only with people you trust.
5. After your testing month, replace the shared login with a proper user entry-code system.

### Custom domain

After the service is live on `*.onrender.com`, add your own domain from the Render dashboard under custom domains.
