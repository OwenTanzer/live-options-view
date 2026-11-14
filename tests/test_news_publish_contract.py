"""M5.2: the publisher refuses any generation the unchanged panel renderer cannot render.

validatePage (and its Python mirror page_render_problems) is shallow, so M5.1 published a malformed
index (KeyError after the index write) and pages whose nested shapes make renderPage throw. These tests
use the real renderer (tests/news_panel/render_probe.js runs site/js/news-panel.js unchanged) as well
as the validator. Local only: fixture feeds and temporary directory stores.
"""
import copy
import json
import re
import shutil
import subprocess
import unittest

from test_news_publish import BUILD_INDEX, INDEX, ROOT, qualified
from test_news_publish_repairs import P1, P2, Small, navigate
from market_data.news import publish as pub
from market_data.news.views import build_views, commit_index, encode, limits_from

PROBE = ROOT / "tests" / "news_panel" / "render_probe.js"
FIXTURE_VIEWS = ROOT / "tests" / "fixtures" / "news" / "views"
# Text the renderer only produces when it reads a field of the wrong shape.
PLACEHOLDERS = tuple(re.compile(p) for p in (r"undefined", r"NaN", r"\[object Object\]", r"\bnull\b"))


def probe(cases):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    out = subprocess.run([node, str(PROBE)], input=json.dumps({"cases": cases}), capture_output=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


def render_failed(result, baseline_text, *, mount=False) -> str | None:
    """Why the renderer could not render cleanly, or None if it did."""
    if result["threw"]:
        return "threw: " + result["threw"]
    if result["text"].startswith("Cannot render this page"):
        return "refused by validatePage"
    if mount and result["state"] != "ready":
        return f"panel state {result['state']}"
    for token in PLACEHOLDERS:
        if len(token.findall(result["text"])) > len(token.findall(baseline_text)):
            return f"renders {token.pattern!r}"
    return None


def generic_paths(value, path=()):
    """Every node path, visiting only the first element of each list (enough to cover each shape)."""
    yield path
    if isinstance(value, dict):
        for k, v in value.items():
            yield from generic_paths(v, path + (k,))
    elif isinstance(value, list) and value:
        yield from generic_paths(value[0], path + (0,))


def set_path(doc, path, new):
    doc = copy.deepcopy(doc)
    target = doc
    for p in path[:-1]:
        target = target[p]
    target[path[-1]] = new
    return doc


REPLACEMENTS = (None, {}, [], "x", -1, 1.5, True, [None], {"x": 1})


class ChejCases(Small):
    """The five rejections Chej reproduced on c6f9247, each with no publication writes."""

    def setUp(self):
        super().setUp()
        self.good = self.gen("2026-11-09T10:20:00Z", 2)
        self.old = self.gen("2026-11-09T10:10:00Z", 50)
        self.reset_public(self.old)
        pub.publish(self.private, self.public, self.old, self.clock)
        self.log.clear()

    def private_files(self):
        return {k: self.private.get(k) for k in self.private.list("")}

    def page_case(self, change):
        page = json.loads(self.good[P1])
        change(page)
        return dict(self.good, **{P1: encode(page)}), page

    def index_case(self, field):
        index = json.loads(self.good[BUILD_INDEX])
        del index[field]
        return dict(self.good, **{BUILD_INDEX: encode(index)}), index

    def assert_refused_without_writes(self, files, field):
        before = self.private_files()
        with self.assertRaises(pub.PublishError) as cm:
            pub.publish(self.private, self.public, files, self.clock)
        self.assertIn(field, str(cm.exception))
        self.assertEqual([e for e in self.log if e[0] in ("put", "delete")], [], "no public writes")
        self.assertEqual(self.private_files(), before, "no state, snapshot, receipt or lease writes")
        self.assertEqual(self.public_files(), qualified(self.old), "old publication byte-identical")
        self.assertEqual(pub.status(self.private, self.public, self.clock)["problems"], [], "still healthy")

    def test_index_without_complete_or_views_total_is_refused(self):
        for field in ("complete", "views_total"):
            with self.subTest(field):
                files, index = self.index_case(field)
                self.assert_refused_without_writes(files, f"index.{field} is missing")
                # The unchanged panel shows the broken index as an incomplete one with "undefined" counts.
                # The panel reads published (generation-qualified) files, so the probe uses those (G3).
                qgood = qualified(self.good)
                qindex = json.loads(qgood[INDEX])
                pages = {k: json.loads(b) for k, b in qgood.items() if k != INDEX}
                good = probe([{"kind": "mount", "index": qindex, "pages": pages}])[0]
                if field == "views_total":
                    broken = {k: v for k, v in qindex.items() if k != "views_total"}
                    bad = probe([{"kind": "mount", "index": dict(broken, complete=False), "pages": pages}])[0]
                    self.assertIn("undefined views omitted", bad["text"])
                self.assertIsNone(render_failed(good, good["text"], mount=True))

    def test_index_counts_that_disagree_are_refused(self):
        """Renderable but false: the panel would show a complete index as incomplete, or the reverse."""
        index = json.loads(self.good[BUILD_INDEX])
        self.assertTrue(index["complete"])
        cases = {
            "views_total": dict(index, views_total=index["views_total"] + 1),
            "complete (said incomplete, nothing omitted)": dict(index, complete=False),
            "complete (said complete, one omitted)": dict(index, views_omitted=1, views_total=index["views_total"] + 1),
            "complete (said complete, overflow reason)": dict(index, overflow_reasons=["index_views"]),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                self.assert_refused_without_writes(dict(self.good, **{BUILD_INDEX: encode(bad)}), name.split(" ")[0])

    def test_nested_shapes_the_renderer_reads_are_refused(self):
        cases = {
            "rows[0].instruments": lambda p: p["rows"][0].update(instruments=None),
            "coverage.segments[0].sources.attempted": lambda p: p["coverage"]["segments"][0].update(sources={}),
            "coverage.price.missing": lambda p: p["coverage"]["price"].update(missing={}),
        }
        baseline = json.loads(self.good[P1])
        self.assertTrue(baseline["rows"] and baseline["coverage"]["segments"][0]["sources"])
        for field, change in cases.items():
            with self.subTest(field):
                files, page = self.page_case(change)
                self.assertEqual(pub.page_render_problems(page), [], "validatePage's checks alone pass it")
                ok, bad = probe([{"kind": "page", "page": baseline}, {"kind": "page", "page": page}])
                self.assertIsNone(ok["threw"])
                self.assertIsNotNone(bad["threw"], f"the unchanged renderPage throws on {field}")
                self.assertIsNotNone(render_failed(bad, ok["text"]))
                self.assert_refused_without_writes(files, f"page.{field}")

    def test_valid_generation_recovers_after_the_rejections(self):
        for files, _ in (self.index_case("complete"), self.index_case("views_total"),
                         self.page_case(lambda p: p["rows"][0].update(instruments=None))):
            with self.assertRaises(pub.PublishError):
                pub.publish(self.private, self.public, files, self.clock)
        report = pub.publish(self.private, self.public, self.good, self.clock)
        self.assertIn(INDEX, report.written)
        self.assertEqual(self.public_files(), qualified(self.good))
        navigate(self.raw_public)
        st = pub.status(self.private, self.public, self.clock)
        self.assertEqual(st["problems"], [])
        self.assertEqual(st["committed"]["views_total"], json.loads(self.good[BUILD_INDEX])["views_total"])
        # Every published page and the whole panel render through the unchanged renderer.
        published = {k: json.loads(b) for k, b in self.public_files().items()}
        results = probe([{"kind": "page", "page": p} for k, p in published.items() if k != INDEX] +
                        [{"kind": "mount", "index": published[INDEX], "pages": published}])
        for r in results[:-1]:
            self.assertIsNone(render_failed(r, ""), r["text"][:200])
        self.assertIsNone(render_failed(results[-1], "", mount=True))


class RendererSweep(Small):
    """Every field the renderer reads, set to each wrong shape: if the real renderer cannot render it
    cleanly, the publisher's validator must refuse it. Valid builder output must pass and render."""

    def setUp(self):
        super().setUp()
        self.good = self.gen("2026-11-09T10:20:00Z", 2)

    def sources(self):
        pages = [json.loads(b) for k, b in self.good.items() if k != BUILD_INDEX]
        pages += [json.loads(p.read_bytes()) for p in FIXTURE_VIEWS.rglob("page-*.json")]
        return pages

    def test_every_renderer_failure_is_refused(self):
        seen, cases, meta = set(), [], []
        for page in self.sources():
            self.assertEqual(pub.page_contract_problems(page), [])
            for path in generic_paths(page):
                if not path:
                    continue
                generic = tuple("[]" if isinstance(p, int) else p for p in path)
                if generic in seen:
                    continue
                seen.add(generic)
                cases.append({"kind": "page", "page": page})
                meta.append((path, None, page))
                for new in REPLACEMENTS:
                    cases.append({"kind": "page", "page": set_path(page, path, new)})
                    meta.append((path, new, page))
        results = probe(cases)
        missed, broken = [], 0
        clean = {}   # each mutation is compared with the clean render of the page it came from
        for (path, new, page), case, result in zip(meta, cases, results):
            if case["page"] is page:
                self.assertIsNone(render_failed(result, result["text"]), f"valid page fails: {path}")
                clean[id(page)] = result["text"]
                continue
            why = render_failed(result, clean[id(page)])
            if why:
                broken += 1
                if not pub.page_contract_problems(case["page"]):
                    missed.append((".".join(map(str, path)), new, why))
        print(f"page sweep: {len(cases)} renders, {broken} renderer failures, {len(missed)} accepted", flush=True)
        self.assertGreater(broken, 100)
        self.assertEqual(missed, [], "renderer failures the validator accepted")

    def test_every_index_failure_is_refused(self):
        # G3: the panel mounts published indexes (gen, pages_sha256, qualified keys); refusals are checked
        # with validate_qualified, which also applies every validate_generation check.
        from market_data.news.views import generation_name
        qgood = qualified(self.good)
        gen = generation_name(self.good) + ".1"            # G6: the incarnation id the published index carries
        index = json.loads(qgood[INDEX])
        pages = {k: json.loads(b) for k, b in qgood.items() if k != INDEX}
        self.assertEqual(pub.index_contract_problems(index, pub.PUBLISHED_INDEX_SCHEMA), [])
        mutations = [(path, new, set_path(index, path, new))
                     for path in generic_paths(index) if path for new in REPLACEMENTS]
        results = probe([{"kind": "mount", "index": index, "pages": pages}] +
                        [{"kind": "mount", "index": m, "pages": pages} for _, _, m in mutations])
        clean = results[0]
        self.assertIsNone(render_failed(clean, clean["text"], mount=True))
        missed, broken, commit_refused = [], 0, 0
        for (path, new, mutated), result in zip(mutations, results[1:]):
            why = render_failed(result, clean["text"], mount=True)
            if path[0] == "commit":
                # G7: the commit nonce is not builder input. commit_index writes a fresh 128-bit nonce into
                # each index write, so the publisher never sends a malformed one; the reader must refuse
                # one anyway (fail closed).
                self.assertTrue(why, f"the panel mounted an index with commit {'.'.join(map(str, path))}={new!r}")
                commit_refused += 1
                continue
            if why:
                broken += 1
                files = dict(qgood, **{INDEX: encode(mutated)})
                try:
                    pub.validate_qualified(gen, files)
                except pub.PublishError:
                    continue
                missed.append((".".join(map(str, path)), new, why))
        print(f"index sweep: {len(mutations)} mutations, {broken} renderer failures, {len(missed)} accepted, "
              f"{commit_refused} commit mutations refused by the panel", flush=True)
        self.assertGreater(broken, 10)
        self.assertGreater(commit_refused, 0)
        nonce = json.loads(commit_index(qgood[INDEX], "a" * 32))["commit"]["nonce"]
        self.assertEqual(nonce, "a" * 32, "commit_index writes the nonce it is given")
        self.assertEqual(missed, [], "index failures the publisher accepted")

    def test_overflowing_generations_pass_and_render(self):
        """Views the builder marks incomplete (rows, segments and source ids over their limits) still publish."""
        files = build_views(self.archive, pub.unavailable_prices(), as_of="2026-11-09T10:20:00Z",
                            tolerance_s=120, data_label="PILOT news: prices unavailable (no price input connected)",
                            dates=["2026-11-09"],
                            limits=limits_from(rows_per_page=1, pages_per_view=2, segments_per_page=1,
                                               source_ids_per_segment=1))
        index = pub.validate_generation(files)
        self.assertTrue(any(not v["complete"] for v in index["views"]))
        self.assertTrue(any(not json.loads(b)["complete"] for k, b in files.items() if k != BUILD_INDEX))
        parsed = {k: json.loads(b) for k, b in qualified(files).items()}
        results = probe([{"kind": "page", "page": p} for k, p in parsed.items() if k != INDEX] +
                        [{"kind": "mount", "index": parsed[INDEX], "pages": parsed}])
        for r in results[:-1]:
            self.assertIsNone(render_failed(r, ""), r["text"][:200])
        self.assertEqual(results[-1]["state"], "ready")
        self.assertIn("INCOMPLETE", results[-1]["text"])
        pub.publish(self.private, self.public, files, self.clock)
        self.assertEqual(self.public_files(), qualified(files))


if __name__ == "__main__":
    unittest.main()
