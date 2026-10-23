"""Reading who owns a topic out of infrastructure-as-code, in the formats that
already exist.

The first version of this took a hand-written layout: dotted paths into an
organisation's own YAML (`produces: kafka.topics`). That is the right escape
hatch and it stays -- but writing it means reading your own infrastructure
repository and translating it, which is exactly the maintenance burden this
product exists to avoid. Most estates already declare access in a standard
form:

  strimzi         `KafkaUser` ACLs: Write on a topic is a producer, Read is a
                  consumer. The cluster enforces these, so they are the one
                  declaration that cannot quietly rot.
  asyncapi        the channel documents a service publishes for its own API.
  terraform-acl   `kafka_acl` / `confluent_kafka_acl` resources: the same ACLs,
                  managed from Terraform instead of a CRD.

Each reader turns one file into `{app, topic, edge, evidence}` rows -- the same
rows the hand-written layout produces, so everything downstream (app-to-repo
mapping, `who_owns`, `brief`) is unchanged.

Readers are deliberately strict about what they accept. A preset scans every
YAML file in a repository, so a file that is not what the preset reads must
produce nothing rather than a guess: Strimzi requires its own `apiVersion`,
AsyncAPI requires the `asyncapi` key.
"""

from __future__ import annotations

import re
from typing import Callable

import yaml

#: ACL operations that make an app a party to a topic. `Describe` and
#: `DescribeConfigs` are granted to nearly everyone and mean only that the app
#: may look the topic up, so they are not an edge.
_WRITE_OPS = {"write", "create"}
_READ_OPS = {"read"}


def _docs(text: str):
    """Every YAML document in a file, skipping any that will not parse.

    Kubernetes manifests arrive several to a file, and one unparseable
    document must not lose the others.
    """
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                yield doc
    except yaml.YAMLError:
        return


def _topic_name(value) -> str | None:
    """A literal topic name, or None for a pattern nobody can match against."""
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    return None if ("*" in name or "{" in name) else name


def _app_name(raw: str) -> str:
    """The service behind a principal or a document title.

    Strimzi users and Terraform principals are named after the service with a
    suffix (`orders-service-user`); an AsyncAPI title is prose. Both are
    normalised here rather than in the app-to-repo mapping, which must keep
    working for hand-written layouts too.
    """
    name = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    # Only when something recognisable is left: Strimzi's own example names a
    # user `my-user`, whose service is `my-user`, not `my`.
    stripped = re.sub(r"-(kafka-)?user$", "", name)
    return stripped if stripped.count("-") >= 1 else name


# ------------------------------------------------------------------ strimzi --

def strimzi(path: str, text: str) -> list[dict]:
    """`KafkaUser` ACLs.

    Prefix and literal-pattern ACLs both appear; only a literal one names a
    topic, and a prefix ACL is skipped rather than guessed at.
    """
    out: list[dict] = []
    for doc in _docs(text):
        if not str(doc.get("apiVersion", "")).startswith("kafka.strimzi.io/"):
            continue
        if doc.get("kind") != "KafkaUser":
            continue
        meta = doc.get("metadata") or {}
        app = _app_name(str(meta.get("name") or ""))
        acls = ((doc.get("spec") or {}).get("authorization") or {}).get("acls") or []
        if not app or not isinstance(acls, list):
            continue
        for acl in acls:
            if not isinstance(acl, dict):
                continue
            resource = acl.get("resource") or {}
            if not isinstance(resource, dict) or resource.get("type") != "topic":
                continue
            if resource.get("patternType", "literal") != "literal":
                continue
            topic = _topic_name(resource.get("name"))
            if not topic:
                continue
            ops = acl.get("operations") or ([acl["operation"]] if acl.get("operation") else [])
            ops = {str(o).lower() for o in ops if isinstance(o, str)}
            for edge, wanted in (("publishes", _WRITE_OPS), ("consumes", _READ_OPS)):
                if ops & wanted:
                    out.append({"app": app, "topic": topic, "edge": edge})
    return out


# ----------------------------------------------------------------- asyncapi --

def _channel_topic(channel_id: str, channel, id_is_address: bool) -> str | None:
    """The topic a channel maps to: its Kafka binding, then its address.

    In 2.x the channel key *is* the address; in 3.x it is a document-internal
    key (`costingResponseChannel`) that names no topic, so it is never used
    there. A parameterised address -- real ones look like
    `adeo-{env}-case-study-COSTING-REQUEST-{version}` -- names no single
    topic either, and must not fall back to anything.
    """
    if isinstance(channel, dict):
        binding = ((channel.get("bindings") or {}).get("kafka") or {})
        declared = [binding.get("topic") if isinstance(binding, dict) else None,
                    channel.get("address"), channel.get("x-topic")]
        if any(isinstance(d, str) and d.strip() for d in declared):
            for candidate in declared:
                name = _topic_name(candidate)
                if name:
                    return name
            return None
    return _topic_name(channel_id) if id_is_address else None


def asyncapi(path: str, text: str, perspective: str = "client") -> list[dict]:
    """AsyncAPI 2.x and 3.x service documents.

    3.x states the direction plainly: `action: send` is the application
    publishing. 2.x states it from the point of view of *the document's
    reader*, so a channel's `publish` operation is something the reader
    publishes and therefore something the application **consumes**. That
    inversion is the single most misread part of the specification, and real
    documents are written both ways, so `perspective: application` reads 2.x
    the other way round for an estate that authored it that way.
    """
    rows: list[dict] = []
    for doc in _docs(text):
        version = str(doc.get("asyncapi") or "")
        if not version:
            continue
        title = str(((doc.get("info") or {}).get("title")) or "")
        app = _app_name(title) or _app_name(path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        channels = doc.get("channels") or {}
        if not isinstance(channels, dict) or not app:
            continue
        if version.startswith("3."):
            for op in (doc.get("operations") or {}).values():
                if not isinstance(op, dict):
                    continue
                ref = ((op.get("channel") or {}).get("$ref") or "").rsplit("/", 1)[-1]
                topic = _channel_topic(ref, channels.get(ref), id_is_address=False)
                edge = {"send": "publishes", "receive": "consumes"}.get(op.get("action"))
                if topic and edge:
                    rows.append({"app": app, "topic": topic, "edge": edge})
            continue
        # 2.x
        sends, receives = ("subscribe", "publish") if perspective == "client" else ("publish", "subscribe")
        for channel_id, channel in channels.items():
            topic = _channel_topic(channel_id, channel, id_is_address=True)
            if not topic or not isinstance(channel, dict):
                continue
            for key, edge in ((sends, "publishes"), (receives, "consumes")):
                if channel.get(key):
                    rows.append({"app": app, "topic": topic, "edge": edge})
    return rows


# -------------------------------------------------------------- terraform ----

#: `resource "kafka_acl" "name" {` -- Mongey's provider, Confluent's, and any
#: other whose resource type ends in `kafka_acl`.
_TF_RESOURCE = re.compile(r'resource\s+"([\w-]*kafka_acl)"\s+"[\w-]+"\s*\{', re.I)
_TF_ATTR = re.compile(r"""^\s*(?P<key>[\w.]+)\s*=\s*(?P<value>"[^"]*"|\[[^\]]*\])""", re.M)


def _tf_blocks(text: str):
    """Each `*kafka_acl` resource body, by brace depth.

    HCL is not parsed here: the attributes wanted are flat strings, and a
    dependency-free reader keeps the product installable anywhere.
    """
    for m in _TF_RESOURCE.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        yield text[m.end():i - 1]


def terraform_acl(path: str, text: str) -> list[dict]:
    """`kafka_acl` resources: the same ACLs as Strimzi, managed in Terraform."""
    out: list[dict] = []
    for body in _tf_blocks(text):
        attrs: dict[str, list[str]] = {}
        for m in _TF_ATTR.finditer(body):
            raw = m.group("value")
            values = re.findall(r'"([^"]*)"', raw)
            attrs[m.group("key").rsplit(".", 1)[-1]] = values
        def one(*keys: str) -> str:
            for k in keys:
                if attrs.get(k):
                    return attrs[k][0]
            return ""
        if one("resource_type", "acl_resource_type").lower() not in ("topic", ""):
            continue
        if one("pattern_type", "acl_pattern_type", "resource_pattern_type").lower() not in ("literal", ""):
            continue
        topic = _topic_name(one("resource_name", "acl_resource_name"))
        principal = one("principal", "acl_principal")
        if not topic or not principal:
            continue
        app = _app_name(principal.split(":", 1)[-1])
        ops = {o.lower() for k in ("operation", "acl_operation", "operations") for o in attrs.get(k, [])}
        for edge, wanted in (("publishes", _WRITE_OPS), ("consumes", _READ_OPS)):
            if ops & wanted and app:
                out.append({"app": app, "topic": topic, "edge": edge})
    return out


#: format name -> (default file glob, reader). The glob is only a default: a
#: deployment that keeps these files somewhere unusual sets `files:` itself.
FORMATS: dict[str, tuple[str, Callable[[str, str], list[dict]]]] = {
    "strimzi": ("*.yaml", strimzi),
    "asyncapi": ("*asyncapi*.y*ml", asyncapi),
    "terraform-acl": ("*.tf", terraform_acl),
}


def read_file(fmt: str, path: str, text: str, options: dict | None = None) -> list[dict]:
    """One file, in the named format. Unknown format is a configuration error."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown declaration format {fmt!r}; known: {', '.join(sorted(FORMATS))}")
    reader = FORMATS[fmt][1]
    if fmt == "asyncapi":
        return asyncapi(path, text, (options or {}).get("perspective", "client"))
    return reader(path, text)
