"""Valor de temporada: quanto um jogador rende por SEMANA no Lock-In.

No formato normal de NBA, valor e media x jogos. No Lock-In nao: so um jogo
por semana conta, e voce escolhe qual travar. Entao o que um jogador vale e
o valor de uma parada otima sobre os ~3,4 jogos da semana dele — que e a
media MAIS um premio por variancia. O cara de 20 de media e desvio 9 vale
mais que o de 20 e desvio 5, porque a semana dele tem mais chance de trazer
um jogo de 30 pra travar. E o que a RotoWire chama de "upside de bench
scorer" no Lock-In, so que medido.

Duas consequencias pro draft:
  * disponibilidade pesa menos: quem joga 2 de 4 jogos ainda tem um jogo pra
    travar. O que derruba valor e semana INTEIRA fora;
  * pivo e escasso. O slot C so aceita C, e o replacement de C sai mais baixo
    — o VOR do pivo sobe sozinho.

VOR = valor semanal - valor semanal do replacement da posicao. Replacement
e o melhor jogador que sobra depois que os 12 times preenchem os 8 slots
titulares (do mais restrito pro mais flexivel) e o banco.
"""

from __future__ import annotations

import math

from lineup import cabe, flexibilidade, starting_slots
from dados import taxa_carga
from lockin import valor_semanal
from names import POSICOES

JOGOS_POR_SEMANA = 3.4


def jogos_por_semana(ctx) -> float:
    """Media de jogos por time por semana na temporada da liga."""
    try:
        jogos = ctx.agenda.jogos
        semanas = {j.week for j in jogos}
        return 2 * len(jogos) / (30 * len(semanas)) if semanas else JOGOS_POR_SEMANA
    except Exception:
        return JOGOS_POR_SEMANA


# Saude recalibrada em 2020-2025 em quem CHEGA SAUDAVEL a temporada (todo
# jogador projetado 18+ pelo Sleeper nas semanas 1-2, sem filtro de jogos
# feitos depois; 570 casos): N+1 = 0,795 + 0,079 x N, a fracao de semanas
# jogadas no ano anterior. A formula antiga (0,705 + 0,14 x N, medida so em
# quem fez 22+ pts) errava ~10% mais nos 3 cortes treino/teste: punia lesao
# passada demais. Quem jogou tudo fica em ~87%, quem jogou metade em ~83%.
# Uma marca extra pra "lesao isolada" nao somou nada fora da amostra. O
# historico de lesao custa pouco no Lock-In; lesao ATUAL custa (ESPN).
SAUDE_BASE, SAUDE_PESO = 0.795, 0.079
SAUDE_SEM_HISTORICO = 0.83     # novato: sem ano anterior
# Quem nao jogou NADA no ano anterior: os 4 casos medidos (Kawhi, Bridges,
# Simmons, Zion) jogaram 70% no ano da volta.
SAUDE_ANO_PERDIDO = 0.70
# Out sem prazo de volta na ESPN: chute declarado de 1/4 da temporada fora.
# Com prazo (quase sempre), conta as semanas que acabam antes dele.
FATOR_OUT = 0.75


# A projecao de temporada do Sleeper, medida contra o que aconteceu em
# 2020-2025 (projecao das semanas 1-2, feita antes dos jogos; 1.310 casos de
# 10+ pts/jogo projetados; intervalos de 95% por bootstrap). Novato e 2o ano
# so erram num pedaco da fila — e nao no que importa no draft:
#   * novato projetado baixo (10-14) faz 1,13x [1,04-1,22]; o projetado alto
#     (14+, as escolhas de cima) faz 1,00-1,02, sem vies;
#   * 2o ano projetado alto (18+) faz 0,94x [0,88-0,99]: o salto do 2o ano
#     de quem ja era bom vem menor; abaixo disso, sem vies;
#   * 4-9 anos 0,98x [0,97-0,99], 10+ 0,97x [0,95-0,99].
NOVATO_BAIXO, NOVATO_CORTE = 1.132, 14.0
SEGUNDO_ALTO, SEGUNDO_CORTE = 0.937, 18.0
VETERANO = ((8, 0.981), (99, 0.968))
# Do 4o ano em diante, 35% da media do ano anterior na projecao cortou o erro
# em 0,14 pts/jogo [0,09-0,20] fora da amostra (melhor em 5 das 6 temporadas).
# Nao e que quem foi mal repete (o erro contra a projecao num ano nao preve o
# do seguinte: correlacao -0,02): e que a MUDANCA que o Sleeper projeta em
# relacao ao ano anterior so acontece em ~65%. So com 20+ jogos no ano.
PESO_ANO_ANTERIOR = 0.35
ANOS_PRA_MISTURA = 3
# Depois dessas correcoes, a grade (experiencia x faixa x posicao x troca de
# time x minutos, 128 celulas, Benjamini-Hochberg a 10%) achou dois vieses
# que sobram, e os dois melhoraram o erro nos 3 cortes treino/teste:
#   * quem TROCOU DE TIME faz 0,955x [0,930-0,979] da previsao (n=226);
#   * PIVO projetado 18-24 faz 0,955x [0,928-0,981] (n=106).
FATOR_TIME_NOVO = 0.955
FATOR_PIVO_MEIO, PIVO_FAIXA = 0.955, (18.0, 24.0)
# E o ritmo com que ele TERMINOU o ano anterior (minutos nos ultimos 15
# jogos menos o resto, `tendencia_fim`): caiu 4+ min faz 0,935x [0,906-0,964]
# da previsao (n=93), subiu 4+ faz 1,034x [1,008-1,061] (n=189), o resto
# 1,010x. Melhorou o erro nos 3 cortes treino/teste (-0,02 a -0,03 pts/jogo),
# o achado mais forte da pesquisa. Idade, premio individual e time campeao
# foram testados e descartados (nao melhoram fora da amostra, ou n=6).
FATOR_TENDENCIA = {"cai": 0.935, "estavel": 1.010, "sobe": 1.034}
LIMIAR_TENDENCIA = 4.0


# A oscilacao jogo a jogo de cada jogador, alem do que a media explica, se
# repete de um ano pro outro: correlacao 0,21 [0,14-0,29] em 788 pares.
# Usar 20% dela (o peso medido ficou entre 0,17 e 0,26) melhorou a previsao
# do desvio em 2 dos 3 cortes treino/teste. Efeito pequeno, mas no Lock-In
# oscilar paga. So com 25+ jogos no ano anterior.
PESO_OSCILACAO_PROPRIA = 0.20


def sd_proprio(sd_media: float, ant: list, sd_de) -> float:
    """Desvio por jogo: o da media (modelo), puxado 20% pro do proprio
    jogador no ano anterior."""
    fp = [j.fp for j in ant if _regular(j.data)]
    if len(fp) < 25:
        return sd_media
    m = sum(fp) / len(fp)
    sd_ant = (sum((x - m) ** 2 for x in fp) / len(fp)) ** 0.5
    razao = sd_ant / sd_de(m)
    return sd_media * (1 + PESO_OSCILACAO_PROPRIA * (razao - 1))


def projecao_ajustada(proj: float, anos: int | None, ant: list,
                      time_novo: bool = False, pivo: bool = False,
                      tend: float | None = None) -> float:
    """Projecao do Sleeper corrigida pelos vieses medidos (anos de liga,
    time novo, pivo de faixa media, ritmo do fim do ano anterior)."""
    f = 1.0
    if tend is not None:
        f *= FATOR_TENDENCIA["sobe" if tend >= LIMIAR_TENDENCIA
                             else "cai" if tend <= -LIMIAR_TENDENCIA else "estavel"]
    if time_novo:
        f *= FATOR_TIME_NOVO
    if pivo and PIVO_FAIXA[0] <= proj < PIVO_FAIXA[1]:
        f *= FATOR_PIVO_MEIO
    return f * _por_experiencia(proj, anos, ant)


def _por_experiencia(proj: float, anos: int | None, ant: list) -> float:
    if anos is None:
        return proj
    if anos == 0:
        return proj * (NOVATO_BAIXO if proj < NOVATO_CORTE else 1.0)
    if anos == 1:
        return proj * (SEGUNDO_ALTO if proj >= SEGUNDO_CORTE else 1.0)
    if anos < ANOS_PRA_MISTURA:
        return proj
    mu = proj * next(f for ate, f in VETERANO if anos <= ate)
    regs = [j.fp for j in ant if _regular(j.data)]
    if len(regs) >= 20:
        mu = (1 - PESO_ANO_ANTERIOR) * mu + PESO_ANO_ANTERIOR * sum(regs) / len(regs)
    return mu


# O fiasco se corta, o estouro fica: quem rende abaixo do replacement vai pra
# waiver e e trocado por um livre desse nivel. Entao o valor de um jogador
# nao e E[valor] - replacement, e E[max(valor, replacement)] - replacement.
# A dispersao do ano em volta da previsao corrigida (2020-2025) depende da
# FAIXA de projecao muito mais que da experiencia: projetado 10-14 desvia
# 23%, 14-18 18%, 18-24 14%, 24+ 11%. Novato e 2o ano oscilam um pouco mais
# dentro da faixa (+2 pontos percentuais).
DISPERSAO_FAIXA = ((14.0, 0.233), (18.0, 0.177), (24.0, 0.141), (999.0, 0.109))
DISPERSAO_JOVEM = 0.02


def dispersao(mu: float, anos: int | None) -> float:
    d = next(v for ate, v in DISPERSAO_FAIXA if mu < ate)
    return d + (DISPERSAO_JOVEM if anos is not None and anos <= 1 else 0.0)


def vor_com_corte(semana: float, repl: float, anos: int | None, mu: float = 20.0) -> float:
    """E[max(V, repl)] - repl, com V ~ Normal(semana, (disp x semana)^2).

    Pro craque nao muda nada (nunca cai abaixo do replacement); quem esta
    perto do replacement ganha a opcao de estourar sem pagar o fiasco.
    """
    s = semana * dispersao(mu, anos)
    m = semana - repl
    if s <= 0:
        return max(m, 0.0)
    d = m / s
    return m * 0.5 * (1 + math.erf(d / math.sqrt(2))) + s * math.exp(-d * d / 2) / math.sqrt(2 * math.pi)


def _semana(d: str):
    from datetime import date
    return date.fromisoformat(d).isocalendar()[:2]


def _regular(d: str) -> bool:
    """Temporada regular: os playoffs (meados de abril em diante) nao contam."""
    return bool(d) and (d[5:7] not in ("04", "05", "06") or d[5:10] <= "04-14")


def semanas_temporada(ant: dict) -> set:
    return {_semana(j.data) for js in ant.values() for j in js if _regular(j.data)}


def disponibilidade(js: list, semanas: set) -> float | None:
    """Fracao das semanas da temporada regular em que ele jogou ao menos uma vez.

    Conta desde a primeira semana da TEMPORADA, nao a dele: quem perdeu os
    dois primeiros meses machucado perdeu essas semanas. E a medida de saude
    que importa no Lock-In: faltar um jogo da semana quase nao custa, faltar
    a semana inteira zera o slot.
    """
    if not semanas:
        return None
    jogou = {_semana(j.data) for j in js if _regular(j.data)}
    return len(jogou & semanas) / len(semanas)


def fracao_fora(agenda, retorno: str | None) -> float | None:
    """Fracao das semanas da temporada que acabam antes do prazo de volta."""
    if not retorno:
        return None
    semanas = sorted({j.week for j in agenda.jogos})
    if not semanas:
        return None
    fora = sum(1 for w in semanas if max(agenda.datas_da_semana(w)) < retorno[:10])
    return fora / len(semanas)


def saude(disp: float | None, status: str | None, fora: float | None = None) -> float:
    a = (SAUDE_SEM_HISTORICO if disp is None else SAUDE_ANO_PERDIDO if disp == 0
         else SAUDE_BASE + SAUDE_PESO * disp)
    if fora is not None:
        return a * (1.0 - fora)
    return a * (FATOR_OUT if status in ("Out", "IR") else 1.0)


def tendencia_fim(js: list, n: int = 15) -> float | None:
    """Minutos nos ultimos `n` jogos menos o resto da temporada (o ritmo com
    que ele terminou o ano). Ganho organico de minutos fica ~80%: e o sinal
    que mais persiste no backtest_momentum."""
    if len(js) < n + 15:
        return None
    rec = js[-n:]
    base = js[:-n]
    return (sum(j.min for j in rec) / len(rec)) - (sum(j.min for j in base) / len(base))


def reposicao(rows: list[dict], slots: list[str], n_times: int, elenco: int) -> dict[str, float]:
    """{posicao: valor semanal do replacement}.

    Enche n_times x slots titulares em ordem de valor, cada jogador no slot
    mais RESTRITO em que ele cabe (assim o UTIL sobra pra quem nao cabe em
    mais nada). Depois o banco. O replacement de cada posicao e o melhor que
    sobrou elegivel a ela.
    """
    vagas = sorted([s for _ in range(n_times) for s in slots], key=flexibilidade)
    ordem = sorted(rows, key=lambda r: -r["semana"])
    escolhidos = set()
    for r in ordem:
        for k, s in enumerate(vagas):
            if cabe(r["posicoes"], s):
                vagas.pop(k)
                escolhidos.add(r["pid"])
                break
        if not vagas:
            break
    banco = n_times * (elenco - len(slots))
    for r in ordem:
        if banco <= 0:
            break
        if r["pid"] not in escolhidos:
            escolhidos.add(r["pid"])
            banco -= 1
    sobra = [r for r in ordem if r["pid"] not in escolhidos]
    out = {}
    for p in POSICOES:
        cand = [r["semana"] for r in sobra if p in r["posicoes"]]
        out[p] = max(cand) if cand else 0.0
    return out


def tabela(ctx, sd_de) -> list[dict]:
    """Uma linha por jogador com projecao: valor semanal, VOR, ADP, sinais.

    `sd_de(mu)` e o desvio por jogo (o do modelo, medido em 2025).
    """
    n = jogos_por_semana(ctx)
    ant = ctx.anterior
    semanas = semanas_temporada(ant)
    rows = []
    for pid, pr in ctx.proj_temp.items():
        p = ctx.pool.get(pid) or {}
        pos = ctx.posicoes(pid)
        if not pos or not p.get("team"):
            continue
        js = ant.get(pid) or []
        regs = [j for j in js if _regular(j.data)]
        min_ant = sum(j.min for j in regs) / len(regs) if len(regs) >= 10 else None
        time_novo = bool(js) and bool(p.get("team")) and js[-1].team != p.get("team")
        tend = tendencia_fim(regs)
        mu = (projecao_ajustada(pr["fp"], p.get("years_exp"), js, time_novo, "C" in pos, tend)
              if pr["fp"] > 0 else 0.0)
        fonte = "proj"
        if mu <= 0 and len(js) >= 10:
            mu = sum(j.fp for j in js) / len(js)
            fonte = "2025"
        if mu <= 0:
            continue
        # Sem jogo em 2025 e nao e novato: ficou a temporada inteira fora.
        disp = disponibilidade(js, semanas) if js or (p.get("years_exp") or 0) > 0 else None
        status = ctx.modelo.status(pid)
        les = ctx.lesoes.get(pid) or {}
        fora = fracao_fora(ctx.agenda, les.get("retorno")) if status in ("Out", "IR") else None
        sd = sd_proprio(sd_de(mu), js, sd_de)
        carga = ctx.carga.get(pid)
        sem = saude(disp, status, fora) * valor_semanal(mu, sd, taxa_carga(carga), n)
        rows.append({
            "pid": pid, "nome": ctx.name(pid), "posicoes": pos, "pos": "/".join(pos),
            "team": p.get("team"), "mu": mu, "sd": sd, "semana": sem,
            "adp": pr.get("adp"), "disp": disp, "fonte": fonte,
            "tend": tend, "mu_ant": (sum(j.fp for j in js) / len(js)) if js else None,
            "anos": p.get("years_exp"), "status": status, "fora": fora,
            "retorno": les.get("retorno"), "lesao": les.get("tipo") or p.get("injury_body_part"),
            "carga": carga,
            "time_ant": js[-1].team if js else None, "min_ant": min_ant,
            "min_proj": pr.get("min") or None,
        })
    slots = starting_slots(ctx.sl.roster_positions())
    elenco = len([s for s in ctx.sl.roster_positions() if s != "IR"])
    repl = reposicao(rows, slots, ctx.sl.n_teams(), elenco)
    for r in rows:
        r["vor"] = vor_com_corte(r["semana"], min(repl[p] for p in r["posicoes"]), r["anos"], r["mu"])
    rows.sort(key=lambda r: -r["vor"])
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    adps = sorted((r for r in rows if r["adp"]), key=lambda r: r["adp"])
    for k, r in enumerate(adps, 1):
        r["rank_adp"] = k
    ctx._cache["repl"] = repl
    return rows
