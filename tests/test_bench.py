"""The bench reads past briefs back as items and scores the ranking on them."""

import json

from ledger import bench


def case(n, text, label, sid="s1", branch="b", **kw):
    return {"sid": sid, "branch": branch, "n": n, "text": text, "label": label, "verdict": "REVIEW_WITH", **kw}


PAIR = "in flight: leads sk-1 by ann — same author, pushed around the same time; no ticket links them"
STAGE = "in flight: crm 0295f3833b5e (in stage, 0295f3833b5e) by kim\n    relation: Ledger found no topic, route or table named by both diffs. That is"
TABLE = "shared contract table `user`: also in a, b"


class TestReadBack:
    def test_link_reasons_are_read_from_the_text(self):
        assert bench.item_of(case(1, PAIR, "useful")).link == "author"
        assert bench.item_of(case(2, STAGE, "noise")).link == "integration"
        assert bench.item_of(case(3, TABLE, "noise")).kind == "contract"


class TestScore:
    ROWS = [case(1, PAIR, "useful"), case(2, STAGE, "noise"), case(3, TABLE, "noise")]

    def test_before_shows_everything(self):
        s = bench.score(self.ROWS, everything=True)
        assert (s.shown, s.useful, s.noise_out) == (3, 1, 0)

    def test_ranked_leads_with_the_link_the_work_made(self):
        s = bench.score(self.ROWS)
        assert (s.shown, s.useful, s.noise_out) == (1, 1, 2) and s.precision == 1.0

    def test_a_model_score_can_promote(self):
        rows = [case(1, STAGE, "useful", h2_touches=3)]
        assert bench.score(rows).useful == 0
        assert bench.score(rows, touches=True).useful == 1

    def test_plan_context_is_not_ranked(self):
        rows = [dict(case(1, TABLE, "useful"), verdict="PLAN")]
        assert bench.score(rows).useful == 1

    def test_the_table_reads_a_file(self, tmp_path):
        p = tmp_path / "cases.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in self.ROWS))
        out = bench.table(bench.read_cases(p))
        assert "| everything shown (before) | 3 | 1/1 | 0/2 | 33% |" in out


class TestRenderedForAReviewer:
    def test_nothing_strong_is_said_plainly(self):
        text = bench.render_ranked([case(1, STAGE, "noise")], "Ledger brief for core @ x: REVIEW_WITH", touches=False)
        assert "nothing in flight or pending that this diff touches" in text and "0295f3833b5e" not in text

    def test_the_lead_carries_how_to_check_it(self):
        text = bench.render_ranked([case(1, PAIR, "useful")], "Ledger brief for core @ x: REVIEW_WITH")
        assert "how to check: read leads sk-1" in text
