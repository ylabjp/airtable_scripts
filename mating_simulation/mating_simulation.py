"""
Resource_Mice 繁殖シミュレーション
期間: 実行日の次の金曜日 (交配日) から 52 週間 (実行日が金曜日なら翌週の金曜日から)

運用:
  毎週 (金曜の交配の前) に実行し、系統ごとに次を確認する
    - 今週金曜の交配リスト (どのオスとどのメスを交配するか。個体ID・飼育場所つき)
    - 2〜4週先の見通し (交配件数と使えるオスの数)
    - WTメスの購入リスト (必要日と発注期限)
  月1回と実験計画の変更時に、「何ヶ月先の動けるマウスを数えるか」の比較を見直す

使い方:
  画面から: start_webapp.bat をダブルクリック (または python webapp.py) して、ブラウザで条件を選んで実行
  python mating_simulation.py                       # 交配リスト + 比較 (既定 3〜5ヶ月, 各200回)
  python mating_simulation.py --runs 0              # 交配リストだけ (比較を省略して速く)
  python mating_simulation.py --horizon 4 --horizons 3,3.5,4,4.5,5,6 --runs 500
  python mating_simulation.py --auto                # 見る期間を系統ごとに自動で決める (年間欠品ペア 0.5 以下で最も安い期間, 各1000回)
  出力: results/<日時>/report.html, actions.csv, purchases.csv, comparison_summary.csv, comparison_weekly.csv

Airtable は読み取りのみ (シミュレーション結果は書き込まない)。
AIRTABLE_TOKEN を環境変数、またはこのスクリプトと同じフォルダの .env に設定 (スコープ: data.records:read)
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE_ID = "appBwTnvAtEXHip0i"  # Resource_Mice
API_URL = f"https://api.airtable.com/v0/{BASE_ID}"

T_PLAN = "Plan4Experiment"
T_MICE = "MiceForReproduction"
T_STRAIN = "Strain"
T_MATING = "Mating"
T_LOCATION = "Location"
T_EARTAG = "EarTag"

# ---- 交配の条件 ----
WEEKS = 52
MIN_AGE_MONTHS = 2
MAX_AGE_MONTHS = 12
COOLDOWN_DAYS = {"M": 21, "F": 56}
COOLDOWN_WEEKS = {"M": 3, "F": 8}
FEMALE_MAX_MATINGS = 4
GESTATION_DAYS = 25
FEMALES_PER_MALE = 2   # オス1匹とメス2匹で、同じ週に2回分の交配ができる

# ---- 親オスの目標数 ----
# 実験用マウスを作るオス = ceil(MatingPerWeek × 休止週数3 ÷ 2) + 維持繁殖用のオス 2匹 (急死に備えて2匹)
BREEDER_MALES = 2

# ---- 在庫予測 ----
# 何ヶ月先の「動けるマウス」(生存・生後2ヶ月以上・生後12ヶ月以下) を数えて目標数と比べるか
DEFAULT_HORIZON_MONTHS = 3.5
DEFAULT_HORIZONS = "3,3.5,4,4.5,5"
# 系統 (実験計画の目的系統名) ごとの見る期間。ここにない系統は DEFAULT_HORIZON_MONTHS (--horizon) を使う
# 例: {"CUL1-KO-Het": 4.5}。実行時に --strain-horizon CUL1-KO-Het=4.5 でも指定できる
STRAIN_HORIZONS = {}
# 維持交配は、翌週以降 (休止期間1回分) の実験用交配の欠品を増やさない数までに抑える
SPARE_LIMIT = True
# WT は不足時に購入で充当する (シミュレーション上は必要な週にすぐ使える)
PURCHASE_WT_FOR_BREEDING = True

# ---- 見直しの注意 ----
# 標準の見る期間より良い期間があれば、レポートの冒頭に注意を出す。小さな差で標準を変えないための閾値
ALERT_COST_PCT = 3.0       # 欠品が増えずに、年間ケージ費がこの割合 (%) 以上安くなる
ALERT_MISSING_PAIRS = 0.5  # 年間ケージ費が増えずに、欠品ペアがこの数以上減る / 標準でこの数以上欠品が出ている
# 差は同じ乱数どうし (同じ試行番号) で比べ、差の95%の範囲が0をまたぐ (偶然でも出る) なら注意を出さない
ALERT_Z = 1.96

# ---- 見る期間の自動判定 (--auto) ----
# 系統ごとに候補の期間を比べ、年間欠品ペアが上限以下と言える期間のうち年間ケージ費が最も安い期間を使う
# 3ヶ月未満は、今週の交配の子 (妊娠25日 + 生後2ヶ月) が見る期間に入らず数えられないため、維持交配が過剰になる。候補に入れない
AUTO_HORIZONS = "3,3.5,4,4.5,5,5.5,6"
AUTO_RUNS = 1000            # 200回では欠品ペアが半分程度にぶれることがあるため
MAX_MISSING_PAIRS = 0.5     # 系統ごとの年間欠品ペアの上限。平均の95%範囲の上側 (ALERT_Z) で判定する

# ---- 購入 ----
WT_LEAD_WEEKS = 3        # WTメスの発注から交配に使えるまでの週数 (仮置き。分かり次第変更)
PURCHASE_LIST_WEEKS = 12  # 購入リストに載せる期間
OUTLOOK_WEEKS = 4         # 見通しに載せる週数 (今週を含む)

# ---- 確率的産仔数 ----
OUTCOME_SETTLED_DAYS = 60   # 交配からこの日数を過ぎた履歴だけを産仔分布に使う
LITTER_HISTORY_DAYS = 365   # そのうち直近この日数分 (Strain テーブルの成績と同じく直近1年) を使う
MIN_LITTER_SAMPLES = 5      # 履歴がこれ未満の系統は全系統をプールした分布で代用

# ---- ケージ ----
CAGE_COST_PER_DAY = 60  # 円 / ケージ・日
FEMALES_PER_CAGE = 5    # 同一 Strain のみ同居可。オスは 1 匹 / ケージ

SCRIPT_DIR = Path(__file__).resolve().parent


# ======================================================================
# Airtable REST client (読み取りのみ)
# ======================================================================
class Airtable:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})

    def list_all(self, table, fields=None):
        url = f"{API_URL}/{requests.utils.quote(table)}"
        records, params = [], {"pageSize": 100}
        if fields:
            params["fields[]"] = fields
        while True:
            for attempt in range(6):
                r = self.s.get(url, params=params, timeout=60)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt if r.status_code != 429 else 30)
                    continue
                if not r.ok:
                    raise RuntimeError(f"GET {table} -> {r.status_code}: {r.text}")
                break
            else:
                raise RuntimeError(f"GET {table}: retries exhausted")
            time.sleep(0.22)  # 5 req/s 制限
            data = r.json()
            records.extend(data["records"])
            if "offset" not in data:
                return records
            params["offset"] = data["offset"]


def load_token():
    token = os.environ.get("AIRTABLE_TOKEN")
    env_file = SCRIPT_DIR / ".env"
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "AIRTABLE_TOKEN":
                token = v.strip().strip('"').strip("'")
    if not token:
        sys.exit("AIRTABLE_TOKEN が未設定です (環境変数 or .env)")
    return token


def fetch_inputs(at):
    print("入力テーブル取得中...")
    inputs = {
        "plans": at.list_all(T_PLAN),
        "strains": at.list_all(T_STRAIN, ["name"]),
        "mice": at.list_all(T_MICE, ["mouseId", "strainCode", "sex", "birthDate", "terminationDate", "retire",
                                     "matingCount", "MatingNext", "location", "earTag"]),
        "matings": at.list_all(T_MATING, ["purposeStrain", "purpose", "motherMouseId", "fatherMouseId",
                                          "matingDate", "separation", "male", "female"]),
        "locations": at.list_all(T_LOCATION, ["locationName"]),
        "eartags": at.list_all(T_EARTAG, ["Name"]),
    }
    print("  " + " / ".join(f"{k} {len(v)}" for k, v in inputs.items()))
    return inputs


# ======================================================================
# ユーティリティ
# ======================================================================
def parse_date(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if not v or not isinstance(v, str):
        return None
    return datetime.strptime(v[:10], "%Y-%m-%d").date()


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    days_in_month = [31, 29 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days_in_month))


def add_months_f(d, months):
    """小数の月数を加える (端数は 1ヶ月 = 30.44日 で日数に換算)"""
    whole = int(months)
    return add_months(d, whole) + timedelta(days=round((months - whole) * 30.44))


def next_friday(today):
    """today より後の最初の金曜日 (today が金曜日なら翌週の金曜日)"""
    return today + timedelta(days=(4 - today.weekday()) % 7 or 7)


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v) else None


def is_wt(strain_name):
    return "WT" in strain_name


def genotype(strain_name):
    """系統名から遺伝子型 (KO アレルの数)。WT=0, Het=1, Homo=2。分からなければ None"""
    if is_wt(strain_name):
        return 0
    if "Homo" in strain_name:
        return 2
    if "Het" in strain_name:
        return 1
    return None


def genotype_ratio(target, father, mother):
    """父母の系統名から、子が target の遺伝子型になる割合 (メンデル比。常染色体・致死なしを仮定)
    親は KO アレルを WT 0 / Het 1/2 / Homo 1 の確率で渡す。遺伝子型が分からない系統は 1 (全頭が target)"""
    t, f, m = genotype(target), genotype(father), genotype(mother)
    if None in (t, f, m):
        return 1.0
    pf, pm = f / 2, m / 2
    return {0: (1 - pf) * (1 - pm), 1: pf * (1 - pm) + pm * (1 - pf), 2: pf * pm}[t]


def first(v):
    return (v or [None])[0]


def quantile(values, q):
    s = sorted(values)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def months_label(m):
    return f"{m:g}ヶ月"


# ======================================================================
# シミュレーションモデル
# ======================================================================
@dataclass
class Mouse:
    key: str                 # 既存: Airtable rec id / 仮想: "sim-N"
    strains: list            # Strain rec ids
    sex: str
    birth: date
    termination: date = None
    mating_count: int = 0
    next_available: date = None
    existing: bool = True
    origin: str = None       # 仮想マウスのみ: 購入 / 繁殖
    add_date: date = None    # 仮想マウスがコロニーに入る日 (購入日 / 出生日)
    done_date: date = None   # メスが4回目の交配サイクルを終える日 (以降は飼育しない)
    used_on: date = None     # オスが交配に使われた週 (その週は最大 FEMALES_PER_MALE 回まで)
    uses: int = 0
    label: str = ""          # 表示名 (mouseId)
    location: str = ""
    ear: str = ""

    def alive_and_active(self, d):
        """生存かつ引退前 (生後12ヶ月以下)"""
        if self.termination and self.termination <= d:
            return False
        return self.birth <= d and d <= add_months(self.birth, MAX_AGE_MONTHS)

    def movable(self, d):
        """d の時点で「動ける」: 生存・生後2ヶ月以上・引退前・メスは交配回数上限未満"""
        if not self.alive_and_active(d) or d < add_months(self.birth, MIN_AGE_MONTHS):
            return False
        return not (self.sex == "F" and self.mating_count >= FEMALE_MAX_MATINGS)

    def idle(self, d):
        if not self.movable(d):
            return False
        if self.sex == "M" and self.used_on == d:
            return self.uses < FEMALES_PER_MALE  # 同じ週の2匹目のメスと組める
        return self.next_available is None or self.next_available <= d


@dataclass
class SimResult:
    start: date
    horizon: float
    matings: list = field(default_factory=list)        # dict
    virtual_mice: list = field(default_factory=list)   # Mouse
    shortages: list = field(default_factory=list)      # dict
    targets: dict = field(default_factory=dict)
    exp_req: dict = field(default_factory=lambda: defaultdict(list))   # plan strain -> 週ごとの必要数
    exp_done: dict = field(default_factory=lambda: defaultdict(list))  # plan strain -> 週ごとの成立数
    outlook: dict = field(default_factory=lambda: defaultdict(list))   # plan strain -> 週ごとの状況 (record 時)
    cages_by_strain: dict = field(default_factory=dict)                # strain -> 日ごとのケージ数


class Simulator:
    def __init__(self, inputs, today):
        strains = inputs["strains"]
        self.today = today
        self.strain_name = {s["id"]: s["fields"].get("name", s["id"]) for s in strains}
        self.location_name = {r["id"]: r["fields"].get("locationName", "") for r in inputs["locations"]}
        self.ear_name = {r["id"]: r["fields"].get("Name", "") for r in inputs["eartags"]}
        self.start = next_friday(today)
        self.end = self.start + timedelta(weeks=WEEKS)  # 排他的終端
        self.recipes = self._learn_recipes(inputs["matings"], inputs["mice"])
        # 維持交配の子のうち目的系統の遺伝子型になる割合 (Mating の male/female は遺伝子型判定前の全頭数)
        self.geno_ratio = {s: genotype_ratio(*(self.strain_name.get(x, "") for x in (s, r["father"], r["mother"])))
                           for s, r in self.recipes.items()}
        self.litters, self.pooled_litters = self._litter_samples(inputs["matings"])
        self.yields = self._calc_yields()
        self.plans = self._parse_plans(inputs["plans"])
        self.plans_by_father = defaultdict(list)
        for p in self.plans:
            self.plans_by_father[p["father"]].append(p)
        self.base_mice = self._parse_mice(inputs["mice"])
        self.groups = self._groups()
        self.targets = self._targets()
        # 目標を持つ系統 → それを含むグループ (系統別の見る期間を引くため)
        self.target_groups = {s: [k for k, g in self.groups.items() if s in g["members"]]
                              for s, _ in self.targets}

    def resolve_horizons(self, default, by_name):
        """{グループ key: 見る期間}。by_name は目的系統名 → ヶ月"""
        by_key = {k: default for k in self.groups}
        for name, h in by_name.items():
            keys = [k for k in self.groups if self.strain_name.get(k) == name]
            if not keys:
                raise SystemExit(f"系統別の見る期間: 実験計画に {name} がありません "
                                 f"(候補: {', '.join(self.strain_name.get(k, k) for k in self.groups)})")
            for k in keys:
                by_key[k] = h
        return by_key

    # ---- 前処理 ----
    def _calc_yields(self):
        """1交配あたりの産仔数の期待値 {strain: {"M": 匹, "F": 匹}}。抽選に使う履歴 (直近1年) の平均で、丸めない
        (遺伝子型判定前の全頭数。0匹に終わる交配を含む)"""
        out = {}
        for s in self.strain_name:
            src = self.litter_source(s)
            if src:
                out[s] = {"M": sum(x[0] for x in src) / len(src), "F": sum(x[1] for x in src) / len(src)}
        return out

    def _learn_recipes(self, matings, mice_records):
        """目的Strainごとに最頻の (父Strain, 母Strain)。Breeding 履歴を優先"""
        mouse_strain = {m["id"]: first(m["fields"].get("strainCode")) for m in mice_records}
        counts = {"Breeding": defaultdict(Counter), "all": defaultdict(Counter)}
        for r in matings:
            f = r["fields"]
            purpose_strain = first(f.get("purposeStrain"))
            father = mouse_strain.get(first(f.get("fatherMouseId")))
            mother = mouse_strain.get(first(f.get("motherMouseId")))
            if not (purpose_strain and father and mother):
                continue
            counts["all"][purpose_strain][(father, mother)] += 1
            if f.get("purpose") == "Breeding":
                counts["Breeding"][purpose_strain][(father, mother)] += 1
        recipes = {}
        for s in set(counts["all"]):
            src = counts["Breeding"][s] or counts["all"][s]
            (father, mother), n = src.most_common(1)[0]
            recipes[s] = {"father": father, "mother": mother, "count": n}
        return recipes

    def _litter_samples(self, matings):
        """結果が確定した直近 LITTER_HISTORY_DAYS 日分の交配の (オス数, メス数)。
        離乳 (separation) 記録も頭数もない交配は失敗 = (0, 0)"""
        cutoff = self.start - timedelta(days=OUTCOME_SETTLED_DAYS)
        since = cutoff - timedelta(days=LITTER_HISTORY_DAYS)
        by_strain, pooled = defaultdict(list), []
        for r in matings:
            f = r["fields"]
            strain = first(f.get("purposeStrain"))
            md = parse_date(f.get("matingDate"))
            if not strain or not md or md > cutoff or md <= since:
                continue
            male, female = num(f.get("male")), num(f.get("female"))
            if f.get("separation") or male is not None or female is not None:
                sample = (int(male or 0), int(female or 0))
            else:
                sample = (0, 0)
            by_strain[strain].append(sample)
            pooled.append(sample)
        return by_strain, pooled

    def _parse_plans(self, plans):
        out = []
        for p in sorted(plans, key=lambda p: p["fields"].get("id", 0)):
            f = p["fields"]
            strain, father, mother = first(f.get("Strain")), first(f.get("Father")), first(f.get("Mother"))
            per_week = num(f.get("MatingPerWeek")) or 0
            if strain and father and mother and per_week > 0:
                out.append({"id": f.get("id"), "strain": strain, "father": father,
                            "mother": mother, "per_week": per_week})
        return out

    def _parse_mice(self, records):
        mice = []
        for r in records:
            f = r["fields"]
            birth = parse_date(f.get("birthDate"))
            strains = f.get("strainCode") or []
            sex = f.get("sex")
            if f.get("retire") or not birth or not strains or sex not in ("M", "F"):
                continue
            term = parse_date(f.get("terminationDate"))
            if term and term <= self.start:
                continue
            count = int(num(f.get("matingCount")) or 0)
            mice.append(Mouse(
                key=r["id"], strains=strains, sex=sex, birth=birth, termination=term,
                mating_count=count, next_available=parse_date(f.get("MatingNext")),
                done_date=self.start if sex == "F" and count >= FEMALE_MAX_MATINGS else None,
                label=f.get("mouseId") or r["id"],
                location=", ".join(self.location_name.get(x, "") for x in f.get("location") or []),
                ear=", ".join(self.ear_name.get(x, "") for x in f.get("earTag") or [])))
        return mice

    def _groups(self):
        """実験計画ごとのグループ: 目的系統 + 父母系統 + 父母系統の維持レシピの親"""
        groups = {}
        for p in self.plans:
            members = {p["strain"], p["father"], p["mother"]}
            for parent in (p["father"], p["mother"]):
                r = self.recipes.get(parent)
                if r and not is_wt(self.strain_name.get(parent, "")):
                    members |= {r["father"], r["mother"]}
            g = groups.setdefault(p["strain"], {"key": p["strain"], "plans": [], "members": set()})
            g["plans"].append(p)
            g["members"] |= members
        return groups

    def _targets(self):
        """{(strain, sex): 目標頭数}。WT は購入で賄うため対象外
        オス = ceil(Σ MatingPerWeek × 3 ÷ 2) + 維持繁殖用 2匹
        メス (WT以外の母系統がある場合) = ceil(Σ MatingPerWeek × 8)"""
        exp = defaultdict(float)
        for p in self.plans:
            exp[(p["father"], "M")] += p["per_week"] * COOLDOWN_WEEKS["M"] / FEMALES_PER_MALE
            exp[(p["mother"], "F")] += p["per_week"] * COOLDOWN_WEEKS["F"]
        out = {}
        for (strain, sex), v in exp.items():
            if is_wt(self.strain_name.get(strain, "")):
                continue
            out[(strain, sex)] = math.ceil(v - 1e-9) + (BREEDER_MALES if sex == "M" else 0)
        return out

    def litter_source(self, strain):
        samples = self.litters.get(strain, [])
        return samples if len(samples) >= MIN_LITTER_SAMPLES else self.pooled_litters

    def mouse_view(self, m):
        """レポート用の個体表示"""
        if m.existing:
            return {"id": m.key, "label": m.label, "location": m.location, "ear": m.ear, "count": m.mating_count}
        name = self.strain_name.get(m.strains[0], "")
        sex = "オス" if m.sex == "M" else "メス"
        if m.origin == "購入":
            text = f"購入予定 {name} {sex}（{m.add_date:%m/%d} 必要）"
        else:
            text = f"維持交配で誕生予定 {name} {sex}（{m.birth:%m/%d} 生まれ）"
        return {"label": text, "location": "", "ear": "", "count": m.mating_count, "virtual": True}

    # ---- 1回分の実行 ----
    def run(self, horizon=DEFAULT_HORIZON_MONTHS, rng=None, record=False, spare_limit=SPARE_LIMIT, horizon_by=None):
        """rng=None: 産仔数は期待値 / rng 指定: 履歴分布から抽選
        record=True: 交配リスト・見通し用に、交配したときの個体状態を記録する
        horizon_by: {グループ key: 見る期間}。指定のないグループは horizon"""
        self.horizon, self.rng, self.record, self.spare_limit = horizon, rng, record, spare_limit
        # 産仔数の乱数は系統ごとに分ける。ある系統の交配数が変わっても、ほかの系統の抽選結果は変わらない
        self.rng_base = rng.getrandbits(64) if rng is not None else None
        self.strain_rngs = {}
        # 系統ごとの見る期間。複数のグループに属する系統は長いほうを使う
        hb = horizon_by or {}
        self.strain_horizon = {s: max((hb.get(k, horizon) for k in ks), default=horizon)
                               for s, ks in self.target_groups.items()}
        self.mice = [replace(m) for m in self.base_mice]
        self.pool = defaultdict(list)
        self.pending_births = defaultdict(list)  # 系統 → 維持交配の出生予定日
        self.frac_pups = defaultdict(float)      # 期待値で計算するときの端数 (系統, 性別) → 匹
        for m in self.mice:
            self._index(m)
        self.seq = 0
        self.res = SimResult(start=self.start, horizon=horizon, targets=self.targets)
        acc = {id(p): 0.0 for p in self.plans}
        # 実験用交配は父系統ごとに FEMALES_PER_MALE ペアずつまとめて組む。最初のまとめ交配は起点の週に行う
        self.pending = {}
        for father, plans in self.plans_by_father.items():
            self.pending[father] = max(0.0, FEMALES_PER_MALE - sum(p["per_week"] for p in plans))

        for w in range(WEEKS):
            d = self.start + timedelta(weeks=w)
            if record and w < OUTLOOK_WEEKS:
                idle_start = {p["strain"]: sum(1 for m in self.pool[(p["father"], "M")] if m.idle(d))
                              for p in self.plans}
            self._phase_experiment(d, acc)
            self._phase_breeding(d)
            if record and w < OUTLOOK_WEEKS:
                for key, g in self.groups.items():
                    fathers = {p["father"] for p in g["plans"]}
                    self.res.outlook[key].append({
                        "date": d.isoformat(),
                        "exp_req": self.res.exp_req[key][-1], "exp_done": self.res.exp_done[key][-1],
                        "breed": sum(1 for x in self.res.matings if x["date"] == d and x["purpose"] == "Breeding"
                                     and x["purposeStrain"] in g["members"]),
                        "idle_males": idle_start[key],
                        "males_used": sum(1 for s in fathers for m in self.pool[(s, "M")] if m.used_on == d),
                    })

        self._count_cages()
        return self.res

    def _batch(self, father, pending):
        """父系統の未交配分に今週分を足し、オス1匹分 (FEMALES_PER_MALE ペア) 単位で今週組む数を返す"""
        pending += sum(p["per_week"] for p in self.plans_by_father[father])
        n = int((pending + 1e-9) // FEMALES_PER_MALE) * FEMALES_PER_MALE
        return n, pending - n

    def _phase_experiment(self, d, acc):
        """フェーズ1: 実験用交配。同じ父系統の計画をまとめ、2ペアずつ組む
        (週1ペアの計画なら2週に1回2ペア)。組んだペアは計画ごとの累計の遅れが大きい順に割り振る"""
        week = defaultdict(lambda: [0, 0])
        for father, plans in self.plans_by_father.items():
            n, self.pending[father] = self._batch(father, self.pending[father])
            for p in plans:
                acc[id(p)] += p["per_week"]
            per_plan = defaultdict(lambda: [0, 0])
            for _ in range(n):
                p = max(plans, key=lambda p: acc[id(p)])
                acc[id(p)] -= 1
                ok = self._try_pair(d, p["strain"], "Experiment", p["father"], p["mother"], True)
                per_plan[id(p)][0] += 1
                per_plan[id(p)][1] += ok
            for p in plans:
                req, done = per_plan[id(p)]
                week[p["strain"]][0] += req
                week[p["strain"]][1] += done
                if done < req:
                    self.res.shortages.append({"date": d, "phase": "Experiment", "strain": p["strain"],
                                               "required": req, "done": done})
        for key in self.groups:
            self.res.exp_req[key].append(week[key][0])
            self.res.exp_done[key].append(week[key][1])

    def _phase_breeding(self, d):
        """フェーズ2: 維持繁殖。horizon ヶ月後の動ける頭数が目標を下回れば交配する。
        維持交配の産仔数 (失敗を含む) は出生日まで分からないので、出生前の交配は期待値で数える"""
        for (strain, sex), target in self.targets.items():
            at = add_months_f(d, self.strain_horizon[strain])
            recipe = self.recipes.get(strain)
            # 期待値 (丸めない)。0 (履歴で一度も生まれていない) なら、1回の交配で1匹とみなして交配数を決める
            y = self.yields.get(strain, {"M": 1, "F": 1})[sex] * self.geno_ratio.get(strain, 1.0) or 1.0
            born =sum(1 for m in self.pool[(strain, sex)]
                       if m.movable(at) and not (m.origin == "繁殖" and m.add_date > d))
            unborn = sum(y for b in self.pending_births[strain]
                         if b > d and add_months_f(b, MIN_AGE_MONTHS) <= at < add_months_f(b, MAX_AGE_MONTHS))
            forecast = born + unborn
            if forecast >= target:
                continue
            if not recipe:
                self.res.shortages.append({"date": d, "phase": "Breeding(no recipe)", "strain": strain,
                                           "sex": sex, "target": target, "forecast": forecast})
                continue
            need = math.ceil((target - forecast) / y)
            if self.spare_limit:
                need = min(need, self._spare(recipe["father"], "M", d, need),
                           self._spare(recipe["mother"], "F", d, need))
            done = 0
            for _ in range(need):
                if not self._try_pair(d, strain, "Breeding", recipe["father"], recipe["mother"],
                                      PURCHASE_WT_FOR_BREEDING):
                    break
                done += 1
                birth = d + timedelta(days=GESTATION_DAYS)
                self.pending_births[strain].append(birth)
                n_male, n_female = self._litter(strain)
                for pup_sex, n in (("M", n_male), ("F", n_female)):
                    for _ in range(n):
                        self._new_mouse(strain, pup_sex, birth, "繁殖", birth)
            if done < need:
                self.res.shortages.append({"date": d, "phase": "Breeding", "strain": strain, "sex": sex,
                                           "required": need, "done": done})

    # ---- 共通操作 ----
    def _index(self, m):
        for s in m.strains:
            self.pool[(s, m.sex)].append(m)

    def _new_mouse(self, strain, sex, birth, origin, add_date):
        self.seq += 1
        m = Mouse(key=f"sim-{self.seq}", strains=[strain], sex=sex, birth=birth,
                  existing=False, origin=origin, add_date=add_date)
        self.mice.append(m)
        self._index(m)
        self.res.virtual_mice.append(m)
        return m

    def _litter(self, strain):
        """維持交配で生まれる目的系統の (オス数, メス数)。全頭数に遺伝子型の割合をかける"""
        ratio = self.geno_ratio.get(strain, 1.0)
        if self.rng is None:
            # 期待値: 端数は次の交配に持ち越す (割合 1/2 で期待値1匹なら、2回に1回1匹)
            out = []
            for sex in ("M", "F"):
                self.frac_pups[(strain, sex)] += self.yields.get(strain, {"M": 1, "F": 1})[sex] * ratio
                n = int(self.frac_pups[(strain, sex)] + 1e-9)
                self.frac_pups[(strain, sex)] -= n
                out.append(n)
            return tuple(out)
        r = self.strain_rngs.get(strain)
        if r is None:
            r = self.strain_rngs[strain] = random.Random(f"{self.rng_base}-{strain}")
        n_male, n_female = r.choice(self.litter_source(strain))
        if ratio >= 1:
            return n_male, n_female
        # 子1匹ずつ、目的の遺伝子型になるかを抽選
        return (sum(r.random() < ratio for _ in range(n_male)),
                sum(r.random() < ratio for _ in range(n_female)))

    def _pick(self, strain, sex, d):
        """Idle な個体を1匹。オスはその週すでに1回使ったオス (2匹目のメス用) を優先し、次に月齢の高い順"""
        cands = [m for m in self.pool[(strain, sex)] if m.idle(d)]
        if not cands:
            return None
        return min(cands, key=lambda m: (0 if m.used_on == d else 1, m.birth, m.key))

    def _acquire(self, strain, sex, d, allow_purchase):
        m = self._pick(strain, sex, d)
        if m is None and allow_purchase and is_wt(self.strain_name.get(strain, "")):
            m = self._new_mouse(strain, sex, add_months(d, -MIN_AGE_MONTHS), "購入", d)
        return m

    def _try_pair(self, d, purpose_strain, purpose, father_strain, mother_strain, allow_purchase):
        # 在庫を消費する前に両親が揃うか確認 (WT 購入は揃う場合のみ発生させる)
        f_ok = self._pick(father_strain, "M", d) or (allow_purchase and is_wt(self.strain_name.get(father_strain, "")))
        m_ok = self._pick(mother_strain, "F", d) or (allow_purchase and is_wt(self.strain_name.get(mother_strain, "")))
        if not (f_ok and m_ok):
            return False
        father = self._acquire(father_strain, "M", d, allow_purchase)
        mother = self._acquire(mother_strain, "F", d, allow_purchase)
        view = (self.mouse_view(father), self.mouse_view(mother)) if self.record and d == self.start else None
        for m in (father, mother):
            m.mating_count += 1
            if m.sex == "M":
                if m.used_on != d:
                    m.used_on, m.uses = d, 0
                    m.next_available = d + timedelta(days=COOLDOWN_DAYS["M"])
                m.uses += 1
            else:
                m.next_available = d + timedelta(days=COOLDOWN_DAYS["F"])
                if m.mating_count >= FEMALE_MAX_MATINGS:
                    m.done_date = m.next_available
        self.res.matings.append({"date": d, "purposeStrain": purpose_strain, "purpose": purpose,
                                 "father": father, "mother": mother, "view": view})
        return True

    def _spare(self, strain, sex, d, need):
        """今週 x 回を維持交配に使っても、その後の休止期間1回分 (オス3週・メス8週) の
        実験用交配の欠品が x=0 のときより増えない最大の x (≤ need)。WT は購入で補えるので制限しない
        オスは1匹で同じ週に FEMALES_PER_MALE 回まで交配できるものとして数え、
        今後の実験用交配は父系統ごとのまとめ交配のスケジュールどおりに必要になるものとする"""
        if is_wt(self.strain_name.get(strain, "")):
            return need
        demand = sum(p["per_week"] for p in self.plans if (p["father"] if sex == "M" else p["mother"]) == strain)
        if demand <= 0:
            return need
        # 翌週以降の各週に必要な実験用交配の数
        weeks = COOLDOWN_WEEKS[sex]
        if sex == "M" and strain in self.plans_by_father:
            future, pending = [], self.pending[strain]
            for _ in range(weeks):
                n, pending = self._batch(strain, pending)
                future.append(n)
        else:
            future = [math.ceil(demand - 1e-9)] * weeks
        slots = FEMALES_PER_MALE if sex == "M" else 1
        pool = self.pool[(strain, sex)]
        cool = timedelta(days=COOLDOWN_DAYS[sex])
        partial = sum(slots - m.uses for m in pool if sex == "M" and m.used_on == d and m.idle(d))
        idle_new = sorted((m for m in pool if m.idle(d) and m.used_on != d), key=lambda m: (m.birth, m.key))

        def shortfall(x):
            n_new = math.ceil(max(0, x - partial) / slots)
            if n_new > len(idle_new):
                return None
            free = {m.key: m.next_available for m in pool}
            count = {m.key: m.mating_count for m in pool}
            for m in idle_new[:n_new]:
                free[m.key], count[m.key] = d + cool, count[m.key] + 1
            short = 0
            for k, per_week in enumerate(future, start=1):
                if per_week == 0:
                    continue
                dk = d + timedelta(weeks=k)
                cands = sorted((m for m in pool
                                if m.alive_and_active(dk) and dk >= add_months(m.birth, MIN_AGE_MONTHS)
                                and not (sex == "F" and count[m.key] >= FEMALE_MAX_MATINGS)
                                and (free[m.key] is None or free[m.key] <= dk)),
                               key=lambda m: (m.birth, m.key))
                use = cands[:math.ceil(per_week / slots)]
                short += max(0, per_week - len(use) * slots)
                for m in use:
                    free[m.key], count[m.key] = dk + cool, count[m.key] + 1
            return short

        base = shortfall(0)
        x = need
        while x > 0:
            s = shortfall(x)
            if s is not None and s <= base:
                break
            x -= 1
        return x

    # ---- ケージ数 ----
    def _count_cages(self):
        """系統ごとの日別ケージ数。オス 1匹/ケージ、メスは同一系統 5匹/ケージ。
        飼育期間 = [コロニー入り, 死亡・生後12ヶ月引退・メス4回交配サイクル終了 の最早日)"""
        n = WEEKS * 7
        diffs = defaultdict(lambda: {"M": [0] * (n + 1), "F": [0] * (n + 1)})
        for m in self.mice:
            start = max(self.start, m.add_date or self.start)
            ends = [self.end, add_months(m.birth, MAX_AGE_MONTHS) + timedelta(days=1)]
            ends += [x for x in (m.termination, m.done_date) if x]
            end = min(ends)
            if end <= start:
                continue
            arr = diffs[m.strains[0]][m.sex]
            arr[(start - self.start).days] += 1
            arr[(end - self.start).days] -= 1
        out = {}
        for strain, by_sex in diffs.items():
            daily, cm, cf = [], 0, 0
            for i in range(n):
                cm += by_sex["M"][i]
                cf += by_sex["F"][i]
                daily.append(cm + math.ceil(cf / FEMALES_PER_CAGE))
            out[strain] = daily
        self.res.cages_by_strain = out


# ======================================================================
# 集計
# ======================================================================
def group_cages(res, members):
    """グループに属する系統の日別ケージ数の合計 (members=None なら全系統)"""
    n = WEEKS * 7
    total = [0] * n
    for strain, daily in res.cages_by_strain.items():
        if members is None or strain in members:
            total = [a + b for a, b in zip(total, daily)]
    return total


def run_metrics(sim, res):
    """1回分の結果を、グループごと + 全体 ("all") の指標にまとめる"""
    out = {}
    keys = list(sim.groups) + ["all"]
    for key in keys:
        members = sim.groups[key]["members"] if key != "all" else None
        if key == "all":
            req = [sum(x) for x in zip(*(res.exp_req[k] for k in sim.groups))]
            done = [sum(x) for x in zip(*(res.exp_done[k] for k in sim.groups))]
        else:
            req, done = res.exp_req[key], res.exp_done[key]
        daily = group_cages(res, members)
        in_group = (lambda s: True) if members is None else (lambda s: s in members)
        out[key] = {
            "required": sum(req), "done": sum(done),
            "weekly_missing": [a - b for a, b in zip(req, done)],
            "cost": sum(daily) * CAGE_COST_PER_DAY,
            "weekly_cages": [sum(daily[w * 7:(w + 1) * 7]) / 7 for w in range(WEEKS)],
            "breeding": sum(1 for x in res.matings if x["purpose"] == "Breeding" and in_group(x["purposeStrain"])),
            "purchased": sum(1 for m in res.virtual_mice if m.origin == "購入" and in_group(m.strains[0])),
        }
    return out


def run_comparison(sim, horizons, runs, seed, horizon_by=None):
    """見る期間ごとに runs 回くり返す。horizon_by を渡すと、その系統だけ見る期間を固定する
    per_run[(group, horizon)] = {"missing": [...], "cost": [...]} は試行番号順 (条件間で同じ乱数どうしを比べるため)"""
    summary, weekly, per_run = [], [], {}
    for h in horizons:
        t0 = time.time()
        per_key = defaultdict(list)
        for i in range(runs):
            res = sim.run(h, random.Random(seed * 1_000_003 + i), horizon_by=horizon_by)  # 条件間で同じ乱数系列
            for key, mtr in run_metrics(sim, res).items():
                per_key[key].append(mtr)
        for key, rows in per_key.items():
            col = lambda k: [x[k] for x in rows]
            missing = [x["required"] - x["done"] for x in rows]
            per_run[(key, h)] = {"missing": missing, "cost": col("cost")}
            req = rows[0]["required"]
            summary.append({
                "group": key, "horizon": h, "required": req,
                "done_mean": sum(col("done")) / runs, "done_p10": quantile(col("done"), 0.1),
                "supply_rate_mean": sum(col("done")) / runs / req if req else None,
                "missing_mean": sum(missing) / runs, "missing_p90": quantile(missing, 0.9),
                "shortage_prob": sum(1 for v in missing if v > 0) / runs,
                "cost_mean": sum(col("cost")) / runs,
                "cost_p10": quantile(col("cost"), 0.1), "cost_p90": quantile(col("cost"), 0.9),
                "breeding_mean": sum(col("breeding")) / runs,
                "purchased_mean": sum(col("purchased")) / runs,
            })
            for w in range(WEEKS):
                cages = [x["weekly_cages"][w] for x in rows]
                miss = [x["weekly_missing"][w] for x in rows]
                weekly.append({
                    "group": key, "horizon": h, "date": (sim.start + timedelta(weeks=w)).isoformat(),
                    "missing_mean": sum(miss) / runs, "shortage_prob": sum(1 for v in miss if v > 0) / runs,
                    "cages_mean": sum(cages) / runs,
                    "cages_p10": quantile(cages, 0.1), "cages_p90": quantile(cages, 0.9),
                })
        print(f"  {months_label(h)}先: {runs} 回 ({time.time() - t0:.1f} 秒)")
    return summary, weekly, per_run


def paired_diff(base, other, z=ALERT_Z):
    """同じ試行番号どうしの差 (other - base) の平均と、その z 倍の標準誤差の範囲 (lo, hi)"""
    d = [b - a for a, b in zip(base, other)]
    n = len(d)
    mean = sum(d) / n
    if n < 2:
        return mean, -math.inf, math.inf
    se = math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1) / n)
    return mean, mean - z * se, mean + z * se


def horizon_alerts(sim, summary, per_run, std_by_key, cost_pct=ALERT_COST_PCT, missing_pairs=ALERT_MISSING_PAIRS):
    """系統ごとに、その系統の標準の見る期間 (std_by_key) を見直すべきかを判定する。
    better  : 欠品が増えずに費用が cost_pct% 以上安い、または費用が増えずに欠品が missing_pairs 以上少ない期間がある
    shortage: 標準で欠品が missing_pairs 以上出ていて、費用をかければ欠品を減らせる期間がある
    どちらも、良くなる側の差が同じ乱数どうしの比較で偶然と言えない (95%の範囲が0をまたがない) ときだけ出す"""
    alerts = []
    for key in sim.groups:
        horizon = std_by_key[key]
        rows = [s for s in summary if s["group"] == key]
        std = next((s for s in rows if s["horizon"] == horizon), None)
        if std is None:
            continue
        name = group_label(sim, key)
        others = [s for s in rows if s is not std]
        diff = lambda s: s["cost_mean"] - std["cost_mean"]
        pd_ = lambda s, k: paired_diff(per_run[(key, horizon)][k], per_run[(key, s["horizon"])][k])
        range_note = lambda s: "（差の95%の範囲 {1:+.1f}〜{2:+.1f} ペア）".format(*pd_(s, "missing"))
        better = [s for s in others
                  if (s["missing_mean"] <= std["missing_mean"] + 1e-9 and s["shortage_prob"] <= std["shortage_prob"] + 1e-9
                      and -diff(s) >= std["cost_mean"] * cost_pct / 100 and pd_(s, "cost")[2] < 0)
                  or (diff(s) <= 0 and std["missing_mean"] - s["missing_mean"] >= missing_pairs
                      and pd_(s, "missing")[2] < 0)]
        if better:
            b = min(better, key=lambda s: (s["missing_mean"], s["cost_mean"]))
            alerts.append({
                "group": key, "name": name, "kind": "better", "horizon": b["horizon"],
                "text": f"{months_label(b['horizon'])}にすると、欠品ペア {std['missing_mean']:.1f} → {b['missing_mean']:.1f}、"
                        f"年間ケージ費 {std['cost_mean'] / 1e4:.1f} → {b['cost_mean'] / 1e4:.1f} 万円"
                        f"（{diff(b) / 1e4:+.1f} 万円）"
                        + (range_note(b) if std["missing_mean"] - b["missing_mean"] >= missing_pairs else "")
                        + f"。標準の{months_label(horizon)}の見直しを検討してください。"})
            continue
        fixes = [s for s in others if std["missing_mean"] >= missing_pairs
                 and std["missing_mean"] - s["missing_mean"] >= missing_pairs and pd_(s, "missing")[2] < 0]
        if fixes:
            # 欠品が閾値未満になる中で最も安い期間。なければ欠品が最も少ない期間
            ok = [s for s in fixes if s["missing_mean"] < missing_pairs]
            b = (min(ok, key=lambda s: s["cost_mean"]) if ok
                 else min(fixes, key=lambda s: (s["missing_mean"], s["cost_mean"])))
            alerts.append({
                "group": key, "name": name, "kind": "shortage", "horizon": b["horizon"],
                "text": f"標準の{months_label(horizon)}では年間 {std['missing_mean']:.1f} ペアの欠品が出ます。"
                        f"{months_label(b['horizon'])}なら {b['missing_mean']:.1f} ペアに減りますが{range_note(b)}、"
                        f"年間ケージ費が {diff(b) / 1e4:+.1f} 万円変わります。欠品と費用のどちらを優先するか判断してください。"})
    return alerts


def mean_upper(values, z=ALERT_Z):
    """平均の z 倍の標準誤差の範囲の上側"""
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return math.inf
    return mean + z * math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1) / n)


def choose_horizons(sim, summary, per_run, max_missing=MAX_MISSING_PAIRS):
    """系統ごとに、年間欠品ペアの95%範囲の上側が max_missing 以下の期間のうち、年間ケージ費が最も安い期間を選ぶ。
    どの期間も満たさなければ欠品が最も少ない期間を選び、警告 (kind="shortage") にする
    返り値: {グループ key: 見る期間}, レポート・チャット用の判定理由 (alerts と同じ形)"""
    chosen, notes = {}, []
    for key in sim.groups:
        rows = [s for s in summary if s["group"] == key]
        upper = {s["horizon"]: mean_upper(per_run[(key, s["horizon"])]["missing"]) for s in rows}
        ok = [s for s in rows if upper[s["horizon"]] <= max_missing + 1e-9]
        if ok:
            b = min(ok, key=lambda s: (s["cost_mean"], s["horizon"]))
            kind = "auto"
            text = (f"{months_label(b['horizon'])}を使います。年間の欠品ペア {b['missing_mean']:.2f}"
                    f"（95%の範囲の上側 {upper[b['horizon']]:.2f}）が上限 {max_missing:g} ペア以下になる期間のうち、"
                    f"年間ケージ費が最も安い（{b['cost_mean'] / 1e4:.1f} 万円）。")
        else:
            b = min(rows, key=lambda s: (s["missing_mean"], s["cost_mean"]))
            kind = "shortage"
            text = (f"どの期間でも年間の欠品ペアが上限 {max_missing:g} ペア以下とは言えません。"
                    f"欠品が最も少ない{months_label(b['horizon'])}を使います（欠品ペア {b['missing_mean']:.2f}、"
                    f"95%の範囲の上側 {upper[b['horizon']]:.2f}、年間ケージ費 {b['cost_mean'] / 1e4:.1f} 万円）。"
                    "親の在庫か実験計画の見直しを検討してください。")
        chosen[key] = b["horizon"]
        notes.append({"group": key, "name": group_label(sim, key), "kind": kind, "horizon": b["horizon"],
                      "missing_mean": b["missing_mean"], "missing_upper": upper[b["horizon"]],
                      "shortage_prob": b["shortage_prob"], "cost_mean": b["cost_mean"], "text": text})
    return chosen, notes


def auto_rule(max_missing, horizons):
    return (f"見る期間の自動判定：系統ごとに{'、'.join(months_label(h) for h in horizons)}を比べ、"
            f"年間の欠品ペアの平均の95%範囲の上側が {max_missing:g} ペア以下になる期間のうち、年間ケージ費が最も安い期間を使う。"
            "どの期間も満たさないときは、欠品が最も少ない期間を使い、警告を出す。"
            "各期間は全系統を同じ期間にして計算し、系統ごとの欠品と費用で判定する。")


def build_actions(sim, res, wt_lead_weeks):
    """期待値での1回分から、系統ごとの今週の交配リスト・見通し・購入リストを作る"""
    actions, purchases = {}, {}
    for key, g in sim.groups.items():
        members = g["members"]
        # 今週金曜の交配リスト (オスごとにまとめる。オス1匹にメス最大2匹)
        by_father = {}
        for x in res.matings:
            if x["date"] != sim.start or x["purposeStrain"] not in members:
                continue
            fv, mv = x["view"]
            entry = by_father.setdefault(id(x["father"]), {"father": fv, "matings": []})
            entry["matings"].append({"purpose": "実験用" if x["purpose"] == "Experiment" else "維持用",
                                     "purposeStrain": sim.strain_name.get(x["purposeStrain"], ""),
                                     "purposeStrainId": x["purposeStrain"],
                                     "mother": mv})
        actions[key] = list(by_father.values())
        # 購入リスト (WT。必要日 - 納期 = 発注期限)
        limit = sim.start + timedelta(weeks=PURCHASE_LIST_WEEKS)
        cnt = Counter((m.strains[0], m.sex, m.add_date) for m in res.virtual_mice
                      if m.origin == "購入" and m.strains[0] in members and m.add_date < limit)
        rows = []
        for (strain, sex, need), n in sorted(cnt.items(), key=lambda kv: kv[0][2]):
            order_by = need - timedelta(weeks=wt_lead_weeks)
            status = ("期限切れ（至急手配・在庫で調整）" if order_by < sim.today
                      else "今週中に発注" if order_by <= sim.today + timedelta(days=7) else "予定")
            rows.append({"need": need.isoformat(), "strain": sim.strain_name.get(strain, ""),
                         "sex": "オス" if sex == "M" else "メス", "n": n,
                         "order_by": order_by.isoformat(), "status": status})
        purchases[key] = rows
    return actions, purchases


# ======================================================================
# 出力
# ======================================================================
def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def group_label(sim, key):
    return "全体" if key == "all" else sim.strain_name.get(key, key)


def print_actions(sim, actions, purchases, outlook):
    print(f"\n==== 今週金曜 ({sim.start}) の交配リスト ====")
    for key, rows in actions.items():
        print(f"\n[{group_label(sim, key)}]")
        if not rows:
            print("  交配なし")
        for r in rows:
            f = r["father"]
            print(f"  オス {f['label']} ({f['location'] or '-'})")
            for mt in r["matings"]:
                print(f"     + メス {mt['mother']['label']} ({mt['mother']['location'] or '-'})  {mt['purpose']}")
        for o in outlook[key][1:]:
            print(f"  {o['date']}: 実験 {o['exp_done']}/{o['exp_req']}  維持 {o['breed']}  "
                  f"使えるオス {o['idle_males']} 匹 (使用 {o['males_used']})")
        for p in purchases[key][:5]:
            print(f"  購入: {p['strain']} {p['sex']} {p['n']} 匹  必要 {p['need']}  発注期限 {p['order_by']}  {p['status']}")


def print_comparison(sim, summary, runs):
    print(f"\n==== 何ヶ月先を見るかの比較 ({runs} 回/条件, {WEEKS}週) ====")
    for key in list(sim.groups) + ["all"]:
        print(f"\n[{group_label(sim, key)}]")
        print(f"  {'見る期間':>6} {'欠品ペア 平均/P90':>16} {'欠品確率':>6} {'ケージ費 平均 [P10-P90]':>30} {'維持交配':>6} {'購入':>6}")
        for s in (x for x in summary if x["group"] == key):
            print(f"  {months_label(s['horizon']):>8} {s['missing_mean']:>9.2f} / {s['missing_p90']:<4.0f} "
                  f"{s['shortage_prob']:>8.0%} {s['cost_mean']:>12,.0f} [{s['cost_p10']:,.0f}-{s['cost_p90']:,.0f}] "
                  f"{s['breeding_mean']:>8.1f} {s['purchased_mean']:>6.1f}")


def shared_members(sim):
    """複数のグループに属する系統 (WT は購入で賄うので除く)"""
    cnt = Counter(s for g in sim.groups.values() for s in g["members"])
    return [s for s, n in cnt.items() if n > 1 and not is_wt(sim.strain_name.get(s, ""))]


def std_label(sim, horizon, std_by_key):
    """標準の見る期間の表示。例: 3.5ヶ月（CUL1-KO-Het は 4.5ヶ月）"""
    diff = [f"{group_label(sim, k)} は {months_label(h)}" for k, h in std_by_key.items() if h != horizon]
    return months_label(horizon) + (f"（{'、'.join(diff)}）" if diff else "")


def alert_rule():
    return (f"見直しの注意：各系統の標準の見る期間と比べて、欠品が増えずに年間ケージ費が{ALERT_COST_PCT:g}%以上安い期間、"
            f"または費用が増えずに欠品が{ALERT_MISSING_PAIRS:g}ペア以上少ない期間があれば表示する。"
            f"標準で欠品が{ALERT_MISSING_PAIRS:g}ペア以上出ていて、費用をかければ減らせる場合も表示する。"
            "これより小さい差では標準を変えない。"
            "差は同じ乱数（同じ試行番号）どうしで比べ、差の95%の範囲が0をまたぐ（試行回数が足りず偶然でも出る）ときは表示しない。")


def print_alerts(alerts, label):
    print(f"\n==== 見直しの注意 (標準 {label}) ====")
    if not alerts:
        print("  なし (標準の見る期間のままでよい)")
    for a in alerts:
        print(f"  [{a['name']}] {a['text']}")


def report_premises(sim, runs, horizon, std_by_key, wt_lead_weeks, rule=None):
    """レポートの各結果の上に出す前提。数値は設定値と入力データから組み立てる"""
    rule_common = [
        "毎週金曜に、まず実験用交配を組み、そのあと親の在庫を補う維持交配を行う。",
        f"実験用交配は同じ父系統の計画をまとめ、オス1匹分の{FEMALES_PER_MALE}ペアずつ組む"
        f"（週1ペアの計画なら2週に1回{FEMALES_PER_MALE}ペア。最初のまとめ交配は今週金曜）。",
        f"オス1匹はメス2匹と同じ週に2回分交配でき、そのあと{COOLDOWN_DAYS['M']}日休む。"
        f"メスは交配後{COOLDOWN_DAYS['F']}日休み、交配は生涯{FEMALE_MAX_MATINGS}回まで。",
        f"交配に使える親は生後{MIN_AGE_MONTHS}〜{MAX_AGE_MONTHS}ヶ月。オスは、"
        "その週すでに1回使ったオス（2匹目のメス用）→ 月齢の高いオスの順に選ぶ。",
        "親がWTで足りなければ購入（生後2ヶ月の個体）。WT以外が足りなければ、その週の実験ペアは欠品。",
    ]
    target = [
        f"親オスの目標数 = ceil(MatingPerWeek × 休止週数{COOLDOWN_WEEKS['M']} ÷ {FEMALES_PER_MALE}) "
        f"＋ 維持繁殖用 {BREEDER_MALES} 匹（急死に備えて2匹）。",
        "維持交配は「見る期間」先の動ける頭数（生存・生後2ヶ月以上・生後12ヶ月以下）が目標数を下回る週に行い、"
        "交配数は翌週以降の実験用交配が組めなくならない範囲に抑える。",
        f"維持交配で生まれた子は交配の{GESTATION_DAYS}日後に誕生し、生後{MIN_AGE_MONTHS}ヶ月から親として使える。",
        "維持交配の子のうち目的系統として使えるのは、維持レシピの父母の遺伝子型から決まる割合（メンデル比）の子だけ"
        "（" + "、".join(f"{sim.strain_name.get(s, s)} {sim.strain_name.get(r['father'], '')} × "
                        f"{sim.strain_name.get(r['mother'], '')} → {sim.geno_ratio[s]:.0%}"
                        for s, r in sim.recipes.items() if (s, "M") in sim.targets or (s, "F") in sim.targets) +
        "）。Homo の胎生致死や、Het の生存率の低下は考慮しない。",
        "維持交配の結果（子の数・0匹に終わったか）は出生日まで分からない。出生前の交配は期待値の子が生まれるとして数え、"
        "出生日に実際の数で数え直す（0匹なら、その時点で不足が見えて次の維持交配を組む）。",
        "期待値 = Mating 履歴の直近1年分の平均（0匹の交配を含む）× 目的の遺伝子型の割合。丸めずに使い、"
        "維持交配の数は（目標数 − 予測頭数）÷ 期待値 を切り上げる（"
        + "、".join(f"{sim.strain_name.get(s, s)} {'オス' if sex == 'M' else 'メス'} "
                    f"{sim.yields.get(s, {}).get(sex, 0) * sim.geno_ratio.get(s, 1.0):.2f}匹"
                    for s, sex in sim.targets) + "）。",
    ]
    cage = [
        f"1ケージ・1日 {CAGE_COST_PER_DAY}円。オスは1匹で1ケージ、メスは同じ系統どうし{FEMALES_PER_CAGE}匹まで1ケージ。",
        f"飼育期間：既存マウスはシミュレーション開始日から、購入・誕生したマウスはその日から。"
        f"生後{MAX_AGE_MONTHS}ヶ月（引退）、死亡日、メスは{FEMALE_MAX_MATINGS}回目の交配の{COOLDOWN_DAYS['F']}日後のうち最も早い日まで。",
        "交配中・妊娠中・授乳中の同居や単独飼育は考慮しない。実験用の子マウスは含めない。"
        "系統タブには、その計画の目的系統・父母系統・維持レシピの親系統のケージを含める（複数の計画で共通の系統は、それぞれに含まれる）。",
    ]
    return {
        "actions": [
            f"見る期間 {std_label(sim, horizon, std_by_key)}・産仔数は過去の平均どおりとして1回計算した結果。"
            "実績とはずれていくので、毎週実行し直して最新のリストを使う。",
            *rule_common,
            "まだ生まれていない子や購入前の個体は、IDの代わりに予定として表示する。",
            "交配回数は、今週の交配を含まない、これまでの回数（Airtable の matingCount）。"
            f"メスは{FEMALE_MAX_MATINGS}回が上限なので、{FEMALE_MAX_MATINGS - 1}回のメスは今回が最後の交配になる。",
        ],
        "purchases": [
            f"発注期限 = 必要日 − 納期 {wt_lead_weeks} 週（納期は仮置き）。",
            f"今後 {PURCHASE_LIST_WEEKS} 週に必要になる分を表示。シミュレーション上は必要な週にすぐ使える前提で必要数を出している。",
        ],
        "outlook": [
            "使えるオス：その週の交配前に休止明けで交配できるオスの数（1匹で2回分）。使用：その週に交配したオスの数。",
            "2週目以降は、出産・失敗・死亡などで実際とずれる。件数の目安として見る。",
        ],
        "comparison": [
            *target,
            f"1回の交配で生まれる子の数は、Mating 履歴のうち結果が確定した直近1年分"
            f"（交配日が開始日の{OUTCOME_SETTLED_DAYS}日前までの{LITTER_HISTORY_DAYS}日間）から抽選。"
            f"0匹に終わる交配も含む。履歴の数は遺伝子型判定前の全頭数なので、維持交配では子1匹ずつ目的の遺伝子型かを抽選する。"
            f"条件ごとに {runs} 回くり返した平均と、P10・P90（下から10%・90%の位置）を示す。",
            "欠品ペア数：親が足りずに組めなかった実験用交配の数（52週の合計）。",
            "系統どうしは親を共有しないので、系統タブの各行は「その系統の見る期間だけを変えた結果」として読める。"
            if not shared_members(sim) else
            "一部の系統が親を共有しているため（" + "、".join(group_label(sim, s) for s in shared_members(sim)) +
            "）、系統タブの各行は全系統の見る期間を同時に変えた結果。",
            rule or alert_rule(),
        ],
        "cages": cage,
    }


REPORT_TEMPLATE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>交配シミュレーション</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--warn:#b45309;
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
 --ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--warn:#fab219;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;
 --axis:#383835;--ring:rgba(255,255,255,.10);--warn:#fab219;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Yu Gothic UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 2px}h3{font-size:14px;margin:12px 0 4px}
.sub{color:var(--ink2);margin:0 0 20px}.note{color:var(--ink2);font-size:12px;margin:4px 0 12px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px;margin:0 0 16px;position:relative}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font-size:11px}svg .lbl{fill:var(--ink2);font-size:12px}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:4px 0 8px;color:var(--ink2);font-size:12px}
.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.tip{position:absolute;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
 border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;white-space:nowrap;z-index:2}
.tip b{font-weight:600}
.intro p{margin:8px 0 12px}
.intro dl{display:grid;grid-template-columns:7em 1fr;gap:8px 16px;margin:0}
.intro dt{color:var(--ink2);font-weight:600}.intro dd{margin:0}
@media (max-width:600px){.intro dl{grid-template-columns:1fr}.intro dd{margin-bottom:8px}}
.premise{margin:8px 0 12px;padding:8px 12px;border-left:3px solid var(--axis);background:var(--page);border-radius:0 8px 8px 0;font-size:12.5px;color:var(--ink2)}
.premise summary{cursor:pointer;font-weight:600;color:var(--ink)}
.premise ul{margin:6px 0 2px;padding-left:1.2em}.premise li{margin:2px 0}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th{color:var(--ink2);font-weight:600}td:first-child,th:first-child{text-align:left}
td.l,th.l{text-align:left}
.virtual{color:var(--ink2);font-style:italic}
.warn{color:var(--warn);font-weight:600}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px;position:sticky;top:0;background:var(--page);padding:8px 0;z-index:3}
.tabs button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--ring);border-radius:999px;padding:6px 16px;cursor:pointer}
.tabs button[aria-selected="true"]{border-color:var(--ink);font-weight:600}
.panel[hidden]{display:none}
.members{color:var(--ink2);font-size:12.5px;margin:0 0 12px}
.alert{border-left:4px solid var(--warn)}.alert ul{margin:8px 0 4px;padding-left:1.2em}.alert li{margin:4px 0}
.alert-ok{color:var(--ink2)}
.alert-line{margin:8px 0;padding:8px 12px;border-left:3px solid var(--warn);background:var(--page);border-radius:0 8px 8px 0}
</style></head><body><main>
<h1>交配シミュレーション</h1>
<p class="sub" id="sub"></p>
<div class="card intro">
<h2>このレポートについて</h2>
<p><strong>目的：</strong>系統ごとに、今週金曜にどのマウスどうしを交配するか、この先の交配とWTメスの購入、
親の在庫を何ヶ月先まで見て補充するのがよいか（欠品とケージ費のバランス）を判断する材料にします。</p>
<dl>
<dt>使い方</dt><dd>毎週、金曜の交配の前（水〜木曜）に実行し、各系統タブの「今週金曜の交配リスト」と「購入リスト」を確認します。
月1回と実験計画を変えたとき、または下に「見直しの注意」が出たときに「見る期間の比較」を見直し、標準の見る期間を決めます。</dd>
<dt>対象</dt><dd id="intro-plans"></dd>
<dt>方法</dt><dd id="intro-method"></dd>
<dt>注意</dt><dd>シミュレーション結果は Airtable には書き込みません。実際に組んだ交配は、これまでどおり Mating に記録してください。</dd>
</dl>
</div>
<div id="alerts"></div>
<div class="tabs" role="tablist" id="tabs"></div>
<div id="panels"></div>
</main>
<script>
const DATA = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const yen = v => Math.round(v).toLocaleString('ja-JP') + ' 円';
const man = v => (v / 10000).toLocaleString('ja-JP', {maximumFractionDigits: 1}) + ' 万円';
const pct = v => (v * 100).toFixed(1) + '%';
const mo = h => h + 'ヶ月';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const NS = 'http://www.w3.org/2000/svg';
const el = (t, a = {}, p) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; };
function nice(lo, hi, n = 5) {
  const span = hi - lo || Math.abs(hi) || 1, step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0);
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, out = [];
  for (let v = a; v <= b + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}
function showTip(tip, card, html, x, y) {
  tip.innerHTML = html; tip.style.display = 'block';
  const r = card.getBoundingClientRect(), w = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(x + 12, 8), r.width - w - 8) + 'px'; tip.style.top = (y + 12) + 'px';
}
const premise = items => `<details class="premise" open><summary>この結果の前提</summary><ul>${items.map(t => `<li>${esc(t)}</li>`).join('')}</ul></details>`;
const mouse = m => m.virtual ? `<span class="virtual">${esc(m.label)}</span>` : esc(m.label);

document.getElementById('sub').textContent =
  `${DATA.start} 〜 ${DATA.end}（${DATA.weeks}週）・今週の交配日 ${DATA.start}・標準の見る期間 ${DATA.std_label}・生成 ${DATA.generated}`;
document.getElementById('intro-plans').textContent = `Plan4Experiment の実験計画（${DATA.plans.join('、')}）。`;
document.getElementById('intro-method').textContent =
  `Airtable（Resource_Mice）の在庫マウスと交配履歴をもとに、${DATA.start} から${DATA.weeks}週分の交配と在庫を週ごとに再現します。` +
  (DATA.runs ? `見る期間の比較では、子の数を過去の実績から抽選し、${DATA.horizons.map(mo).join('、')}のそれぞれで ${DATA.runs} 回くり返します。` : '');

// ---- 見直しの注意 ----
(() => {
  const host = document.getElementById('alerts');
  if (!DATA.runs) return;
  const missing = DATA.groups.filter(g => !DATA.horizons.includes(DATA.std[g.key])).map(g => g.name);
  const miss = missing.length ? `<p>${esc(missing.join('、'))} は標準の見る期間が比較に含まれていないため、判定していません。</p>` : '';
  if (DATA.auto) {
    const warn = DATA.alerts.some(a => a.kind === 'shortage');
    host.innerHTML = `<div class="card${warn ? ' alert' : ''}"><h2>見る期間の自動判定：${esc(DATA.std_label)}</h2><ul>` +
      DATA.alerts.map(a => `<li><strong>${esc(a.name)}</strong>：${esc(a.text)}</li>`).join('') +
      `</ul>${premise([DATA.alert_rule])}</div>`;
    return;
  }
  host.innerHTML = DATA.alerts.length
    ? `<div class="card alert"><h2>見直しの注意：標準の見る期間（${esc(DATA.std_label)}）</h2><ul>` +
      DATA.alerts.map(a => `<li><strong>${esc(a.name)}</strong>：${esc(a.text)}</li>`).join('') +
      `</ul>${miss}${premise([DATA.alert_rule])}</div>`
    : `<div class="card"><h2>見直しの注意</h2><p class="alert-ok">なし。どの系統も、標準の見る期間（${esc(DATA.std_label)}）より明らかに良い期間はありません。</p>${miss}${premise([DATA.alert_rule])}</div>`;
})();

// ---- タブ ----
const tabs = document.getElementById('tabs'), panels = document.getElementById('panels');
const keys = DATA.groups.map(g => g.key).concat(DATA.runs ? ['all'] : []);
const nameOf = k => k === 'all' ? '全体' : DATA.groups.find(g => g.key === k).name;
keys.forEach((k, i) => {
  const b = document.createElement('button');
  b.type = 'button'; b.role = 'tab'; b.textContent = nameOf(k); b.dataset.key = k;
  b.addEventListener('click', () => select(k));
  tabs.appendChild(b);
  const p = document.createElement('section');
  p.className = 'panel'; p.id = 'panel-' + i; p.dataset.key = k;
  panels.appendChild(p);
});
function select(k) {
  tabs.querySelectorAll('button').forEach(b => b.setAttribute('aria-selected', b.dataset.key === k));
  panels.querySelectorAll('.panel').forEach(p => p.hidden = p.dataset.key !== k);
}

// ---- 系統パネル ----
function groupPanel(p, g) {
  const acts = DATA.actions[g.key] || [], buys = DATA.purchases[g.key] || [], look = DATA.outlook[g.key] || [];
  let h = `<p class="members">計画：${esc(g.plans.join('、'))}　関連系統：${esc(g.members.join('、'))}</p>`;
  // 今週の交配リスト
  h += `<div class="card"><h2>今週金曜（${DATA.start}）の交配リスト</h2>${premise(DATA.premises.actions)}`;
  if (!acts.length) h += '<p>今週の交配はありません。</p>';
  else {
    h += '<div class="tablewrap"><table><thead><tr><th class="l">オス</th><th class="l">場所</th><th class="l">耳標</th><th>オスの交配回数<br>（今週分を除く）</th>' +
         `<th class="l">メス</th><th class="l">場所</th><th>メスの交配回数<br>（今週分を除く・上限${DATA.female_max}回）</th><th class="l">用途</th></tr></thead><tbody>`;
    acts.forEach(a => a.matings.forEach((m, i) => {
      h += '<tr>' + (i === 0 ? `<td class="l" rowspan="${a.matings.length}">${mouse(a.father)}</td><td class="l" rowspan="${a.matings.length}">${esc(a.father.location)}</td>` +
        `<td class="l" rowspan="${a.matings.length}">${esc(a.father.ear)}</td><td rowspan="${a.matings.length}">${a.father.count}</td>` : '') +
        `<td class="l">${mouse(m.mother)}</td><td class="l">${esc(m.mother.location)}</td><td>${m.mother.virtual ? '' : m.mother.count}</td>` +
        `<td class="l">${m.purpose}${m.purpose === '維持用' ? `（${esc(m.purposeStrain)}を補充）` : ''}</td></tr>`;
    }));
    h += '</tbody></table></div>';
  }
  h += '</div>';
  // 購入リスト
  h += `<div class="card"><h2>WTメスの購入リスト</h2>${premise(DATA.premises.purchases)}`;
  if (!buys.length) h += `<p>今後 ${DATA.purchase_weeks} 週に購入は必要ありません。</p>`;
  else h += '<div class="tablewrap"><table><thead><tr><th class="l">必要日</th><th class="l">系統</th><th class="l">性別</th><th>匹数</th><th class="l">発注期限</th><th class="l">状態</th></tr></thead><tbody>' +
    buys.map(b => `<tr><td class="l">${b.need}</td><td class="l">${esc(b.strain)}</td><td class="l">${b.sex}</td><td>${b.n}</td><td class="l">${b.order_by}</td>` +
      `<td class="l ${b.status === '予定' ? '' : 'warn'}">${b.status}</td></tr>`).join('') + '</tbody></table></div>';
  h += '</div>';
  // 見通し
  h += `<div class="card"><h2>${look.length}週間の見通し</h2>${premise(DATA.premises.outlook)}` +
    '<div class="tablewrap"><table><thead><tr><th class="l">交配日</th><th>実験用 成立 / 必要</th><th>維持用</th><th>使えるオス</th><th>使用したオス</th><th class="l">注意</th></tr></thead><tbody>' +
    look.map(o => `<tr><td class="l">${o.date}</td><td>${o.exp_done} / ${o.exp_req}</td><td>${o.breed}</td><td>${o.idle_males} 匹</td><td>${o.males_used} 匹</td>` +
      `<td class="l ${o.exp_done < o.exp_req ? 'warn' : ''}">${o.exp_done < o.exp_req ? `実験ペアが ${o.exp_req - o.exp_done} 不足` : ''}</td></tr>`).join('') +
    '</tbody></table></div></div>';
  h += comparisonCards(g.key);
  p.innerHTML = h;
  if (DATA.runs) drawCharts(p, g.key);
}

function comparisonCards(key) {
  if (!DATA.runs) return '<div class="card"><h2>見る期間の比較</h2><p>今回は比較を省略しました（--runs 0）。</p></div>';
  // 系統タブはその系統の標準、全体タブは全系統が同じ標準のときだけ標準の行がある
  const mixed = Object.values(DATA.std).some(h => h !== DATA.horizon);
  const stdH = key === 'all' ? (mixed ? null : DATA.horizon) : DATA.std[key];
  const S = DATA.summary.filter(s => s.group === key), base = S.find(s => s.horizon === stdH) || S[0];
  const al = DATA.alerts.filter(a => a.group === key);
  let h = `<div class="card"><h2>見る期間の比較</h2>${premise(DATA.premises.comparison)}` +
    al.map(a => `<p class="alert-line"><strong>${DATA.auto ? '自動判定' : '見直しの注意'}：</strong>${esc(a.text)}</p>`).join('') +
    `<p class="note">52週で必要な実験ペアは ${base.required} ペア。数字は ${DATA.runs} 回の平均です。</p>` +
    '<div class="tablewrap"><table><thead><tr><th>見る期間</th><th>欠品ペア（年間）</th><th>P90</th><th>欠品確率</th><th>年間ケージ費</th><th>P10〜P90</th>' +
    `<th>${mo(base.horizon)}との差</th><th>維持交配</th><th>WT購入</th></tr></thead><tbody>` +
    S.map(s => `<tr><td>${mo(s.horizon)}${s.horizon === stdH ? '（標準）' : ''}</td><td>${s.missing_mean.toFixed(1)} ペア</td><td>${s.missing_p90}</td>` +
      `<td>${pct(s.shortage_prob)}</td><td>${man(s.cost_mean)}</td><td>${man(s.cost_p10)}〜${man(s.cost_p90)}</td>` +
      `<td>${s === base ? '—' : (s.cost_mean >= base.cost_mean ? '+' : '−') + man(Math.abs(s.cost_mean - base.cost_mean))}</td>` +
      `<td>${s.breeding_mean.toFixed(1)} 回</td><td>${s.purchased_mean.toFixed(1)} 匹</td></tr>`).join('') +
    '</tbody></table></div></div>';
  h += `<div class="card"><h2>実験ペア供給とケージコスト</h2><p class="note">点 = 平均、横線 = ケージコストの P10–P90。上に行くほど供給が安定、右に行くほどコスト増。</p>` +
    '<div class="c1"></div><div class="tip"></div></div>';
  h += '<div class="card"><h2>週ごとの実験ペア欠品数（平均）</h2><div class="legend lg3"></div><div class="c3"></div><div class="tip"></div></div>';
  h += `<div class="card"><h2>週あたり平均ケージ数</h2>${premise(DATA.premises.cages)}<div class="legend lg2"></div><div class="c2"></div><div class="tip"></div></div>`;
  return h;
}

function allPanel(p) {
  const a = DATA.all_std;
  const std = a ? `<div class="card"><h2>系統別の標準（${esc(DATA.std_label)}）での全体</h2>` +
    `<p>欠品ペア 平均 ${a.missing_mean.toFixed(1)} ペア（欠品確率 ${pct(a.shortage_prob)}）、年間ケージ費 ${man(a.cost_mean)}（P10〜P90 ${man(a.cost_p10)}〜${man(a.cost_p90)}）、` +
    `維持交配 ${a.breeding_mean.toFixed(1)} 回、WT購入 ${a.purchased_mean.toFixed(1)} 匹。</p>` +
    '<p class="note">下の表は、全系統の見る期間を同じにした場合です。</p></div>' : '';
  p.innerHTML = '<p class="members">全系統の合計（ケージ費は計画にない系統の既存マウスも含む）。</p>' + std + comparisonCards('all');
  drawCharts(p, 'all');
}

// ---- グラフ ----
function drawCharts(p, key) {
  const S = DATA.summary.filter(s => s.group === key);
  const W = DATA.weekly.filter(w => w.group === key);
  scatter(p.querySelector('.c1'), S);
  if (W.every(w => w.missing_mean === 0)) {
    p.querySelector('.c3').innerHTML = '<p>どの見る期間でも、52週を通して実験ペアの欠品はありません。</p>';
  } else weeklyChart(p.querySelector('.c3'), p.querySelector('.lg3'), W, 'missing_mean', '週ごとの実験ペア欠品数', true,
    w => `欠品 ${w.missing_mean.toFixed(2)} ペア（欠品が起きる確率 ${pct(w.shortage_prob)}）`);
  weeklyChart(p.querySelector('.c2'), p.querySelector('.lg2'), W, 'cages_mean', '週ごとの平均ケージ数', false,
    w => `${w.cages_mean.toFixed(1)} ケージ（P10–P90 ${w.cages_p10.toFixed(1)}–${w.cages_p90.toFixed(1)}）`);
}

function scatter(host, S) {
  const W = 960, H = 320, m = {l: 56, r: 24, t: 16, b: 40};
  const card = host.parentNode, tip = card.querySelector('.tip');
  const svg = el('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '見る期間ごとのケージコストと実験ペア充足率'}, host);
  const xs = nice(Math.min(...S.map(s => s.cost_p10)) / 1e4, Math.max(...S.map(s => s.cost_p90)) / 1e4);
  const ylo = Math.min(...S.map(s => s.done_p10 / s.required)), ys = nice(Math.min(ylo * 100, 95), 100, 4);
  const X = v => m.l + (v / 1e4 - xs[0]) / (xs.at(-1) - xs[0]) * (W - m.l - m.r);
  const Y = v => H - m.b - (v * 100 - ys[0]) / (ys.at(-1) - ys[0]) * (H - m.t - m.b);
  ys.forEach(v => { el('line', {x1: m.l, x2: W - m.r, y1: Y(v / 100), y2: Y(v / 100), stroke: css('--grid')}, svg);
    el('text', {x: m.l - 8, y: Y(v / 100) + 4, 'text-anchor': 'end'}, svg).textContent = v + '%'; });
  xs.forEach(v => { el('text', {x: X(v * 1e4), y: H - m.b + 18, 'text-anchor': 'middle'}, svg).textContent = v + '万'; });
  el('line', {x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: css('--axis')}, svg);
  el('text', {x: (W + m.l) / 2, y: H - 4, 'text-anchor': 'middle'}, svg).textContent = '年間ケージコスト（円）';
  el('text', {x: 14, y: m.t + (H - m.t - m.b) / 2, 'text-anchor': 'middle', transform: `rotate(-90 14 ${m.t + (H - m.t - m.b) / 2})`}, svg).textContent = '実験ペア充足率（平均）';
  const c = css('--s1');
  el('polyline', {points: S.map(s => `${X(s.cost_mean)},${Y(s.supply_rate_mean)}`).join(' '), fill: 'none', stroke: c, 'stroke-width': 2, 'stroke-opacity': .35}, svg);
  let prev = null;
  S.forEach(s => {
    const cx = X(s.cost_mean), cy = Y(s.supply_rate_mean);
    el('line', {x1: X(s.cost_p10), x2: X(s.cost_p90), y1: cy, y2: cy, stroke: c, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-opacity': .6}, svg);
    el('circle', {cx, cy, r: 6, fill: c, stroke: css('--surface'), 'stroke-width': 2}, svg);
    const below = prev && !prev.below && Math.abs(prev.x - cx) < 70 && Math.abs(prev.y - cy) < 20;
    el('text', {x: cx, y: below ? cy + 22 : cy - 12, 'text-anchor': 'middle', class: 'lbl'}, svg).textContent = mo(s.horizon);
    prev = {x: cx, y: cy, below};
    const hit = el('circle', {cx, cy, r: 18, fill: 'transparent'}, svg);
    hit.addEventListener('mousemove', e => { const r = card.getBoundingClientRect();
      showTip(tip, card, `<b>${mo(s.horizon)}先を見る</b><br>実験ペア 平均 ${s.done_mean.toFixed(1)} / 必要 ${s.required}（P10 ${s.done_p10}）<br>欠品ペア 平均 ${s.missing_mean.toFixed(2)}<br>ケージ費 平均 ${yen(s.cost_mean)}<br>P10–P90 ${man(s.cost_p10)} – ${man(s.cost_p90)}`,
        e.clientX - r.left, e.clientY - r.top); });
    hit.addEventListener('mouseleave', () => tip.style.display = 'none');
  });
}

function weeklyChart(host, legend, rows, key, label, fromZero, rowFmt) {
  const W = 960, H = 280, m = {l: 48, r: 64, t: 12, b: 32};
  const card = host.parentNode, tip = card.querySelector('.tip');
  const hs = [...new Set(rows.map(r => r.horizon))].slice(0, 8);
  const series = hs.map((h, i) => ({h, color: css('--s' + (i + 1)), pts: rows.filter(r => r.horizon === h)}));
  legend.innerHTML = series.map(s => `<span><i style="background:${s.color}"></i>${mo(s.h)}</span>`).join('');
  const svg = el('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': label}, host);
  const all = series.flatMap(s => s.pts.map(p => p[key]));
  const ys = nice(fromZero ? 0 : Math.min(...all) * 0.95, Math.max(...all) || 1, 4);
  const n = series[0].pts.length;
  const X = i => m.l + i / (n - 1) * (W - m.l - m.r), Y = v => H - m.b - (v - ys[0]) / (ys.at(-1) - ys[0]) * (H - m.t - m.b);
  ys.forEach(v => { el('line', {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: css('--grid')}, svg);
    el('text', {x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end'}, svg).textContent = v; });
  series[0].pts.forEach((p, i) => { if (i % 8 === 0) el('text', {x: X(i), y: H - m.b + 18, 'text-anchor': 'middle'}, svg).textContent = p.date.slice(2).replace(/-/g, '/'); });
  el('line', {x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: css('--axis')}, svg);
  series.forEach(s => el('polyline', {points: s.pts.map((p, i) => `${X(i)},${Y(p[key])}`).join(' '), fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round'}, svg));
  const ends = series.map(s => ({s, y: Y(s.pts.at(-1)[key])})).sort((a, b) => a.y - b.y);
  if (ends.at(-1).y - ends[0].y > 6) {
    for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 13);
    ends.forEach(e => { el('text', {x: W - m.r + 8, y: e.y + 4, class: 'lbl'}, svg).textContent = mo(e.s.h); });
  }
  const cross = el('line', {y1: m.t, y2: H - m.b, stroke: css('--axis'), visibility: 'hidden'}, svg);
  const dots = series.map(s => el('circle', {r: 4, fill: s.color, stroke: css('--surface'), 'stroke-width': 2, visibility: 'hidden'}, svg));
  const ov = el('rect', {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: 'transparent'}, svg);
  ov.addEventListener('mousemove', e => {
    const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
    const i = Math.max(0, Math.min(n - 1, Math.round((loc.x - m.l) / (W - m.l - m.r) * (n - 1))));
    cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('visibility', 'visible');
    series.forEach((s, k) => { dots[k].setAttribute('cx', X(i)); dots[k].setAttribute('cy', Y(s.pts[i][key])); dots[k].setAttribute('visibility', 'visible'); });
    const r = card.getBoundingClientRect();
    showTip(tip, card, `<b>${series[0].pts[i].date} の週</b><br>` + series.map(s =>
      `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color};margin-right:6px"></span>${mo(s.h)}　${rowFmt(s.pts[i])}`).join('<br>'),
      e.clientX - r.left, e.clientY - r.top);
  });
  ov.addEventListener('mouseleave', () => { tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); });
}

panels.querySelectorAll('.panel').forEach(p => {
  if (p.dataset.key === 'all') allPanel(p);
  else groupPanel(p, DATA.groups.find(g => g.key === p.dataset.key));
});
select(keys[0]);
</script></body></html>
"""


def write_report(path, sim, horizon, std_by_key, horizons, runs, actions, purchases, outlook, summary, weekly,
                 alerts, all_std, wt_lead_weeks, rule=None):
    sn = lambda s: sim.strain_name.get(s, s)
    data = {
        "start": sim.start.isoformat(), "end": (sim.end - timedelta(days=1)).isoformat(), "weeks": WEEKS,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "runs": runs,
        "horizon": horizon, "std": std_by_key, "std_label": std_label(sim, horizon, std_by_key),
        "all_std": all_std, "horizons": horizons, "purchase_weeks": PURCHASE_LIST_WEEKS,
        "female_max": FEMALE_MAX_MATINGS,
        "plans": [f"{sn(p['strain'])} 週{p['per_week']:g}ペア" for p in sim.plans],
        "groups": [{"key": k, "name": sn(k),
                    "plans": [f"{sn(p['strain'])}（父 {sn(p['father'])} × 母 {sn(p['mother'])}、週{p['per_week']:g}ペア）"
                              for p in g["plans"]],
                    "members": sorted(sn(s) for s in g["members"])} for k, g in sim.groups.items()],
        "actions": actions, "purchases": purchases, "outlook": outlook,
        "summary": summary, "weekly": weekly, "alerts": alerts, "alert_rule": rule or alert_rule(), "auto": rule is not None,
        "premises": report_premises(sim, runs, horizon, std_by_key, wt_lead_weeks, rule),
    }
    path.write_text(REPORT_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)), encoding="utf-8")


def actions_csv_rows(sim, actions):
    rows = []
    for key, items in actions.items():
        for a in items:
            for mt in a["matings"]:
                rows.append({"交配日": sim.start.isoformat(), "計画": group_label(sim, key), "用途": mt["purpose"],
                             "目的系統": mt["purposeStrain"],
                             "オス": a["father"]["label"], "オス場所": a["father"]["location"], "オス耳標": a["father"]["ear"],
                             "メス": mt["mother"]["label"], "メス場所": mt["mother"]["location"], "メス耳標": mt["mother"]["ear"]})
    return rows


def print_litter_info(sim):
    print("\n[産仔分布 (履歴)  ※1交配あたり・遺伝子型判定前の全頭数。失敗交配=0匹を含む]")
    strains = dict.fromkeys(s for p in sim.plans for s in (p["strain"], p["father"], p["mother"]))
    for s in strains:
        if is_wt(sim.strain_name.get(s, "")):
            continue
        own = sim.litters.get(s, [])
        src = sim.litter_source(s)
        mm = sum(x[0] for x in src) / len(src)
        mf = sum(x[1] for x in src) / len(src)
        fail = sum(1 for x in src if x == (0, 0)) / len(src)
        note = "" if src is own else f" (履歴{len(own)}件のため全系統プール)"
        ratio = sim.geno_ratio.get(s, 1.0)
        print(f"  {sim.strain_name.get(s, s):20s} n={len(src):4d}  平均 M{mm:.2f} F{mf:.2f}  "
              f"0匹率 {fail:.0%}  目的の遺伝子型 {ratio:.0%} → 期待値 M{mm * ratio:.2f} F{mf * ratio:.2f}{note}")
    print("\n[親オスの目標数]  ceil(MatingPerWeek × 3 ÷ 2) + 2")
    for (s, sex), t in sim.targets.items():
        print(f"  {sim.strain_name.get(s, s)} {sex}: {t} 匹")


# ======================================================================
def write_proposal(path, sim, std_by_key, label, auto_notes, actions, purchases, summary, runs, out_rel):
    """チャット・Airtable 反映用の提案 (今週の交配リスト・購入リスト・見る期間の判定)"""
    def row(key, h):
        s = next((x for x in summary if x["group"] == key and x["horizon"] == h), None)
        return s and {k: s[k] for k in ("missing_mean", "shortage_prob", "cost_mean")}
    data = {
        "generated": datetime.now().isoformat(timespec="seconds"), "today": sim.today.isoformat(),
        "mating_date": sim.start.isoformat(), "runs": runs, "horizon_label": label, "report": f"{out_rel}/report.html",
        "groups": [{"key": k, "name": group_label(sim, k), "horizon": h, "metrics": row(k, h),
                    "auto": next((a for a in auto_notes if a["group"] == k), None),
                    "matings": [{"purpose": "Experiment" if mt["purpose"] == "実験用" else "Breeding",
                                 "purposeStrainId": mt["purposeStrainId"],
                                 "purposeStrain": mt["purposeStrain"],
                                 "father": a["father"], "mother": mt["mother"]}
                                for a in actions[k] for mt in a["matings"]],
                    "purchases": purchases[k]}
                   for k, h in std_by_key.items()],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Resource_Mice 繁殖シミュレーション (Airtable は読み取りのみ)")
    ap.add_argument("--auto", action="store_true",
                    help=f"見る期間を系統ごとに自動で決める (年間欠品ペアの上限 --max-missing 以下で最も安い期間)")
    ap.add_argument("--max-missing", type=float, default=MAX_MISSING_PAIRS,
                    help="--auto のときの系統ごとの年間欠品ペアの上限")
    ap.add_argument("--horizon", type=float, default=DEFAULT_HORIZON_MONTHS,
                    help="交配リストで使う、何ヶ月先の動けるマウスを数えるか (標準)")
    ap.add_argument("--strain-horizon", action="append", default=[], metavar="系統名=ヶ月",
                    help="系統ごとの標準の見る期間 (例: CUL1-KO-Het=4.5)。くり返し指定可。STRAIN_HORIZONS より優先")
    ap.add_argument("--horizons", help=f"比較する見る期間 (ヶ月, カンマ区切り。既定 {DEFAULT_HORIZONS}、--auto では {AUTO_HORIZONS})")
    ap.add_argument("--runs", type=int, help=f"比較の試行回数 (0 なら比較を省略。既定 200、--auto では {AUTO_RUNS})")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--wt-lead-weeks", type=int, default=WT_LEAD_WEEKS, help="WTメスの納期 (週)")
    ap.add_argument("--today", help="基準日 YYYY-MM-DD (既定: 今日)")
    args = ap.parse_args()
    runs = args.runs if args.runs is not None else (AUTO_RUNS if args.auto else 200)
    horizons = [float(x) for x in (args.horizons or (AUTO_HORIZONS if args.auto else DEFAULT_HORIZONS)).split(",")
                if x.strip()]
    if args.auto and runs <= 0:
        sys.exit("--auto には比較が必要です (--runs を 1 以上に)")

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    at = Airtable(load_token())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sim = Simulator(fetch_inputs(at), today)
    out_dir = SCRIPT_DIR / "results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print_litter_info(sim)

    # 見る期間の比較 (モンテカルロ)
    summary, weekly, per_run, alerts, all_std, rule = [], [], {}, [], None, None
    if runs > 0:
        print(f"\n比較を実行中 (見る期間 {[months_label(h) for h in horizons]}, 各 {runs} 回)...")
        summary, weekly, per_run = run_comparison(sim, horizons, runs, args.seed)
        write_csv(out_dir / "comparison_summary.csv",
                  [{**r, "group": group_label(sim, r["group"])} for r in summary])
        write_csv(out_dir / "comparison_weekly.csv",
                  [{**r, "group": group_label(sim, r["group"])} for r in weekly])
        print_comparison(sim, summary, runs)

    if args.auto:
        std_by_key, alerts = choose_horizons(sim, summary, per_run, args.max_missing)
        rule = auto_rule(args.max_missing, horizons)
        horizon = Counter(std_by_key.values()).most_common(1)[0][0]  # 表示上の標準 (系統で最も多い期間)
        label = std_label(sim, horizon, std_by_key)
        print(f"\n==== 見る期間の自動判定 (系統ごとの年間欠品ペアの上限 {args.max_missing:g}) ====")
        for a in alerts:
            print(f"  [{a['name']}] {'警告: ' if a['kind'] == 'shortage' else ''}{a['text']}")
    else:
        by_name = dict(STRAIN_HORIZONS)
        for item in args.strain_horizon:
            name, _, h = item.partition("=")
            by_name[name.strip()] = float(h)
        horizon = args.horizon
        std_by_key = sim.resolve_horizons(horizon, by_name)
        label = std_label(sim, horizon, std_by_key)
        if runs > 0:
            missing = [group_label(sim, k) for k, h in std_by_key.items() if h not in horizons]
            if missing:
                print(f"\n標準の見る期間が比較に含まれていない系統 ({'、'.join(missing)}) は、見直しの注意を判定しません。")
            alerts = horizon_alerts(sim, summary, per_run, std_by_key)
            print_alerts(alerts, label)

    if runs > 0 and any(h != horizon for h in std_by_key.values()):
        # 系統別の見る期間どおりに組んだときの全体 (計画にない系統も含むケージ費は、この実行でしか出せない)
        print("\n系統別の見る期間で全体を計算中...")
        all_std = next(r for r in run_comparison(sim, [horizon], runs, args.seed, std_by_key)[0]
                       if r["group"] == "all")
        print(f"  全体 ({label}): 欠品ペア {all_std['missing_mean']:.2f}  欠品確率 {all_std['shortage_prob']:.0%}  "
              f"ケージ費 {all_std['cost_mean']:,.0f} 円")

    # 週次の交配リスト (期待値で1回)
    print(f"\n見る期間: {label}")
    res = sim.run(horizon, record=True, horizon_by=std_by_key)
    actions, purchases = build_actions(sim, res, args.wt_lead_weeks)
    outlook = dict(res.outlook)
    print_actions(sim, actions, purchases, outlook)
    write_csv(out_dir / "actions.csv", actions_csv_rows(sim, actions))
    write_csv(out_dir / "purchases.csv",
              [{"計画": group_label(sim, k), **r} for k, rows in purchases.items() for r in rows])

    write_report(out_dir / "report.html", sim, horizon, std_by_key, horizons, runs,
                 actions, purchases, outlook, summary, weekly, alerts, all_std, args.wt_lead_weeks, rule)
    write_proposal(out_dir / "proposal.json", sim, std_by_key, label, alerts if args.auto else [],
                   actions, purchases, summary, runs, f"results/{stamp}")
    print(f"\n出力: results/{stamp}/ (report.html, proposal.json, actions.csv, purchases.csv"
          + (", comparison_summary.csv, comparison_weekly.csv" if runs > 0 else "") + ")")


if __name__ == "__main__":
    main()
