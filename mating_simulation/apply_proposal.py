"""
シミュレーションの提案 (proposal.json) のうち、今週の交配を Airtable の Mating テーブルに予定として登録する

使い方:
  python apply_proposal.py results/<日時>/proposal.json           # 登録する内容を表示するだけ (書き込まない)
  python apply_proposal.py results/<日時>/proposal.json --apply   # Mating に登録する (書き込み権限のあるトークンが必要)
  python apply_proposal.py results/<日時>/proposal.json --emit-json  # 登録するレコードを JSON で出す (Airtable コネクタで登録するとき)

登録する項目: scheduledDate (交配日), purposeStrain, purpose, fatherMouseId, motherMouseId, Note
  - matingDate は空のまま (実際に交配したら記入する)。indexInYear は Airtable の自動化 (MatingAddYear) が採番する
  - 購入予定・誕生予定など、まだ Airtable にいない個体を使う交配は登録しない (表示だけ)
  - 同じ交配日・同じオス・同じメスの Mating がすでにあれば登録しない (二重登録の防止)
  - 交配日が過ぎた提案は登録しない
表示と --emit-json は読み取りのみ (data.records:read)。--apply には data.records:write のスコープが必要
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

import mating_simulation as ms

BATCH = 10  # Airtable の1リクエストで作れるレコード数の上限


def find_scheduled(at, mating_date):
    """その交配日の Mating (scheduledDate または matingDate が一致)"""
    formula = (f"OR(IS_SAME({{scheduledDate}}, '{mating_date}', 'day'), "
               f"IS_SAME({{matingDate}}, '{mating_date}', 'day'))")
    url = f"{ms.API_URL}/{requests.utils.quote(ms.T_MATING)}"
    params = {"pageSize": 100, "filterByFormula": formula,
              "fields[]": ["matingId", "purpose", "purposeStrain", "fatherMouseId", "motherMouseId"]}
    records = []
    while True:
        r = at.s.get(url, params=params, timeout=60)
        if not r.ok:
            raise RuntimeError(f"GET Mating -> {r.status_code}: {r.text}")
        data = r.json()
        records.extend(data["records"])
        if "offset" not in data:
            return records
        params["offset"] = data["offset"]
        time.sleep(0.22)


def create(at, fields_list):
    url = f"{ms.API_URL}/{requests.utils.quote(ms.T_MATING)}"
    created = []
    for i in range(0, len(fields_list), BATCH):
        chunk = fields_list[i:i + BATCH]
        r = at.s.post(url, json={"records": [{"fields": f} for f in chunk]}, timeout=60)
        if not r.ok:
            raise RuntimeError(f"POST Mating -> {r.status_code}: {r.text}"
                               + (f"（{len(created)} 件は登録済み）" if created else ""))
        created.extend(r.json()["records"])
        time.sleep(0.22)
    return created


def main():
    ap = argparse.ArgumentParser(description="今週の交配の提案を Airtable の Mating に予定として登録する")
    ap.add_argument("proposal", help="results/<日時>/proposal.json")
    ap.add_argument("--apply", action="store_true", help="実際に登録する (指定しなければ表示だけ)")
    ap.add_argument("--emit-json", action="store_true", help="登録するレコードの fields を JSON で出す (書き込まない)")
    ap.add_argument("--today", help="基準日 YYYY-MM-DD (既定: 今日)")
    args = ap.parse_args()

    path = Path(args.proposal)
    prop = json.loads(path.read_text(encoding="utf-8"))
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    mating_date = prop["mating_date"]
    if date.fromisoformat(mating_date) < today:
        sys.exit(f"交配日 {mating_date} が過ぎているため登録しません。シミュレーションを実行し直してください。")

    at = ms.Airtable(ms.load_token())
    existing = find_scheduled(at, mating_date)
    pairs = {(ms.first(r["fields"].get("fatherMouseId")), ms.first(r["fields"].get("motherMouseId"))) for r in existing}

    to_create, skipped = [], []
    for g in prop["groups"]:
        for mt in g["matings"]:
            fa, mo = mt["father"], mt["mother"]
            desc = (f"{g['name']} {'実験用' if mt['purpose'] == 'Experiment' else '維持用'} "
                    f"{mt['purposeStrain']}：オス {fa['label']} × メス {mo['label']}")
            if not fa.get("id") or not mo.get("id"):
                skipped.append(f"{desc}（Airtable にまだいない個体のため登録しない）")
            elif (fa["id"], mo["id"]) in pairs:
                skipped.append(f"{desc}（同じ交配がすでにあるため登録しない）")
            else:
                pairs.add((fa["id"], mo["id"]))
                to_create.append((desc, {
                    "scheduledDate": mating_date,
                    "purposeStrain": [mt["purposeStrainId"]],
                    "purpose": mt["purpose"],
                    "fatherMouseId": [fa["id"]],
                    "motherMouseId": [mo["id"]],
                    "Note": f"シミュレーション提案 ({path.parent.name})",
                }))

    if args.emit_json:
        print(json.dumps({"table": ms.T_MATING, "base": ms.BASE_ID, "records": [{"fields": f} for _, f in to_create],
                          "skipped": skipped}, ensure_ascii=False, indent=1))
        return
    print(f"交配日 {mating_date}（提案 {path.parent.name}、見る期間 {prop['horizon_label']}）")
    print(f"\n登録する Mating: {len(to_create)} 件")
    for desc, _ in to_create:
        print(f"  + {desc}")
    if skipped:
        print(f"\n登録しないもの: {len(skipped)} 件")
        for s in skipped:
            print(f"  - {s}")
    if existing:
        print(f"\n（この交配日の Mating はすでに {len(existing)} 件あります）")

    if not args.apply:
        print("\n表示のみ（Airtable は変更していません）。登録するには --apply を付けて実行します。")
        return
    if not to_create:
        print("\n登録するものはありません。")
        return
    created = create(at, [f for _, f in to_create])
    log = path.parent / "applied.json"
    log.write_text(json.dumps({"applied": datetime.now().isoformat(timespec="seconds"),
                               "records": [r["id"] for r in created]}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\nMating に {len(created)} 件登録しました（記録: {log}）。")


if __name__ == "__main__":
    main()
