"""Standard declaration formats.

Every fixture here is the shape the tool itself emits -- a Strimzi `KafkaUser`
as the operator writes it, an AsyncAPI document as a generator produces it,
`kafka_acl` resources from both common Terraform providers -- because the
point of a preset is that nobody has to describe their own layout.
"""

from __future__ import annotations

import pytest

from ledger.core.declarations import _app_name, asyncapi, read_file, strimzi, terraform_acl

STRIMZI = """
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaUser
metadata:
  name: orders-service-user
  labels:
    strimzi.io/cluster: main
spec:
  authentication:
    type: scram-sha-512
  authorization:
    type: simple
    acls:
      - resource:
          type: topic
          name: orders.events
          patternType: literal
        operations: [Write, Describe]
      - resource:
          type: topic
          name: payments.events
          patternType: literal
        operations: [Read]
      - resource:
          type: topic
          name: audit.
          patternType: prefix
        operations: [Read]
      - resource:
          type: group
          name: orders
        operations: [Read]
---
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: orders.events
spec:
  partitions: 12
"""


class TestStrimzi:
    def test_write_publishes_and_read_consumes(self):
        got = {(r["app"], r["topic"], r["edge"]) for r in strimzi("k8s/users.yaml", STRIMZI)}
        assert got == {("orders-service", "orders.events", "publishes"),
                       ("orders-service", "payments.events", "consumes")}

    def test_describe_alone_is_not_a_party(self):
        """Nearly every user is granted Describe; it says nothing about use."""
        doc = STRIMZI.replace("operations: [Write, Describe]", "operations: [Describe]")
        assert [r for r in strimzi("u.yaml", doc) if r["topic"] == "orders.events"] == []

    def test_a_prefix_acl_names_no_topic(self):
        assert not [r for r in strimzi("u.yaml", STRIMZI) if r["topic"].startswith("audit")]

    def test_other_kubernetes_manifests_declare_nothing(self):
        assert strimzi("k8s/deploy.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n") == []

    def test_the_legacy_singular_operation_key(self):
        doc = ("apiVersion: kafka.strimzi.io/v1beta1\nkind: KafkaUser\nmetadata:\n  name: legacy\n"
               "spec:\n  authorization:\n    acls:\n      - resource: {type: topic, name: a.b}\n        operation: Write\n")
        assert strimzi("u.yaml", doc) == [{"app": "legacy", "topic": "a.b", "edge": "publishes"}]


ASYNCAPI_2 = """
asyncapi: 2.6.0
info:
  title: Orders Service
  version: 1.0.0
channels:
  orders.events:
    subscribe:
      operationId: publishOrderEvent
      message:
        $ref: '#/components/messages/OrderEvent'
  payments.events:
    publish:
      operationId: onPaymentEvent
"""

ASYNCAPI_3 = """
asyncapi: 3.0.0
info:
  title: Orders Service
channels:
  orderEvents:
    address: orders.events
  paymentEvents:
    address: payments.events
operations:
  sendOrderEvent:
    action: send
    channel:
      $ref: '#/channels/orderEvents'
  onPaymentEvent:
    action: receive
    channel:
      $ref: '#/channels/paymentEvents'
"""


class TestAsyncApi:
    def test_v3_states_the_direction_plainly(self):
        got = {(r["app"], r["topic"], r["edge"]) for r in asyncapi("docs/asyncapi.yaml", ASYNCAPI_3)}
        assert got == {("orders-service", "orders.events", "publishes"),
                       ("orders-service", "payments.events", "consumes")}

    def test_v2_is_written_from_the_readers_point_of_view(self):
        """`subscribe` is what the reader subscribes to, so the app publishes it.

        Read the other way round, every producer and consumer in the estate
        swaps sides -- which is why this has its own test and its own knob.
        """
        got = {(r["topic"], r["edge"]) for r in asyncapi("docs/asyncapi.yaml", ASYNCAPI_2)}
        assert got == {("orders.events", "publishes"), ("payments.events", "consumes")}

    def test_v2_read_the_way_some_estates_write_it(self):
        got = {(r["topic"], r["edge"]) for r in asyncapi("a.yaml", ASYNCAPI_2, perspective="application")}
        assert got == {("orders.events", "consumes"), ("payments.events", "publishes")}

    def test_the_kafka_binding_names_the_topic_the_channel_does_not(self):
        doc = ("asyncapi: 3.0.0\ninfo: {title: Billing}\nchannels:\n  invoiceIssued:\n"
               "    bindings:\n      kafka:\n        topic: billing.invoice-issued\n"
               "operations:\n  send:\n    action: send\n    channel: {$ref: '#/channels/invoiceIssued'}\n")
        assert asyncapi("a.yml", doc) == [{"app": "billing", "topic": "billing.invoice-issued", "edge": "publishes"}]

    def test_a_parameterised_address_names_no_topic(self):
        doc = ASYNCAPI_3.replace("address: orders.events", "address: orders.{tenant}.events")
        assert [r["topic"] for r in asyncapi("a.yaml", doc)] == ["payments.events"]

    def test_an_openapi_file_declares_nothing(self):
        assert asyncapi("openapi.yaml", "openapi: 3.0.0\ninfo: {title: Orders}\npaths: {}\n") == []


TERRAFORM = """
resource "kafka_acl" "orders_write" {
  resource_name       = "orders.events"
  resource_type       = "Topic"
  acl_principal       = "User:orders-service"
  acl_host            = "*"
  acl_operation       = "Write"
  acl_permission_type = "Allow"
}

resource "confluent_kafka_acl" "payments_read" {
  kafka_cluster { id = var.cluster }
  resource_type = "TOPIC"
  resource_name = "payments.events"
  pattern_type  = "LITERAL"
  principal     = "User:orders-service"
  operation     = "READ"
  permission    = "ALLOW"
}

resource "kafka_acl" "audit_prefix" {
  resource_name      = "audit."
  resource_type      = "Topic"
  acl_principal      = "User:auditor"
  acl_operation      = "Read"
  acl_pattern_type   = "Prefixed"
}

resource "kafka_topic" "orders" {
  name       = "orders.events"
  partitions = 12
}
"""


class TestTerraformAcl:
    def test_both_providers_spellings(self):
        got = {(r["app"], r["topic"], r["edge"]) for r in terraform_acl("kafka/acls.tf", TERRAFORM)}
        assert got == {("orders-service", "orders.events", "publishes"),
                       ("orders-service", "payments.events", "consumes")}

    def test_a_prefixed_acl_names_no_topic(self):
        assert not [r for r in terraform_acl("a.tf", TERRAFORM) if r["app"] == "auditor"]

    def test_a_nested_block_does_not_end_the_resource(self):
        """`kafka_cluster { id = ... }` sits before the attributes that matter."""
        assert any(r["topic"] == "payments.events" for r in terraform_acl("a.tf", TERRAFORM))


class TestDispatch:
    def test_an_unknown_format_names_the_ones_that_exist(self):
        with pytest.raises(ValueError, match="strimzi"):
            read_file("kafka-acls", "a.yaml", "")

    def test_the_perspective_option_reaches_the_reader(self):
        got = read_file("asyncapi", "a.yaml", ASYNCAPI_2, {"perspective": "application"})
        assert {(r["topic"], r["edge"]) for r in got} == {("orders.events", "consumes"),
                                                          ("payments.events", "publishes")}


class TestThroughTheConfig:
    """A deployment names a format and nothing else: no layout, no paths."""

    def read(self, tree, spec):
        from ledger.adapters.vcs.base import BranchRef, RepoRef
        from ledger.adapters.vcs.fake import FakeAdapter
        from ledger.pipeline.wiring import read_declarations
        vcs = FakeAdapter(repos=[RepoRef(full_name="infra", default_branch="main", host="fake")],
                          branches={"infra": [BranchRef(repo="infra", name="main", head_sha="a" * 40)]},
                          tree={"infra": tree})
        return read_declarations(vcs, spec, "main")

    def test_strimzi_needs_only_the_repository(self):
        got = self.read({"k8s/users/orders.yaml": STRIMZI,
                         "k8s/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n"},
                        {"repo": "infra", "format": "strimzi"})
        assert {(r["app"], r["edge"]) for r in got} == {("orders-service", "publishes"),
                                                        ("orders-service", "consumes")}
        assert got[0]["evidence"] == "infra:k8s/users/orders.yaml"

    def test_the_default_glob_finds_terraform(self):
        got = self.read({"kafka/acls.tf": TERRAFORM, "README.md": "# infra"},
                        {"repo": "infra", "format": "terraform-acl"})
        assert {r["topic"] for r in got} == {"orders.events", "payments.events"}

    def test_ignore_applies_to_a_preset_too(self):
        doc = STRIMZI.replace("name: payments.events", "name: payments.events.dlq")
        got = self.read({"u.yaml": doc}, {"repo": "infra", "format": "strimzi", "ignore": ["*.dlq"]})
        assert [r["topic"] for r in got] == ["orders.events"]


class TestConfigValidation:
    def load(self, tmp_path, body):
        from ledger.config import Config
        p = tmp_path / "l.yml"
        p.write_text("org: o\nproject_keys: [ABC]\n" + body)
        return Config.load(str(p))

    def test_a_misspelt_format_fails_at_load(self, tmp_path):
        with pytest.raises(ValueError, match="unknown declaration format"):
            self.load(tmp_path, "declarations:\n  - repo: infra\n    format: strimzy\n")

    def test_a_declaration_that_declares_nothing_fails_at_load(self, tmp_path):
        """It used to sync cleanly and report zero declared edges, which looks
        exactly like an estate that declares none."""
        with pytest.raises(ValueError, match="declares nothing"):
            self.load(tmp_path, "declarations:\n  - repo: infra\n    files: '*.yaml'\n")

    def test_a_format_is_enough(self, tmp_path):
        c = self.load(tmp_path, "declarations:\n  - repo: infra\n    format: asyncapi\n")
        assert c.declarations[0]["format"] == "asyncapi"


class TestUpstreamExamples:
    """The documents the tools themselves ship. Each of these found a bug.

    Fixtures written by hand agree with the reader by construction. These are
    copied from upstream, and they disagreed: Strimzi's own example is named
    `my-user`, and AsyncAPI's own examples address channels in ways a
    hand-written fixture never would.
    """

    #: strimzi-kafka-operator, examples/user/kafka-user.yaml, verbatim.
    STRIMZI_UPSTREAM = """apiVersion: kafka.strimzi.io/v1
kind: KafkaUser
metadata:
  name: my-user
  labels:
    strimzi.io/cluster: my-cluster
spec:
  authentication:
    type: tls
  authorization:
    type: simple
    acls:
      # Example consumer Acls for topic my-topic using consumer group my-group
      - resource:
          type: topic
          name: my-topic
          patternType: literal
        operations:
          - Describe
          - Read
        host: "*"
      - resource:
          type: group
          name: my-group
          patternType: literal
        operations:
          - Read
        host: "*"
      # Example Producer Acls for topic my-topic
      - resource:
          type: topic
          name: my-topic
          patternType: literal
        operations:
          - Create
          - Describe
          - Write
        host: "*"
"""

    def test_a_service_whose_name_ends_in_user_keeps_it(self):
        """`my-user` is the service. Stripping the suffix renamed it to `my`."""
        got = strimzi("user/kafka-user.yaml", self.STRIMZI_UPSTREAM)
        assert {(r["app"], r["topic"], r["edge"]) for r in got} == {
            ("my-user", "my-topic", "consumes"), ("my-user", "my-topic", "publishes")}

    def test_a_suffix_is_still_stripped_where_a_service_remains(self):
        assert _app_name("orders-service-user") == "orders-service"

    def test_a_v3_channel_with_no_address_names_no_topic(self):
        """asyncapi/spec, adeo-kafka-request-reply: `address: null` on a reply
        channel. The channel id is a document key, not a topic."""
        doc = ("asyncapi: 3.1.0\ninfo: {title: Costing}\nchannels:\n"
               "  costingResponseChannel:\n    address: null\n"
               "operations:\n  reply:\n    action: send\n"
               "    channel: {$ref: '#/channels/costingResponseChannel'}\n")
        assert asyncapi("adeo.yml", doc) == []

    def test_a_parameterised_v3_address_names_no_topic(self):
        """asyncapi/spec, streetlights-kafka: every address carries
        `{streetlightId}`. There is no single topic to be a party to."""
        doc = ("asyncapi: 3.1.0\ninfo: {title: Streetlights Kafka API}\nchannels:\n"
               "  lightingMeasured:\n"
               "    address: 'smartylighting.streetlights.1.0.event.{streetlightId}.lighting.measured'\n"
               "operations:\n  receive:\n    action: receive\n"
               "    channel: {$ref: '#/channels/lightingMeasured'}\n")
        assert asyncapi("streetlights.yml", doc) == []

    def test_the_v2_inversion_against_upstreams_own_operation_ids(self):
        """asyncapi/spec v2.6.0, streetlights-kafka.yml. Upstream names the
        `publish` operation `receiveLightMeasurement` and the `subscribe`
        operation `turnOn` -- which is the specification's reading, and the
        opposite of how the keys read. The `{streetlightId}` segment is
        dropped here so the channels name topics; nothing else is changed.
        """
        doc = ("asyncapi: '2.6.0'\ninfo:\n  title: Streetlights Kafka API\nchannels:\n"
               "  smartylighting.streetlights.1.0.event.lighting.measured:\n"
               "    publish:\n      operationId: receiveLightMeasurement\n"
               "  smartylighting.streetlights.1.0.action.turn.on:\n"
               "    subscribe:\n      operationId: turnOn\n")
        got = {r["topic"].rsplit(".", 2)[0].rsplit(".", 1)[-1]: r["edge"] for r in asyncapi("s.yml", doc)}
        assert got == {"event": "consumes", "action": "publishes"}


class TestSecretsBesideTheConfig:
    """Credentials belong with the deployment, not with the product and not in
    the config file -- which is why every one is named there as an environment
    variable rather than given a value."""

    def write(self, tmp_path, env_body):
        cfg = tmp_path / "ledger.yml"
        cfg.write_text("org: o\nproject_keys: [ABC]\n")
        (tmp_path / ".env").write_text(env_body)
        return cfg

    def test_it_reads_a_dotenv_next_to_the_config(self, tmp_path, monkeypatch):
        from ledger.config import Config
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        Config.load(str(self.write(tmp_path, "LEDGER_GITHUB_TOKEN=ghp_example\n")))
        import os
        assert os.environ["LEDGER_GITHUB_TOKEN"] == "ghp_example"

    def test_the_environment_always_wins(self, tmp_path, monkeypatch):
        """A shell or a service manager must be able to override the file."""
        from ledger.config import Config
        monkeypatch.setenv("LEDGER_GITHUB_TOKEN", "from-the-shell")
        Config.load(str(self.write(tmp_path, "LEDGER_GITHUB_TOKEN=from-the-file\n")))
        import os
        assert os.environ["LEDGER_GITHUB_TOKEN"] == "from-the-shell"

    def test_comments_blanks_quotes_and_export_are_handled(self, tmp_path, monkeypatch):
        from ledger.config import Config
        for v in ("A_ONE", "A_TWO", "A_THREE"):
            monkeypatch.delenv(v, raising=False)
        names = Config.load_secrets(self.write(
            tmp_path, "# a comment\n\nA_ONE='x'\nexport A_TWO=\"y\"\nA_THREE=z\nnot a line\n"))
        import os
        assert sorted(names) == ["A_ONE", "A_THREE", "A_TWO"]
        assert (os.environ["A_ONE"], os.environ["A_TWO"], os.environ["A_THREE"]) == ("x", "y", "z")

    def test_no_dotenv_is_not_an_error(self, tmp_path):
        from ledger.config import Config
        cfg = tmp_path / "ledger.yml"
        cfg.write_text("org: o\nproject_keys: [ABC]\n")
        assert Config.load_secrets(cfg) == []

    def test_it_returns_names_never_values(self, tmp_path, monkeypatch):
        from ledger.config import Config
        monkeypatch.delenv("A_SECRET", raising=False)
        got = Config.load_secrets(self.write(tmp_path, "A_SECRET=hunter2\n"))
        assert got == ["A_SECRET"] and "hunter2" not in str(got)
