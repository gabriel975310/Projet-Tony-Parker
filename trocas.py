"""Trocas: as melhores com cada time da liga, pelo valor de temporada.

Testa TODA troca 1 por 1, 2 por 1 e 1 por 2 com cada time (mesmo desenho do
bot da NFL, que na Receba Bowl achou 14 de 27 mil e nenhum agente achava
todas). O que muda e a moeda: aqui e `valor_temporada`, o que cada jogador
rende num slot nas proximas semanas pela parada otima do Lock-In, ja com
lesao, prazo de volta e controle de carga.

A moeda e pontos por SEMANA, comparavel ao que um slot rende. Duas passadas:

  1. TRIAGEM, em todas as ~25 mil: o valor do time e o do seu TITULAR
     otimo (cada jogador num slot em que cabe), pelo valor de temporada
     dividido pelas semanas do horizonte. Como o valor nao depende do slot,
     o guloso do `lockin._titulares` e exato e roda em microssegundos. O
     banco vale zero aqui, entao os cortes sao frouxos.
  2. CONTA DE VERDADE, nas que sobram: o plano da semana do Lock-In
     (`lockin.Planejador`, o mesmo da escalacao) em cada semana do
     horizonte, com o calendario real de cada um. Ai o banco vale o que
     vale: o jogo dele entra no slot de quem foi mal. Quem fica com elenco
     acima do limite dispensa o pior do banco antes da conta.

Uma troca passa quando o meu time ganha `ganho_minimo` e o deles nao perde
mais que `perda_maxima` (pelo MEU modelo: o deles e outro, e uma troca que
piora o time deles no papel nao e aceita).

    python trocas.py --dry-run
"""

from __future__ import annotations

import sys
from itertools import combinations

from clients import CACHE, esc
from lineup import starting_slots
from lockin import Planejador, _titulares
from contexte import base_parser, build, ja_avisado, marcar_avisado, run_for_each_league
from report import bloco, n, rodape, secao, titulo
from semana import opcao_futura, valor_temporada

AVISOS = CACHE / "trocas_avisos.json"

# (quantos eu mando, quantos eu recebo). 2 por 2 fica de fora como na NFL:
# multiplica as trocas por ~6 e quase nunca passa nos dois cortes.
FORMATOS = ((1, 1), (2, 1), (1, 2))


def titular(slots: list[str], jogadores, v: dict, posicoes) -> dict[int, str]:
    """{slot: jogador} do titular otimo pelo valor `v`."""
    ordem = sorted((p for p in jogadores if v.get(p, 0) > 0), key=lambda p: -v[p])
    return _titulares(slots, list(range(len(slots))), ordem, posicoes)


def valor_time(slots, jogadores, v, posicoes) -> float:
    return sum(v[p] for p in titular(slots, jogadores, v, posicoes).values())


def buscar(slots, elencos: dict, eu, v: dict, posicoes, ganho_min: float,
           perda_max: float, fixos=frozenset(), formatos=FORMATOS) -> tuple[list[dict], int]:
    """(trocas que passam, quantas foram testadas). `elencos` e
    {roster_id: [jogadores]}; `eu` e o meu roster_id; `fixos` sao os meus
    que contam no titular mas nunca saem."""
    meus = elencos[eu]
    meu_antes = valor_time(slots, meus, v, posicoes)
    envio = [p for p in meus if p not in fixos]
    achadas, testadas = [], 0
    for rid, deles in sorted(elencos.items()):
        if rid == eu:
            continue
        del_antes = valor_time(slots, deles, v, posicoes)
        for n_manda, n_recebe in formatos:
            for g in combinations(envio, n_manda):
                fica = [p for p in meus if p not in g]
                for r in combinations(deles, n_recebe):
                    testadas += 1
                    ganho = valor_time(slots, fica + list(r), v, posicoes) - meu_antes
                    if ganho < ganho_min:
                        continue
                    sobra = [p for p in deles if p not in r] + list(g)
                    deles_ganho = valor_time(slots, sobra, v, posicoes) - del_antes
                    if deles_ganho < -perda_max:
                        continue
                    achadas.append({"rid": rid, "manda": list(g), "recebe": list(r),
                                    "ganho": ganho, "deles": deles_ganho})
    return achadas, testadas


class Plano:
    """Pts/semana de um elenco no Lock-In de verdade, com banco: o plano da
    semana em cada semana do horizonte, na media. Memoriza por elenco."""

    def __init__(self, ctx, slots: list[str], semanas: int):
        self.ctx, self.slots = ctx, slots
        ultimo = max((j.week for j in ctx.agenda.jogos), default=ctx.week)
        self.ws = list(range(ctx.week + 1, min(ultimo, ctx.week + semanas) + 1)) or [ctx.week]
        # direto do calendario, sem buscar horario na ESPN: pra planejar
        # semanas a frente, o dia do jogo basta
        self._jogos: dict[tuple[str, int], list] = {}
        for j in ctx.agenda.jogos:
            if j.week in self.ws:
                for t in j.times:
                    self._jogos.setdefault((t, j.week), []).append(j)
        self._ops: dict = {}
        self._memo: dict = {}

    def _opcoes(self, p: str, w: int):
        if (p, w) not in self._ops:
            js = sorted(self._jogos.get((self.ctx.team(p), w), []), key=lambda j: j.data)
            self._ops[(p, w)] = [opcao_futura(self.ctx, p, j) for j in js]
        return self._ops[(p, w)]

    def valor(self, jogadores) -> float:
        k = frozenset(jogadores)
        if k not in self._memo:
            todos = list(range(len(self.slots)))
            tot = 0.0
            for w in self.ws:
                pl = Planejador(self.slots, {p: self._opcoes(p, w) for p in k},
                                self.ctx.posicoes)
                tot += pl.plano(todos).valor
            self._memo[k] = tot / len(self.ws)
        return self._memo[k]


def cortar_excesso(elenco: list[str], ativos_max: int, ir: set, v: dict, slots,
                   posicoes, protegidos=frozenset()) -> tuple[list[str], str | None]:
    """Elenco ativo acima do limite dispensa o pior do banco (pelo valor de
    triagem). Devolve (elenco, quem saiu)."""
    if len([p for p in elenco if p not in ir]) <= ativos_max:
        return elenco, None
    tit = set(titular(slots, elenco, v, posicoes).values())
    banco = [p for p in elenco if p not in tit and p not in ir and p not in protegidos]
    if not banco:
        return elenco, None
    sai = min(banco, key=lambda p: v.get(p, 0))
    return [p for p in elenco if p != sai], sai


def melhores(achadas: list[dict], limite: int = 3) -> list[dict]:
    """As de maior ganho meu, uma por time: tres propostas pro mesmo time
    sao a mesma conversa."""
    out, vistos = [], set()
    for t in sorted(achadas, key=lambda t: (-t["ganho"], -t["deles"])):
        if t["rid"] in vistos:
            continue
        vistos.add(t["rid"])
        out.append(t)
        if len(out) == limite:
            break
    return out


def por_semana(ctx, semanas: int):
    """valor_temporada dividido pelas semanas que de fato restam no
    horizonte: a moeda vira pontos por semana, comparavel a um slot."""
    ultimo = max((j.week for j in ctx.agenda.jogos), default=ctx.week)
    k = max(1, min(ultimo, ctx.week + semanas) - ctx.week)
    return lambda p: valor_temporada(ctx, p, semanas) / k


def calcular(ctx) -> tuple[list[dict], int, int]:
    """(melhores trocas, testadas, semanas do horizonte)."""
    cfg = ctx.cfg.get("trocas") or {}
    semanas = int(cfg.get("horizonte_semanas") or 10)
    ganho_min = float(cfg.get("ganho_minimo", 2.0))
    perda_max = float(cfg.get("perda_maxima", 1.0))
    slots = starting_slots(ctx.sl.roster_positions())
    ativos_max = len([s for s in ctx.sl.roster_positions() if s not in ("IR", "TAXI")])
    meu = ctx.sl.my_roster()
    eu = meu.get("roster_id")
    nunca = set(cfg.get("nunca_trocar") or [])
    rosters = ctx.sl.rosters()
    elencos = {r.get("roster_id"): [p for p in (r.get("players") or []) if ctx.posicoes(p)]
               for r in rosters}
    ir = {p for r in rosters for p in (r.get("reserve") or []) + (r.get("taxi") or [])}
    fixos = frozenset(p for p in elencos[eu] if ctx.name(p) in nunca)
    vs = por_semana(ctx, semanas)
    v = {p: vs(p) for js in elencos.values() for p in js}

    def pos(p):
        return ctx.posicoes(p)

    # 1. triagem com cortes frouxos (o banco ainda vale zero aqui)
    achadas, testadas = buscar(slots, elencos, eu, v, pos, ganho_min / 2, perda_max * 3, fixos)
    achadas.sort(key=lambda t: -t["ganho"])
    por_time: dict = {}
    triadas = []
    for t in achadas:
        if por_time.get(t["rid"], 0) < int(cfg.get("triagem_por_time", 8)):
            por_time[t["rid"]] = por_time.get(t["rid"], 0) + 1
            triadas.append(t)

    # 2. a conta de verdade, com banco e calendario
    plano = Plano(ctx, slots, semanas)
    meu_antes = plano.valor(elencos[eu])
    antes = {}
    passam = []
    for t in triadas:
        deles = elencos[t["rid"]]
        if t["rid"] not in antes:
            antes[t["rid"]] = plano.valor(deles)
        meu_novo, sai = cortar_excesso(
            [p for p in elencos[eu] if p not in t["manda"]] + t["recebe"],
            ativos_max, ir, v, slots, pos, fixos | set(t["recebe"]))
        del_novo, _ = cortar_excesso(
            [p for p in deles if p not in t["recebe"]] + t["manda"],
            ativos_max, ir, v, slots, pos, frozenset(t["manda"]))
        g = plano.valor(meu_novo) - meu_antes
        d = plano.valor(del_novo) - antes[t["rid"]]
        if g >= ganho_min and d >= -perda_max:
            passam.append(dict(t, ganho=g, deles=d, sai=sai))
    tops = melhores(passam, int(cfg.get("limite", 3)))
    for t in tops:
        deles = elencos[t["rid"]]
        depois = titular(slots, [p for p in deles if p not in t["recebe"]] + t["manda"], v, pos)
        t["entram_deles"] = [p for p in t["manda"] if p in depois.values()]
    return tops, testadas, semanas


def linhas(ctx, tops: list[dict], semanas: int) -> list[str]:
    if not tops:
        return []
    nomes = ctx.sl.team_names()
    L = secao("🔁", "Trocas", f"pts/semana no Lock-In, próximas {semanas} semanas")
    for t in tops:
        manda = " + ".join(f"{esc(ctx.name(p))} <i>{esc(ctx.pos(p))}</i>" for p in t["manda"])
        recebe = " + ".join(f"<b>{esc(ctx.name(p))}</b> <i>{esc(ctx.pos(p))}</i>"
                            for p in t["recebe"])
        time = nomes.get(t["rid"]) or f"time {t['rid']}"
        L.append(f"🤝 com <b>{esc(time)}</b>")
        L.append(f"      📤 manda {manda}")
        L.append(f"      📥 recebe {recebe}")
        L.append(f"      📊 você <b>{n(t['ganho'], 1, True)}</b> · eles {n(t['deles'], 1, True)}")
        por_que = []
        if t["entram_deles"]:
            verbo = " entram" if len(t["entram_deles"]) > 1 else " entra"
            por_que.append(" e ".join(ctx.name(p) for p in t["entram_deles"])
                           + verbo + " no titular deles")
        if t.get("sai"):
            por_que.append(f"pra abrir a vaga, sai {ctx.name(t['sai'])}")
        elif len(t["recebe"]) > len(t["manda"]):
            por_que.append("você precisa abrir 1 vaga")
        elif len(t["manda"]) > len(t["recebe"]):
            por_que.append("eles precisam abrir 1 vaga")
        if por_que:
            L.append(f"      <i>💬 {esc(' · '.join(por_que))}</i>")
        L.append("")
    return L


def main_with(args, config=None) -> int:
    ctx = build(args, config)
    deadline = int(ctx.sl.settings().get("trade_deadline") or 99)
    if ctx.week > deadline:
        print(f"[{ctx.rotulo}] prazo de trocas passou (semana {deadline})")
        return 0
    chave = f"trocas:{ctx.week}"
    if getattr(args, "gate", False) and ja_avisado(AVISOS, chave):
        print(f"[{ctx.rotulo}] trocas da semana {ctx.week} ja sairam")
        return 0
    if not ctx.sl.my_roster().get("players"):
        print(f"[{ctx.rotulo}] elenco vazio (o draft ainda nao aconteceu)")
        return 0
    tops, testadas, semanas = calcular(ctx)
    print(f"[{ctx.rotulo}] {testadas} trocas testadas, {len(tops)} propostas")
    if not tops:
        if getattr(args, "gate", False):
            marcar_avisado(AVISOS, chave)
        return 0
    L = [titulo("TROCAS", ctx.league_name, f"semana {ctx.week}")]
    L += linhas(ctx, tops, semanas)
    ok = ctx.notify(bloco(L) + rodape(ctx.warnings))
    if ok:
        marcar_avisado(AVISOS, chave)
    return 0


def main(argv=None) -> int:
    args = base_parser(__doc__.splitlines()[0]).parse_args(argv)
    return run_for_each_league(args, main_with)


if __name__ == "__main__":
    sys.exit(main())
