"""Log jogo a jogo de cada jogador, ja em pontos da liga.

Uma chamada por semana traz TODOS os jogos de todos os jogadores daquela
semana (`api.sleeper.com/stats/nba/{temporada}/{semana}`). A temporada
inteira sao ~25 chamadas, e semana fechada nao muda: fica no cache pra
sempre. So a semana corrente e rebaixada, de 15 em 15 minutos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from scoring import minutos, pontos

# Semanas da temporada regular no Sleeper. 2025 teve 25; a busca para na
# primeira semana vazia, entao o numero so precisa ser um teto.
MAX_SEMANAS = 26


@dataclass
class Jogo:
    data: str
    team: str
    opp: str
    game_id: str
    fp: float          # pontos de fantasy pela regra da liga
    min: float
    stats: dict = field(repr=False, default_factory=dict)


def _ttl_semana(datas: list[str], hoje: date) -> float:
    if datas and max(date.fromisoformat(d) for d in datas) < hoje - timedelta(days=1):
        return 24 * 365
    return 0.25


def linhas_semana(sl, tipo: str, season: int, week: int,
                  hoje: date | None = None, datas: list[str] | None = None) -> list[dict]:
    hoje = hoje or datetime.now(timezone.utc).date()
    ttl = _ttl_semana(datas or [], hoje) if datas is not None else 0.25
    if season < hoje.year - 1 or (season == hoje.year - 1 and hoje.month >= 7):
        ttl = 24 * 365     # temporada encerrada
    return sl.linhas(tipo, season, week, ttl)


def logs(sl, season: int, regras: dict, semanas=None, agenda=None,
         hoje: date | None = None) -> dict[str, list[Jogo]]:
    """{player_id: [Jogo, ...]} em ordem cronologica.

    `semanas` None = a temporada inteira ate a primeira semana vazia (pra
    historico). Na temporada corrente passe as semanas que ja existem.
    """
    out: dict[str, list[Jogo]] = {}
    alvo = list(semanas) if semanas is not None else range(1, MAX_SEMANAS + 1)
    for w in alvo:
        datas = agenda.datas_da_semana(w) if agenda else None
        rows = linhas_semana(sl, "stats", season, w, hoje, datas)
        if not rows and semanas is None and w > 3:
            break
        for r in rows:
            # O endpoint mistura a linha do TIME (`TEAM_OKC`, 240 minutos e
            # 160 pontos por jogo) com as dos jogadores. Sem este filtro o
            # time virava o melhor jogador da liga.
            if not str(r.get("player_id") or "").isdigit():
                continue
            st = r.get("stats") or {}
            mins = minutos(st)
            if mins <= 0:
                continue
            out.setdefault(str(r["player_id"]), []).append(Jogo(
                r.get("date") or "", r.get("team") or "", r.get("opponent") or "",
                str(r.get("game_id") or ""), pontos(st, regras), mins, st))
    for v in out.values():
        v.sort(key=lambda j: j.data)
    return out


def projecoes_jogo(sl, season: int, week: int, regras: dict,
                   hoje: date | None = None,
                   ttl_hours: float | None = None) -> dict[tuple[str, str], dict]:
    """{(player_id, data): {"fp", "min"}} — a projecao do Sleeper pra cada
    jogo da semana. Jogador sem linha num jogo que o time tem = o Sleeper
    nao espera que ele jogue (lesao, descanso)."""
    hoje = hoje or datetime.now(timezone.utc).date()
    encerrada = season < hoje.year - 1 or (season == hoje.year - 1 and hoje.month >= 7)
    out = {}
    ttl = ttl_hours if ttl_hours is not None else (24 * 365 if encerrada else 1.0)
    for r in sl.linhas("projections", season, week, ttl):
        if not str(r.get("player_id") or "").isdigit():
            continue
        st = r.get("stats") or {}
        out[(str(r["player_id"]), r.get("date") or "")] = {
            "fp": pontos(st, regras), "min": minutos(st), "game_id": r.get("game_id")}
    return out


# -- controle de carga -------------------------------------------------------
# Medido em 2021-2025 (279 jogadores de 20+ pts/jogo, backtest em NOTES_LOCALES.md):
# nas semanas em que o jogador esta ATIVO (jogou ao menos uma vez), ele joga
# 90% dos jogos normais e 82% das segundas noites de back-to-back. A taxa de
# cada um persiste 35% de um ano pro outro (lesao de semana inteira: 14%) —
# o Embiid de 2024-25 jogou 17-30% dos b2b e segue pulando. Encolher pro
# tipico com o peso de m jogos fantasmas reproduz o 35% numa temporada.
CARGA_NORMAL, CARGA_B2B = 0.903, 0.818
M_NORMAL, M_B2B = 109, 25


def b2b(datas_time, data: str) -> bool:
    """Segunda noite de back-to-back: o time jogou na vespera."""
    return (date.fromisoformat(data) - timedelta(days=1)).isoformat() in datas_time


def contagem_carga(L: dict) -> dict[str, list[int]]:
    """{pid: [jogos normais, jogou, b2b, jogou]} nas semanas em que ele jogou,
    contando os jogos do time em que ele estava naquela semana."""
    sem = lambda d: date.fromisoformat(d).isocalendar()[:2]
    datas: dict[str, set] = {}
    for js in L.values():
        for j in js:
            if j.data:
                datas.setdefault(j.team, set()).add(j.data)
    out = {}
    for pid, js in L.items():
        jogou = {j.data for j in js}
        time_sem = {sem(j.data): j.team for j in js if j.data}
        c = [0, 0, 0, 0]
        for t in set(time_sem.values()):
            for d in datas.get(t, ()):
                if time_sem.get(sem(d)) != t:
                    continue
                k = 2 if b2b(datas[t], d) else 0
                c[k] += 1
                c[k + 1] += d in jogou
        out[pid] = c
    return out


def taxa_carga(c: list[int] | None, eh_b2b: bool | None = None) -> float:
    """Chance de jogar um jogo do time numa semana ativa, encolhida pro tipico.
    `eh_b2b` None = a media da temporada (~17% dos jogos sao b2b)."""
    c = c or [0, 0, 0, 0]
    normal = (c[1] + M_NORMAL * CARGA_NORMAL) / (c[0] + M_NORMAL)
    seg = (c[3] + M_B2B * CARGA_B2B) / (c[2] + M_B2B)
    if eh_b2b is None:
        return 0.83 * normal + 0.17 * seg
    return seg if eh_b2b else normal
