"""繁殖シミュレーションの仕様テスト (Airtable にはつながない。小さな架空データで確かめる)

実行: python -m unittest discover -s tests -v   (mating_simulation フォルダで)

ここにある期待値は、依頼者と合意した仕様そのもの。テストが落ちたら、
コードの不具合か、仕様を変えたのにテストを直していないかのどちらか。
仕様を変えるときは、テストの期待値も同じ変更で直す。
"""

import contextlib
import io
import json
import math
import random
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import apply_proposal  # noqa: E402
import mating_simulation as ms  # noqa: E402

# ----------------------------------------------------------------------
# 架空データ
#   系統: XKO-Het (目的系統・父系統) と WT-X (母系統)
#   実験計画: XKO-Het を 父 XKO-Het × 母 WT-X で 週1ペア
#   今日 2026-11-18 (水) → 起点 2026-11-20 (金)
# ----------------------------------------------------------------------
TODAY = date(2026, 11, 18)
START = date(2026, 11, 20)
HET, WT = "recStrainHet0001", "recStrainWT00001"


def rec(rid, **fields):
    return {"id": rid, "fields": fields}


def make_inputs(per_week=1, n_males=4, extra_mice=(), extra_matings=()):
    strains = [rec(HET, name="XKO-Het"), rec(WT, name="WT-X")]
    plans = [rec("recPlan000000001", id=1, Strain=[HET], Father=[HET], Mother=[WT], MatingPerWeek=per_week)]
    mice = [rec(f"recMaleHet{i:06d}", mouseId=f"M{i:03d}-XKO-Het", strainCode=[HET], sex="M",
                birthDate="2026-07-01", matingCount=0, location=["recLoc0000000001"], earTag=["recEar0000000001"])
            for i in range(n_males)]
    # 交配履歴の親 (引退済み。レシピの学習にだけ使う)
    mice += [rec("recOldFather0001", mouseId="M900", strainCode=[HET], sex="M", birthDate="2025-01-01", retire=True),
             rec("recOldMother0001", mouseId="F900", strainCode=[WT], sex="F", birthDate="2025-01-01", retire=True)]
    mice += list(extra_mice)
    breed = dict(purposeStrain=[HET], purpose="Breeding",
                 fatherMouseId=["recOldFather0001"], motherMouseId=["recOldMother0001"])
    matings = []
    # 結果が確定した直近1年 (交配日 2025-09-21 より後 〜 2026-09-21 以前): 成功6回 (各オス2・メス2) + 失敗4回
    for i in range(6):
        matings.append(rec(f"recMatOk{i:07d}", matingDate=f"2026-0{3 + i}-01", separation="2026-01-01",
                           male=2, female=2, **breed))
    for i in range(4):
        matings.append(rec(f"recMatFail{i:05d}", matingDate=f"2026-0{3 + i}-15", **breed))
    # 期間外 (使わない): 直近60日以内 (結果未確定) と 1年より前
    matings.append(rec("recMatRecent0001", matingDate="2026-10-15", separation="2026-11-01", male=10, female=10, **breed))
    matings.append(rec("recMatOld0000001", matingDate="2025-01-01", separation="2025-02-01", male=10, female=10, **breed))
    matings += list(extra_matings)
    return {"plans": plans, "strains": strains, "mice": mice, "matings": matings,
            "locations": [rec("recLoc0000000001", locationName="RackA")],
            "eartags": [rec("recEar0000000001", Name="E1")]}


def make_sim(**kw):
    return ms.Simulator(make_inputs(**kw), TODAY)


class NoNetwork:
    """テスト中に Airtable へつなごうとしたら失敗させる"""
    def __init__(self, *a, **k):
        raise AssertionError("テストから Airtable に接続しようとしました")


def setUpModule():
    ms.Airtable = NoNetwork
    ms.load_token = lambda: "test-token"


# ======================================================================
class TestUtilities(unittest.TestCase):
    def test_next_friday(self):
        self.assertEqual(ms.next_friday(date(2026, 11, 18)), date(2026, 11, 20))  # 水 → 同じ週の金
        self.assertEqual(ms.next_friday(date(2026, 11, 20)), date(2026, 11, 27))  # 金 → 翌週の金
        self.assertEqual(ms.next_friday(date(2026, 11, 21)), date(2026, 11, 27))  # 土 → 翌週の金

    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(ms.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(ms.add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(ms.add_months(date(2026, 11, 30), 3), date(2027, 2, 28))

    def test_add_months_fractional(self):
        # 端数は 1ヶ月 = 30.44日
        self.assertEqual(ms.add_months_f(date(2026, 1, 1), 3.5), date(2026, 4, 1) + timedelta(days=15))

    def test_wt_and_genotype_from_name(self):
        self.assertTrue(ms.is_wt("WT-Xpo7"))
        self.assertEqual(ms.genotype("WT-Cul1"), 0)
        self.assertEqual(ms.genotype("CUL1-KO-Het"), 1)
        self.assertEqual(ms.genotype("XKO-Homo"), 2)
        self.assertIsNone(ms.genotype("TNF-alpha"))

    def test_genotype_ratio_mendel(self):
        self.assertAlmostEqual(ms.genotype_ratio("A-Het", "A-Het", "WT-A"), 0.5)     # Het × WT → Het 1/2
        self.assertAlmostEqual(ms.genotype_ratio("A-Homo", "A-Het", "A-Het"), 0.25)  # Het × Het → Homo 1/4
        self.assertAlmostEqual(ms.genotype_ratio("A-Het", "A-Het", "A-Het"), 0.5)
        self.assertAlmostEqual(ms.genotype_ratio("WT-A", "WT-A", "WT-A"), 1.0)
        self.assertAlmostEqual(ms.genotype_ratio("A-Het", "A-Homo", "WT-A"), 1.0)
        self.assertEqual(ms.genotype_ratio("TNF-alpha", "TNF-alpha", "WT-A"), 1.0)   # 分からない系統は全頭

    def test_missing_limit_is_percent_of_required_pairs(self):
        self.assertAlmostEqual(ms.missing_limit(104, 1.0), 1.04)
        self.assertAlmostEqual(ms.missing_limit(52, 1.0), 0.52)
        self.assertAlmostEqual(ms.missing_limit(52, 2.0), 1.04)

    def test_mean_upper(self):
        self.assertAlmostEqual(ms.mean_upper([0.5] * 10), 0.5)
        self.assertEqual(ms.mean_upper([1]), math.inf)
        vals = [0, 1] * 50
        se = math.sqrt(sum((v - 0.5) ** 2 for v in vals) / 99 / 100)
        self.assertAlmostEqual(ms.mean_upper(vals), 0.5 + 1.96 * se)

    def test_paired_diff(self):
        mean, lo, hi = ms.paired_diff([1, 2, 3, 4], [2, 3, 4, 5])
        self.assertAlmostEqual(mean, 1.0)
        self.assertAlmostEqual(lo, 1.0)
        self.assertAlmostEqual(hi, 1.0)


# ======================================================================
class TestSettings(unittest.TestCase):
    """依頼者と合意した設定値。変えるときは利用ガイド・仕様書も直す"""
    def test_agreed_values(self):
        self.assertEqual(ms.WEEKS, 52)
        self.assertEqual((ms.MIN_AGE_MONTHS, ms.MAX_AGE_MONTHS), (2, 12))
        self.assertEqual(ms.COOLDOWN_DAYS, {"M": 21, "F": 56})
        self.assertEqual(ms.FEMALE_MAX_MATINGS, 4)
        self.assertEqual(ms.GESTATION_DAYS, 25)
        self.assertEqual(ms.FEMALES_PER_MALE, 2)
        self.assertEqual(ms.BREEDER_MALES, 2)
        self.assertEqual(ms.CAGE_COST_PER_DAY, 60)
        self.assertEqual(ms.FEMALES_PER_CAGE, 5)
        self.assertEqual(ms.OUTCOME_SETTLED_DAYS, 60)
        self.assertEqual(ms.LITTER_HISTORY_DAYS, 365)
        self.assertEqual(ms.MAX_MISSING_PCT, 1.0)
        self.assertEqual(ms.AUTO_RUNS, 1000)
        self.assertEqual([float(x) for x in ms.AUTO_HORIZONS.split(",")], [3, 3.5, 4, 4.5, 5, 5.5, 6])
        self.assertTrue(ms.HORIZON_DEFINITION.startswith("在庫の先読み期間："))


# ======================================================================
class TestPreprocessing(unittest.TestCase):
    def setUp(self):
        self.sim = make_sim()

    def test_start_is_next_friday_and_52_weeks(self):
        self.assertEqual(self.sim.start, START)
        self.assertEqual(self.sim.end, START + timedelta(weeks=52))

    def test_recipe_learned_from_breeding_history(self):
        self.assertEqual(self.sim.recipes[HET]["father"], HET)
        self.assertEqual(self.sim.recipes[HET]["mother"], WT)

    def test_genotype_ratio_of_recipe(self):
        self.assertAlmostEqual(self.sim.geno_ratio[HET], 0.5)

    def test_litter_window_and_failures(self):
        # 直近60日以内と1年より前は使わない。離乳記録も頭数もない交配は 0匹
        samples = self.sim.litters[HET]
        self.assertEqual(len(samples), 10)
        self.assertEqual(Counter(samples), Counter({(2, 2): 6, (0, 0): 4}))

    def test_expected_yield_is_unrounded_history_mean(self):
        self.assertAlmostEqual(self.sim.yields[HET]["M"], 1.2)
        self.assertAlmostEqual(self.sim.yields[HET]["F"], 1.2)

    def test_targets(self):
        # オス = ceil(週ペア × 休止3週 ÷ 2) + 維持繁殖用2匹。WT は購入するので目標なし
        self.assertEqual(self.sim.targets, {(HET, "M"): math.ceil(1 * 3 / 2) + 2})
        self.assertEqual(make_sim(per_week=2).targets[(HET, "M")], math.ceil(2 * 3 / 2) + 2)

    def test_plans_with_zero_per_week_are_ignored(self):
        self.assertEqual(make_sim(per_week=0).plans, [])

    def test_parse_mice_filters(self):
        extra = [
            rec("recRetired000001", strainCode=[HET], sex="M", birthDate="2026-07-01", retire=True),
            rec("recDead000000001", strainCode=[HET], sex="M", birthDate="2026-07-01", terminationDate="2026-11-01"),
            rec("recNoSex00000001", strainCode=[HET], birthDate="2026-07-01"),
            rec("recFemaleDone001", strainCode=[WT], sex="F", birthDate="2026-07-01", matingCount=4),
        ]
        sim = make_sim(extra_mice=extra)
        keys = {m.key for m in sim.base_mice}
        self.assertNotIn("recRetired000001", keys)
        self.assertNotIn("recDead000000001", keys)
        self.assertNotIn("recNoSex00000001", keys)
        done = next(m for m in sim.base_mice if m.key == "recFemaleDone001")
        self.assertEqual(done.done_date, START)  # 4回交配したメスは以降飼育しない

    def test_location_and_eartag_names(self):
        m = next(m for m in self.sim.base_mice if m.key == "recMaleHet000000")
        self.assertEqual((m.location, m.ear, m.label), ("RackA", "E1", "M000-XKO-Het"))


# ======================================================================
class TestMouseState(unittest.TestCase):
    def mouse(self, **kw):
        base = dict(key="k", strains=[HET], sex="M", birth=date(2026, 7, 1))
        base.update(kw)
        return ms.Mouse(**base)

    def test_age_window(self):
        m = self.mouse()
        self.assertFalse(m.movable(date(2026, 8, 31)))   # 生後2ヶ月未満
        self.assertTrue(m.movable(date(2026, 9, 1)))
        self.assertTrue(m.movable(date(2027, 7, 1)))     # 生後12ヶ月ちょうどまで
        self.assertFalse(m.movable(date(2027, 7, 2)))

    def test_female_max_matings(self):
        self.assertFalse(self.mouse(sex="F", mating_count=4).movable(date(2026, 10, 1)))
        self.assertTrue(self.mouse(sex="F", mating_count=3).movable(date(2026, 10, 1)))

    def test_male_two_females_same_week(self):
        d = date(2026, 10, 2)
        m = self.mouse(used_on=d, uses=1, next_available=d + timedelta(days=21))
        self.assertTrue(m.idle(d))                        # 同じ週の2匹目のメスと組める
        m.uses = 2
        self.assertFalse(m.idle(d))
        self.assertFalse(m.idle(d + timedelta(days=14)))  # 休止中
        self.assertTrue(m.idle(d + timedelta(days=21)))


# ======================================================================
class TestRunRules(unittest.TestCase):
    """1回分のシミュレーション (期待値) で守られるべき規則"""
    @classmethod
    def setUpClass(cls):
        cls.sim = make_sim()
        cls.res = cls.sim.run(3.5, record=True)

    def test_experiment_batches_two_pairs_every_two_weeks(self):
        # 週1ペアの計画は、2週に1回2ペアまとめて組む (起点の週から)
        req = self.res.exp_req[HET]
        self.assertEqual(len(req), 52)
        self.assertEqual(req[:6], [2, 0, 2, 0, 2, 0])
        self.assertEqual(sum(req), 52)

    def test_parents_are_in_age_window(self):
        for x in self.res.matings:
            for m in (x["father"], x["mother"]):
                self.assertGreaterEqual(x["date"], ms.add_months(m.birth, ms.MIN_AGE_MONTHS), m.key)
                self.assertLessEqual(x["date"], ms.add_months(m.birth, ms.MAX_AGE_MONTHS), m.key)

    def test_male_cooldown_and_two_per_week(self):
        by_male = defaultdict(list)
        for x in self.res.matings:
            by_male[x["father"].key].append(x["date"])
        for key, dates in by_male.items():
            per_day = Counter(dates)
            self.assertLessEqual(max(per_day.values()), ms.FEMALES_PER_MALE, key)
            days = sorted(per_day)
            for a, b in zip(days, days[1:]):
                self.assertGreaterEqual((b - a).days, ms.COOLDOWN_DAYS["M"], key)

    def test_female_cooldown_and_max_four(self):
        by_female = defaultdict(list)
        start_count = {}
        for x in self.res.matings:
            by_female[x["mother"].key].append(x["date"])
        for m in self.sim.base_mice:
            start_count[m.key] = m.mating_count
        for key, dates in by_female.items():
            dates.sort()
            self.assertEqual(len(dates), len(set(dates)), key)  # メスは同じ週に1回だけ
            self.assertLessEqual(start_count.get(key, 0) + len(dates), ms.FEMALE_MAX_MATINGS, key)
            for a, b in zip(dates, dates[1:]):
                self.assertGreaterEqual((b - a).days, ms.COOLDOWN_DAYS["F"], key)

    def test_wt_females_are_purchased(self):
        bought = [m for m in self.res.virtual_mice if m.origin == "購入"]
        self.assertTrue(bought)
        self.assertTrue(all(m.strains == [WT] and m.sex == "F" for m in bought))

    def test_breeding_uses_recipe_and_pups_arrive_after_gestation(self):
        breeding = [x for x in self.res.matings if x["purpose"] == "Breeding"]
        self.assertTrue(breeding)
        for x in breeding:
            self.assertEqual(x["father"].strains[0], HET)
            self.assertEqual(x["mother"].strains[0], WT)
        pups = [m for m in self.res.virtual_mice if m.origin == "繁殖"]
        breed_dates = {x["date"] for x in breeding}
        for m in pups:
            self.assertIn(m.birth - timedelta(days=ms.GESTATION_DAYS), breed_dates)

    def test_expected_pups_carry_fraction(self):
        # 期待値 1.2匹 × Het 1/2 = 0.6匹 / 交配。端数を持ち越すので、5回で 3匹
        sim = make_sim()
        sim.run(3.5)
        sim.frac_pups = defaultdict(float)
        got = [sim._litter(HET)[0] for _ in range(5)]
        self.assertEqual(sum(got), 3)

    def test_deterministic_and_seeded_runs_are_reproducible(self):
        a = make_sim().run(3.5)
        b = make_sim().run(3.5)
        self.assertEqual([(x["date"], x["purpose"]) for x in a.matings], [(x["date"], x["purpose"]) for x in b.matings])
        c = make_sim().run(3.5, random.Random(7))
        d = make_sim().run(3.5, random.Random(7))
        self.assertEqual(len(c.virtual_mice), len(d.virtual_mice))
        self.assertEqual(c.exp_done[HET], d.exp_done[HET])

    def test_this_week_list_shows_ids_and_places(self):
        actions, purchases = ms.build_actions(self.sim, self.res, ms.WT_LEAD_WEEKS)
        entries = actions[HET]
        self.assertTrue(entries)
        exp = [mt for a in entries for mt in a["matings"] if mt["purpose"] == "実験用"]
        self.assertEqual(len(exp), 2)  # 起点の週は実験用2ペア
        for a in entries:
            self.assertLessEqual(len(a["matings"]), ms.FEMALES_PER_MALE)  # オス1匹にメス最大2匹
            self.assertTrue(a["father"]["label"])
        for row in purchases[HET]:
            need, order_by = date.fromisoformat(row["need"]), date.fromisoformat(row["order_by"])
            self.assertEqual(need - order_by, timedelta(weeks=ms.WT_LEAD_WEEKS))


# ======================================================================
class TestCages(unittest.TestCase):
    def test_males_one_per_cage_females_five_per_cage(self):
        sim = make_sim(n_males=0, extra_mice=[
            rec("recM00000000001", strainCode=[HET], sex="M", birthDate="2026-07-01"),
            *[rec(f"recF{i:011d}", strainCode=[WT], sex="F", birthDate="2026-07-01") for i in range(6)],
        ])
        sim.plans, sim.plans_by_father, sim.targets, sim.target_groups = [], {}, {}, {}
        res = sim.run(3.5)
        self.assertEqual(res.cages_by_strain[HET][0], 1)
        self.assertEqual(res.cages_by_strain[WT][0], 2)  # 6匹 → ceil(6/5)
        # 生後12ヶ月 (2027-07-01) の翌日からは飼育しない
        retire = (date(2027, 7, 2) - START).days
        self.assertEqual(res.cages_by_strain[HET][retire - 1], 1)
        self.assertEqual(res.cages_by_strain[HET][retire], 0)


# ======================================================================
class TestAutoHorizon(unittest.TestCase):
    """在庫の先読み期間の自動判定"""
    def make(self, rows):
        """rows: {horizon: (cost, [欠品ペア per run])}、必要な実験ペア 100"""
        sim = make_sim()
        summary, per_run = [], {}
        for h, (cost, missing) in rows.items():
            summary.append({"group": HET, "horizon": h, "required": 100, "missing_mean": sum(missing) / len(missing),
                            "shortage_prob": sum(v > 0 for v in missing) / len(missing), "cost_mean": cost})
            per_run[(HET, h)] = {"missing": missing, "cost": [cost] * len(missing)}
        return sim, summary, per_run

    def test_picks_cheapest_within_limit(self):
        # 上限 = 100 × 1% = 1 ペア
        sim, summary, per_run = self.make({
            3.0: (200_000, [3] * 50 + [0] * 50),      # 平均1.5 → 超える
            3.5: (220_000, [1] * 20 + [0] * 80),      # 平均0.2 → 満たす
            4.5: (260_000, [0] * 100),                # 満たすが高い
        })
        chosen, notes = ms.choose_horizons(sim, summary, per_run, 1.0)
        self.assertEqual(chosen[HET], 3.5)
        self.assertEqual(notes[0]["kind"], "auto")
        self.assertAlmostEqual(notes[0]["missing_limit"], 1.0)
        self.assertIn("在庫の先読み期間は3.5ヶ月にします", notes[0]["text"])
        self.assertIn("必要な実験ペア 100 の 1%", notes[0]["text"])

    def test_judged_on_upper_bound_not_mean(self):
        # 平均 0.9 は上限1以下だが、95%範囲の上側は上限を超える → 選ばない
        noisy = [9] * 10 + [0] * 90
        self.assertGreater(ms.mean_upper(noisy), 1.0)
        sim, summary, per_run = self.make({3.0: (200_000, noisy), 4.0: (240_000, [0] * 100)})
        chosen, _ = ms.choose_horizons(sim, summary, per_run, 1.0)
        self.assertEqual(chosen[HET], 4.0)

    def test_shortage_warning_when_nothing_passes(self):
        sim, summary, per_run = self.make({3.0: (200_000, [5] * 100), 4.0: (240_000, [2] * 100)})
        chosen, notes = ms.choose_horizons(sim, summary, per_run, 1.0)
        self.assertEqual(chosen[HET], 4.0)  # 欠品が最も少ない期間
        self.assertEqual(notes[0]["kind"], "shortage")

    def test_limit_scales_with_percent(self):
        sim, summary, per_run = self.make({3.0: (200_000, [1] * 100), 4.0: (240_000, [0] * 100)})
        self.assertEqual(ms.choose_horizons(sim, summary, per_run, 1.0)[0][HET], 3.0)   # 1 ≤ 1
        self.assertEqual(ms.choose_horizons(sim, summary, per_run, 0.5)[0][HET], 4.0)   # 1 > 0.5


# ======================================================================
class TestMainOutputs(unittest.TestCase):
    """main() の出力 (proposal.json / report.html)。架空データで、試行回数を減らして実行"""
    def run_main(self, inputs, *args):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = (ms.fetch_inputs, ms.SCRIPT_DIR, sys.argv)
        ms.fetch_inputs = lambda at: inputs
        ms.SCRIPT_DIR = Path(tmp.name)
        ms.Airtable = lambda token: None
        sys.argv = ["mating_simulation.py", "--today", TODAY.isoformat(), *args]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ms.main()
        finally:
            ms.fetch_inputs, ms.SCRIPT_DIR, sys.argv = saved
            ms.Airtable = NoNetwork
        out = next((Path(tmp.name) / "results").iterdir())
        return out

    def test_auto_writes_proposal_and_report(self):
        out = self.run_main(make_inputs(), "--auto", "--runs", "3", "--horizons", "3,4")
        prop = json.loads((out / "proposal.json").read_text(encoding="utf-8"))
        self.assertEqual(prop["mating_date"], START.isoformat())
        self.assertEqual(prop["horizon_definition"], ms.HORIZON_DEFINITION)
        self.assertTrue((out / "report.html").exists())
        g = prop["groups"][0]
        self.assertEqual(g["name"], "XKO-Het")
        self.assertIn(g["horizon"], (3.0, 4.0))
        self.assertIn(g["auto"]["kind"], ("auto", "shortage"))
        self.assertAlmostEqual(g["auto"]["missing_limit"], 52 * ms.MAX_MISSING_PCT / 100)
        for mt in g["matings"]:
            self.assertIn(mt["purpose"], ("Experiment", "Breeding"))
            self.assertEqual(mt["purposeStrainId"], HET)
            self.assertTrue(mt["father"].get("id") or mt["father"].get("virtual"))
            self.assertTrue(mt["mother"].get("id") or mt["mother"].get("virtual"))
        html = (out / "report.html").read_text(encoding="utf-8")
        self.assertIn("在庫の先読み期間", html)
        self.assertNotIn("見る期間", html)

    def test_no_plans_does_not_crash(self):
        out = self.run_main(make_inputs(per_week=0), "--auto", "--runs", "3")
        prop = json.loads((out / "proposal.json").read_text(encoding="utf-8"))
        self.assertTrue(prop["no_plans"])
        self.assertEqual(prop["groups"], [])
        self.assertFalse((out / "report.html").exists())


# ======================================================================
class TestApplyProposal(unittest.TestCase):
    """Mating への登録内容 (--emit-json)。Airtable には書き込まない"""
    def proposal(self, mating_date="2026-11-20"):
        exp = {"purpose": "Experiment", "purposeStrainId": HET, "purposeStrain": "XKO-Het",
               "father": {"id": "recFather0000001", "label": "M001"}, "mother": {"id": "recMother0000001", "label": "F001"}}
        dup = {**exp, "mother": {"id": "recMother0000002", "label": "F002"}}
        virtual = {**exp, "purpose": "Breeding",
                   "mother": {"label": "購入予定 WT-X メス（11/20 必要）", "virtual": True}}
        return {"mating_date": mating_date, "horizon_label": "3ヶ月",
                "groups": [{"name": "XKO-Het", "matings": [exp, dup, virtual]}]}

    def run_apply(self, prop, *args, existing=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "20261118_100000" / "proposal.json"
        path.parent.mkdir()
        path.write_text(json.dumps(prop, ensure_ascii=False), encoding="utf-8")
        saved = (apply_proposal.find_scheduled, apply_proposal.create, sys.argv)
        apply_proposal.find_scheduled = lambda at, d: list(existing)
        apply_proposal.create = lambda at, fields: self.fail("--apply なしで書き込もうとしました")
        ms.Airtable = lambda token: None
        sys.argv = ["apply_proposal.py", str(path), "--today", "2026-11-18", *args]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                apply_proposal.main()
        finally:
            apply_proposal.find_scheduled, apply_proposal.create, sys.argv = saved
            ms.Airtable = NoNetwork
        return buf.getvalue()

    def test_emit_json_fields_and_skips(self):
        existing = [rec("recExisting00001", fatherMouseId=["recFather0000001"], motherMouseId=["recMother0000002"])]
        out = json.loads(self.run_apply(self.proposal(), "--emit-json", existing=existing))
        self.assertEqual(out["table"], "Mating")
        self.assertEqual(len(out["records"]), 1)
        fields = out["records"][0]["fields"]
        # 入れる項目はこれだけ。matingDate と indexInYear は入れない
        self.assertEqual(set(fields), {"scheduledDate", "purposeStrain", "purpose", "fatherMouseId", "motherMouseId", "Note"})
        self.assertEqual(fields["scheduledDate"], "2026-11-20")
        self.assertEqual(fields["purposeStrain"], [HET])
        self.assertEqual(fields["fatherMouseId"], ["recFather0000001"])
        self.assertIn("シミュレーション提案", fields["Note"])
        self.assertEqual(len(out["skipped"]), 2)  # すでにある交配・購入予定の個体
        self.assertTrue(any("すでにある" in s for s in out["skipped"]))
        self.assertTrue(any("まだいない個体" in s for s in out["skipped"]))

    def test_dry_run_does_not_write(self):
        text = self.run_apply(self.proposal())
        self.assertIn("Airtable は変更していません", text)

    def test_past_mating_date_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_apply(self.proposal(mating_date="2026-11-13"), "--emit-json")


if __name__ == "__main__":
    unittest.main()
