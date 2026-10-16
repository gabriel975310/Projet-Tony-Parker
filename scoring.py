"""Regras de pontuacao da liga -> pontos de fantasy de UM jogo.

As linhas jogo a jogo do Sleeper trazem as stats brutas (pts, reb, ast, ...)
mas NAO trazem duplo-duplo, triplo-duplo nem os bonus de 40/50 pontos — e a
nossa liga paga 1, 2, 2 e 2 por eles. Aqui eles sao derivados das proprias stats.

Conferido contra a temporada 2025 inteira da liga: todo titular cujo placar
semanal veio diferente de zero bateu com um jogo dele recalculado aqui (1559
de 1559). A conta e a mesma do Sleeper.
"""

from __future__ import annotations

CATEGORIAS_DD = ("pts", "reb", "ast", "stl", "blk")


def derivadas(stats: dict) -> dict:
    """Completa o que a linha do jogo nao traz e a liga pontua."""
    s = dict(stats or {})
    dez = sum(1 for k in CATEGORIAS_DD if (s.get(k) or 0) >= 10)
    s.setdefault("dd", 1.0 if dez >= 2 else 0.0)
    s.setdefault("td", 1.0 if dez >= 3 else 0.0)
    p = s.get("pts") or 0
    # Cumulativo: 50 pontos tambem passaram de 40. Conferido em 2025 — os
    # jogos de 51 pontos travados nas semanas 1 e 2 so batem com o placar da
    # liga (55,5 e 49,5) com os dois bonus somados.
    s.setdefault("bonus_pt_40p", 1.0 if p >= 40 else 0.0)
    s.setdefault("bonus_pt_50p", 1.0 if p >= 50 else 0.0)
    for k in ("bonus_reb_20p", "bonus_ast_15p"):
        lim = 20 if k == "bonus_reb_20p" else 15
        cat = "reb" if k == "bonus_reb_20p" else "ast"
        s.setdefault(k, 1.0 if (s.get(cat) or 0) >= lim else 0.0)
    return s


def pontos(stats: dict, regras: dict) -> float:
    """Pontos de fantasy de uma linha de stats, pela regra da liga.

    Serve pra jogo real E pra projecao: numa projecao `dd` ja vem como
    probabilidade (0.69 pro Wembanyama), e o `setdefault` respeita o que veio.
    """
    s = derivadas(stats)
    return round(sum(float(peso) * float(s.get(k) or 0.0)
                     for k, peso in (regras or {}).items() if peso), 2)


def minutos(stats: dict) -> float:
    """`sp` vem em SEGUNDOS jogados."""
    return float((stats or {}).get("sp") or 0.0) / 60.0
