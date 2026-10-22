"""A partir de quanto o add (e o claim) compensa? Rejoga 2024 e 2025 da liga.

O bot sugere um add quando o ganho previsto passa de `waivers.ganho_minimo`
(2 pts/semana) e um claim acima de `ganho_minimo_claim` (3). Os dois eram
chute. Aqui, pra cada time e cada semana da liga:

  * o elenco real daquela semana e os livres (quem nao estava em elenco
    nenhum);
  * a previsao do bot pra cada um com o que se sabia ANTES da semana (o
    horizonte de 6 semanas do previsao_waiver);
  * o melhor add contra o pior do elenco, e o ganho previsto;
  * o que os dois fizeram de verdade nos 20 jogos seguintes do time.

Com isso: o ganho previsto se confirma? A partir de quanto o realizado e
positivo com folga? E nas transacoes reais da liga, quantos claims foram
disputados (a prioridade decidiu)?

    python backtest_seuil.py
"""

from __future__ import annotations

import sys
from collections import defaultdict

from clients import Sleeper
from contexte import HERE, load_config
from dados import logs, projecoes_jogo, taxa_carga
from lockin import valor_semanal
from modelo import carregar_params, prever_de_log
from names import posicoes
from previsao_waiver import MIN_JOGOS, TETO, prever as prever_horizonte

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LIGAS = {2024: "1152009771571306496", 2025: "1284170719487352832"}
HORIZONTE = 20          # jogos do time
P_CARGA = taxa_carga(None)
JOGOS_SEMANA = 3.5


def _media(xs):
    return sum(xs) / len(xs) if xs else 0.0


def semanal(h: float, P: dict) -> float:
    """Pts por jogo do time -> pts por semana num slot do Lock-In."""
    mu = h / P_CARGA
    sd = max(2.0, P["sd_a"] + P["sd_b"] * mu)
    return valor_semanal(mu, sd, P_CARGA, JOGOS_SEMANA)


def temporada(season: int, cfg) -> list[dict]:
    sl = Sleeper(cfg["sleeper"]["username"], season, LIGAS[season])
    regras = sl.scoring_settings()
    pool = sl.players(24)
    P = carregar_params()
    L = logs(sl, season, regras)
    ant = logs(sl, season - 1, regras)
    cal = sl.calendario(season)
    datas_semana, datas_time = defaultdict(set), defaultdict(set)
    from names import team_code
    for g in cal:
        if g.get("date") and g.get("week"):
            datas_semana[g["week"]].add(g["date"])
            datas_time[team_code(g["home"])].add(g["date"])
            datas_time[team_code(g["away"])].add(g["date"])
    datas_time = {t: sorted(v) for t, v in datas_time.items()}
    semanas = sorted(datas_semana)
    out = []
    for w in semanas:
        dias = sorted(datas_semana[w])
        d0 = dias[0]
        futuro = [x for x in semanas if x > w]
        if len(futuro) < 6:
            break
        try:
            ms = sl.matchups(w)
        except Exception:
            continue
        elencos = {m["roster_id"]: [p for p in (m.get("players") or []) if posicoes(pool.get(p) or {})]
                   for m in ms}
        if not elencos:
            continue
        no_elenco = {p for v in elencos.values() for p in v}
        proj = projecoes_jogo(sl, season, w, regras)

        def prev(pid):
            js = [j for j in L.get(pid, []) if j.data < d0]
            if not js:
                return None
            time = js[-1].team
            ds = [d for d in datas_time.get(time, []) if d < d0]
            no_time = [j for j in js if j.team == time]
            a = ant.get(pid) or []
            mu = prever_de_log(js, P, "organico")["mu"]
            if len(no_time) < MIN_JOGOS or not ds:
                return {"h": mu * P_CARGA, "time": time, "faixa": False}
            jogou = {j.data for j in no_time}
            ult10 = ds[-10:]
            pres10 = sum(d in jogou for d in ult10) / len(ult10)
            desde = [d for d in ds if d >= no_time[0].data]
            pres = min(1.0, len(no_time) / max(1, len(desde)))
            fp = [j.fp for j in js]
            S = _media(fp)
            prox = next((d for d in datas_time.get(time, []) if d >= d0), None)
            sp = proj.get((pid, prox)) if prox else None
            if S * pres10 > TETO:
                return {"h": mu * pres10, "time": time, "faixa": False}
            h = prever_horizonte(S, _media(fp[-5:]), _media(fp[-10:]), mu, pres10, pres,
                                 sp["fp"] if sp else None,
                                 _media([j.fp for j in a]) if len(a) >= 10 else None)
            return {"h": h, "time": time, "faixa": True}

        def real(pid, time):
            ds = [d for d in datas_time.get(time, []) if d >= d0][:HORIZONTE]
            if len(ds) < 10:
                return None
            fps = {j.data: j.fp for j in L.get(pid, [])}
            return sum(fps.get(d, 0.0) for d in ds) / len(ds)

        # livres: jogou na temporada ate aqui e nao esta em elenco nenhum
        livres = [p for p, js in L.items() if p not in no_elenco
                  and any(j.data < d0 for j in js) and posicoes(pool.get(p) or {})]
        pl = {p: prev(p) for p in livres}
        pl = {p: v for p, v in pl.items() if v}
        top = sorted(pl, key=lambda p: -pl[p]["h"])[:15]
        for rid, el in elencos.items():
            pe = {p: prev(p) for p in el}
            pe = {p: v for p, v in pe.items() if v}
            if len(pe) < 8:
                continue
            # Pares: cada um dos 15 melhores livres contra os 3 piores do
            # elenco que estao jogando (5+ jogos no time, faixa do modelo).
            # Sem o machucado guardado no fim do banco: ele tem previsao baixa
            # e volta forte, e ninguem o dispensaria.
            jogando = sorted((p for p in pe if pe[p]["faixa"]), key=lambda p: pe[p]["h"])[:3]
            for drop in jogando:
                rd = real(drop, pe[drop]["time"])
                if rd is None:
                    continue
                for add in top:
                    if not pl[add]["faixa"]:
                        continue
                    ra = real(add, pl[add]["time"])
                    if ra is None:
                        continue
                    out.append({"season": season, "w": w, "rid": rid, "add": add, "drop": drop,
                                "prev": pl[add]["h"] - pe[drop]["h"], "real": ra - rd,
                                "prev_sem": semanal(pl[add]["h"], P) - semanal(pe[drop]["h"], P),
                                "real_sem": semanal(ra, P) - semanal(rd, P), "faixa": True,
                                "pa": semanal(pl[add]["h"], P), "ra": semanal(ra, P),
                                "pd": semanal(pe[drop]["h"], P), "rd": semanal(rd, P),
                                "melhor": add == top[0] and drop == jogando[0]})
    return out


def disputas(season: int, cfg) -> None:
    """Claims reais da liga: quantos foram disputados (perdidos pela fila)."""
    sl = Sleeper(cfg["sleeper"]["username"], season, LIGAS[season])
    ok = falhou = 0
    por_jogador = defaultdict(int)
    for w in range(1, 26):
        try:
            for t in sl.transactions(w):
                if t.get("type") != "waiver":
                    continue
                if t.get("status") == "complete":
                    ok += 1
                elif t.get("status") == "failed":
                    falhou += 1
                for p in (t.get("adds") or {}):
                    por_jogador[(w, p)] += 1
        except Exception:
            continue
    disput = sum(1 for v in por_jogador.values() if v > 1)
    print(f"- {season}: {ok} claims completos, {falhou} falhos; "
          f"{disput} jogadores com mais de um claim na mesma semana")


def main():
    cfg = load_config(HERE / "config.yaml")
    rs = []
    for s in LIGAS:
        rs += temporada(s, cfg)
    print(f"\n{len(rs)} pares (time × semana × livre × um dos 3 piores do elenco)")
    for nome, filtro in (("todos os pares", lambda r: True),
                         ("2024", lambda r: r["season"] == 2024), ("2025", lambda r: r["season"] == 2025)):
        v = [r for r in rs if filtro(r)]
        print(f"\n### Ganho previsto x realizado ({nome}) — pts por semana num slot do Lock-In")
        print("| previsto | n | previsto médio | realizado médio | IC 95% | % realizado > 0 |")
        print("|---|---|---|---|---|---|")
        for lo, hi in ((-99, -2), (-2, 0), (0, 1), (1, 2), (2, 3), (3, 4), (4, 6), (6, 99)):
            x = [r for r in v if lo <= r["prev_sem"] < hi]
            if len(x) < 40:
                continue
            m = _media([r["real_sem"] for r in x])
            sd = (sum((r["real_sem"] - m) ** 2 for r in x) / (len(x) - 1)) ** 0.5
            ic = 1.96 * sd / len(x) ** 0.5
            rot = f"< {hi}" if lo == -99 else f"{lo}+" if hi == 99 else f"{lo}-{hi}"
            print(f"| {rot} | {len(x)} | {_media([r['prev_sem'] for r in x]):+.1f} | {m:+.1f} "
                  f"| {m - ic:+.1f} a {m + ic:+.1f} | {100 * sum(r['real_sem'] > 0 for r in x) / len(x):.0f}% |")
        # inclinacao: quanto do ganho previsto se realiza
        n = len(v)
        mx, my = _media([r["prev_sem"] for r in v]), _media([r["real_sem"] for r in v])
        b = (sum((r["prev_sem"] - mx) * (r["real_sem"] - my) for r in v)
             / max(sum((r["prev_sem"] - mx) ** 2 for r in v), 1e-9))
        print(f"\nRealizado ≈ {my - b * mx:+.2f} + {b:.2f} × previsto (n={n})")
        for corte in (0, 1, 2, 3, 4):
            x = [r for r in v if r["prev_sem"] >= corte]
            if x:
                print(f"- corte {corte}: {len(x)} adds, realizado médio {_media([r['real_sem'] for r in x]):+.2f}"
                      f" pts/semana, {100 * sum(r['real_sem'] > 0 for r in x) / len(x):.0f}% deram certo")
    # De que lado esta o erro? O livre escolhido entre 15 tende a ser
    # superestimado (o melhor de muitos e o que teve sorte); o pior do
    # elenco, subestimado. Por lado, sem repetir o mesmo jogador na semana.
    print("\n### Por lado: previsto x realizado, pts por semana num slot")
    for lado, kp, kr, chave in (("livre (entra)", "pa", "ra", "add"), ("do elenco (sai)", "pd", "rd", "drop")):
        vistos = {}
        for r in rs:
            vistos[(r["season"], r["w"], r["rid"] if chave == "drop" else 0, r[chave])] = r
        v = list(vistos.values())
        mx, my = _media([r[kp] for r in v]), _media([r[kr] for r in v])
        b = (sum((r[kp] - mx) * (r[kr] - my) for r in v)
             / max(sum((r[kp] - mx) ** 2 for r in v), 1e-9))
        print(f"- {lado}: n={len(v)}, previsto {mx:.2f}, realizado {my:.2f} "
              f"(erro {my - mx:+.2f}); realizado ≈ {my - b * mx:+.2f} + {b:.2f} × previsto")
    print("\n### Claims de verdade na liga")
    for s in LIGAS:
        disputas(s, cfg)


if __name__ == "__main__":
    main()
