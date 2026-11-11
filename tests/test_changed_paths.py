"""Executable changed-path-to-service contract for #225.

Run directly (``python tests/test_changed_paths.py``) or through unittest
discovery. Every expectation below is a fact about the checked-out commit or a
dated provider baseline; none of it is a claim about what a provider currently
has enabled.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import changed_paths as cp  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "RELEASE_BOUNDARIES.md"

TRIGGERS = cp.workflow_triggers(REPO)


class LaneExpectation(unittest.TestCase):
    """Repository workflow lanes, one case per kind of ordinary change."""

    def check(self, changed, lanes):
        result = cp.effects(changed, REPO, TRIGGERS)
        self.assertEqual(
            set(result.triggered),
            set(lanes),
            msg=f"{changed} -> {result.describe()}",
        )
        return result

    def test_site_only_change_promotes_the_worker(self):
        self.check(["site/index.html"], {"cloudflare_deploy", "web_ci"})

    def test_backend_only_change_promotes_the_worker(self):
        self.check(["backend/index.js"], {"cloudflare_deploy", "web_ci"})

    def test_wrangler_configuration_change_promotes_the_worker(self):
        self.check(["wrangler.toml"], {"cloudflare_deploy", "web_ci"})

    def test_application_test_change_runs_ci_without_promoting(self):
        self.check(["tests/worker_logic.test.js"], {"web_ci"})

    def test_build_stamp_test_is_the_one_test_that_promotes(self):
        result = self.check(["tests/build_stamp.test.mjs"], {"cloudflare_deploy", "web_ci"})
        self.assertEqual(result.auto_promoting, ())

    def test_root_documentation_runs_nothing(self):
        self.check(["docs/RELEASE_BOUNDARIES.md"], set())
        self.check(["README.md"], set())
        self.check(["docs/specs/ADAPTIVE_QUOTE_PANEL.md"], set())

    def test_deploy_workflow_edit_promotes_itself(self):
        self.check([".github/workflows/deploy.yml"], {"cloudflare_deploy", "web_ci"})

    def test_web_ci_workflow_edit_does_not_promote(self):
        self.check([".github/workflows/web-ci.yml"], {"web_ci"})

    def test_producer_ci_workflow_edit_stays_in_its_lane(self):
        self.check([".github/workflows/collector-ci.yml"], {"collector_ci"})
        self.check([".github/workflows/crassus-ci.yml"], {"crassus_ci"})
        self.check([".github/workflows/synthetic-days-ci.yml"], {"synthetic_ci"})

    def test_research_that_collector_ci_names(self):
        self.check(["research/archive_qualification.py"], {"collector_ci"})

    def test_synthetic_days_only_touches_its_own_lane(self):
        self.check(["research/synthetic_days/day_generator.py"], {"synthetic_ci"})

    def test_every_producer_package_still_runs_the_producer_suite(self):
        # collector-ci's own filter is a broad market_data/** plus named files.
        # Narrowing it to one package would leave the others tested nowhere.
        for path in (
            "market_data/options_scanner/active.py",
            "market_data/options_scanner/windows_publish.py",
            "market_data/tradier/probe.py",
            "market_data/diagnostics.py",
            "deployment/python/requirements.txt",
            "deployment/python/Dockerfile",
        ):
            with self.subTest(path=path):
                self.check([path], {"collector_ci"})


class ProducerExpectation(unittest.TestCase):
    """Which producer services a change really reaches, and how."""

    def check(self, changed, *, auto, manual):
        result = cp.effects(changed, REPO, TRIGGERS)
        self.assertEqual(
            set(result.auto_promoting), set(auto), msg=f"{changed} -> {result.describe()}"
        )
        self.assertEqual(
            set(result.requiring_promotion),
            set(manual),
            msg=f"{changed} -> {result.describe()}",
        )
        return result

    def test_collector_code_autopromotes_the_main_collector(self):
        self.check(["market_data/live_market/state.py"], auto=["main-collector"], manual=[])

    def test_prose_inside_the_collector_scope_still_restarts_it(self):
        # Documented disposition, not a fix: narrowing this needs a provider
        # watch-pattern change, which is Nova's surface, not a repository edit.
        self.check(["market_data/live_market/README.md"], auto=["main-collector"], manual=[])

    def test_crassus_code_autopromotes_the_runner(self):
        self.check(["crassus/runner.py"], auto=["crassus-runner"], manual=[])

    def test_crassus_prose_also_restarts_the_runner(self):
        self.check(["crassus/README.md"], auto=["crassus-runner"], manual=[])

    def test_banana_code_needs_a_deliberate_promotion(self):
        self.check(
            ["market_data/options_scanner/active.py"],
            auto=[],
            manual=["banana-options-scanner"],
        )

    def test_banana_module_reached_through_windows_code(self):
        self.check(
            ["market_data/options_scanner/windows_combine.py"],
            auto=[],
            manual=["banana-options-scanner"],
        )

    def test_tradier_probe_reaches_two_producers_and_neither_promotes(self):
        self.check(
            ["market_data/tradier/probe.py"],
            auto=[],
            manual=["tradier-collector", "banana-options-scanner"],
        )

    def test_diagnostics_reaches_two_producers(self):
        self.check(
            ["market_data/diagnostics.py"],
            auto=[],
            manual=["tradier-collector", "banana-options-scanner"],
        )

    def test_shared_packaging_names_all_three_python_producers(self):
        expected = {"auto": ["main-collector"], "manual": ["tradier-collector", "banana-options-scanner"]}
        for path in (
            "deployment/python/requirements.txt",
            "deployment/python/Dockerfile",
            "deployment/python/Dockerfile.dockerignore",
            "market_data/__init__.py",
        ):
            with self.subTest(path=path):
                self.check([path], **expected)

    def test_another_producers_code_is_not_a_reason_to_restart_this_one(self):
        result = cp.effects(["market_data/options_scanner/active.py"], REPO, TRIGGERS)
        # The file is copied into every producer image, so the digest moves for
        # all of them, but only Banana executes it.
        for name in ("main-collector", "tradier-collector"):
            with self.subTest(service=name):
                self.assertTrue(result.services[name].image_input)
                self.assertFalse(result.services[name].runtime)
                self.assertNotIn(name, result.requiring_promotion)


class MixedExpectation(unittest.TestCase):
    """A commit takes the union of the effects of its files."""

    def test_site_plus_collector_code(self):
        result = cp.effects(
            ["site/index.html", "market_data/live_market/state.py"], REPO, TRIGGERS
        )
        self.assertEqual(
            set(result.triggered), {"cloudflare_deploy", "web_ci", "collector_ci"}
        )
        self.assertEqual(set(result.auto_promoting), {"main-collector"})
        self.assertEqual(set(result.requiring_promotion), set())

    def test_site_plus_banana_code_does_not_quietly_promote_banana(self):
        result = cp.effects(
            ["site/index.html", "market_data/options_scanner/active.py"], REPO, TRIGGERS
        )
        self.assertIn("cloudflare_deploy", result.triggered)
        self.assertEqual(set(result.auto_promoting), set())
        self.assertEqual(set(result.requiring_promotion), {"banana-options-scanner"})

    def test_site_plus_crassus(self):
        result = cp.effects(["site/index.html", "crassus/runner.py"], REPO, TRIGGERS)
        self.assertEqual(
            set(result.triggered), {"cloudflare_deploy", "web_ci", "crassus_ci"}
        )
        self.assertEqual(set(result.auto_promoting), {"crassus-runner"})

    def test_docs_do_not_ride_along(self):
        result = cp.effects(
            ["docs/README.md", "research/synthetic_days/day_generator.py"], REPO, TRIGGERS
        )
        self.assertEqual(set(result.triggered), {"synthetic_ci"})
        self.assertEqual(set(result.auto_promoting), set())


class PromotionOrdering(unittest.TestCase):
    """#225's CI-ordering clause, as a check rather than a reading."""

    def test_everything_that_can_promote_is_also_tested(self):
        problems = cp.promotion_coverage_problems(REPO, TRIGGERS)
        self.assertEqual(problems, [], msg="\n".join(problems))

    def test_promotion_lane_has_no_serialisation_yet(self):
        """Locks known state so a fix forces this record to be revisited.

        deploy.yml has no `concurrency` group and no `needs:` edge, so two
        pushes can promote out of order and neither waits on web-ci. If this
        fails, the workflow gained serialisation: update
        docs/RELEASE_BOUNDARIES.md and the #225 finding register instead of
        deleting the test.
        """
        text = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertNotIn("concurrency:", text)
        self.assertNotIn("needs:", text)
        self.assertIn("concurrency", DOC.read_text(encoding="utf-8"))

    def test_cloudflare_builds_cannot_silently_become_a_second_lane(self):
        """The provider-native build path is read-only and manual by design."""
        parsed = TRIGGERS["cloudflare-builds-readonly.yml"]
        self.assertEqual(set(parsed), {"workflow_dispatch"})
        for workflow in ("publish-market-mcp-f2.yml", "publish-market-mcp-pr133.yml"):
            with self.subTest(workflow=workflow):
                triggers = TRIGGERS[workflow]
                self.assertNotIn("push", triggers)


class DependencyClosure(unittest.TestCase):
    """#225: validate the closure before proposing to restore a trigger."""

    def test_provided_watch_scopes_cover_their_own_entry_point(self):
        for service in cp.PROVIDER_SERVICES:
            if not service.watch:
                continue
            with self.subTest(service=service.name):
                outside = cp.closure_outside_scope(
                    REPO, service.entry_module, service.watch, service.module_root
                )
                self.assertEqual(outside, [])

    def test_banana_candidate_scope_covers_its_closure(self):
        service = next(s for s in cp.PROVIDER_SERVICES if s.name == "banana-options-scanner")
        outside = cp.closure_outside_scope(
            REPO, service.entry_module, cp.BANANA_CANDIDATE_SCOPE, service.module_root
        )
        self.assertEqual(outside, [])

    def test_banana_scanner_only_scope_is_provably_incomplete(self):
        service = next(s for s in cp.PROVIDER_SERVICES if s.name == "banana-options-scanner")
        outside = cp.closure_outside_scope(
            REPO, service.entry_module, cp.BANANA_SCANNER_ONLY_SCOPE, service.module_root
        )
        self.assertEqual(
            outside,
            [
                "market_data/__init__.py",
                "market_data/diagnostics.py",
                "market_data/tradier/__init__.py",
                "market_data/tradier/probe.py",
            ],
        )

    def test_banana_shares_the_tradier_probe_module(self):
        service = next(s for s in cp.PROVIDER_SERVICES if s.name == "banana-options-scanner")
        closure = cp.import_closure(REPO, service.entry_module, service.module_root)
        self.assertIn("market_data/tradier/probe.py", closure)
        self.assertIn("market_data/diagnostics.py", closure)

    def test_closure_of_a_missing_entry_point_is_empty(self):
        self.assertEqual(cp.import_closure(REPO, "market_data.nothing_here"), ())


class ImageInputs(unittest.TestCase):
    """Build inputs are not promotions; the distinction is the point."""

    def test_producer_dockerfile_copies_market_data(self):
        image = cp.image_inputs(str(REPO), cp.PRODUCER_DOCKERFILE)
        self.assertTrue(image.contains("market_data/options_scanner/active.py"))
        self.assertTrue(image.contains("deployment/python/requirements.txt"))
        self.assertFalse(image.contains("site/index.html"))
        self.assertFalse(image.contains("backend/index.js"))

    def test_producer_dockerfile_excludes_local_environment_files(self):
        image = cp.image_inputs(str(REPO), cp.PRODUCER_DOCKERFILE)
        self.assertFalse(image.contains("market_data/.env"))
        self.assertFalse(image.contains("market_data/live_market/__pycache__/x.pyc"))

    def test_crassus_build_context_is_the_subdirectory(self):
        image = cp.image_inputs(str(REPO), "crassus/Dockerfile", "crassus")
        self.assertTrue(image.contains("crassus/runner.py"))
        self.assertFalse(image.contains("market_data/live_market/state.py"))


class PatternSemantics(unittest.TestCase):
    def test_double_star_requires_the_separator(self):
        self.assertFalse(cp.matches("site/**", "site"))
        self.assertTrue(cp.matches("site/**", "site/index.html"))
        self.assertTrue(cp.matches("site/**", "site/a/b/c.js"))

    def test_single_star_stays_inside_one_segment(self):
        self.assertTrue(cp.matches("market_data/*.py", "market_data/__init__.py"))
        self.assertFalse(cp.matches("market_data/*.py", "market_data/live_market/state.py"))

    def test_embedded_double_star(self):
        self.assertTrue(cp.matches("**/workflows/deploy.yml", ".github/workflows/deploy.yml"))

    def test_exact_paths(self):
        self.assertTrue(cp.matches("wrangler.toml", "wrangler.toml"))
        self.assertFalse(cp.matches("wrangler.toml", "site/wrangler.toml"))

    def test_scope_reads_a_bare_directory_as_a_subtree(self):
        self.assertTrue(cp.scope_matches("market_data/tradier", "market_data/tradier/probe.py"))


class WorkflowParsing(unittest.TestCase):
    def test_reads_push_and_pull_request_filters(self):
        parsed = cp.parse_workflow_triggers(
            "on:\n"
            "  push:\n"
            "    branches: [master]\n"
            "    paths:\n"
            '      - "site/**"\n'
            "  pull_request:\n"
            "    paths:\n"
            '      - "backend/**"\n'
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - run: echo 'site/**'\n"
        )
        self.assertEqual(parsed["push"]["branches"], ["master"])
        self.assertEqual(parsed["push"]["paths"], ["site/**"])
        self.assertEqual(parsed["pull_request"]["paths"], ["backend/**"])
        self.assertNotIn("paths", parsed.get("jobs", {}))

    def test_refuses_to_ignore_what_it_does_not_model(self):
        with self.assertRaises(cp.UnsupportedWorkflow):
            cp.parse_workflow_triggers("on:\n  push:\n    paths-ignore:\n      - 'docs/**'\n")

    def test_every_workflow_in_the_repository_parses_or_is_known(self):
        for name, parsed in TRIGGERS.items():
            with self.subTest(workflow=name):
                self.assertIsInstance(parsed, dict)

    def test_unmodelled_dockerfile_form_fails_loudly(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text(
                "FROM python:3.12-slim\nADD https://example.invalid/x.tar /x.tar\n",
                encoding="utf-8",
            )
            with self.assertRaises(cp.UnsupportedWorkflow):
                cp.image_inputs(str(root), "Dockerfile")


class AssetApiSplit(unittest.TestCase):
    """G1's contract: which artifact a change belongs to.

    `docs/ASSET_BOUNDARY.md` in Musef's worktree (branch
    `musef/225-assets-preview`, base `90cdc56`, uncommitted when read) proposes
    one public origin serving a static gateway that forwards the API paths to a
    separately bound service. The topology is not applied and no provider fact
    here is verified; what is testable is the classification.
    """

    def view(self, changed):
        return cp.effects(changed, REPO, TRIGGERS).artifact

    def test_site_assets_belong_to_the_asset_artifact(self):
        view = self.view(["site/index.html"])
        self.assertTrue(view.asset)
        self.assertFalse(view.api)
        self.assertFalse(view.prose)

    def test_site_prose_is_classified_as_prose_not_asset(self):
        view = self.view(["site/DEPLOYMENTS.md"])
        self.assertTrue(view.prose)
        self.assertFalse(
            view.asset,
            "a Markdown file inside site/ is a document, not a shipped asset",
        )
        # The current combined workflow still fires on it; that coupling is the
        # thing the split removes, and the test states both halves.
        self.assertIn("cloudflare_deploy", cp.effects(["site/DEPLOYMENTS.md"], REPO, TRIGGERS).triggered)

    def test_gateway_and_build_script_belong_to_the_asset_artifact(self):
        for path in (
            "frontend/worker.mjs",
            "frontend/preview-policy.mjs",
            "scripts/build-assets.mjs",
            "scripts/prepare-asset-release.mjs",
            "scripts/stamp-build.mjs",
        ):
            with self.subTest(path=path):
                view = self.view([path])
                self.assertTrue(view.asset)
                self.assertFalse(view.api)

    def test_frontend_prose_is_prose(self):
        view = self.view(["frontend/README.md"])
        self.assertTrue(view.prose)
        self.assertFalse(view.asset)

    def test_backend_belongs_to_the_api_artifact_only(self):
        for path in ("backend/index.js", "backend/execution.js", "backend/bots.js"):
            with self.subTest(path=path):
                view = self.view([path])
                self.assertTrue(view.api)
                self.assertFalse(view.asset, "the asset artifact excludes backend code")
                self.assertFalse(view.prose)

    def test_shared_configuration_is_a_reviewed_mixed_change(self):
        for path in (
            "wrangler.toml",
            "wrangler.recovery.toml",
            "wrangler.assets.toml",
            "wrangler.api.toml",
            ".github/workflows/deploy.yml",
        ):
            with self.subTest(path=path):
                view = self.view([path])
                self.assertTrue(view.shared)
                self.assertTrue(view.needs_integration_review)
                self.assertFalse(view.asset)
                self.assertFalse(view.api)

    def test_a_commit_spanning_both_artifacts_needs_review(self):
        for changed in (
            ["site/index.html", "backend/index.js"],
            ["frontend/worker.mjs", "backend/index.js"],
        ):
            with self.subTest(changed=changed):
                view = self.view(changed)
                self.assertTrue(view.asset)
                self.assertTrue(view.api)
                self.assertTrue(view.mixed)
                self.assertTrue(view.needs_integration_review)

    def test_tests_docs_and_producers_are_not_artifacts(self):
        for path in (
            "tests/asset_boundary.test.mjs",
            "docs/ASSET_BOUNDARY.md",
            "market_data/live_market/state.py",
            "crassus/runner.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.view([path]).describe(), "none", f"{path} must not be an artifact input"
                )

    def test_preview_is_stale_for_asset_and_config_but_not_for_api_or_docs(self):
        stale = {
            "site/index.html": True,
            "frontend/worker.mjs": True,
            "scripts/build-assets.mjs": True,
            "scripts/prepare-asset-release.mjs": True,
            "scripts/stamp-build.mjs": True,
            "wrangler.toml": True,
            "wrangler.assets.toml": True,
            "backend/index.js": False,
            "tests/worker_logic.test.js": False,
            "docs/README.md": False,
            "site/README.md": False,
        }
        for path, expected in stale.items():
            with self.subTest(path=path):
                self.assertEqual(cp.effects([path], REPO, TRIGGERS).preview_stale, expected)

    def test_preview_contract_is_named_once(self):
        contract = cp.PREVIEW_CONTRACT
        self.assertEqual(contract["ENVIRONMENT"], "preview")
        self.assertEqual(contract["ISOLATION"], "disposable")
        self.assertIn("/r2-proxy/", contract["denied_routes"])

    def test_every_existing_input_is_also_tested(self):
        problems = cp.input_coverage_problems(REPO, TRIGGERS)
        self.assertEqual(problems, [], msg="\n".join(problems))


class PreviewAcceptance(unittest.TestCase):
    """Two different kinds of staleness, and the case-sensitivity of the packager."""

    def view(self, changed):
        return cp.effects(changed, REPO, TRIGGERS)

    def test_real_handler_changes_stale_acceptance_without_becoming_payload(self):
        for path in ("backend/index.js", "backend/execution.js"):
            with self.subTest(path=path):
                result = self.view([path])
                self.assertTrue(result.artifact.api)
                self.assertTrue(result.artifact.acceptance)
                self.assertFalse(result.artifact.payload)
                self.assertFalse(result.preview_stale, "preview bytes are unchanged")
                self.assertTrue(result.acceptance_evidence_stale)

    def test_preview_constructor_helpers_and_suites_stale_acceptance(self):
        for path in (
            "scripts/preview-local.mjs",
            "frontend/preview-policy.mjs",
            "tests/helpers/test_env.mjs",
            "tests/preview_isolation.test.mjs",
            "tests/preview_storage_boundary.test.mjs",
        ):
            with self.subTest(path=path):
                result = self.view([path])
                self.assertTrue(result.artifact.acceptance, f"{path} gates acceptance evidence")
                self.assertTrue(result.acceptance_evidence_stale)

    def test_asset_and_migration_changes_stale_acceptance_too(self):
        for path in (
            "site/index.html",
            "backend/migrations/0001_accounts_sessions.sql",
            "wrangler.assets.toml",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.view([path]).acceptance_evidence_stale)

    def test_unrelated_documents_leave_acceptance_alone(self):
        for path in ("docs/README.md", "README.md", "site/plans/2026-07-vwap-rvol.md"):
            with self.subTest(path=path):
                self.assertFalse(self.view([path]).acceptance_evidence_stale)

    def test_either_selected_config_makes_recorded_config_evidence_stale(self):
        for path in cp.SELECTED_CONFIG_INPUTS:
            with self.subTest(path=path):
                result = self.view([path])
                self.assertTrue(result.artifact.config, f"{path} can be the staged config")
                self.assertTrue(result.config_evidence_stale)
                self.assertTrue(result.artifact.shared, "a config change is reviewed")
                self.assertFalse(result.artifact.payload)

    def test_a_payload_change_does_not_touch_config_evidence(self):
        result = self.view(["site/index.html"])
        self.assertFalse(result.config_evidence_stale)
        self.assertTrue(result.needs_asset_rebuild)

    def test_extension_matching_is_case_sensitive_like_the_packager(self):
        self.assertFalse(
            cp.artifact_view(["site/UPPER.JS"], REPO).payload,
            "the packager compares extname() case-sensitively, so this file does not ship",
        )
        self.assertFalse(cp.artifact_view(["site/index.HTML"], REPO).payload)
        self.assertTrue(cp.artifact_view(["site/index.html"], REPO).payload)

    def test_the_gateway_never_imports_the_api(self):
        problems = cp.gateway_import_problems(REPO)
        self.assertEqual(problems, [], msg="\n".join(problems))

    def test_no_browser_file_addresses_the_api_absolutely(self):
        problems = cp.absolute_api_route_problems(REPO)
        self.assertEqual(problems, [], msg="\n".join(problems))


class PackagingContract(unittest.TestCase):
    """The classifier against the packager that actually runs.

    `scripts/build-assets.mjs` copies `site/` through an extension allowlist and
    then a literal pair of gateway files. Those rules, not a `site/**` sweep,
    decide what ships, so the classifier reads them from the script and this
    suite fails if the declared copy drifts from it.
    """

    def test_declared_copy_matches_the_packaging_script(self):
        problems = cp.packaging_contract_problems(REPO)
        self.assertEqual(problems, [], msg="\n".join(problems))

    def test_contract_source_matches_what_is_in_the_tree(self):
        source = cp.packaging_contract(str(REPO))["source"]
        script_present = (REPO / "scripts" / "build-assets.mjs").is_file()
        self.assertEqual(source, "script" if script_present else "declared")

    def test_site_payload_is_an_extension_allowlist(self):
        packaged = {
            "site/index.html": True,
            "site/js/auth.js": True,
            "site/shared/heatmap.js": True,
            "site/margin-call.png": True,
            "site/robots.txt": True,
            "site/notes.md": False,
            "site/tool.mjs": False,
            "site/archive.zip": False,
        }
        for path, expected in packaged.items():
            with self.subTest(path=path):
                self.assertEqual(cp.artifact_view([path], REPO).payload, expected)

    def test_markdown_inside_an_asset_tree_is_prose(self):
        for path in ("site/DEPLOYMENTS.md", "site/plans/2026-07-vwap-rvol.md", "frontend/README.md"):
            with self.subTest(path=path):
                view = cp.artifact_view([path], REPO)
                self.assertTrue(view.prose)
                self.assertFalse(view.payload)

    def test_only_the_two_named_gateway_files_are_packaged(self):
        for path in cp.PACKAGED_GATEWAY_FILES:
            with self.subTest(path=path):
                self.assertTrue(cp.artifact_view([path], REPO).payload)
        self.assertFalse(cp.artifact_view(["frontend/extra.mjs"], REPO).payload)
        self.assertFalse(cp.artifact_view(["frontend/preview-notes.txt"], REPO).payload)

    def test_migrations_are_contract_inputs_not_payload(self):
        for name in cp.PACKAGED_SCHEMA_FILES:
            path = f"{cp.PACKAGED_SCHEMA_DIR}/{name}"
            with self.subTest(path=path):
                view = cp.artifact_view([path], REPO)
                self.assertTrue(view.contract)
                self.assertFalse(view.payload)
                self.assertTrue(view.api)
                self.assertTrue(view.needs_asset_rebuild)

    def test_tooling_belongs_to_the_asset_release_without_shipping_bytes(self):
        for path in cp.ASSET_TOOLING_INPUTS:
            with self.subTest(path=path):
                view = cp.artifact_view([path], REPO)
                self.assertTrue(view.tooling)
                self.assertFalse(view.payload)
                self.assertTrue(view.asset)
                self.assertTrue(view.needs_asset_rebuild)

    def test_an_unparsable_packager_fails_loudly(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "build-assets.mjs").write_text(
                "// rewritten without the literals this module reads\nconst x = 1;\n",
                encoding="utf-8",
            )
            with self.assertRaises(cp.UnparsablePackaging):
                cp.packaging_contract(str(root))


class DocumentAgreement(unittest.TestCase):
    """The prose record and the executable record name the same things."""

    def test_document_names_every_lane_and_service(self):
        text = DOC.read_text(encoding="utf-8")
        for lane in cp.LANE_WORKFLOWS:
            with self.subTest(lane=lane):
                self.assertIn(lane, text)
        for service in cp.PROVIDER_SERVICES:
            with self.subTest(service=service.name):
                self.assertIn(service.name, text)

    def test_document_names_the_asset_api_split(self):
        text = DOC.read_text(encoding="utf-8")
        for needed in (
            "frontend/",
            "scripts/build-assets.mjs",
            "backend/**",
            "prose",
            "preview",
        ):
            with self.subTest(needed=needed):
                self.assertIn(needed, text)

    def test_document_carries_its_own_provenance(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn(cp.BASELINE_REVISION, text)
        self.assertIn("2026-11-11", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
