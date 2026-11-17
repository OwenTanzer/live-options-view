"""OA-241 findings features: per-contract snapshot columns follow explicit contract keys
and are identical across Python hash seeds (Chej PR270 review, item 1)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ["QQQ260925C00744000", "QQQ260925C00745000", "QQQ260925P00744000", "QQQ260925P00745000"]
VALUES = {s: (0.01 * (i + 1), 0.1 * (i + 1), 100 * (i + 1)) for i, s in enumerate(SYMBOLS)}  # gamma, delta, OI
PROBE = """
import json, sys
sys.path.insert(0, {features!r})
import features
from pathlib import Path
snaps, digest = features.snapshots(Path({root!r}), "2026-09-25", {symbols!r})
print(json.dumps([[s[1], s[2], s[3], s[4]] for s in snaps] + [digest]))
"""


class FeatureAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name) / "intraday" / "20260925"
        folder.mkdir(parents=True)
        header = "OptionSymbol,Strike,Type,OpenInterest,Gamma,Delta,UnderlyingPrice"
        # Rows deliberately out of symbol order, with an unrelated contract mixed in.
        order = [SYMBOLS[3], "QQQ260925C00799000", SYMBOLS[0], SYMBOLS[2], SYMBOLS[1]]
        lines = [header] + [f"{s},0,x,{VALUES.get(s, (9, 9, 999))[2]},{VALUES.get(s, (9, 9, 9))[0]},"
                            f"{VALUES.get(s, (9, 9, 9))[1]},744.5" for s in order]
        (folder / "snapshot_100000000000.csv").write_text("\n".join(lines) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, seed):
        code = PROBE.format(features=str(ROOT / "research/oa241/findings"), root=self.tmp.name, symbols=SYMBOLS)
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_columns_follow_explicit_contract_keys(self):
        result = self.probe(0)
        gamma, delta, oi, spot = result[0]
        self.assertEqual(gamma, [VALUES[s][0] for s in SYMBOLS])
        self.assertEqual(delta, [VALUES[s][1] for s in SYMBOLS])
        self.assertEqual(oi, [VALUES[s][2] for s in SYMBOLS])
        self.assertEqual(spot, 744.5)

    def test_identical_across_hash_seeds(self):
        self.assertEqual(self.probe(1), self.probe(2))
        self.assertEqual(self.probe(1), self.probe(31337))

    def test_unordered_symbol_set_is_refused(self):
        sys.path.insert(0, str(ROOT / "research/oa241/findings"))
        import features
        with self.assertRaises(AssertionError):
            features.snapshots(Path(self.tmp.name), "2026-09-25", set(SYMBOLS))


if __name__ == "__main__":
    unittest.main()
