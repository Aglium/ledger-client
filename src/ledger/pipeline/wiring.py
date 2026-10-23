"""Topic wiring: who produces and consumes each topic, from declarations and
from bindings -- not from where a name happens to be written.

Two sources, in order of authority:

  declarations  infrastructure-as-code that states ownership and access:
                Strimzi `KafkaUser` ACLs, Terraform `kafka_acl` resources, an
                AsyncAPI document, or -- where the declaration is the
                organisation's own -- a layout described in the deployment
                config (see core/declarations.py).
  bindings      env vars, settings and parameters bound to topic names, and
                the code that uses them (see core/bindings.py).

Declared apps are mapped to repositories by evidence -- the repository that
binds the app's own topic namespace -- because app names and repo names
drift apart (an app `platform-orders-service` deployed from a repo named
`orders_svc`). Exact names and explicit overrides come first.
"""

from __future__ import annotations

import difflib
import fnmatch
import yaml
from ledger.adapters.vcs.base import ForgeUnavailable, VcsAdapter
from ledger.core.bindings import Binding, bindings_in, usages_in
from ledger.core.declarations import FORMATS, read_file

#: Files worth reading for bindings: deployment values, env files, framework
#: parameters, settings modules.
_CONFIG_FILE = (
    "*.yaml", "*.yml", "*.env", ".env*", "*.ini", "*.toml", "*.properties",
    "*settings*.py", "*config*.py", "*topics*.py", "*constants*.py", "*config*.ts", "*config*.php",
)
#: Files worth reading for usages: code, and the config that wires services.
_USAGE_EXT = (".py", ".ts", ".tsx", ".js", ".php", ".go", ".java", ".kt", ".rb", ".yaml", ".yml")
_MAX_FILES = 1500


def _matches(path: str, patterns) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def repo_wiring(vcs: VcsAdapter, repo: str, ref: str) -> tuple[list[Binding], list]:
    """(bindings, usages) for one repository at one ref."""
    tree = vcs.list_tree(repo, ref)
    config_paths = [p for p in tree if _matches(p, _CONFIG_FILE)][:_MAX_FILES]
    texts = vcs.read_files(repo, ref, config_paths) if config_paths else {}
    bindings = [b for p, text in texts.items() for b in bindings_in(p, text)]
    if not bindings:
        return [], []
    idents: dict[str, set[str]] = {}
    for b in bindings:
        idents.setdefault(b.ident, set()).add(b.raw)
    # Ask git where the identifiers appear instead of reading the repository:
    # large codebases put the wiring far past any sensible file cap.
    needles = sorted({raw for raws in idents.values() for raw in raws})
    usage_paths = [p for p in vcs.grep_paths(repo, ref, needles) if p.endswith(_USAGE_EXT)]
    missing = [p for p in usage_paths if p not in texts]
    if missing:
        texts.update(vcs.read_files(repo, ref, missing))
    usages = [u for p in usage_paths if p in texts for u in usages_in(p, texts[p], idents)]
    return bindings, usages


def estate_wiring(vcs: VcsAdapter, repo: str, default: str,
                  integration_branches: tuple[str, ...]) -> tuple[list[Binding], list]:
    """Bindings and usages on the default branch and every integration branch.

    What is deployed is often configured on the integration branch first: on a
    real estate the service's own topic names were bound only on `stage`.
    """
    bindings, usages = repo_wiring(vcs, repo, default)
    for branch in integration_branches:
        if branch == default:
            continue
        try:
            vcs.head_sha(repo, branch)
        except ForgeUnavailable:
            continue
        b, u = repo_wiring(vcs, repo, branch)
        bindings += b
        usages += u
    return list(dict.fromkeys(bindings)), list(dict.fromkeys(usages))


def _dig(doc, dotted: str):
    for part in dotted.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def read_declarations(vcs: VcsAdapter, spec: dict, default_ref: str) -> list[dict]:
    """Declared (app, topic, edge, evidence) rows from infrastructure-as-code.

    `spec` comes from the deployment config, and names either a standard
    format::

        repo: infra
        format: strimzi                 # or asyncapi, terraform-acl

    or, where the declaration is the organisation's own, the layout of it::

        repo: infra
        files: "apps/*.yaml"
        produces: kafka.topics          # dotted path to a list
        consumes: kafka.consumes
        produced_name: "{app}.{value}"  # how an owned name becomes the real topic
        ignore: ["*.dlq"]
    """
    repo = spec["repo"]
    fmt = spec.get("format")
    tree = vcs.list_tree(repo, default_ref)
    pattern = spec.get("files") or (FORMATS[fmt][0] if fmt in FORMATS else "*.yaml")
    files = [p for p in tree if fnmatch.fnmatch(p, pattern)]
    texts = vcs.read_files(repo, default_ref, files)
    ignore = spec.get("ignore", [])
    out: list[dict] = []
    for path, text in sorted(texts.items()):
        if fmt:
            out += [dict(row, evidence=f"{repo}:{path}") for row in read_file(fmt, path, text, spec)]
            continue
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        app = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for key, edge, template in (("produces", "publishes", spec.get("produced_name", "{value}")),
                                    ("consumes", "consumes", spec.get("consumed_name", "{value}"))):
            values = _dig(doc, spec[key]) if spec.get(key) else None
            for value in values or []:
                if not isinstance(value, str):
                    continue
                out.append({"app": app, "topic": template.format(app=app, value=value), "edge": edge,
                            "channel": value, "evidence": f"{repo}:{path}"})
    return [r for r in out if not any(fnmatch.fnmatch(r["topic"], g) for g in ignore)]


def _stem(name: str) -> str:
    return "_".join(w for w in name.lower().replace("-", "_").split("_") if w)


def map_apps(apps: set[str], repos: set[str], bindings_by_repo: dict[str, list[Binding]],
             declared: list[dict], overrides: dict[str, str]) -> dict[str, str]:
    """App name -> repository, by override, exact name, evidence, then spelling.

    A repository matched to its own app by name is taken: evidence cannot hand
    it to a second app. That rule exists because the first version gave a
    service's topics to a consumer that happened to bind more of them.
    """
    owned: dict[str, set[str]] = {}
    for d in declared:
        if d["edge"] == "publishes":
            owned.setdefault(d["app"], set()).add(d["topic"])
    out: dict[str, str] = {a: r for a, r in overrides.items() if a in apps}
    for app in apps - set(out):
        if app in repos:
            out[app] = app
    taken = set(out.values())
    for app in sorted(apps - set(out)):
        mine = owned.get(app, set())
        scores = {r: len(mine & {b.value for b in bs}) for r, bs in bindings_by_repo.items() if r not in taken}
        best = max(scores.items(), key=lambda kv: kv[1], default=(None, 0))
        # At least half the app's own topics, and nobody else close.
        runner_up = sorted(scores.values(), reverse=True)[1:2] or [0]
        if mine and best[1] * 2 >= len(mine) and best[1] > runner_up[0]:
            out[app] = best[0]
            taken.add(best[0])
            continue
        candidates = {_stem(r): r for r in repos - taken}
        close = difflib.get_close_matches(_stem(app), list(candidates), n=1, cutoff=0.75)
        if close:
            out[app] = candidates[close[0]]
            taken.add(out[app])
    return out
