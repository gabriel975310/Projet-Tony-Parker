"""Slots da NBA e atribuicao exata de jogador -> slot.

    PG  SG  G(PG/SG)  SF  PF  F(SF/PF)  C  UTIL(qualquer)

Um jogador tem varias posicoes ('PG','SG') e cada slot aceita um conjunto.
Quem vai pra onde e um problema de atribuicao: com 8 slots, programacao
dinamica sobre o subconjunto de slots ja ocupados e exata e instantanea
(256 estados x jogadores).

O valor depende do PAR (jogador, slot), nao so do jogador: no Lock-In travar
alguem no UTIL custa mais caro que travar no C, porque o UTIL e o slot que
qualquer um do banco ainda poderia ocupar. E por isso que o otimizador
recebe `valor(pid, i)`.
"""

from __future__ import annotations

ELEGIVEIS = {
    "PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"},
    "SF": {"SF"}, "PF": {"PF"}, "F": {"SF", "PF"},
    "C": {"C"}, "UTIL": {"PG", "SG", "SF", "PF", "C"},
}
BENCH_SLOTS = {"BN", "IR", "TAXI"}


def starting_slots(roster_positions: list[str]) -> list[str]:
    """Os slots titulares, na ordem — e a ordem do array `starters` do Sleeper."""
    return [s for s in (roster_positions or []) if s not in BENCH_SLOTS]


def cabe(posicoes: list[str], slot: str) -> bool:
    return bool(set(posicoes or []) & ELEGIVEIS.get(slot, set()))


def flexibilidade(slot: str) -> int:
    """Quantas posicoes o slot aceita. UTIL 5, G/F 2, os fixos 1."""
    return len(ELEGIVEIS.get(slot, ()))


def otimizar(slots: list[str], jogadores: list[str], valor, posicoes,
             fixos: dict[int, str] | None = None) -> tuple[list[str | None], float]:
    """(jogador por slot, soma de valor). Exato.

    `valor(pid, i)` e o valor do jogador no slot i (None = nao serve ali).
    `fixos` sao slots que nao mudam (travado, ou jogo ja comecou): entram
    como estao e saem da conta.
    """
    fixos = fixos or {}
    n = len(slots)
    mask0 = 0
    for i in fixos:
        mask0 |= 1 << i
    usados = set(fixos.values())
    best: dict[int, tuple[float, tuple]] = {
        mask0: (0.0, tuple(fixos.get(i) for i in range(n)))}
    for pid in jogadores:
        if pid in usados:
            continue
        pos = posicoes(pid)
        opcoes = []
        for i, s in enumerate(slots):
            if i in fixos or not cabe(pos, s):
                continue
            v = valor(pid, i)
            if v is not None:
                opcoes.append((i, v))
        if not opcoes:
            continue
        nxt = dict(best)
        for mask, (score, assign) in best.items():
            for i, v in opcoes:
                bit = 1 << i
                if mask & bit:
                    continue
                m2, s2 = mask | bit, score + v
                if s2 > nxt.get(m2, (float("-inf"),))[0] + 1e-12:
                    a2 = list(assign)
                    a2[i] = pid
                    nxt[m2] = (s2, tuple(a2))
        best = nxt
    _, (score, assign) = max(best.items(),
                             key=lambda kv: (round(kv[1][0], 9), bin(kv[0]).count("1")))
    return list(assign), score


def diff(slots: list[str], atual: list[str | None],
         nova: list[str | None]) -> list[tuple[int, str, str | None, str | None]]:
    """[(i, slot, sai, entra)] so onde muda."""
    out = []
    for i, slot in enumerate(slots):
        a = atual[i] if i < len(atual) else None
        b = nova[i] if i < len(nova) else None
        if a != b:
            out.append((i, slot, a, b))
    return out


def elenco_minimo_ok(posicoes_elenco: list[list[str]], slots: list[str]) -> bool:
    """Da pra preencher TODOS os slots titulares com esse elenco?"""
    ids = [str(k) for k in range(len(posicoes_elenco))]
    assign, _ = otimizar(slots, ids, lambda pid, i: 1.0,
                         lambda pid: posicoes_elenco[int(pid)])
    return all(a is not None for a in assign)
