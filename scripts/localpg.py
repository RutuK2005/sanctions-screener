"""Run a throwaway PostgreSQL 16 server without Docker or an installer.

I don't have Docker on my dev machine, so this script drives the portable
EnterpriseDB Windows binaries (or any `bin/` dir containing initdb +
pg_ctl, e.g. a Linux/macOS install) from a git-ignored `.pg/` folder.

    python scripts/localpg.py download   # Windows only: fetch + unzip EDB binaries into .pg/
    python scripts/localpg.py init       # initdb into .pg/data (trust auth, localhost only)
    python scripts/localpg.py start      # start on 127.0.0.1:54329, create screener, screener_test, screener_eval
    python scripts/localpg.py stop
    python scripts/localpg.py status

pg_trgm ships in the EDB "contrib" set, so `CREATE EXTENSION pg_trgm` works.
Override the binary dir with PG_BIN=/path/to/bin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PG_HOME = ROOT / ".pg"
DATA = PG_HOME / "data"
LOG = PG_HOME / "postgres.log"
PORT = int(os.environ.get("PG_PORT", "54329"))
EDB_VERSION = "16.13-1"
EDB_URL = f"https://get.enterprisedb.com/postgresql/postgresql-{EDB_VERSION}-windows-x64-binaries.zip"
DATABASES = ("screener", "screener_test", "screener_eval")


def bin_dir() -> Path:
    env = os.environ.get("PG_BIN")
    return Path(env) if env else PG_HOME / "pgsql" / "bin"


def exe(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    path = bin_dir() / f"{name}{suffix}"
    if not path.exists():
        sys.exit(f"{path} not found - run `python scripts/localpg.py download` or set PG_BIN")
    return str(path)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def download() -> None:
    if (PG_HOME / "pgsql" / "bin").exists():
        print("binaries already present")
        return
    PG_HOME.mkdir(exist_ok=True)
    zpath = PG_HOME / "pg.zip"
    print(f"downloading {EDB_URL} (~300 MB)...")
    urllib.request.urlretrieve(EDB_URL, zpath)
    with zipfile.ZipFile(zpath) as zf:
        skip = ("pgsql/pgAdmin 4/", "pgsql/StackBuilder/", "pgsql/doc/")
        members = [m for m in zf.namelist() if not m.startswith(skip)]
        zf.extractall(PG_HOME, members)
    zpath.unlink()
    print("extracted to", PG_HOME / "pgsql")


def init() -> None:
    if (DATA / "PG_VERSION").exists():
        print("cluster already initialised at", DATA)
        return
    DATA.parent.mkdir(exist_ok=True)
    run(exe("initdb"), "-D", str(DATA), "-U", "postgres", "--auth=trust", "--encoding=UTF8", "--locale=C")
    print("initialised", DATA)


def is_running() -> bool:
    return run(exe("pg_ctl"), "status", "-D", str(DATA), check=False).returncode == 0


def start() -> None:
    init()
    if not is_running():
        opts = f"-p {PORT} -c listen_addresses=127.0.0.1"
        # Don't capture output here: the postmaster inherits the pipe handles and
        # keeps them open, so capture_output would block forever (esp. on Windows).
        subprocess.run([exe("pg_ctl"), "start", "-D", str(DATA), "-l", str(LOG), "-w", "-o", opts],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for db in DATABASES:
        res = run(exe("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", "-tAc",
                  f"SELECT 1 FROM pg_database WHERE datname='{db}'")
        if res.stdout.strip() != "1":
            run(exe("createdb"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", db)
    print(f"postgres running on 127.0.0.1:{PORT} (dbs: {', '.join(DATABASES)})")
    print(f"DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:{PORT}/screener")


def stop() -> None:
    if is_running():
        run(exe("pg_ctl"), "stop", "-D", str(DATA), "-m", "fast", "-w")
    print("stopped")


def status() -> None:
    print("running" if is_running() else "not running")


def destroy() -> None:
    stop()
    shutil.rmtree(DATA, ignore_errors=True)
    print("removed", DATA)


if __name__ == "__main__":
    commands = {"download": download, "init": init, "start": start, "stop": stop, "status": status, "destroy": destroy}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: localpg.py {{{'|'.join(commands)}}}")
    commands[sys.argv[1]]()
