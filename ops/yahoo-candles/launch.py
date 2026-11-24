"""Load the two receipt-enabled modules, then run the existing hourly publisher."""
import os
from pathlib import Path
import runpy
import sys
root = Path.home() / ".local/share/optionsview-panel-publishers"
sys.path.insert(0, str(root / "app"))
from market_data import nasdaq_collector
nasdaq_collector.__path__.insert(0, str(Path(__file__).resolve().parent))
if sys.argv[1:] == ["--first-read"]:
    import requests
    from market_data.nasdaq_collector.candles import fetch_1m_bars, fetch_daily_bars
    with requests.Session() as session:
        for fetch in [fetch_1m_bars, fetch_daily_bars]:
            print(fetch.__name__, len(fetch("AAPL", session)["bars"]))
else:
    sys.argv = [str(root / "publish.py"), "nasdaq"]
    runpy.run_path(sys.argv[0], run_name="__main__")
