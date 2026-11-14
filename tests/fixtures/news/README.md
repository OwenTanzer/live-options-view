# News archive fixtures

**Every file here is SYNTHETIC test data, not real news or market data.** The headlines, links
and times are invented to exercise parsing, identity, revisions, timing and
cross-stream membership. Feed structure follows each source's real format, and
file names are the source ids in `market_data/news/config.py`.

| Path | Purpose |
|---|---|
| `feeds/` | Default `collect-once` input for the four enabled sources (OilPrice.com, EIA Today in Energy, TechCrunch, Ars Technica) |
| `parser/` | Atom, RSS 1.0 (RDF), malformed and entity-declaring feeds for the adapter tests |
| `scenarios/a_v1`, `a_v2` | Fixture A: published 10:00, collected 10:05, archived 10:06, revised 10:10 |
| `scenarios/b` | Fixture B: one EIA item that belongs to both the oil and ai_buildout streams |
| `scenarios/c` | M3 crash run (10:15): fixture A v2 plus new item C; the test crashes it after the manifest, before `commit.json` |
| `scenarios/d` | M3 recovery runs (10:25, 11:10): C plus one entry whose normalization the test makes fail |
| `prices/qqq_2026-11-09.synthetic.jsonl` | M3 price records, **invented QQQ prices, not market data**: underlying quotes, a late quote, a deliberate 10:09-10:20 gap, option quotes (a separate instrument), 1-minute and daily bars. Generated once by a script and committed as-is; the header record lists what it contains |

`tests/test_news_fixtures.py` pins the SHA-256 of every file, so any fixture
edit shows up in review.
