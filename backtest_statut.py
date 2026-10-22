"""Chance de jogar por status: o injury report oficial da NBA x quem jogou.

O bot usa chances por status (GTD 50%, Questionable 55%...) que eram chute:
nem o Sleeper nem a ESPN guardam historico de status. A NBA guarda: o
injury report oficial sai em PDF varias vezes por dia e o arquivo das
temporadas passadas esta no ar. Cruzando o status de cada jogador no
relatorio com o boxscore da ESPN (backtest_ecart.py baixar), mede:

  * a chance de jogar por status, no ultimo relatorio antes do tip-off e
    nos das 13:30 e 17:30 ET (o bot decide a escalacao horas antes);
  * como isso muda com o motivo (lesao x descanso), a minutagem, a
    experiencia, a fase da temporada, a campanha do time e o b2b;
  * e, de quem joga, quantos minutos e pontos faz perto do normal.

    python backtest_statut.py baixar    # relatorios de 2023-2025 (~30 min)
    python backtest_statut.py           # as tabelas
"""

from __future__ import annotations

import io
import json
import re
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from backtest_ecart import PBP, TEMPORADAS, jogos

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = ZoneInfo("America/New_York")
PASTA = PBP.parent / "injury_report"
URL = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{}.pdf"
# O arquivo "05PM" e o relatorio das 17:30. A partir de meados de dez/2025
# o nome vira "05_00PM" e sai a cada 15-30 minutos.
HORAS_VELHO = ["01PM", "05PM", "06PM", "07PM", "08PM", "09PM"]
HORAS_NOVO = ["01_00PM", "05_00PM", "06_00PM", "06_30PM", "07_00PM", "08_00PM", "09_00PM"]
STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")


def norm(nome: str) -> str:
    nome = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", nome.lower())


def nome_relatorio(s: str) -> str:
    """"McCullarJr.,Kevin" -> "kevinmccullarjr" (a ESPN escreve Nome Sobrenome)."""
    if "," in s:
        sob, nom = s.split(",", 1)
        return norm(nom + sob)
    return norm(s)


# ==========================================================================
# download e leitura do PDF
# ==========================================================================
def ler_pdf(conteudo: bytes) -> tuple[str | None, list[dict]]:
    """(hora do relatorio 'HH:MM', [{data, hora_jogo, jogo, nome, status, motivo}])."""
    import pdfplumber
    linhas, hora = [], None
    with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
        cols = None
        atual = {"data": None, "hora": None, "jogo": None}
        for pg in pdf.pages:
            ws = pg.extract_words(keep_blank_chars=False, use_text_flow=False)
            txt = " ".join(w["text"] for w in ws[:8])
            m = re.search(r"(\d{2}:\d{2})\s*(AM|PM)", txt)
            if m and hora is None:
                h, mm = m.group(1).split(":")
                hora = f"{(int(h) % 12) + (12 if m.group(2) == 'PM' else 0):02d}:{mm}"
            cab = {w["text"]: w["x0"] for w in ws if w["text"] in
                   ("GameDate", "GameTime", "Matchup", "Team", "PlayerName", "CurrentStatus", "Reason")}
            if len(cab) >= 6:
                cols = cab
            if not cols:
                continue
            x_nome, x_st, x_mot = cols["PlayerName"], cols["CurrentStatus"], cols.get("Reason", 9e9)
            por_linha: dict[int, list] = {}
            for w in ws:
                por_linha.setdefault(round(w["top"]), []).append(w)
            # motivo que quebra em duas linhas fica acima e abaixo da linha do
            # jogador: cada pedaco solto vai pro jogador mais perto
            com_nome = [t for t, lw in por_linha.items()
                        if any(x_nome - 5 <= w["x0"] < x_st - 5 and "," in w["text"] for w in lw)]
            solto: dict[int, list] = {}
            for t, lw in por_linha.items():
                if t in com_nome or not com_nome:
                    continue
                if all(w["x0"] >= x_mot - 5 for w in lw):
                    dono = min(com_nome, key=lambda c: abs(c - t))
                    if abs(dono - t) <= 14:
                        solto.setdefault(dono, []).extend(w["text"] for w in lw)
            for top in sorted(por_linha):
                lw = sorted(por_linha[top], key=lambda w: w["x0"])
                for w in lw:
                    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", w["text"]):
                        atual["data"] = w["text"]
                    elif re.fullmatch(r"\d{2}:\d{2}\(ET\)", w["text"]):
                        atual["hora"] = w["text"][:5]
                    elif re.fullmatch(r"[A-Z]{2,3}@[A-Z]{2,3}", w["text"]):
                        atual["jogo"] = w["text"]
                nome = [w["text"] for w in lw if x_nome - 5 <= w["x0"] < x_st - 5]
                st = [w["text"] for w in lw if x_st - 5 <= w["x0"] < x_mot - 5 and w["text"] in STATUS]
                mot = " ".join([w["text"] for w in lw if w["x0"] >= x_mot - 5] + solto.get(top, []))
                if nome and st and "," in "".join(nome):
                    linhas.append({**atual, "nome": nome_relatorio("".join(nome)), "status": st[0],
                                   "motivo": mot})
    return hora, linhas


def baixar_dia(d: date) -> None:
    arq = PASTA / f"{d.isoformat()}.json"
    if arq.exists():
        return
    novo = d >= date(2025, 12, 15)
    horas = HORAS_NOVO if novo else HORAS_VELHO
    out = {}
    for h in horas:
        try:
            r = requests.get(URL.format(f"{d.isoformat()}_{h}"), timeout=40)
            if r.status_code != 200 and novo:
                r = requests.get(URL.format(f"{d.isoformat()}_{h.replace('_00', '')}"), timeout=40)
            if r.status_code != 200 and not novo:
                r = requests.get(URL.format(f"{d.isoformat()}_{h[:2]}_00{h[2:]}"), timeout=40)
            if r.status_code != 200:
                continue
            hora, linhas = ler_pdf(r.content)
            if linhas:
                out[hora or h] = linhas
        except Exception as e:
            print(f"({d} {h}: {e})")
    arq.write_text(json.dumps(out), encoding="utf-8")


def baixar() -> None:
    PASTA.mkdir(parents=True, exist_ok=True)
    dias = []
    for s, (ini, fim) in TEMPORADAS.items():
        d, f = date.fromisoformat(ini), date.fromisoformat(fim)
        while d <= f:
            dias.append(d)
            d += timedelta(days=1)
    falta = [d for d in dias if not (PASTA / f"{d.isoformat()}.json").exists()]
    print(f"{len(dias)} dias, {len(falta)} pra baixar")
    # ler PDF e CPU pura: processos, nao threads
    with ProcessPoolExecutor(1) as ex:
        list(ex.map(baixar_dia, falta, chunksize=4))


# ==========================================================================
# cruzamento: status no relatorio x jogou
# ==========================================================================
FP_BOX = {"PTS": 0.5, "REB": 1.0, "AST": 1.0, "STL": 2.0, "BLK": 2.0, "TO": -1.0}
EXP = [("calouro", 0, 0), ("2º-3º ano", 1, 2), ("4º-7º ano", 3, 6), ("veterano 8+", 7, 99)]
TIERS = [("estrela 32+", 32, 99), ("titular 26-32", 26, 32), ("rotação 18-26", 18, 26),
         ("banco 10-18", 10, 18), ("terrão <10", 0, 10)]
FASES = [("out-dez", (10, 11, 12)), ("jan-fev", (1, 2)), ("mar-abr", (3, 4))]
CAMPANHA = [("tank <35%", 0, .35), ("35-50%", .35, .5), ("50-65%", .5, .65), ("favorito 65%+", .65, 1.01)]


def _fp_box(st: dict) -> float:
    v = 0.0
    for k, p in FP_BOX.items():
        try:
            v += p * float(st.get(k) or 0)
        except ValueError:
            pass
    tres = (st.get("3PT") or "0-0").split("-")[0]
    return v + 0.5 * float(tres or 0)


def motivo(m: str) -> str:
    m = m.lower()
    if "gleague" in m.replace(" ", "") or "g league" in m:
        return "G League"
    if "rest" in m.split(";")[-1] or "injurymanagement" in m.replace(" ", "") or "load" in m:
        return "descanso/gestão"
    if "personal" in m or "suspension" in m or "notwithteam" in m.replace(" ", ""):
        return "pessoal/suspensão"
    return "lesão/doença"


def base_jogos():
    """Um passe nos boxscores: por data ET, quem estava no elenco de cada
    jogo, quanto jogou, e o perfil (minutagem, b2b, campanha)."""
    from backtest_ecart import estreias
    est = estreias()
    lst = []
    for s in TEMPORADAS:
        for gid, g in jogos(s):
            w = next((p.get("w") for p in g["plays"] if p.get("w")), None)
            if not w:
                continue
            tip = datetime.fromisoformat(w.replace("Z", "+00:00")).astimezone(ET)
            fim = g["plays"][-1]
            lst.append((s, gid, tip, g, (fim.get("h") or 0) - (fim.get("a") or 0)))
    lst.sort(key=lambda x: x[2])
    rec, ultimo_jogo, minutos = {}, {}, {}
    for s, gid, tip, g, marg in lst:
        for t in g["times"]:
            for a in t["at"]:
                try:
                    m = float((dict(zip(t.get("lab") or [], a.get("st") or [])).get("MIN")) or 0)
                except ValueError:
                    m = 0.0
                if m > 0:
                    minutos.setdefault((s, a["id"]), []).append(m)
    por_dia: dict[str, dict] = {}
    for s, gid, tip, g, marg in lst:
        d = tip.date().isoformat()
        for t in g["times"]:
            tid = t["id"]
            v, dd = rec.get((s, tid), (0, 0))
            pct = v / (v + dd) if v + dd >= 10 else None
            camp = next((n for n, lo, hi in CAMPANHA if pct is not None and lo <= pct < hi), "início")
            b2b = ultimo_jogo.get(tid) == (tip.date() - timedelta(days=1))
            for a in t["at"]:
                box = dict(zip(t.get("lab") or [], a.get("st") or []))
                try:
                    m = float(box.get("MIN") or 0)
                except ValueError:
                    m = 0.0
                ms = minutos.get((s, a["id"]), [])
                mm = sum(ms) / len(ms) if len(ms) >= 8 else None
                e = max(0, s - est[a["id"]]) if a["id"] in est else None
                por_dia.setdefault(d, {})[norm(a.get("nome") or "")] = {
                    "season": s, "g": gid, "pid": a["id"], "tip": tip, "min": m, "mm": mm,
                    "fp": _fp_box(box) if m > 0 else 0.0, "b2b": b2b, "camp": camp,
                    "tier": next((n for n, lo, hi in TIERS if mm is not None and lo <= mm < hi), None),
                    "exp": next((n for n, lo, hi in EXP if e is not None and lo <= e <= hi), None),
                    "fase": next((n for n, ms_ in FASES if tip.month in ms_), None),
                    "dnp": a.get("dnp"), "mot_espn": a.get("mot")}
        casa = g["casa"]
        for t in g["times"]:
            ultimo_jogo[t["id"]] = tip.date()
            v, dd = rec.get((s, t["id"]), (0, 0))
            ganhou = (marg > 0) == (t["id"] == casa)
            rec[(s, t["id"])] = (v + ganhou, dd + (not ganhou))
    return por_dia


def cruzar(por_dia) -> list[dict]:
    """Uma linha por jogador-jogo do elenco: status em cada momento (None =
    nao estava no relatorio) e se jogou."""
    out = []
    for d, quem in sorted(por_dia.items()):
        arq = PASTA / f"{d}.json"
        if not arq.exists():
            continue
        rel = json.loads(arq.read_text(encoding="utf-8"))
        if not rel:
            continue
        snaps = sorted(rel)                       # "HH:MM"
        idx = {h: {r["nome"]: r for r in rel[h]} for h in snaps}
        for nome, j in quem.items():
            tip = j["tip"].strftime("%H:%M")
            lim = (j["tip"] - timedelta(minutes=30)).strftime("%H:%M")
            antes = [h for h in snaps if h <= lim]
            def st(h):
                r = idx[h].get(nome) if h else None
                return (r["status"], motivo(r["motivo"])) if r else (None, None)
            def ate(hh):
                hs = [h for h in antes if h <= hh]
                return hs[-1] if hs else None
            ult = antes[-1] if antes else None
            s13, s17, su = st(ate("14:00")), st(ate("18:00")), st(ult)
            out.append({**j, "s13": s13[0], "s17": s17[0], "su": su[0], "mot": su[1] or s17[1] or s13[1],
                        "jogou": j["min"] > 0, "tip_h": tip})
    return out


def _tab(rs, linhas, colunas, chave_l, chave_c, minimo=30):
    print("| | " + " | ".join(map(str, colunas)) + " |\n|---|" + "---|" * len(colunas))
    for l in linhas:
        cel = []
        for c in colunas:
            v = [r for r in rs if chave_l(r) == l and chave_c(r) == c]
            cel.append(f"{100 * sum(r['jogou'] for r in v) / len(v):.0f}% <sub>{len(v)}</sub>"
                       if len(v) >= minimo else "·")
        print(f"| {l} | " + " | ".join(cel) + " |")


def tabelas(rs):
    sts = ["Out", "Doubtful", "Questionable", "Probable", "Available", None]
    nomes = {None: "fora do relatório"}
    print(f"\n{len(rs)} jogador-jogo, {sum(r['su'] is not None for r in rs)} com status no último relatório")
    print("\n### Chance de jogar pelo status em cada momento (todos com minutagem conhecida)")
    v = [r for r in rs if r["tier"]]
    print("| status | 13h | 17h | último antes do jogo |\n|---|---|---|---|")
    for s in sts:
        cel = []
        for k in ("s13", "s17", "su"):
            x = [r for r in v if r[k] == s]
            cel.append(f"{100 * sum(r['jogou'] for r in x) / len(x):.1f}% <sub>{len(x)}</sub>" if x else "·")
        print(f"| {nomes.get(s, s)} | " + " | ".join(cel) + " |")

    sem_fora = [r for r in v if r["tier"] != "terrão <10"]
    print("\n### Último status × motivo (sem o terrão)")
    _tab(sem_fora, sts[1:4], ["lesão/doença", "descanso/gestão", "pessoal/suspensão", "G League"],
         lambda r: r["su"], lambda r: r["mot"])
    for nome, campo, grupos in (("minutagem", "tier", [t for t, *_ in TIERS]),
                                ("experiência", "exp", [e for e, *_ in EXP]),
                                ("fase da temporada", "fase", [f for f, _ in FASES]),
                                ("campanha do time", "camp", ["início"] + [c for c, *_ in CAMPANHA]),
                                ("b2b", "b2b", [False, True])):
        print(f"\n### Último status × {nome} (lesão/doença, sem o terrão)")
        _tab([r for r in (v if campo == "tier" else sem_fora) if r["mot"] in ("lesão/doença", None)],
             sts[1:4] + [None], grupos, lambda r: r["su"], lambda r, c=campo: r[c])
    print("\n### Questionable às 17h: o que vira no último relatório e quanto joga")
    q = [r for r in sem_fora if r["s17"] == "Questionable"]
    print("| último | % dos Q | jogou |\n|---|---|---|")
    for s in sts:
        x = [r for r in q if r["su"] == s]
        if x:
            print(f"| {nomes.get(s, s)} | {100 * len(x) / len(q):.0f}% | "
                  f"{100 * sum(r['jogou'] for r in x) / len(x):.0f}% |")
    print("\n### Quem joga com status: minutos e pontos sobre o normal dele")
    print("| último status | n | minutos | pontos |\n|---|---|---|---|")
    for s in sts:
        x = [r for r in sem_fora if r["su"] == s and r["jogou"] and r["mm"]]
        if len(x) >= 30:
            mins = sum(r["min"] for r in x) / sum(r["mm"] for r in x)
            print(f"| {nomes.get(s, s)} | {len(x)} | {mins:.2f} | · |")
    print("\n### Horário do jogo: Questionable no último relatório, por tip-off (ET)")
    qq = [r for r in sem_fora if r["su"] == "Questionable"]
    for faixa_h, lo, hi in (("até 19:30", "00:00", "19:31"), ("19:31-21:00", "19:31", "21:01"),
                            ("depois das 21h", "21:01", "99")):
        x = [r for r in qq if lo <= r["tip_h"] < hi]
        if x:
            print(f"- {faixa_h}: {100 * sum(r['jogou'] for r in x) / len(x):.0f}% (n={len(x)})")


def oos(rs):
    """Qual corte melhora a chance de jogar fora da amostra (Brier, teste na
    ultima temporada)? Sempre por cima do status do ultimo relatorio."""
    ult = max(r["season"] for r in rs)
    v = [r for r in rs if r["su"] in ("Doubtful", "Questionable", "Probable") and r["tier"]]
    chaves = {
        "só o status": lambda r: (r["su"],),
        "+ motivo": lambda r: (r["su"], r["mot"]),
        "+ minutagem": lambda r: (r["su"], r["tier"]),
        "+ experiência": lambda r: (r["su"], r["exp"]),
        "+ fase": lambda r: (r["su"], r["fase"]),
        "+ campanha": lambda r: (r["su"], r["camp"]),
        "+ b2b": lambda r: (r["su"], r["b2b"]),
        "+ status das 13h": lambda r: (r["su"], r["s13"]),
        "+ motivo + minutagem": lambda r: (r["su"], r["mot"], r["tier"]),
        "+ motivo + campanha + fase": lambda r: (r["su"], r["mot"], r["camp"], r["fase"]),
    }
    bot = {"Doubtful": 0.2, "Questionable": 0.55, "Probable": 0.9}
    teste = [r for r in v if r["season"] == ult]
    b_bot = sum((r["jogou"] - bot[r["su"]]) ** 2 for r in teste) / len(teste)
    print(f"\n### Fora da amostra (ajusta antes de {ult}, testa {ult}; n={len(teste)} D/Q/P)")
    print(f"Brier das chances que o bot usa hoje (D 20%, Q 55%, P 90%): {b_bot:.4f}")
    print("| modelo | Brier | melhora sobre o bot |\n|---|---|---|")
    for nome, ch in chaves.items():
        agg, glob = {}, {}
        for r in v:
            if r["season"] == ult:
                continue
            a = agg.setdefault(ch(r), [0, 0])
            a[0] += r["jogou"]
            a[1] += 1
            gl = glob.setdefault(r["su"], [0, 0])
            gl[0] += r["jogou"]
            gl[1] += 1
        def p(r):
            a = agg.get(ch(r))
            g = glob[r["su"]]
            pg = g[0] / g[1]
            # encolhe pro status quando a celula e pequena
            return (a[0] + 20 * pg) / (a[1] + 20) if a else pg
        b = sum((r["jogou"] - p(r)) ** 2 for r in teste) / len(teste)
        print(f"| {nome} | {b:.4f} | {100 * (1 - b / b_bot):+.1f}% |")


def main():
    rs = cruzar(base_jogos())
    tabelas(rs)
    oos(rs)


if __name__ == "__main__":
    if sys.argv[1:2] == ["baixar"]:
        baixar()
    elif sys.argv[1:2] == ["teste"]:
        d = date.fromisoformat(sys.argv[2])
        PASTA.mkdir(parents=True, exist_ok=True)
        (PASTA / f"{d.isoformat()}.json").unlink(missing_ok=True)
        baixar_dia(d)
        x = json.loads((PASTA / f"{d.isoformat()}.json").read_text(encoding="utf-8"))
        for h, ls in x.items():
            print(h, len(ls), ls[:2])
    else:
        main()
