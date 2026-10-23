"""Topic names reached through a variable. Every case is a shape from a real
estate, where literal matching found neither the producer nor the consumers
of a topic whose name the code never wrote where it used it."""

from __future__ import annotations

from ledger.core.bindings import alias_groups, bindings_in, usages_in


def idents(*bindings):
    out = {}
    for b in bindings:
        out.setdefault(b.ident, set()).add(b.raw)
    return out


class TestBindings:
    def test_deployment_values(self):
        got = bindings_in("helm/values-stage.yaml", '  KAFKA_TOPIC_INVOICE_EVENTS: "app.invoice-events"\n  LOG_LEVEL: info\n')
        assert [(b.raw, b.value) for b in got] == [("KAFKA_TOPIC_INVOICE_EVENTS", "app.invoice-events")]

    def test_env_default_binds_both_the_env_var_and_the_constant(self):
        got = bindings_in("app/topics.py", 'INVOICE_EVENTS_TOPIC = _topic("KAFKA_TOPIC_INVOICE_EVENTS", "invoice.events.v1")')
        assert {b.raw for b in got} == {"INVOICE_EVENTS_TOPIC", "KAFKA_TOPIC_INVOICE_EVENTS"}

    def test_typed_settings_field(self):
        got = bindings_in("app/config.py", '    kafka_topic_invoice_events: str = "invoice.events.v1"')
        assert got[0].value == "invoice.events.v1"

    def test_framework_parameter(self):
        got = bindings_in("app/config/parameters.yml", "    kafka_topic_invoice_events: 'invoice.events.v1'")
        assert got[0].raw == "kafka_topic_invoice_events"

    def test_an_identifier_that_does_not_say_topic_is_not_a_binding(self):
        """The one rule that keeps `NAME = "a.b"` from becoming a topic."""
        assert bindings_in("app/config.py", 'SERVICE_NAME = "orders.api"') == []

    def test_hosts_and_files_are_not_topics(self):
        assert bindings_in("x.yaml", "topic_host: kafka.example.com\ntopic_schema: events.json\n") == []


class TestUsages:
    ALL = idents(*bindings_in("c.yml", "kafka_topic_invoice_events: 'invoice.events.v1'\nKAFKA_EVENTS_TOPIC: 'app.invoice-events'"),
                 *bindings_in("t.py", 'INVOICE_EVENTS_TOPIC = _topic("X_TOPIC", "invoice.events.v1")'))

    def test_a_parameter_wired_into_a_consumer_consumes(self):
        text = ("    AppBundle\\Sync\\InvoiceStatusEventConsumer:\n        arguments:\n"
                "            - '@model.invoice'\n            - '%kafka_topic_invoice_events%'\n")
        got = usages_in("services.yml", text, self.ALL)
        assert [(u.ident, u.edge) for u in got] == [("kafka_topic_invoice_events", "consumes")]

    def test_a_constant_handed_to_a_producer_publishes(self):
        got = usages_in("relay.py", "await producer.send(INVOICE_EVENTS_TOPIC, payload)", self.ALL)
        assert got[0].edge == "publishes"

    def test_attribute_access_is_a_use(self):
        """`settings.KAFKA_EVENTS_TOPIC` was missed while a leading dot was excluded."""
        got = usages_in("worker.py", "consumer.subscribe(settings.KAFKA_EVENTS_TOPIC)", self.ALL)
        assert got and got[0].edge == "consumes"

    def test_the_binding_line_itself_is_not_a_use(self):
        assert usages_in("c.yml", "kafka_topic_invoice_events: 'invoice.events.v1'", self.ALL) == []

    def test_no_direction_signal_is_a_mention(self):
        assert usages_in("x.py", "log.info(INVOICE_EVENTS_TOPIC)", self.ALL)[0].edge == "mentions"


class TestAliases:
    def test_values_bound_to_one_identifier_are_one_topic(self):
        b = (bindings_in("helm/values-stage.yaml", 'KAFKA_TOPIC_INVOICE_EVENTS: "app.invoice-events"')
             + bindings_in(".env.example", "KAFKA_TOPIC_INVOICE_EVENTS=invoice.events.v1"))
        assert alias_groups(b) == [{"app.invoice-events", "invoice.events.v1"}]

    def test_chains_collapse(self):
        b = (bindings_in("a.yaml", "X_TOPIC: a.one") + bindings_in("b.py", 'Y_TOPIC = _topic("X_TOPIC", "a.two")')
             + bindings_in("c.yaml", "Y_TOPIC: a.three"))
        assert alias_groups(b) == [{"a.one", "a.two", "a.three"}]
