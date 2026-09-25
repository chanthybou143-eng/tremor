# Reference copy of the WSGI config pasted into PythonAnywhere's
# /var/www/<username>_pythonanywhere_com_wsgi.py -- PythonAnywhere owns
# that file's actual path and scaffolds it with a placeholder Flask app
# (flask_app.py) by default, so this repo copy exists purely so the
# deployment config is tracked in git; it is not imported by anything
# in the tremor package itself. Replace <username> and <path-to-tremor>
# below with the real PythonAnywhere account username and the cloned
# repo's path before pasting.

import os
import sys

# --- storage / retention settings (read by tremor.webapp.create_app) ---------------------
# Where every ingested reading is stored permanently. Keep it in your HOME directory (never
# /tmp: it is wiped) and on PythonAnywhere's normal disk; the directory is created on first use.
os.environ.setdefault("TREMOR_DB_PATH", "/home/<username>/tremor_data/readings.db")
# Free accounts have a 512 MiB quota that counts EVERYTHING under your home directory. Pointing
# TREMOR_QUOTA_ROOT at it lets /api/health warn at 80% of the real quota (otherwise it only
# measures the database + exports).
os.environ.setdefault("TREMOR_QUOTA_ROOT", "/home/<username>")
os.environ.setdefault("TREMOR_QUOTA_MB", "512")
# Optional tuning (defaults shown): raw rows are pruned after this many days, once exported
# and verified. See CLAUDE.md "Server persistence".
# os.environ.setdefault("TREMOR_RAW_DAYS", "14")
# os.environ.setdefault("TREMOR_SQLITE_SYNCHRONOUS", "FULL")   # NORMAL only if latency demands it

# The venv created in the PythonAnywhere Bash console (see the "web" extra
# / requirements.txt install steps) is selected via the Web tab's
# "Virtualenv" field, not here -- this path only needs to make the tremor
# package importable.
project_home = "/home/<username>/<path-to-tremor>"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from tremor.webapp import create_app

# Real-hardware-only: no synthetic units, so the dashboard only shows units
# that have actually POSTed real readings via /api/ingest.
application = create_app(simulated_units=[])
