"""Mede a politica de travar na temporada 2025 da propria liga.

Pra cada time e cada semana de 2025 da liga, pega o elenco real daquela
semana e joga a semana de novo, dia a dia, com tres politicas:

  * bot       — a escalacao do dia e a decisao de travar do `lockin.py`,
                com as previsoes que existiam ANTES da semana (log ate a
                vespera + projecao do Sleeper por jogo, misturados como o
                modelo faz);
  * media     — o que um dono atento faria: escala pelo maior media, trava
                quem passar da propria media, e trava qualquer placar no
                ultimo jogo da semana do jogador;
  * primeiro  — trava o primeiro jogo de cada um.

e compara com o placar REAL que o dono fez naquela semana. Os placares
saem dos jogos reais: so a decisao muda.

    python backtest_lock.py                 # 2025, semanas 1-20
    python backtest_lock.py --weeks 3 10
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from clients import Sleeper
from dados import logs, projecoes_jogo
from lineup import otimizar, starting_slots
from lockin import Opcao, decidir_travas, escalar_dia
from modelo import carregar_params, prever_de_log
from names import posicoes, team_code
from contexte import HERE, load_config
from previsao_jogo import grupo_pos, indices, prever

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--league", default=None,
                    help="liga a rejogar (default: a temporada anterior da liga do config)")
    ap.add_argument("--weeks", nargs=2, type=int, default=[1, 20])
    ap.add_argument("--modelo", choices=["novo", "antigo"], default="novo",
                    help="previsao do jogo: a de previsao_jogo.py (novo) ou 50%% modelo "
                         "+ 50%% Sleeper (antigo)")
    args = ap.parse_args(argv)

    cfg = load_config(HERE / "config.yaml")
    liga = args.league or Sleeper(cfg["sleeper"]["username"], cfg["sleeper"]["season"],
                                  cfg["sleeper"]["league_id"]).league().get("previous_league_id")
    sl = Sleeper(cfg["sleeper"]["username"], args.season, liga)
    regras = sl.scoring_settings()
    slots = starting_slots(sl.roster_positions())
    pool = sl.players(24)
    P = carregar_params()
    L = logs(sl, args.season, regras)
    ant = logs(sl, args.season - 1, regras)
    cal = sl.calendario(args.season)
    datas_semana = defaultdict(set)
    casa, datas_time = {}, defaultdict(set)
    for g in cal:
        datas_semana[g["week"]].add(g["date"])
        if g.get("date"):
            h, a = team_code(g["home"]), team_code(g["away"])
            casa[(g["date"], h)], casa[(g["date"], a)] = True, False
            datas_time[h].add(g["date"]); datas_time[a].add(g["date"])
    datas_time = {t: sorted(v) for t, v in datas_time.items()}
    cal_dia = defaultdict(list)
    for g in cal:
        if g.get("date"):
            cal_dia[g["date"]].append((team_code(g["home"]), team_code(g["away"])))
    proj_passado: dict = {}

    def descanso(time, d):
        ds = datas_time.get(time) or []
        antes = [x for x in ds if x < d]
        if not antes:
            return None
        return (datetime.fromisoformat(d) - datetime.fromisoformat(antes[-1])).days
    jogos_de = {pid: {j.data: j for j in js} for pid, js in L.items()}

    def pos(pid):
        return posicoes(pool.get(pid) or {})

    p_joga = 1 - (P.get("p_falta") or {}).get("jogou_ultimo", 0.077)
    tot = defaultdict(list)
    zeros = defaultdict(int)
    for w in range(args.weeks[0], args.weeks[1] + 1):
        dias = sorted(datas_semana.get(w) or [])
        if not dias:
            continue
        proj = projecoes_jogo(sl, args.season, w, regras)
        temp, defesa = (indices(L, proj_passado, pos, dias[0]) if args.modelo == "novo"
                        else ({}, {}))
        for m in sl.matchups(w):
            elenco = [p for p in (m.get("players") or []) if pos(p)]
            if len(elenco) < 8:
                continue
            # previsao por jogo, com o que se sabia antes da semana
            prev = {}
            for pid in elenco:
                prior = [j for j in L.get(pid, []) if j.data < dias[0]]
                a = ant.get(pid) or []
                pm = pt = None
                if len(prior) < 10 and len(a) >= 10:
                    mins = sum(j.min for j in a)
                    pm, pt = mins / len(a), sum(j.fp for j in a) / mins
                base = prever_de_log(prior, P, "organico", pm, pt)["mu"]
                time = ((pool.get(pid) or {}).get("team") or "")
                ultimo = [j for j in L.get(pid, []) if j.data < dias[0]]
                if ultimo:
                    time = ultimo[-1].team
                for d in dias:
                    sp = proj.get((pid, d))
                    if sp is None:
                        mu = base
                    elif args.modelo == "antigo":
                        mu = P["peso_modelo"] * base + (1 - P["peso_modelo"]) * sp["fp"]
                    else:
                        jogo_opp = None
                        for g in cal_dia.get(d, []):
                            if time in g:
                                jogo_opp = g[1] if g[0] == time else g[0]
                        mu = prever(sp["fp"], base if base > 0 else None, temp.get(pid),
                                    defesa.get((jogo_opp, grupo_pos(pos(pid)))) if jogo_opp else None,
                                    casa.get((d, time)), descanso(time, d))
                    prev[(pid, d)] = mu
            # quem tem jogo em que dia (o time jogou; se ele jogou ou nao e o real)
            joga_no_dia = {}
            for pid in elenco:
                for d in dias:
                    if (pid, d) in proj or d in jogos_de.get(pid, {}):
                        joga_no_dia[(pid, d)] = True

            def opc(pid, d):
                mu = prev[(pid, d)]
                inicio = datetime.fromisoformat(d).replace(tzinfo=timezone.utc) + timedelta(hours=23)
                return Opcao(pid, d, inicio, mu, max(2.0, P["sd_a"] + P["sd_b"] * mu), p_joga)

            def real(pid, d):
                j = jogos_de.get(pid, {}).get(d)
                return j.fp if j else None

            for pol in ("bot", "media", "primeiro"):
                travados: dict[int, float] = {}
                usados: set[str] = set()
                for k, d in enumerate(dias):
                    abertos = [i for i in range(len(slots)) if i not in travados]
                    if not abertos:
                        break
                    livres_jog = [p for p in elenco if p not in usados]
                    hoje = {p: opc(p, d) for p in livres_jog if joga_no_dia.get((p, d))}
                    futuro = {p: [opc(p, d2) for d2 in dias[k + 1:] if joga_no_dia.get((p, d2))]
                              for p in livres_jog}
                    if pol == "bot":
                        esc = escalar_dia(slots, abertos, {}, hoje, futuro, pos, [None] * len(slots))
                        hoje_sl = esc.hoje
                    else:
                        assign, _ = otimizar(slots, list(hoje),
                                             lambda p, i: hoje[p].mu if i in abertos else None, pos)
                        hoje_sl = {i: p for i, p in enumerate(assign) if p and i in abertos}
                    cands = [(i, p, real(p, d)) for i, p in hoje_sl.items() if real(p, d) is not None]
                    if not cands:
                        continue
                    if pol == "bot":
                        decs = decidir_travas(slots, abertos, futuro, pos, cands)
                        trava = [(t.i, t.pid, t.pontos) for t in decs if t.travar]
                    elif pol == "media":
                        trava = [(i, p, x) for i, p, x in cands
                                 if x >= hoje[p].mu or not futuro.get(p)]
                    else:
                        trava = cands
                    for i, p, x in trava:
                        travados[i] = x
                        usados.add(p)
                pts = sum(travados.values())
                tot[pol].append(pts)
                zeros[pol] += len(slots) - len(travados)
            tot["real (dono)"].append(m.get("points") or 0.0)
        proj_passado.update(proj)
        print(f"semana {w}: " + "  ".join(f"{k} {sum(v)/len(v):.1f}" for k, v in tot.items()),
              flush=True)

    n = len(tot["bot"])
    print(f"\n{n} placares semanais (time x semana), {args.season}")
    print(f"{'politica':14} {'media/semana':>13} {'slots zerados/sem':>18}")
    for k, v in tot.items():
        z = f"{zeros[k] / n:.2f}" if k in zeros else "—"
        print(f"{k:14} {sum(v) / len(v):>13.1f} {z:>18}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
