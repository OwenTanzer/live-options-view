"""Changed-path to production effect, derived from repository configuration.

This module is the executable half of #225's acceptance item "Automated checks
cover site-only/backend-only changes, tests/docs, producer changes and shared
dependencies, including intentional multi-service effects." It answers one
question for a set of changed files: what does that change reach?

Three relations are kept apart, because conflating them is the exact error
#225 is about.

``watch``
    The provider's GitHub deploy watch list selects the path, so a commit
    touching it becomes a deployment candidate for that service.

``runtime``
    The path is in the transitive import closure of the service's entry point,
    so promoting it changes what the service executes.

``image_input``
    The path is copied into the service's deployed image by its Dockerfile,
    so promoting it changes the bytes of the running service even when the
    service never imports it.

Repository lanes are read out of ``.github/workflows/*.yml`` at test time and
fail when a filter moves. Provider watch lists live in Railway, not here, so
they are recorded as a dated baseline with a source and are never presented as
verified. No test in this repository can prove that a provider trigger is
attached; that is a readback, not a build artifact.
"""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

#: Repository revision every claim in this module was checked against.
BASELINE_REVISION = "90cdc56c207ad0c733bb2f1b1bb15d995472bf47"

#: Where the provider half of the matrix came from, and when.
PROVIDER_BASELINE_SOURCE = (
    "issue #225 provider readback 2026-11-11T17:27Z (09:27 Pacific); "
    "latest-deployment metadata in the 2026-11-11 vulnerability report, "
    "09:34:47 Pacific receipt"
)

#: Repository workflows that gate a lane, by lane name.
LANE_WORKFLOWS = {
    "cloudflare_deploy": "deploy.yml",
    "web_ci": "web-ci.yml",
    "collector_ci": "collector-ci.yml",
    "crassus_ci": "crassus-ci.yml",
    "synthetic_ci": "synthetic-days-ci.yml",
}

#: Lanes that promote code to something a user or a producer runs.
PROMOTING_LANES = ("cloudflare_deploy",)

# --------------------------------------------------------------------------
# The asset/API split (G1)
# --------------------------------------------------------------------------
#
# Musef's G1 design and G2 implementation (`0434bf1b`, branch
# `musef/225-assets-preview`) propose one public origin serving a static asset
# gateway that forwards reserved API paths to a separately bound API service.
# What survives as path selection is decided by `scripts/build-assets.mjs`,
# which is an extension allowlist plus a hard-coded pair of gateway files, not
# a `site/**`/`frontend/**` sweep. The declared lists below are a dated copy of
# that script; `packaging_contract_problems` fails if the two diverge, so a
# change to the script is a deliberate update here rather than silent drift.

#: Extensions the packager copies out of `site/`, in its own order.
PACKAGED_SITE_EXTENSIONS = (
    ".html", ".js", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".ico",
    ".woff", ".woff2", ".txt", ".webp",
)

#: The gateway is a fixed pair of files, not a directory sweep. A new `.mjs`
#: under `frontend/` is not in the artifact until the script names it.
PACKAGED_GATEWAY_FILES = ("frontend/worker.mjs", "frontend/preview-policy.mjs")

#: Migrations are read for their hashes into `release.json`, so their content
#: changes the artifact's embedded contract without ever being packaged. The
#: script names bare filenames and resolves them against a directory; both are
#: read from it, because a mismatch here would silently detach the classifier
#: from the files it claims to track.
PACKAGED_SCHEMA_DIR = "backend/migrations"
PACKAGED_SCHEMA_FILES = ("0001_accounts_sessions.sql", "0002_cutover_support.sql")

#: Scripts that assemble or verify the artifact. A change to one does not change
#: the payload for unchanged source, but it does change how the payload is made.
ASSET_TOOLING_INPUTS = (
    "scripts/build-assets.mjs",
    "scripts/prepare-asset-release.mjs",
    "scripts/stamp-build.mjs",
)

#: The two files that can be selected as the staged asset configuration. The
#: applied root file wins; the proposal template is the labelled fallback while
#: Nova has not integrated it. Either one is what a staged plan hashes, so a
#: change to either makes previously recorded config evidence stale.
SELECTED_CONFIG_INPUTS = (
    "wrangler.assets.toml",
    "patches/musef-225/wrangler.assets.toml.proposed",
)

#: Inputs the preview and A1 acceptance evidence actually depend on. The preview
#: composes the real API handlers through a constructor and a shared helper, and
#: the executed suites are evidence only for the revision they ran against. None
#: of this becomes asset payload: `backend/**` stays API, and a helper or suite
#: change stales the evidence without shipping anything.
PREVIEW_ACCEPTANCE_INPUTS = (
    "backend/**",
    "scripts/preview-local.mjs",
    "frontend/preview-policy.mjs",
    "tests/helpers/**",
    "tests/preview_*.mjs",
    "tests/fixtures/**",
)

#: Paths that belong to the operational API service and never to the artifact.
#: The design says so twice: "Asset artifact excludes backend/producers", and
#: "`backend/**` is API-only; no asset release imports it".
API_INPUTS = ("backend/**",)

#: Files whose change alters how either artifact is built or promoted. Editing
#: one is a reviewed mixed change, not a routine release of either artifact.
SHARED_CONFIG_INPUTS = (
    "wrangler.toml",
    "wrangler.recovery.toml",
    ".github/workflows/**",
)

#: Applied per-artifact configuration, plus the labelled fallback the stager uses
#: while Nova has not integrated it. The proposal is a release input only in that
#: fallback role, which is why it is selected-configuration rather than payload.
APPLIED_ASSET_CONFIG = ("wrangler.assets.toml",)
APPLIED_API_CONFIG = ("wrangler.api.toml",)

#: Everything the asset release is built from, for the "is it tested" guard.
ASSET_INPUTS = (
    "site/**",
    "frontend/**",
) + ASSET_TOOLING_INPUTS

#: The promotion gate itself: the staged-deploy script and the workflow that
#: calls it. Neither produces artifact bytes, so neither is an asset input, but a
#: change to either decides whether anything reaches production at all — which is
#: exactly why the test workflow has to run for them.
PROMOTION_INPUTS = (
    "scripts/deploy-staged-payload.mjs",
    ".github/workflows/deploy-staged.yml",
)

#: Everything that must be exercised by the test workflow when it changes.
COVERAGE_INPUTS = (
    ASSET_INPUTS + SELECTED_CONFIG_INPUTS + PREVIEW_ACCEPTANCE_INPUTS + PROMOTION_INPUTS
)

#: Trees where a Markdown file is prose rather than shipped content. The design
#: requires this be classified explicitly "rather than assume glob exclusion":
#: a `site/notes.md` is not a broken asset, it is a document that happens to
#: live inside the asset tree.
ASSET_PROSE_ROOTS = ("site/", "frontend/")
PROSE_SUFFIXES = (".md", ".markdown")

#: The preview identity the design hands to Steve's G4a/G4b work. Recorded here
#: so one place names the contract; the runtime checks that enforce it are not
#: path selection and are not asserted here.
PREVIEW_CONTRACT = {
    "ENVIRONMENT": "preview",
    "ISOLATION": "disposable",
    "denied_routes": ("/r2-proxy/",),
    "api_transport": "local handlers over disposable D1 and memory stores",
}


class UnparsablePackaging(ValueError):
    """The packaging script changed shape and must be re-read, not guessed."""


def _literal_list(text: str, pattern: str, name: str) -> tuple[str, ...]:
    match = re.search(pattern, text)
    if match is None:
        raise UnparsablePackaging(f"{name}: no literal list found to read")
    items = tuple(re.findall(r"'([^']+)'|\"([^\"]+)\"", match.group(1)))
    values = tuple(a or b for a, b in items)
    if not values:
        raise UnparsablePackaging(f"{name}: literal list is empty")
    return values


@lru_cache(maxsize=8)
def packaging_contract(repo_root: str) -> dict:
    """The packaging inputs, read from `scripts/build-assets.mjs` when present.

    Absent, the declared copy is returned, so this module still classifies paths
    on a tree that predates the gateway.
    """
    root = Path(repo_root)
    script = root / "scripts" / "build-assets.mjs"
    declared = {
        "source": "declared",
        "extensions": PACKAGED_SITE_EXTENSIONS,
        "gateway": PACKAGED_GATEWAY_FILES,
        "schema": PACKAGED_SCHEMA_FILES,
        "schema_dir": PACKAGED_SCHEMA_DIR,
    }
    if not script.is_file():
        return declared
    text = script.read_text(encoding="utf-8")
    schema_match = re.search(r"const schema = \[([^\]]*)\]", text)
    if schema_match is None:
        raise UnparsablePackaging("schema file list: no literal list found to read")
    # The directory is resolved inside the schema mapping that follows that
    # literal; searching the whole file would find the unrelated `site` read.
    tail = text[schema_match.end(): schema_match.end() + 400]
    directory = re.search(r"resolve\(root,\s*'([^']+)',\s*name\)", tail)
    if directory is None:
        raise UnparsablePackaging("schema directory: no literal resolved against name")
    return {
        "source": "script",
        "extensions": _literal_list(
            text, r"ASSET_EXTENSIONS\s*=\s*new Set\(\[([^\]]*)\]\)", "ASSET_EXTENSIONS"
        ),
        "gateway": _literal_list(
            text, r"for \(const name of \[([^\]]*)\]\)", "gateway file list"
        ),
        "schema": tuple(re.findall(r"'([^']+)'", schema_match.group(1))),
        "schema_dir": directory.group(1),
    }


def packaging_contract_problems(repo_root) -> list[str]:
    """Where the script and the declared copy disagree."""
    contract = packaging_contract(str(Path(repo_root).resolve()))
    if contract["source"] != "script":
        return []
    problems: list[str] = []
    for name, declared in (
        ("extensions", PACKAGED_SITE_EXTENSIONS),
        ("gateway", PACKAGED_GATEWAY_FILES),
        ("schema", PACKAGED_SCHEMA_FILES),
    ):
        parsed = contract[name]
        if tuple(sorted(parsed)) != tuple(sorted(declared)):
            problems.append(
                f"{name}: script has {list(parsed)}, declared copy has {list(declared)}"
            )
    if contract["schema_dir"] != PACKAGED_SCHEMA_DIR:
        problems.append(
            f"schema_dir: script resolves against {contract['schema_dir']!r}, "
            f"declared copy uses {PACKAGED_SCHEMA_DIR!r}"
        )
    return problems


@dataclass(frozen=True)
class ArtifactView:
    """Which release artifact a change belongs to."""

    payload: bool
    tooling: bool
    contract: bool
    config: bool
    acceptance: bool
    api: bool
    shared: bool
    prose: bool

    @property
    def asset(self) -> bool:
        """Belongs to the asset release: packaged bytes or the tool that makes them."""
        return self.payload or self.tooling

    @property
    def needs_asset_rebuild(self) -> bool:
        """A new artifact would differ from the last one.

        True for a payload or packaging change, and also for a migration
        change: the artifact embeds the migrations' hashes, so its recorded
        contract goes stale even though no migration is ever packaged.
        """
        return self.payload or self.tooling or self.contract

    @property
    def config_evidence_stale(self) -> bool:
        """A previously staged plan no longer names the configuration it used.

        The plan records `config_source` and `config_sha256` for whichever file
        was selected — the applied root config, or the proposal template when no
        root file exists. Either one changing makes that record a statement
        about the past, so it has to be restaged; the artifact itself may be
        unchanged.
        """
        return self.config

    @property
    def acceptance_evidence_stale(self) -> bool:
        """The preview or A1 evidence must be re-run for this revision.

        Separate from `preview_stale`, which asks whether a built preview would
        serve different bytes. Evidence also goes stale when the real handlers,
        the preview constructor, a shared helper or an executed suite changes,
        and none of those is asset payload.
        """
        return self.preview_stale or self.acceptance

    @property
    def preview_stale(self) -> bool:
        """A built preview would serve different bytes for this revision."""
        return self.payload or self.tooling or self.shared

    @property
    def mixed(self) -> bool:
        """A commit that belongs to both artifacts at once."""
        return self.asset and self.api

    @property
    def needs_integration_review(self) -> bool:
        """Shared configuration, or a commit spanning both artifacts."""
        return self.shared or self.mixed

    def describe(self) -> str:
        parts = []
        for name in (
            "payload",
            "tooling",
            "contract",
            "config",
            "acceptance",
            "api",
            "shared",
            "prose",
        ):
            if getattr(self, name):
                parts.append(name)
        return "+".join(parts) if parts else "none"


def is_asset_prose(path: str) -> bool:
    """True for a Markdown file inside an asset tree."""
    return path.startswith(ASSET_PROSE_ROOTS) and path.endswith(PROSE_SUFFIXES)


def artifact_view(paths, repo_root=None) -> ArtifactView:
    """Classify a change set against the real packaging rules.

    ``payload`` is what the script would copy, ``tooling`` is what the script is,
    ``contract`` is the migrations whose hashes the artifact records, ``config``
    is the file a stage would hash as its configuration, and ``acceptance`` is
    what the preview and A1 evidence were collected against. They are separate
    because each goes stale for a different reason and only one of them ships.
    """
    root = str(Path(repo_root or Path(__file__).resolve().parents[1]).resolve())
    contract = packaging_contract(root)
    extensions = tuple(contract["extensions"])
    gateway = tuple(contract["gateway"])
    schema = tuple(f"{contract['schema_dir']}/{name}" for name in contract["schema"])
    normalised = [_normalise(p) for p in paths]

    def packaged(path: str) -> bool:
        if path in gateway:
            return True
        if not path.startswith("site/"):
            return False
        # Case-sensitive on purpose: the packager compares extname() against a
        # lower-case set, so `site/UPPER.JS` is not packaged and must not be
        # classified as payload here either.
        return Path(path).suffix in extensions

    return ArtifactView(
        payload=any(packaged(p) for p in normalised),
        tooling=any(p in ASSET_TOOLING_INPUTS for p in normalised),
        contract=any(p in schema for p in normalised),
        config=any(p in SELECTED_CONFIG_INPUTS for p in normalised),
        acceptance=any(any_match(PREVIEW_ACCEPTANCE_INPUTS, p) for p in normalised),
        api=any(any_match(API_INPUTS, p) for p in normalised),
        shared=any(
            any_match(
                SHARED_CONFIG_INPUTS + APPLIED_ASSET_CONFIG + APPLIED_API_CONFIG
                + SELECTED_CONFIG_INPUTS,
                p,
            )
            for p in normalised
        ),
        prose=any(is_asset_prose(p) for p in normalised),
    )


#: The image the three Python producers are built from.
PRODUCER_DOCKERFILE = "deployment/python/Dockerfile"

#: Inputs that change every image built from the shared producer Dockerfile.
#:
#: Every file under ``market_data/`` is copied into that image, so any producer
#: change moves every producer's image digest. That is not a reason to redeploy
#: the other producers, and #225 says so: shared image inclusion does not imply
#: every service promotes that image. These are the inputs that do affect a
#: service regardless of which package it imports.
SHARED_BUILD_INPUTS = ("deployment/python/**", "market_data/__init__.py")


@dataclass(frozen=True)
class ProviderService:
    """A Railway service, its dated watch list and its build/run identity."""

    name: str
    watch: tuple[str, ...]
    github_trigger: bool
    check_suites: bool | None
    cron: str | None
    entry_module: str
    dockerfile: str | None
    context: str = "."
    module_root: str = "."
    note: str = ""


#: Dated provider baseline. ``watch=()`` records an empty provider list, which
#: is the hazard #225 names: it selects nothing today and everything the
#: moment a trigger is reattached.
PROVIDER_SERVICES = (
    ProviderService(
        name="main-collector",
        watch=(
            "market_data/live_market/**",
            "market_data/overview/**",
            "market_data/__init__.py",
            "deployment/python/**",
        ),
        github_trigger=True,
        check_suites=True,
        cron=None,
        entry_module="market_data.live_market.__main__",
        dockerfile=PRODUCER_DOCKERFILE,
        note="continuous session-aware collector; no cron",
    ),
    ProviderService(
        name="crassus-runner",
        watch=("crassus/**",),
        github_trigger=True,
        check_suites=True,
        cron=None,
        entry_module="crassus.runner",
        dockerfile="crassus/Dockerfile",
        context="crassus",
        module_root="crassus",
        note="continuous five-minute bot runner; no cron",
    ),
    ProviderService(
        name="tradier-collector",
        watch=(
            "market_data/tradier/**",
            "market_data/diagnostics.py",
            "market_data/__init__.py",
            "deployment/python/**",
        ),
        github_trigger=False,
        check_suites=None,
        cron="15 12 * * 1-5",
        entry_module="market_data.tradier.__main__",
        dockerfile=PRODUCER_DOCKERFILE,
        note="scheduled one-shot capture; provider holds a scope but no GitHub trigger. "
        "The package-initializer entry is read from #225's prose ('its package, "
        "diagnostics, package initializer') and needs a provider readback to confirm",
    ),
    ProviderService(
        name="banana-options-scanner",
        watch=(),
        github_trigger=False,
        check_suites=None,
        cron="45 12 * * 1-5",
        entry_module="market_data.options_scanner.__main__",
        dockerfile=PRODUCER_DOCKERFILE,
        note="scheduled one-shot scan; provider watch list is empty (#225)",
    ),
)

#: The scope #225 requires before Banana's autodeploy may be restored.
BANANA_CANDIDATE_SCOPE = (
    "market_data/options_scanner/**",
    "market_data/tradier/**",
    "market_data/diagnostics.py",
    "market_data/__init__.py",
    "deployment/python/**",
)

#: The scope #225 rejects: the scanner directory alone misses imports.
BANANA_SCANNER_ONLY_SCOPE = ("market_data/options_scanner/**",)


# --------------------------------------------------------------------------
# Path matching
# --------------------------------------------------------------------------


@lru_cache(maxsize=2048)
def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a GitHub-style path filter into a compiled regex.

    ``**`` matches across separators; ``*`` and ``?`` do not. ``site/**``
    requires the separator, so it does not match the bare directory name.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("\\A" + "".join(out) + "\\Z")


def matches(pattern: str, path: str) -> bool:
    """True when a path filter selects a repository-relative slash path."""
    return pattern_to_regex(pattern).match(path) is not None


def scope_matches(pattern: str, path: str) -> bool:
    """True when a path sits inside a hand-written provider watch scope.

    A bare directory entry is read as that directory's subtree, because
    ``market_data/tradier`` and ``market_data/tradier/**`` mean the same thing
    to the operator who typed them into Railway.
    """
    if matches(pattern, path):
        return True
    return matches(pattern.rstrip("/") + "/**", path)


def any_match(patterns, path: str, matcher=matches) -> bool:
    return any(matcher(pattern, path) for pattern in patterns)


# --------------------------------------------------------------------------
# Workflow parsing
# --------------------------------------------------------------------------


class UnsupportedWorkflow(ValueError):
    """Raised for trigger features this module does not model.

    Failing loudly is deliberate: silently ignoring ``paths-ignore`` or an
    inline list would turn the boundary test into a rubber stamp.
    """


def _strip_comment(line: str) -> str:
    if '"' in line or "'" in line:
        return line
    cut = line.find("#")
    return line if cut < 0 else line[:cut]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_workflow_triggers(text: str, name: str = "<workflow>") -> dict:
    """Return ``{event: {"paths": [...], "branches": [...]}}`` from ``on:``."""
    events: dict[str, dict[str, list[str]]] = {}
    event: str | None = None
    subkey: str | None = None
    in_on = False

    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()

        if indent == 0:
            in_on = body == "on:"
            event = None
            subkey = None
            continue
        if not in_on:
            continue

        if body.startswith("- "):
            if event is None or subkey is None:
                raise UnsupportedWorkflow(f"{name}: list item outside a path list")
            events[event].setdefault(subkey, []).append(_unquote(body[2:]))
            continue

        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()

        if indent == 2:
            event = key
            subkey = None
            events.setdefault(event, {})
            if rest:
                raise UnsupportedWorkflow(f"{name}: inline event value for {key!r}")
            continue

        if indent == 4:
            if key in {"paths-ignore", "branches-ignore", "tags", "tags-ignore"}:
                raise UnsupportedWorkflow(f"{name}: {key} is not modelled here")
            subkey = key
            events.setdefault(event or "", {}).setdefault(subkey, [])
            if rest:
                if not (rest.startswith("[") and rest.endswith("]")):
                    raise UnsupportedWorkflow(f"{name}: inline value for {key!r}")
                events[event or ""][subkey] = [
                    _unquote(item) for item in rest[1:-1].split(",") if item.strip()
                ]
            continue

        if subkey in {"paths", "branches"}:
            raise UnsupportedWorkflow(f"{name}: unexpected indent {indent} at {body!r}")
        # Event metadata such as workflow_dispatch inputs; irrelevant here.
        continue

    return events


def workflow_triggers(repo_root) -> dict[str, dict]:
    """Parse every workflow once, keyed by file name."""
    root = Path(repo_root)
    return {
        path.name: parse_workflow_triggers(path.read_text(encoding="utf-8"), path.name)
        for path in sorted((root / ".github" / "workflows").glob("*.yml"))
    }


def _event(triggers: dict, workflow: str, event: str, key: str) -> tuple[str, ...]:
    return tuple(triggers.get(workflow, {}).get(event, {}).get(key, ()))


# --------------------------------------------------------------------------
# Image inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageInputs:
    """Paths that change a Docker image, from its COPY and ignore files.

    The ignore file is read as an ordered rule list because the producer
    Dockerfile uses the whitelist form: ``**`` excludes everything and the
    ``!market_data/**`` lines put it back. A flat set of "ignored patterns"
    would call the whole repository ignored.
    """

    dockerfile: str
    context: str
    copies: tuple[str, ...]
    rules: tuple[tuple[bool, str], ...]

    def contains(self, path: str) -> bool:
        if not any_match(self.copies, path, scope_matches):
            return False
        selected = True
        for re_included, pattern in self.rules:
            if scope_matches(pattern, path):
                selected = re_included
        return selected


def _dockerignore_for(dockerfile: Path, context_root: Path) -> tuple[tuple[bool, str], ...]:
    candidates = [
        dockerfile.with_name(dockerfile.name + ".dockerignore"),
        context_root / ".dockerignore",
    ]
    ignore = next((c for c in candidates if c.is_file()), None)
    if ignore is None:
        return ()
    rules: list[tuple[bool, str]] = []
    for raw in ignore.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        re_included = line.startswith("!")
        pattern = line[1:].strip() if re_included else line
        rules.append((re_included, pattern.rstrip("/")))
    return tuple(rules)


@lru_cache(maxsize=16)
def image_inputs(repo_root: str, dockerfile: str, context: str = ".") -> ImageInputs:
    """Parse ``COPY`` lines out of a Dockerfile, as repository path patterns.

    ``context`` is the Docker build context, relative to the repository root;
    ``COPY . ./`` inside it selects the whole subtree. Only the plain
    ``COPY src [src...] dst`` form is modelled, and ``ADD`` raises rather than
    being silently mis-summarised.
    """
    root = Path(repo_root)
    path = root / dockerfile
    context_root = root if context in (".", "") else root / context
    prefix = "" if context in (".", "") else context.strip("/") + "/"
    copies: list[str] = [dockerfile, dockerfile + ".dockerignore"]

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        if keyword == "ADD":
            raise UnsupportedWorkflow(f"{dockerfile}: ADD is not modelled")
        if keyword != "COPY":
            continue
        if rest.strip().startswith("--"):
            raise UnsupportedWorkflow(f"{dockerfile}: COPY flags are not modelled")
        parts = [p for p in rest.split() if p]
        if len(parts) < 2:
            raise UnsupportedWorkflow(f"{dockerfile}: malformed COPY {rest!r}")
        for source in parts[:-1]:
            cleaned = source.strip()
            if cleaned in (".", "./"):
                copies.append(prefix + "**")
                continue
            normalised = cleaned.rstrip("/")
            while normalised.startswith("./"):
                normalised = normalised[2:]
            target = context_root / normalised
            if target.is_dir() or normalised.endswith("/"):
                copies.append(prefix + normalised + "/**")
            else:
                copies.append(prefix + normalised)

    return ImageInputs(
        dockerfile=dockerfile,
        context=context,
        copies=tuple(copies),
        rules=_dockerignore_for(path, context_root),
    )


# --------------------------------------------------------------------------
# Import closure
# --------------------------------------------------------------------------


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, root: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = list(path.relative_to(root).parts[:-1])
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                anchor = package[:keep] if keep > 0 else []
                target = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                target = node.module or ""
            if not target:
                continue
            found.add(target)
            found.update(f"{target}.{a.name}" for a in node.names if a.name != "*")
    return found


@lru_cache(maxsize=32)
def import_closure(
    repo_root: str, entry_module: str, module_root: str = "."
) -> tuple[str, ...]:
    """Repository files executed by ``python -m <entry_module>``, transitively.

    ``module_root`` is the repository directory that plays the role of the
    interpreter's working directory -- ``crassus`` for the runner, whose build
    context is that subdirectory, and the repository root for the producers.
    Package ``__init__`` files are included for every module reached, because
    importing a submodule runs each ancestor initializer. Standard library and
    third-party imports end the walk.
    """
    root = Path(repo_root)
    top = root if module_root in (".", "") else root / module_root
    seen: set[str] = set()
    files: set[str] = set()
    stack = [entry_module]

    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(top, module)
        if path is None:
            continue
        rel = path.relative_to(root).as_posix()
        files.add(rel)
        parts = rel.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            init = root.joinpath(*parts[:depth]) / "__init__.py"
            if init.is_file():
                files.add(init.relative_to(root).as_posix())
        for name in _imports(path, top):
            if _module_file(top, name) is not None:
                stack.append(name)
    return tuple(sorted(files))


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceView:
    """How one change reaches one provider service."""

    watch: bool
    runtime: bool
    shared: bool
    image_input: bool

    @property
    def relevant(self) -> bool:
        """True when promoting this change would change what the service runs.

        Image membership alone is not relevance: every producer shares one
        image, so another package's file moves every digest without being a
        reason to restart this service.
        """
        return self.watch or self.runtime or self.shared


@dataclass(frozen=True)
class Effects:
    """What a change set reaches."""

    changed: tuple[str, ...]
    lanes: dict[str, bool]
    services: dict[str, ServiceView]
    artifact: ArtifactView
    notes: tuple[str, ...] = field(default=())

    @property
    def triggered(self) -> tuple[str, ...]:
        """Repository CI lanes that run for this change."""
        return tuple(name for name, hit in self.lanes.items() if hit)

    @property
    def asset_release(self) -> bool:
        """A change to the packaged static artifact."""
        return self.artifact.asset

    @property
    def api_release(self) -> bool:
        """A change to the operational API artifact."""
        return self.artifact.api

    @property
    def preview_stale(self) -> bool:
        """Whether a preview built from this commit's inputs would be out of date.

        Anything that determines the artifact's bytes makes a preview stale:
        packaged content, the tooling that assembles it, and the configuration
        that decides how it is served. The API, tests, documents and prose do
        not, because none of them is part of the preview's payload.
        """
        return self.artifact.preview_stale

    @property
    def acceptance_evidence_stale(self) -> bool:
        """Whether preview and A1 evidence has to be re-run for this revision.

        A superset of preview staleness: the real handlers, the preview
        constructor, a shared helper or an executed suite also invalidate the
        evidence, without any of them becoming asset payload.
        """
        return self.artifact.acceptance_evidence_stale

    @property
    def config_evidence_stale(self) -> bool:
        """Whether a staged plan's recorded configuration has been superseded."""
        return self.artifact.config_evidence_stale

    @property
    def needs_asset_rebuild(self) -> bool:
        """A fresh artifact would differ from the last one."""
        return self.artifact.needs_asset_rebuild

    def _service(self, name: str) -> ProviderService:
        for service in PROVIDER_SERVICES:
            if service.name == name:
                return service
        raise KeyError(name)

    @property
    def auto_promoting(self) -> tuple[str, ...]:
        """Services whose attached trigger would deploy this change."""
        return tuple(
            name
            for name, view in self.services.items()
            if view.watch and self._service(name).github_trigger
        )

    @property
    def requiring_promotion(self) -> tuple[str, ...]:
        """Services this change really affects but which cannot autodeploy.

        These are the deliberate, reviewed promotions: the change is real and
        the provider will not deliver it on its own.
        """
        return tuple(
            name
            for name, view in self.services.items()
            if view.relevant and not (view.watch and self._service(name).github_trigger)
        )

    def describe(self) -> str:
        return (
            f"lanes={list(self.triggered)} "
            f"auto={list(self.auto_promoting)} "
            f"manual={list(self.requiring_promotion)} "
            f"artifact={self.artifact.describe()}"
        )


def effects(changed, repo_root=None, triggers: dict | None = None) -> Effects:
    """Compute the effect of a change set."""
    root = str(Path(repo_root or Path(__file__).resolve().parents[1]).resolve())
    paths = tuple(_normalise(p) for p in changed)
    if triggers is None:
        triggers = workflow_triggers(root)

    lanes: dict[str, bool] = {}
    for lane, workflow in LANE_WORKFLOWS.items():
        push = _event(triggers, workflow, "push", "paths")
        branches = _event(triggers, workflow, "push", "branches")
        if branches and "master" not in branches:
            lanes[lane] = False
            continue
        lanes[lane] = any(any_match(push, path) for path in paths)

    services: dict[str, ServiceView] = {}
    for service in PROVIDER_SERVICES:
        runtime = import_closure(root, service.entry_module, service.module_root)
        image = (
            image_inputs(root, service.dockerfile, service.context)
            if service.dockerfile
            else None
        )
        services[service.name] = ServiceView(
            watch=any(any_match(service.watch, path, scope_matches) for path in paths),
            runtime=any(path in runtime for path in paths),
            shared=any(any_match(SHARED_BUILD_INPUTS, path) for path in paths)
            if service.dockerfile == PRODUCER_DOCKERFILE
            else False,
            image_input=bool(image and any(image.contains(path) for path in paths)),
        )

    notes: list[str] = []
    for service in PROVIDER_SERVICES:
        if service.watch:
            continue
        if any(any_match(("market_data/**", "deployment/python/**"), path) for path in paths):
            notes.append(
                f"{service.name}: provider watch list is empty, so this change "
                "would be a candidate if its GitHub trigger were reattached"
            )

    return Effects(
        changed=paths,
        lanes=lanes,
        services=services,
        artifact=artifact_view(paths, root),
        notes=tuple(notes),
    )


def _normalise(path) -> str:
    """Repository-relative slash path, tolerating ``./`` and backslashes."""
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


# --------------------------------------------------------------------------
# Invariants the tests assert
# --------------------------------------------------------------------------


def tracked_files(repo_root, *roots) -> tuple[str, ...]:
    """Repository-relative paths tracked by git under the given roots.

    Tracked, not present: the packager copies `git ls-files -- site/`, so an
    untracked scratch file is not an input to a release and must not make this
    guard fail. When git is unavailable the walk falls back to every file on
    disk, which is the stricter reading.
    """
    root = Path(repo_root)
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *roots],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout:
        return tuple(path for path in result.stdout.split("\0") if path)
    found: list[str] = []
    for base in roots:
        start = root / base
        if start.is_file():
            found.append(base)
            continue
        if not start.is_dir():
            continue
        for path in start.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                found.append(path.relative_to(root).as_posix())
    return tuple(sorted(found))


def _pattern_candidates(repo_root, patterns) -> tuple[str, ...]:
    """Files a pattern list could select: tracked subtrees plus exact paths.

    Exact patterns such as `wrangler.assets.toml` are not under a directory
    root, so they would be missed by a subtree walk alone — which is precisely
    how a narrowed filter for a single file slips through a sample check.
    """
    root = Path(repo_root)
    trees = sorted({pattern.split("/**")[0] for pattern in patterns if "/**" in pattern})
    exact = [pattern for pattern in patterns if "/**" not in pattern]
    found = list(tracked_files(repo_root, *trees)) if trees else []
    found.extend(path for path in exact if (root / path).is_file())
    return tuple(sorted(set(found)))


def promotion_coverage_problems(repo_root, triggers: dict) -> list[str]:
    """Every tracked file that can promote must also be selected by the tests.

    This is the executable form of #225's "Audit CI ordering as well as
    filtering". deploy.yml fires straight at production, so every file its filter
    selects must also be selected by web-ci's filter; otherwise a change can be
    promoted while the suite for that commit was never asked to run.

    Exhaustive over real files rather than one synthetic sample per pattern: a
    narrowed test filter that still matches `site/x` would satisfy a sample check
    while omitting `site/index.html`, which is exactly the false pass this
    replaces.
    """
    deploy = _event(triggers, "deploy.yml", "push", "paths")
    if not deploy:
        return ["deploy.yml has no push path filter"]
    tested = _event(triggers, "web-ci.yml", "push", "paths")
    if not tested:
        return ["web-ci.yml has no push path filter"]

    roots = tuple(sorted({pattern.split("/**")[0] for pattern in deploy if "/**" in pattern}))
    candidates = _pattern_candidates(repo_root, deploy)
    problems: list[str] = []
    for path in candidates:
        if any_match(deploy, path) and not any_match(tested, path):
            problems.append(
                f"promoted file {path!r} is not selected by the test workflow"
            )
    return problems


def input_coverage_problems(repo_root, triggers: dict) -> list[str]:
    """Every tracked asset, config or acceptance input must be selected by web-ci.

    Exhaustive by construction, and forward-looking: a file added tomorrow under
    an input root fails this the moment it is committed, without anyone having
    remembered to add it here. That is the new-path control, not a separate test.
    """
    tested = _event(triggers, "web-ci.yml", "push", "paths")
    if not tested:
        return ["web-ci.yml has no push path filter"]
    roots = tuple(
        sorted({pattern.split("/**")[0] for pattern in COVERAGE_INPUTS if "/**" in pattern})
    )
    candidates = _pattern_candidates(repo_root, COVERAGE_INPUTS)
    problems: list[str] = []
    for path in candidates:
        if not any_match(COVERAGE_INPUTS, path):
            continue
        if not any_match(tested, path):
            problems.append(f"input {path!r} exists but web-ci does not select it")
    return problems


def closure_outside_scope(
    repo_root, entry_module: str, scope, module_root: str = "."
) -> list[str]:
    """Files the entry point executes that the watch scope does not cover."""
    root = str(Path(repo_root).resolve())
    return [
        rel
        for rel in import_closure(root, entry_module, module_root)
        if not any_match(scope, rel, scope_matches)
    ]


def _existing_sample(root: Path, pattern: str) -> str | None:
    """One concrete path the pattern selects that exists in the tree."""
    if pattern.endswith("/**"):
        base = root / pattern[:-3]
        if not base.is_dir():
            return None
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                return path.relative_to(root).as_posix()
        return None
    return pattern if (root / pattern).is_file() else None


_ABSOLUTE_URL = re.compile(r"""https?://[^\s'"`)]+""")


def absolute_api_route_problems(repo_root) -> list[str]:
    """No browser file may address the API by absolute URL.

    G1 keeps every browser URL and relative `/api` call unchanged, and Steve's
    preview contract forbids a URL-based production API fallback. Both hold
    only while the browser says `/api/...` rather than naming a host: an
    absolute origin would make a preview call production, and would keep
    working after the topology split precisely because it skipped the gateway.
    """
    from urllib.parse import urlsplit

    root = Path(repo_root)
    problems: list[str] = []
    for tree, suffixes in (("site", {".js", ".html"}), ("frontend", {".js", ".mjs"})):
        base = root / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in suffixes or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for url in _ABSOLUTE_URL.findall(text):
                if urlsplit(url).path.startswith("/api"):
                    problems.append(
                        f"{path.relative_to(root).as_posix()} addresses the API absolutely: {url}"
                    )
    return problems


def asset_input_coverage_problems(repo_root, triggers: dict) -> list[str]:
    """Deprecated alias for :func:`input_coverage_problems`.

    Kept because the earlier receipts name it; the guard behind it is now
    exhaustive over tracked inputs rather than one sample per pattern.
    """
    return input_coverage_problems(repo_root, triggers)


_IMPORT_SPEC = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|\bimport\s+)['"]([^'"]+)['"]""")


def gateway_import_problems(repo_root) -> list[str]:
    """No file under `frontend/` may import the operational API.

    The design is explicit twice: the asset artifact excludes backend code, and
    no asset release imports it. A static gateway that reaches into `backend/`
    would drag the API into the asset bundle whatever the path filters say.
    """
    root = Path(repo_root) / "frontend"
    if not root.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".js", ".mjs"} or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for spec in _IMPORT_SPEC.findall(text):
            if "backend/" in spec:
                problems.append(
                    f"{path.relative_to(root.parent).as_posix()} imports {spec!r}"
                )
    return problems
