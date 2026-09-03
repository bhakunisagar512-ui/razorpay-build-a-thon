"""Idempotent SQL migration runner: applies db/migrations/*.sql in order, once each."""
import pathlib, sys
import psycopg
from services.app.settings import settings

def main() -> None:
    mig_dir = pathlib.Path(__file__).resolve().parents[2] / "db" / "migrations"
    files = sorted(mig_dir.glob("*.sql"))
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in files:
            if f.name in done:
                print(f"skip  {f.name}"); continue
            print(f"apply {f.name}")
            conn.execute(f.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
        conn.commit()
    print("migrations complete")

if __name__ == "__main__":
    sys.exit(main())
