"""Pesquisa: quem ganha minutos MANTEM? E o que prever o proximo jogo.

E a versao NBA do estudo de snaps da NFL. Quatro perguntas, medidas em
2021-2025 (todo jogo de temporada regular, ~143 mil linhas):

  1. Persistencia de minutos. Se nos ultimos 5 jogos o cara jogou D minutos
     a mais que a base dele, quanto desse D sobrevive nos 10 jogos seguintes?
     E a resposta muda conforme a CAUSA:
       - organico: ninguem da rotacao do time estava fora;
       - lesao_segue: o ganho veio com titular fora, e ele CONTINUA fora;
       - lesao_volta: o ganho veio com titular fora, e ele VOLTOU.
     (O relatorio de lesao diz isso antes do jogo, entao no uso real a
     classe e conhecida na hora da decisao.)
  2. Persistencia de eficiencia (pontos de fantasy por minuto).
  3. Qual previsor acerta mais o PROXIMO jogo: media da temporada, ultimos
     5, media exponencial, o modelo de minutos x eficiencia, a projecao do
     Sleeper — e a mistura.
  4. O tamanho do erro (desvio por jogo), que e o que move a decisao de
     travar, e a chance de nao jogar.

Grava o resultado em `momentum.json`, que o `modelo.py` le. Rode de novo
quando quiser recalibrar (a temporada corrente entra junto):

    python backtest_momentum.py
    python backtest_momentum.py --seasons 2023 2024 2025
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from clients import Sleeper
from dados import logs
from contexte import HERE, load_config

JANELA = 5        # "ultimos jogos"
FUTURO = 10       # horizonte de persistencia
MIN_PRIOR = 10    # jogos antes da decisao pra ter base
ROTACAO = 20.0    # media de minutos pra contar como rotacao
LIMIAR_VAGO = 8.0  # minutos vagos por jogo pra chamar o ganho de "lesao"


def _media(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _beta(ds, fs):
    """Inclinacao pela origem de (F-B) contra D: fracao do ganho que fica."""
    num = sum(d * f for d, f in zip(ds, fs))
    den = sum(d * d for d in ds)
    return num / den if den else float("nan")


def minutos_vagos(L: dict) -> dict[tuple[str, str], float]:
    """{(time, data): minutos/jogo da rotacao que NAO jogou naquele dia}.

    Rotacao = media >= 20 min ate a vespera, com 5+ jogos. Jogador conta
    como do time ate o ULTIMO jogo dele por aquele time: sem isso, todo
    trocado viraria "lesionado" no time antigo pelo resto do ano.
    """
    por_time_data: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    ultimo: dict[tuple[str, str], str] = {}
    for pid, js in L.items():
        for j in js:
            por_time_data[j.team][j.data].add(pid)
            ultimo[(pid, j.team)] = max(ultimo.get((pid, j.team), ""), j.data)
    jogos_de = {pid: {(j.team, j.data): j.min for j in js} for pid, js in L.items()}

    out = {}
    for time, datas in por_time_data.items():
        acum: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for d in sorted(datas):
            vago = 0.0
            for pid, (soma, n) in acum.items():
                if n >= 5 and soma / n >= ROTACAO and pid not in datas[d] \
                        and d <= ultimo.get((pid, time), ""):
                    vago += soma / n
            out[(time, d)] = vago
            for pid in datas[d]:
                a = acum[pid]
                a[0] += jogos_de[pid][(time, d)]
                a[1] += 1
    return out


def amostras(L: dict, vagos: dict) -> list[dict]:
    """Um registro por (jogador, jogo) com o que se sabia ANTES do jogo."""
    out = []
    for pid, js in L.items():
        for i in range(MIN_PRIOR, len(js) - 1):
            prior, fut = js[:i], js[i:i + FUTURO]
            if len(fut) < 5:
                continue
            base, rec = prior[:-JANELA], prior[-JANELA:]
            if prior[-1].team != js[i].team:      # trocado: base de outro time
                continue
            B = _media([j.min for j in base])
            R = _media([j.min for j in rec])
            F = _media([j.min for j in fut])
            Bm = sum(j.min for j in base)
            Br = sum(j.fp for j in base) / Bm if Bm else 0.0
            Rm = sum(j.min for j in rec)
            Rr = sum(j.fp for j in rec) / Rm if Rm else 0.0
            Fm = sum(j.min for j in fut)
            Fr = sum(j.fp for j in fut) / Fm if Fm else 0.0
            # A causa e a VARIACAO de minutos vagos, nao o nivel: quase todo
            # time tem alguem da rotacao fora em quase todo jogo (na primeira
            # medicao, com nivel absoluto, 72% dos casos viravam "lesao").
            # O que explica o ganho dele e o que ficou vago A MAIS na janela
            # recente em relacao a base dele.
            v_base = _media([vagos.get((j.team, j.data), 0.0) for j in base])
            v_rec = _media([vagos.get((j.team, j.data), 0.0) for j in rec]) - v_base
            v_agora = vagos.get((js[i].team, js[i].data), 0.0) - v_base
            if v_rec < LIMIAR_VAGO:
                causa = "organico"
            elif v_agora >= 0.6 * v_rec:
                causa = "lesao_segue"
            else:
                causa = "lesao_volta"
            out.append({
                "pid": pid, "data": js[i].data, "B": B, "R": R, "F": F,
                "F3": _media([j.min for j in fut[:3]]),
                "Br": Br, "Rr": Rr, "Fr": Fr, "causa": causa,
                "prior": prior, "prox": js[i],
            })
    return out


def persistencia(A: list[dict]) -> dict:
    res = {}
    for causa in ("organico", "lesao_segue", "lesao_volta"):
        sub = [a for a in A if a["causa"] == causa]
        ds = [a["R"] - a["B"] for a in sub]
        res[causa] = {
            "n": len(sub),
            "beta_10": _beta(ds, [a["F"] - a["B"] for a in sub]),
            "beta_3": _beta(ds, [a["F3"] - a["B"] for a in sub]),
            # o que interessa de verdade: quem SUBIU forte
            "subiu6_n": sum(1 for d in ds if d >= 6),
            "subiu6_fica": _media([(a["F"] - a["B"]) / (a["R"] - a["B"])
                                   for a in sub if a["R"] - a["B"] >= 6]),
            "caiu6_fica": _media([(a["F"] - a["B"]) / (a["R"] - a["B"])
                                  for a in sub if a["R"] - a["B"] <= -6]),
        }
    ds = [a["Rr"] - a["Br"] for a in A]
    res["eficiencia"] = {"beta_10": _beta(ds, [a["Fr"] - a["Br"] for a in A])}
    return res


def _ewma(xs, meia_vida):
    a = 1 - 0.5 ** (1 / meia_vida)
    m = xs[0]
    for x in xs[1:]:
        m = a * x + (1 - a) * m
    return m


def previsores(A: list[dict], P: dict, proj: dict | None) -> dict:
    """MAE de cada previsor no proximo jogo. `P` = parametros do modelo."""
    from modelo import prever_de_log
    erros = defaultdict(list)
    pares = []    # (modelo, sleeper, real) pra calibrar a mistura
    for a in A:
        real = a["prox"].fp
        fps = [j.fp for j in a["prior"]]
        erros["media_temporada"].append(abs(_media(fps) - real))
        erros["ultimos_5"].append(abs(_media(fps[-5:]) - real))
        for h in (5, 10, 20):
            erros[f"exponencial_{h}"].append(abs(_ewma(fps, h) - real))
        mu = prever_de_log(a["prior"], P, causa=a["causa"])["mu"]
        erros["modelo"].append(abs(mu - real))
        if proj is not None:
            sp = proj.get((a["pid"], a["data"]))
            if sp is not None:
                pares.append((mu, sp, real))
    out = {k: round(_media(v), 3) for k, v in erros.items()}
    if pares:
        out["n_com_sleeper"] = len(pares)
        out["modelo_mesmos_jogos"] = round(_media([abs(m - r) for m, _, r in pares]), 3)
        out["sleeper"] = round(_media([abs(s - r) for _, s, r in pares]), 3)
        melhor = min((round(w / 20, 2) for w in range(21)),
                     key=lambda w: _media([abs(w * m + (1 - w) * s - r)
                                           for m, s, r in pares]))
        out["peso_modelo"] = melhor
        out["mistura"] = round(_media([abs(melhor * m + (1 - melhor) * s - r)
                                       for m, s, r in pares]), 3)
    return out


def dispersao(A: list[dict], P: dict) -> dict:
    """Desvio do erro por faixa de media prevista -> sd = a + b*mu."""
    from modelo import prever_de_log
    faixas = defaultdict(list)
    for a in A:
        mu = prever_de_log(a["prior"], P, causa=a["causa"])["mu"]
        faixas[int(mu // 5) * 5].append(a["prox"].fp - mu)
    pts = [(k + 2.5, math.sqrt(_media([e * e for e in v])), len(v))
           for k, v in sorted(faixas.items()) if len(v) >= 200]
    n = sum(w for _, _, w in pts)
    mx = sum(x * w for x, _, w in pts) / n
    my = sum(y * w for _, y, w in pts) / n
    b = (sum(w * (x - mx) * (y - my) for x, y, w in pts)
         / sum(w * (x - mx) ** 2 for x, _, w in pts))
    return {"a": round(my - b * mx, 3), "b": round(b, 4),
            "faixas": [(x, round(y, 2), w) for x, y, w in pts]}


def ausencia(L: dict) -> dict:
    """Chance de NAO jogar o proximo jogo do time, pra quem e rotacao.

    Separada por "jogou o ultimo" vs "faltou o ultimo" (lesao tende a durar)
    e pela segunda noite de back-to-back (descanso de veterano).
    """
    from datetime import date
    datas_time: dict[str, list[str]] = defaultdict(set)
    for js in L.values():
        for j in js:
            datas_time[j.team].add(j.data)
    datas_time = {t: sorted(v) for t, v in datas_time.items()}
    cont = defaultdict(lambda: [0, 0])
    for pid, js in L.items():
        if len(js) < 20 or _media([j.min for j in js]) < ROTACAO:
            continue
        por_time = defaultdict(set)
        for j in js:
            por_time[j.team].add(j.data)
        for t, jogou in por_time.items():
            ds = [d for d in datas_time[t] if min(jogou) <= d <= max(jogou)]
            for k in range(1, len(ds)):
                antes = "jogou_ultimo" if ds[k - 1] in jogou else "faltou_ultimo"
                b2b = (date.fromisoformat(ds[k]) - date.fromisoformat(ds[k - 1])).days == 1
                for chave in (antes, f"{antes}_b2b" if b2b else None):
                    if chave:
                        cont[chave][0] += ds[k] not in jogou
                        cont[chave][1] += 1
    return {k: {"p_falta": round(f / n, 4), "n": n} for k, (f, n) in sorted(cont.items())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seasons", nargs="+", type=int,
                    default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--out", default=str(HERE / "momentum.json"))
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    sl = Sleeper(cfg["sleeper"]["username"], cfg["sleeper"]["season"],
                 cfg["sleeper"].get("league_id", ""))
    regras = sl.scoring_settings()

    from modelo import PARAMS_PADRAO
    todas: list[dict] = []
    proj_2025 = None
    L_todos = {}
    for s in args.seasons:
        L = logs(sl, s, regras)
        L_todos[s] = L
        A = amostras(L, minutos_vagos(L))
        for a in A:
            a["season"] = s
        todas.extend(A)
        print(f"{s}: {len(L)} jogadores, {len(A)} decisoes")

    # 1-2: persistencia (por temporada e junto, pra ver se e estavel)
    pers = persistencia(todas)
    por_ano = {s: persistencia([a for a in todas if a["season"] == s])
               for s in args.seasons}

    P = dict(PARAMS_PADRAO)
    P["beta_min"] = {c: round(pers[c]["beta_10"], 3)
                     for c in ("organico", "lesao_segue", "lesao_volta")}
    P["beta_eficiencia"] = round(pers["eficiencia"]["beta_10"], 3)

    # 3: previsores. A projecao do Sleeper so existe jogo a jogo em 2025.
    ult = max(args.seasons)
    proj = {}
    from dados import projecoes_jogo
    for w in range(1, 27):
        try:
            for (pid, d), v in projecoes_jogo(sl, ult, w, regras).items():
                proj[(pid, d)] = v["fp"]
        except Exception:
            break
    teste = [a for a in todas if a["season"] == ult]
    prev = previsores(teste, P, proj or None)
    if "peso_modelo" in prev:
        P["peso_modelo"] = prev["peso_modelo"]

    # 4: dispersao e ausencia
    disp = dispersao(teste, P)
    P["sd_a"], P["sd_b"] = disp["a"], disp["b"]
    aus = ausencia(L_todos[ult])
    P["p_falta"] = {k: v["p_falta"] for k, v in aus.items()}

    rel = {"persistencia": pers, "por_ano": por_ano, "previsores": prev,
           "dispersao": disp, "ausencia": aus, "params": P,
           "seasons": args.seasons}
    Path(args.out).write_text(json.dumps(rel, indent=1, default=str), encoding="utf-8")

    def f(x):
        return "—" if x != x else f"{x:+.2f}" if isinstance(x, float) else str(x)
    print("\nPERSISTENCIA DE MINUTOS (fracao do ganho recente que sobrevive)")
    print(f"{'causa':14} {'n':>7} {'10 jogos':>9} {'3 jogos':>8} "
          f"{'subiu 6+ (n)':>14} {'fica':>6} {'caiu 6+ fica':>13}")
    for c in ("organico", "lesao_segue", "lesao_volta"):
        r = pers[c]
        print(f"{c:14} {r['n']:>7} {f(r['beta_10']):>9} {f(r['beta_3']):>8} "
              f"{r['subiu6_n']:>14} {f(r['subiu6_fica']):>6} {f(r['caiu6_fica']):>13}")
    print(f"eficiencia (FP/min): beta_10 = {f(pers['eficiencia']['beta_10'])}")
    print("\npor temporada (organico / lesao_segue / lesao_volta, 10 jogos):")
    for s, r in por_ano.items():
        print(f"  {s}: " + " / ".join(f(r[c]['beta_10']) for c in
                                      ("organico", "lesao_segue", "lesao_volta")))
    print(f"\nPROXIMO JOGO, erro absoluto medio em pontos ({ult}):")
    for k, v in prev.items():
        print(f"  {k:22} {v}")
    print(f"\nDESVIO POR JOGO: sd = {disp['a']} + {disp['b']} * media")
    for x, y, w in disp["faixas"]:
        print(f"  media ~{x:4.1f}: sd {y:5.2f}  (n={w})")
    print("\nCHANCE DE NAO JOGAR o proximo jogo (rotacao, 20+ min):")
    for k, v in aus.items():
        print(f"  {k:22} {v['p_falta']:.1%}  (n={v['n']})")
    print(f"\ngravado em {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
