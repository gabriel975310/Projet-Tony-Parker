"""O estado da semana: quem ja travou, quem ainda joga, o que cada um vale.

E o que a escalacao de hora em hora e o relatorio do dia compartilham.

QUEM JA TRAVOU. A API publica nao tem um campo "travado". O que ela tem e o
placar de cada slot no confronto (`starters_points`), e ele so prova trava
quando o jogo que bate NAO e o ultimo encerrado do jogador, ou quando o
proximo jogo dele ja comecou (o placar velho ficou). No meio da semana o
Sleeper mostra o placar do ultimo jogo travado ou nao (semana 1 de 2026:
tres titulares com placar, um so travado). Entao:

  * placar do slot = placar de um jogo encerrado, e depois dele o jogador
    ja jogou ou esta jogando -> travado, com aquele valor;
  * placar do ultimo jogo, com o proximo ainda por comecar -> pode ser so o
    provisorio: vale a enquete (enquete.py). Sem voto, aberto e PENDENTE;
  * placar zero, ou batendo com jogo em andamento -> aberto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agenda import Jogo, hoje_et
from clients import CACHE
from lineup import starting_slots
from lockin import Opcao, valor_semanal
from contexte import ativos
from enquete import respostas

DECISOES = CACHE / "decisoes_trava.json"


def decisoes() -> dict:
    try:
        return json.loads(DECISOES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def registrar_decisao(week: int, pid: str, data: str, acao: str) -> None:
    d = decisoes()
    d[f"{week}:{pid}:{data}"] = acao
    try:
        DECISOES.write_text(json.dumps(d), encoding="utf-8")
    except OSError as e:
        print(f"(nao gravei a decisao: {e})")


@dataclass
class Estado:
    week: int
    hoje: str                                  # data americana de hoje
    slots: list[str]
    atual: list[str | None]                    # escalacao no Sleeper agora
    travados: dict[int, tuple[str, float]]     # slot -> (jogador, placar)
    abertos: list[int]
    elenco: list[str]                          # ativos (sem IR)
    ir: list[str]
    jogos: dict[str, list[Jogo]]               # jogos da semana por jogador
    placar: dict[tuple[str, str], float]       # (pid, data) -> pontos ja feitos
    origem_trava: dict[int, str] = field(default_factory=dict)
    # slot -> (jogador, placar, data do jogo): talvez travado, sem voto ainda
    pendentes: dict[int, tuple[str, float, str]] = field(default_factory=dict)
    roster_id: int | None = None

    def usados(self) -> set[str]:
        return {p for p, _ in self.travados.values()}

    def restantes(self, pid: str, agora) -> list[Jogo]:
        return [j for j in self.jogos.get(pid, []) if not j.comecou(agora)]

    def em_andamento(self, pid: str, agora) -> Jogo | None:
        return next((j for j in self.jogos.get(pid, [])
                     if j.comecou(agora) and not j.acabou(agora)), None)

    def ultimo_encerrado(self, pid: str, agora) -> Jogo | None:
        fins = [j for j in self.jogos.get(pid, []) if j.acabou(agora)]
        return fins[-1] if fins else None


def estado(ctx) -> Estado:
    agora, week = ctx.agora, ctx.week
    roster = ctx.sl.my_roster()
    slots = starting_slots(ctx.sl.roster_positions())
    atual = [(p if p and p != "0" else None) for p in (roster.get("starters") or [])]
    atual = (atual + [None] * len(slots))[:len(slots)]
    elenco = [p for p in ativos(roster) if ctx.posicoes(p)]
    ir = list(roster.get("reserve") or [])

    jogos = {p: ctx.agenda.jogos_do_time(ctx.team(p), week) for p in elenco + ir}
    placar = {}
    for p in elenco + ir:
        for j in ctx.logs.get(p, []):
            placar[(p, j.data)] = j.fp

    est = Estado(week, hoje_et(agora), slots, atual, {}, [], elenco, ir, jogos, placar,
                 roster_id=roster.get("roster_id"))

    # placar de cada slot no confronto
    pontos_slot: list[float] = [0.0] * len(slots)
    titulares_conf: list[str | None] = list(atual)
    try:
        for m in ctx.sl.matchups(week):
            if m.get("roster_id") == roster.get("roster_id"):
                pontos_slot = list((m.get("starters_points") or []) + [0.0] * len(slots))[:len(slots)]
                titulares_conf = [(p if p and p != "0" else None)
                                  for p in (m.get("starters") or [])] + [None] * len(slots)
                break
    except Exception as e:
        ctx.warnings.append(f"confronto indisponivel ({e}); assumindo nada travado")

    votos = respostas()
    for i in range(len(slots)):
        p = titulares_conf[i] or atual[i]
        pts = float(pontos_slot[i] or 0.0)
        if not p or abs(pts) < 1e-9:
            continue
        fins = [j for j in jogos.get(p, []) if j.acabou(agora)]
        bate = [j for j in fins if abs(placar.get((p, j.data), -999) - pts) < 0.05]
        if not bate:
            continue      # placar ao vivo ou estatistica atrasada: aberto
        j = bate[-1]
        ultimo = j is fins[-1]
        proximo_comecou = any(x.comecou(agora) for x in jogos.get(p, []) if x.data > j.data)
        if ultimo and not proximo_comecou:
            voto = votos.get(f"{week}:{p}:{j.data}")
            if voto is None:
                est.pendentes[i] = (p, pts, j.data)
            if not voto:
                est.origem_trava[i] = "provisorio"
                continue
            est.origem_trava[i] = "enquete"
        else:
            est.origem_trava[i] = j.data
        est.travados[i] = (p, pts)
    est.abertos = [i for i in range(len(slots)) if i not in est.travados]
    return est


def opcao(ctx, pid: str, jogo: Jogo) -> Opcao:
    pr = ctx.modelo.jogo(pid, jogo)
    return Opcao(pid, jogo.data, jogo.inicio, pr.mu, pr.sd, pr.p)


def opcao_futura(ctx, pid: str, jogo: Jogo) -> Opcao:
    """Jogo de semana futura: no waiver/fim de elenco, a media do horizonte
    (previsao_waiver), nao o proximo jogo repetido."""
    pr = ctx.modelo.jogo_futuro(pid, jogo)
    return Opcao(pid, jogo.data, jogo.inicio, pr.mu, pr.sd, pr.p)


def opcoes_futuras(ctx, est: Estado, pids, so_depois_de_hoje: bool = False) -> dict[str, list[Opcao]]:
    """{pid: jogos que ainda nao comecaram} pra quem ainda pode travar."""
    out = {}
    usados = est.usados()
    for p in pids:
        if p in usados:
            continue
        js = [j for j in ctx.agenda.jogos_do_time(ctx.team(p), est.week)
              if not j.comecou(ctx.agora) and (not so_depois_de_hoje or j.data > est.hoje)]
        out[p] = [opcao(ctx, p, j) for j in js]
    return out


def jogos_hoje(ctx, est: Estado, pids) -> dict[str, Opcao]:
    """{pid: o jogo de HOJE que ainda nao comecou}."""
    out = {}
    usados = est.usados()
    for p in pids:
        if p in usados:
            continue
        for j in ctx.agenda.jogos_do_time(ctx.team(p), est.week):
            if j.data == est.hoje and not j.comecou(ctx.agora):
                out[p] = opcao(ctx, p, j)
    return out


def valor_temporada(ctx, pid: str, semanas: int | None = None) -> float:
    """Pontos que ele deve render num slot nas proximas `semanas` semanas
    (a partir da que vem), pela parada otima semanal de cada uma."""
    cfg = ctx.cfg.get("waivers") or {}
    semanas = semanas or int(cfg.get("horizonte_semanas") or 6)
    p = ctx.modelo.carga(pid)
    h = ctx.modelo.horizonte(pid)
    # No waiver e no fim do elenco, o horizonte de 6 semanas medido
    # (previsao_waiver); ele ja inclui as faltas, entao divide pela carga.
    mu = h / max(p, 0.05) if h is not None else ctx.modelo.base(pid)["mu"]
    if mu <= 0:
        return 0.0
    sd = ctx.modelo.sd(mu)
    # Fora HOJE nao e fora por 6 semanas: as semanas antes do prazo de volta
    # da ESPN valem zero. Sem prazo, Out vale 60% do horizonte e IR 30% —
    # chute declarado, nao medida.
    st = ctx.modelo.status(pid)
    ret = ctx.retorno(pid) if st in ("Out", "IR", "Sus") else None
    fator = 1.0 if ret else {"Out": 0.6, "Sus": 0.6, "IR": 0.3}.get(st or "", 1.0)
    ultimo = max((j.week for j in ctx.agenda.jogos), default=ctx.week)
    total = 0.0
    for w in range(ctx.week + 1, min(ultimo, ctx.week + semanas) + 1):
        if ret and max(ctx.agenda.datas_da_semana(w), default="") < ret[:10]:
            continue
        n = ctx.agenda.jogos_por_semana(ctx.team(pid), w)
        total += valor_semanal(mu, sd, p, n)
    return fator * total
