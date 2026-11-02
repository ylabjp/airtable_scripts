"""
繁殖シミュレーションの実行画面 (ブラウザ用)

使い方:
  python webapp.py            # http://127.0.0.1:8765/ がブラウザで開く
  (start_webapp.bat をダブルクリックしても同じ)

画面で条件を選んで「実行」すると mating_simulation.py を実行し、終わったらレポートを開ける。
このパソコンからだけ開ける (127.0.0.1)。Airtable は読み取りのみで、結果は書き込まない。
"""

import html
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import mating_simulation as ms

HOST, PORT = "127.0.0.1", 8765
SCRIPT = ms.SCRIPT_DIR / "mating_simulation.py"
RESULTS = ms.SCRIPT_DIR / "results"
HORIZON_CHOICES = [x / 2 for x in range(3, 15)]  # 1.5〜7ヶ月 (0.5刻み)
RECENT_RESULTS = 10


# ---- 実験計画の系統名 (系統別の見る期間の選択肢) ----
_plan_strains = {"names": None, "error": None}


def load_plan_strains(force=False):
    """実験計画 (MatingPerWeek > 0) の目的系統名。Plan と Strain の2テーブルだけ読む"""
    if _plan_strains["names"] is not None and not force:
        return _plan_strains["names"], None
    try:
        at = ms.Airtable(ms.load_token())
        names = {s["id"]: s["fields"].get("name", s["id"])
                 for s in at.list_all(ms.T_STRAIN, ["name"])}
        out = []
        for p in sorted(at.list_all(ms.T_PLAN), key=lambda p: p["fields"].get("id", 0)):
            f = p["fields"]
            strain = ms.first(f.get("Strain"))
            if strain and (ms.num(f.get("MatingPerWeek")) or 0) > 0:
                out.append(names.get(strain, strain))
        _plan_strains["names"] = list(dict.fromkeys(out))
        return _plan_strains["names"], None
    except (Exception, SystemExit) as e:  # load_token は SystemExit で止まる
        return [], str(e)


# ---- 実行 (同時に1つだけ) ----
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.log = []
        self.args = []
        self.result = None   # results/ 下のフォルダ名
        self.returncode = None

    @property
    def running(self):
        return self.proc is not None and self.returncode is None

    def start(self, args):
        with self.lock:
            if self.running:
                return False
            self.log, self.args, self.result, self.returncode = [], args, None, None
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(SCRIPT), *args], cwd=ms.SCRIPT_DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._read, daemon=True).start()
            return True

    def _read(self):
        for line in self.proc.stdout:
            self.log.append(line.rstrip("\n"))
            m = re.search(r"出力: results/([^/\s]+)/", line)
            if m:
                self.result = m.group(1)
        self.returncode = self.proc.wait()

    def status(self):
        return {"running": self.running, "returncode": self.returncode, "result": self.result,
                "log": "\n".join(self.log), "args": " ".join(self.args)}


JOB = Job()


def recent_results():
    if not RESULTS.exists():
        return []
    dirs = sorted((d for d in RESULTS.iterdir() if (d / "report.html").exists()), reverse=True)
    return [d.name for d in dirs[:RECENT_RESULTS]]


def build_args(form):
    """フォームの値 → mating_simulation.py の引数。選択肢以外の値は ValueError"""
    get = lambda k, d="": (form.get(k) or [d])[0].strip()

    def months(v):
        h = float(v)
        if h not in HORIZON_CHOICES:
            raise ValueError(f"見る期間 {v} は選択肢にありません")
        return h

    args = ["--horizon", f"{months(get('horizon')):g}"]
    names, _ = load_plan_strains()
    for i, name in enumerate(names):
        v = get(f"sh{i}")
        if v:
            args += ["--strain-horizon", f"{name}={months(v):g}"]
    hs = sorted({months(v) for v in form.get("horizons", [])})
    runs = int(get("runs", "200"))
    if not 0 <= runs <= 5000:
        raise ValueError("試行回数は 0〜5000 で指定してください")
    if runs > 0 and not hs:
        raise ValueError("比較する見る期間を1つ以上選んでください (比較しないなら試行回数を 0 に)")
    if hs:
        args += ["--horizons", ",".join(f"{h:g}" for h in hs)]
    args += ["--runs", str(runs)]
    lead = int(get("lead", str(ms.WT_LEAD_WEEKS)))
    if not 0 <= lead <= 26:
        raise ValueError("WTの納期は 0〜26 週で指定してください")
    args += ["--wt-lead-weeks", str(lead)]
    seed = int(get("seed", "1"))
    args += ["--seed", str(seed)]
    today = get("today")
    if today:
        date.fromisoformat(today)
        args += ["--today", today]
    return args


# ---- 画面 ----
STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#5f6b7a;--line:#dde2e8;--accent:#2f6fde;--accent-text:#fff;
--warn-bg:#fff4e0;--warn:#8a5300;--err:#b3261e;--ok:#1e7d3a;--code:#f0f2f5}
@media (prefers-color-scheme: dark){:root{--bg:#15181d;--card:#1e232a;--text:#e6e9ee;--muted:#9aa5b3;--line:#323943;
--accent:#6a9bf0;--accent-text:#0d1117;--warn-bg:#3a2c12;--warn:#f2c374;--err:#f2867c;--ok:#7fd49a;--code:#262c34}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,"Segoe UI","Yu Gothic UI","Meiryo",sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:1.35rem;margin:0 0 4px}h2{font-size:1.05rem;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:0 0 16px}
label{display:block;font-weight:600;margin:12px 0 4px}label:first-child{margin-top:0}
.hint{color:var(--muted);font-size:.85rem;font-weight:400;margin:2px 0 0}
select,input[type=number],input[type=date]{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);
border-radius:6px;padding:6px 8px;min-width:140px}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 12px;align-items:center}
.row span{overflow-wrap:anywhere}
.checks{display:flex;flex-wrap:wrap;gap:6px 14px}.checks label{font-weight:400;margin:0;display:flex;gap:4px;align-items:center}
button,.btn{font:inherit;font-weight:600;background:var(--accent);color:var(--accent-text);border:0;border-radius:8px;
padding:10px 22px;cursor:pointer;text-decoration:none;display:inline-block}
button:disabled{opacity:.5;cursor:default}
details summary{cursor:pointer;color:var(--muted)}
pre{background:var(--code);border-radius:6px;padding:10px;overflow:auto;max-height:420px;font-size:.8rem;white-space:pre-wrap}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:10px 12px;margin:0 0 16px}
.err{color:var(--err);font-weight:600}.ok{color:var(--ok);font-weight:600}
ul.list{padding-left:20px;margin:0}a{color:var(--accent)}
"""


def page(title, body):
    return (f"<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style></head><body><main>{body}</main></body></html>")


def months_options(selected, blank=None):
    opts = [f"<option value=''>{html.escape(blank)}</option>"] if blank is not None else []
    opts += [f"<option value='{h:g}'{' selected' if h == selected else ''}>{ms.months_label(h)}</option>"
             for h in HORIZON_CHOICES]
    return "".join(opts)


def home_page(error=None):
    names, load_err = load_plan_strains()
    default_hs = [float(x) for x in ms.DEFAULT_HORIZONS.split(",")]
    parts = ["<h1>繁殖シミュレーション</h1>",
             "<p class='sub'>条件を選んで「実行」を押すと、今週金曜の交配リスト・見通し・購入リスト・見る期間の比較を作ります。"
             "Airtable は読み取りのみです。</p>"]
    if JOB.running:
        parts.append("<p class='warn'>実行中です。<a href='/job'>進み具合を見る</a></p>")
    if error:
        parts.append(f"<p class='warn'>{html.escape(error)}</p>")
    if load_err:
        parts.append(f"<p class='warn'>実験計画を読み込めませんでした（系統別の見る期間は選べません）：{html.escape(load_err)}</p>")

    strain_rows = "".join(
        f"<div class='row'><span>{html.escape(n)}</span>"
        f"<select name='sh{i}'>{months_options(ms.STRAIN_HORIZONS.get(n), '標準と同じ')}</select></div>"
        for i, n in enumerate(names))
    checks = "".join(
        f"<label><input type='checkbox' name='horizons' value='{h:g}'{' checked' if h in default_hs else ''}>"
        f"{ms.months_label(h)}</label>" for h in HORIZON_CHOICES)
    parts.append(f"""
<form method="post" action="/run">
 <div class="card"><h2>交配リスト</h2>
  <label for="horizon">標準の見る期間</label>
  <select id="horizon" name="horizon">{months_options(ms.DEFAULT_HORIZON_MONTHS)}</select>
  <p class="hint">何ヶ月先の動けるマウスを数えて維持交配を組むか。</p>
  {"<label>系統ごとの見る期間</label><p class='hint'>標準と変えたい系統だけ選びます。</p><div class='row'>" + strain_rows + "</div>" if names else ""}
  <label for="lead">WTメスの納期（週）</label>
  <input id="lead" name="lead" type="number" min="0" max="26" value="{ms.WT_LEAD_WEEKS}">
 </div>
 <div class="card"><h2>見る期間の比較</h2>
  <label>比較する見る期間</label><div class="checks">{checks}</div>
  <label for="runs">試行回数</label>
  <input id="runs" name="runs" type="number" min="0" max="5000" step="50" value="200">
  <p class="hint">200回で約1分。0にすると比較を省略して交配リストだけ作ります（数秒）。</p>
 </div>
 <div class="card"><details><summary>詳細設定</summary>
  <label for="today">基準日</label><input id="today" name="today" type="date">
  <p class="hint">空欄なら今日。この日の次の金曜日から52週を計算します。</p>
  <label for="seed">乱数の種</label><input id="seed" name="seed" type="number" value="1">
  <p class="hint">変えると比較の結果が少し変わります。ぶれの確認用。</p>
 </details></div>
 <button type="submit"{" disabled" if JOB.running else ""}>実行</button>
 {"" if not names and not load_err else "<a href='/?reload=1' style='margin-left:12px'>実験計画を読み直す</a>"}
</form>""")
    recent = recent_results()
    if recent:
        items = "".join(f"<li><a href='/results/{r}/report.html' target='_blank'>{fmt_stamp(r)}</a></li>" for r in recent)
        parts.append(f"<div class='card' style='margin-top:20px'><h2>これまでのレポート</h2><ul class='list'>{items}</ul></div>")
    return page("繁殖シミュレーション", "".join(parts))


def fmt_stamp(s):
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", s)
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]} 作成" if m else html.escape(s)


JOB_PAGE = """<h1>実行中</h1><p class='sub' id='state'>計算しています。終わるとレポートを開けます。</p>
<div class='card'><p id='done' style='margin:0 0 8px'></p><pre id='log'></pre></div>
<p><a href='/'>条件の画面に戻る</a></p>
<script>
const esc = s => s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function poll(){
  const r = await fetch('/status'); const s = await r.json();
  const log = document.getElementById('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = s.log || '開始しています...';
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (s.running) { setTimeout(poll, 1000); return; }
  document.querySelector('h1').textContent = s.returncode === 0 ? '完了' : 'エラー';
  document.getElementById('state').textContent = '';
  const done = document.getElementById('done');
  if (s.returncode === 0 && s.result) {
    const url = '/results/' + encodeURIComponent(s.result) + '/report.html';
    done.innerHTML = "<span class='ok'>完了しました。</span> <a class='btn' href='" + url + "' target='_blank'>レポートを開く</a>";
    window.open(url, '_blank');
  } else if (s.returncode === null) {
    done.textContent = '実行していません。';
  } else {
    done.innerHTML = "<span class='err'>エラーで止まりました。下の表示を確認してください。</span>";
  }
}
poll();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            if "reload" in parse_qs(url.query):
                load_plan_strains(force=True)
            return self.send(home_page())
        if url.path == "/job":
            return self.send(page("実行中", JOB_PAGE))
        if url.path == "/status":
            return self.send(json.dumps(JOB.status(), ensure_ascii=False), "application/json; charset=utf-8")
        m = re.fullmatch(r"/results/([0-9_]+)/([a-z_]+\.(html|csv))", url.path)
        if m:
            f = RESULTS / m[1] / m[2]
            if f.is_file():
                ctype = "text/html; charset=utf-8" if m[3] == "html" else "text/csv; charset=utf-8"
                return self.send(f.read_bytes(), ctype)
        self.send(page("見つかりません", "<p>ページが見つかりません。<a href='/'>戻る</a></p>"), code=404)

    def do_POST(self):
        # 別のサイトからの送信は受けない
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self.send("forbidden", "text/plain", 403)
        if urlparse(self.path).path != "/run":
            return self.send("not found", "text/plain", 404)
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode("utf-8"))
        try:
            args = build_args(form)
        except ValueError as e:
            return self.send(home_page(f"入力を確認してください：{e}"))
        if not JOB.start(args):
            return self.send(home_page("別の実行が終わるまで待ってください。"))
        self.redirect("/job")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"繁殖シミュレーション: {url} を開いています (終了は Ctrl+C か、この窓を閉じる)")
    threading.Thread(target=load_plan_strains, daemon=True).start()
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
