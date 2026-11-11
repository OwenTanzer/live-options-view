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
    notes: tuple[str, ...] = field(default=())

    @property
    def triggered(self) -> tuple[str, ...]:
        """Repository CI lanes that run for this change."""
        return tuple(name for name, hit in self.lanes.items() if hit)

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
            f"manual={list(self.requiring_promotion)}"
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


def promotion_coverage_problems(triggers: dict) -> list[str]:
    """Every path that can promote must also be selected by the test workflow.

    This is the executable form of #225's "Audit CI ordering as well as
    filtering". deploy.yml fires straight at production, so anything its filter
    selects must also be a sample that web-ci's filter selects; otherwise a
    change can be promoted while the suite for that commit was never asked to
    run. Each promoted pattern is sampled once.
    """
    deploy = _event(triggers, "deploy.yml", "push", "paths")
    if not deploy:
        return ["deploy.yml has no push path filter"]
    tested = _event(triggers, "web-ci.yml", "push", "paths")
    if not tested:
        return ["web-ci.yml has no push path filter"]

    problems: list[str] = []
    for pattern in deploy:
        sample = pattern.replace("/**", "/x").replace("**", "x").replace("*", "x")
        if not any_match(tested, sample):
            problems.append(
                f"promoted path {pattern!r} (sample {sample!r}) is not covered by "
                "the test workflow"
            )
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
