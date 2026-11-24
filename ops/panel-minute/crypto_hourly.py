"""Run the installed hourly job with this release's journal-aware publisher."""
import os
from pathlib import Path
import runpy
import sys

minute = Path.home() / ".local/share/optionsview-panel-minute"
hourly = minute.parent / "optionsview-panel-publishers"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from market_data.crypto_collector import publish  # pin before the host launcher sets sys.path

os.environ["OPTIONSVIEW_CRYPTO_SPOT_RECEIPTS"] = str(minute / "private/crypto-spot.sqlite3")
sys.argv = [str(hourly / "publish.py"), "crypto"]
runpy.run_path(sys.argv[0], run_name="__main__")
