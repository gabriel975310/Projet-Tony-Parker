"""Quanto um jogador do fim do elenco / waiver rende nas PROXIMAS ~6 SEMANAS.

A previsao do proximo jogo (`modelo.py`) e boa pro proximo jogo, mas
repetida por 6 semanas ela erra pra cima: quem aparece no topo do waiver
quase sempre esta num bom momento, e momento passa. Medido em 2020-2025
(47 mil decisoes; alvo = pontos por jogo DO TIME nos 20 jogos seguintes,
falta conta zero; validado deixando cada par de temporadas de fora):

  * o bot antigo previa 16,3 pra quem ele mandaria pegar (topo do waiver),
    e eles fizeram 14,2. Pros jogadores do fim do elenco (quem voce
    dispensaria), +1,0 de vies. Este modelo: 15,7 previsto, 15,6 real;
  * do salto recente (ultimos 5 jogos acima da media da temporada), ficam
    45% nos 5 jogos seguintes, 24% nos jogos 6-10 e 15% nos 11-20. A media
    da temporada pesa 6x mais que os ultimos 5;
  * a projecao do Sleeper pro proximo jogo e o melhor sinal novo (ela ja
    sabe de lesao de titular, papel novo, rotacao): sozinha tira 6% do erro;
  * a temporada anterior e a presenca recente (quantos dos ultimos 10 jogos
    do time ele jogou) tambem somam;
  * testado e descartado: a causa do ganho de minutos (titular fora ou
    ganho proprio) depois que o Sleeper entra (-0,3% do erro), tendencia de
    arremessos. Saber EXATAMENTE quando o titular machucado volta (o
    melhor que uma noticia de lesao poderia dizer) tira so 0,7% do erro de
    quem subiu: a projecao do Sleeper ja carrega quase tudo isso.

Erro medio (pts por jogo do time, 6 semanas): livres 3,10 -> 2,95; fim de
elenco 3,55 -> 3,45. Escolhendo o melhor livre de cada dia, o escolhido fez
+0,32 pts/jogo a mais (IC 95% -0,06 a +0,74: melhora provavel, nao
garantida); o ganho firme e parar de superestimar quem esta quente.

So vale pra quem esta na faixa medida: 5+ jogos no time e ate 20 pts por
jogo do time. Acima disso (titulares de verdade) o bot segue no modelo do
proximo jogo.
"""

from __future__ import annotations

COEF = {
    "const": 3.2876,
    "S": 0.3666,        # media da temporada x presenca recente
    "R5": 0.0616,       # ultimos 5 x presenca recente
    "R10": 0.1438,      # ultimos 10 x presenca recente
    "mu": 0.0392,       # previsao do proximo jogo (modelo.py) x presenca
    "sp": 0.187,        # projecao do Sleeper pro proximo jogo do time
    "sem_sp": -0.0099,  # o Sleeper nao projeta o proximo jogo
    "ant": 0.1537,      # media da temporada anterior (10+ jogos)
    "sem_ant": 1.8958,  # sem temporada anterior
    "pres10": -5.9698,  # fracao dos ultimos 10 jogos do time que ele jogou
    "pres": 1.8037,     # idem, desde o primeiro jogo dele no time
}
MIN_JOGOS = 5
TETO = 20.0             # media x presenca acima disso: fora da faixa medida


def prever(S: float, R5: float, R10: float, mu: float, pres10: float, pres: float,
           sp: float | None, ant: float | None) -> float:
    """Pontos esperados por jogo do time nas proximas ~6 semanas."""
    c = COEF
    v = (c["const"] + pres10 * (c["S"] * S + c["R5"] * R5 + c["R10"] * R10 + c["mu"] * mu)
         + (c["sp"] * sp if sp is not None else c["sem_sp"])
         + (c["ant"] * ant if ant is not None else c["sem_ant"])
         + c["pres10"] * pres10 + c["pres"] * pres)
    return max(0.0, v)
