# The Streamlit app

The app (`app/streamlit_app.py`) is the public live view of the analytics layer (plan 13, K14). Phase 2 ships the
Market page. It reads the `marts` schema only, with a database role that can do nothing else, and keeps what it read
for two hours, so visitors do not keep Neon's compute awake.

## 1. The read-only role

Create the role once, in the Neon console's SQL Editor (database `neondb`, as the owner role). Use a long random
password (Neon requires at least 60 bits of entropy; 24 random letters and digits are enough):

```sql
create role radar_reader with login password '<long random password>';
```

Create it with SQL, not on the console's Roles page: roles made there join `neon_superuser` and could read and write
everything. The role gets no other privilege here. After every build, dbt grants it `usage` on the `marts` schema and
`select` on its tables (the `grant_reader` hook in `dbt/dbt_project.yml`), so it can read the marts and nothing in
`raw`, `ref`, `ops`, `staging` or `core`. The grants arrive with the next nightly run, or at once with the `transform`
workflow (Actions, Run workflow).

The app's connection string is the owner's with the role and its password:

```text
postgresql://radar_reader:<password>@<host of NEON_DATABASE_URL>/neondb?sslmode=require
```

Power BI can use the same role: the dashboards read the marts too.

## 2. Streamlit Community Cloud

1. Sign in at <https://share.streamlit.io> with the GitHub account that owns the repository.
2. Create app, then deploy from GitHub: repository `pnidatunca/iphone-price-radar`, branch `main`, main file path
   `app/streamlit_app.py`. Pick the app URL.
3. Advanced settings: Python 3.12, and under Secrets:

   ```toml
   RADAR_READER_URL = "postgresql://radar_reader:<password>@<host>/neondb?sslmode=require"
   ```

4. Deploy. Community Cloud installs `app/requirements.txt` (the entry point's directory comes before the repository
   root, whose `uv.lock` holds the pipeline's dependencies). Every push to `main` redeploys the app.

The app falls asleep when nobody visits for a while; the next visit wakes it in a few seconds. A secret entered on
Community Cloud is never in the repository.

## Locally

```bash
RADAR_READER_URL="postgresql://..." uv run --group app streamlit run app/streamlit_app.py
```

Without the variable the page says that the data cannot be read. `tests/test_app.py` runs the page with synthetic
marts.
