"""Quanto um jogador deve fazer em UM jogo: a moeda da decisao de travar.

Parte da projecao jogo a jogo do Sleeper e corrige o que ela erra de forma
sistematica. Tudo medido em 2020-2025 (109.697 jogos de quem foi projetado
pra 8+ pts e jogou), com os pesos ajustados juntos e validados deixando
cada par de temporadas de fora:

  * o Sleeper projeta ALTO: o jogo sai 0,940x do projetado, em media;
  * ele demora a se ajustar a temporada: quem vem acima (ou abaixo) da
    projecao continua, e 23% do desvio da temporada ate aqui persiste;
  * defesa do adversario CONTRA A POSICAO, medida na temporada ate aqui
    (quanto ela cede em relacao ao projetado): 27% do desvio persiste;
  * casa +0,9%, fora -0,9%; 2a noite de back-to-back -1,0%; 3+ dias de
    folga +2,4%;
  * e o modelo proprio do bot (minutos x eficiencia, `modelo.py`) ainda
    soma: 30% dele na mistura.

Testado e descartado (nao melhora fora da amostra): o jeito de cada jogador
em casa/fora ou no b2b (nem medido na propria temporada), o historico contra
o adversario, vespera de b2b, titular do time fora (o Sleeper ja ajusta).

Erro medio por jogo, fora da amostra: 5,272 pts (previsao antiga, 50% modelo
+ 50% Sleeper, que saia ~2% alta) -> 5,231, e sem vies em nenhum ano.
"""

from __future__ import annotations

VIES_SLEEPER = 0.940
PESO_TEMPORADA = 0.230
PESO_DEFESA = 0.268
CASA = 0.0093
B2B = -0.0100
FOLGA3 = 0.0238
PESO_MODELO = 0.30
CALIBRACAO = 1.0169      # deixa a media da mistura sem vies

PROJ_MIN = 8.0           # so entram na conta jogos projetados pra 8+
JOGOS_TEMPORADA = 10     # minimo pra usar a temporada do jogador
JOGOS_DEFESA = 15        # minimo pra usar a defesa do adversario


def grupo_pos(posicoes) -> str:
    ps = set(posicoes or [])
    if "C" in ps:
        return "C"
    g, f = ps & {"PG", "SG"}, ps & {"SF", "PF"}
    return "G" if g and not f else "F" if f and not g else "G/F"


def _regular(d: str) -> bool:
    return bool(d) and (d[5:7] not in ("04", "05", "06") or d[5:10] <= "04-14")


def indices(logs: dict, proj: dict, pos_de, ate: str | None = None) -> tuple[dict, dict]:
    """({pid: real/projetado na temporada}, {(adversario, grupo): o que a
    defesa cede / projetado}), so com jogos ANTES de `ate`."""
    jog: dict = {}
    dfz: dict = {}
    for pid, js in logs.items():
        g = None
        for j in js:
            if (ate and j.data >= ate) or not _regular(j.data) or (j.min or 0) <= 0:
                continue
            pr = proj.get((pid, j.data))
            if not pr or pr["fp"] < PROJ_MIN:
                continue
            g = g or grupo_pos(pos_de(pid))
            a = jog.setdefault(pid, [0.0, 0.0, 0])
            a[0] += j.fp; a[1] += pr["fp"]; a[2] += 1
            b = dfz.setdefault((j.opp, g), [0.0, 0.0, 0])
            b[0] += j.fp; b[1] += pr["fp"]; b[2] += 1
    temp = {p: a / b for p, (a, b, n) in jog.items() if n >= JOGOS_TEMPORADA and b > 0}
    defesa = {k: a / b for k, (a, b, n) in dfz.items() if n >= JOGOS_DEFESA and b > 0}
    return temp, defesa


def prever(proj_fp: float, modelo_mu: float | None = None, temp: float | None = None,
           defesa: float | None = None, casa: bool | None = None,
           descanso: int | None = None) -> float:
    """Media prevista do jogo. `descanso` = dias desde o jogo anterior do
    time (1 = segunda noite de back-to-back)."""
    m = 1.0
    if temp is not None:
        m += PESO_TEMPORADA * (temp - 1)
    if defesa is not None:
        m += PESO_DEFESA * (defesa / VIES_SLEEPER - 1)
    if casa is not None:
        m += CASA if casa else -CASA
    if descanso == 1:
        m += B2B
    elif descanso is not None and descanso >= 4:
        m += FOLGA3
    novo = proj_fp * VIES_SLEEPER * m
    if modelo_mu and modelo_mu > 0:
        novo = (1 - PESO_MODELO) * novo + PESO_MODELO * modelo_mu
    return max(0.0, novo * CALIBRACAO)
