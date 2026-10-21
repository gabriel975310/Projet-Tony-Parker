"""Lock-In: travar agora ou esperar? E quem escalar em cada slot hoje.

A regra da liga (Sleeper, modo Lock-In):
  * cada slot titular conta UM jogo por semana (segunda a domingo);
  * terminado o jogo, voce pode travar aquele placar — ate o proximo jogo
    DELE comecar. Depois disso aquele jogo nao esta mais disponivel;
  * so trava quem estava escalado no dia. Travado nao sai do slot;
  * slot que chega ao fim da semana sem trava vale ZERO. Na nossa liga em 2025,
    147 de 240 placares semanais tiveram pelo menos um slot zerado com um
    jogador que jogou na semana — o erro mais caro da liga.

TRAVAR E UM PROBLEMA DE PARADA OTIMA. O placar s que acabou de sair e
certo; esperar vale o que a semana ainda pode render naquele slot. E o que
ela pode render nao e so o proprio jogador nos proximos jogos: e o melhor
que se consegue com TODOS os candidatos que ainda jogam (o banco, outro
titular que nao precisa daquele slot). Por isso a conta e sempre do time
inteiro:

    travar  se   s + Plano(slots abertos - este, jogadores - ele)
                     >=  Plano(slots abertos, jogadores)

e o "limiar" mostrado na mensagem e exatamente a diferenca dos dois planos:
o placar a partir do qual travar compensa.

O PLANO de uma semana: cada slot aberto recebe um grupo de jogadores, e o
valor do slot e o de uma parada otima sobre os jogos do grupo em ordem
cronologica (num dia em que dois do grupo jogam, so um pode estar escalado,
entao conta o melhor dos dois). Com X ~ Normal(mu, sd) e chance p de jogar:

    V(depois do ultimo jogo) = 0
    V(dia) = max sobre quem joga no dia de  p*E[max(X, V(dia seguinte))]
                                             + (1-p)*V(dia seguinte)

Quem entra em qual grupo: primeiro um titular por slot (atribuicao exata),
depois cada jogador que sobrou vai pro grupo onde ele mais acrescenta. Nao
e a particao otima (essa e exponencial), mas e a mesma aproximacao pros
dois lados da comparacao, e a decisao e uma diferenca.

POSICAO. Travar alguem no UTIL fecha o slot que qualquer um aceitaria;
travar no C fecha um slot que so pivo ocupa. A conta acima ja enxerga isso
sozinha — o limiar do UTIL sai mais alto, porque o Plano sem o UTIL perde
mais. E por isso que a escalacao do dia poe quem tem mais chance de ser
travado (o confiavel, o que joga cedo) no slot mais restrito.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import erf, exp, pi, sqrt

from lineup import cabe, flexibilidade, otimizar


def _phi(z: float) -> float:
    return exp(-z * z / 2) / sqrt(2 * pi)


def _Phi(z: float) -> float:
    return 0.5 * (1 + erf(z / sqrt(2)))


def e_max(mu: float, sd: float, t: float) -> float:
    """E[max(X, t)] com X ~ Normal(mu, sd)."""
    if sd <= 1e-9:
        return max(mu, t)
    z = (mu - t) / sd
    return t + (mu - t) * _Phi(z) + sd * _phi(z)


def e_excesso(mu: float, sd: float, t: float) -> float:
    """E[(X - t)+]: quanto o jogo de hoje acrescenta se travar so quando
    passar de t. E o valor de um bilhete gratis — passar e de graca."""
    return e_max(mu, sd, t) - t


def p_acima(mu: float, sd: float, t: float) -> float:
    if sd <= 1e-9:
        return 1.0 if mu >= t else 0.0
    return 1 - _Phi((t - mu) / sd)


def chance_troca(o: "Opcao", limiar: float) -> float:
    """Chance de o slot abrir de novo depois do jogo de hoje: ele nao joga,
    ou joga e nao passa do limiar (PASSE). E a chance de ter que trocar."""
    return 1 - o.p * p_acima(o.mu, o.sd, limiar)


@dataclass(frozen=True)
class Opcao:
    """Um jogo futuro de um jogador, ja previsto."""
    pid: str
    dia: str
    inicio: datetime
    mu: float
    sd: float
    p: float


def valor_sequencia(opcoes, piso: float = 0.0) -> float:
    """Valor da parada otima sobre uma sequencia de jogos (um slot)."""
    por_dia: dict[str, list[Opcao]] = {}
    for o in opcoes:
        por_dia.setdefault(o.dia, []).append(o)
    V = piso
    for dia in sorted(por_dia, reverse=True):
        V = max([V] + [o.p * e_max(o.mu, o.sd, V) + (1 - o.p) * V
                       for o in por_dia[dia]])
    return V


def valor_semanal(mu: float, sd: float, p: float, n_jogos: float) -> float:
    """Quanto um jogador SOZINHO num slot rende por semana no Lock-In.

    n fracionario (media de 3,4 jogos por semana) interpola entre os
    inteiros vizinhos. E a moeda do draft e do valor de temporada.
    """
    def v(n):
        V = 0.0
        for _ in range(int(n)):
            V = p * e_max(mu, sd, V) + (1 - p) * V
        return V
    lo = int(n_jogos)
    f = n_jogos - lo
    return (1 - f) * v(lo) + f * v(lo + 1) if f > 1e-9 else v(lo)


# ==========================================================================
# Plano da semana
# ==========================================================================
def _titulares(slots, abertos, jog_ordenados, posicoes) -> dict[int, str]:
    """Um jogador por slot aberto, maximizando a soma dos valores-solo.

    Como o valor nao depende do slot (so de caber nele), o guloso e EXATO:
    em ordem de valor, cada jogador entra se ainda existir um encaixe pra
    todos os escolhidos (caminho aumentante). E a propriedade de matroide
    transversal — e roda ~100x mais rapido que a programacao dinamica, o
    que importa porque a escalacao do dia pede uma centena de planos.
    """
    dono: dict[int, str] = {}
    pos = {p: posicoes(p) for p in jog_ordenados}

    def aumenta(p, vistos):
        for i in abertos:
            if i in vistos or not cabe(pos[p], slots[i]):
                continue
            vistos.add(i)
            if i not in dono or aumenta(dono[i], vistos):
                dono[i] = p
                return True
        return False

    for p in jog_ordenados:
        if len(dono) == len(abertos):
            break
        aumenta(p, set())
    return dono

@dataclass
class Plano:
    valor: float
    grupos: dict[int, list[str]]


class Planejador:
    """Guarda slots, posicoes e opcoes futuras; memoriza os planos.

    A escalacao do dia pede ~100 planos (cada par jogador x slot); sem
    memoria, e a mesma conta refeita dezenas de vezes.
    """

    def __init__(self, slots: list[str], opcoes: dict[str, list[Opcao]], posicoes):
        self.slots = slots
        self.opcoes = {p: list(v) for p, v in opcoes.items() if v}
        self.posicoes = posicoes
        self._solo = {p: valor_sequencia(v) for p, v in self.opcoes.items()}
        self._memo: dict = {}

    def plano(self, abertos, fora: frozenset = frozenset()) -> Plano:
        chave = (tuple(sorted(abertos)), fora)
        if chave in self._memo:
            return self._memo[chave]
        abertos = sorted(abertos)
        jog = sorted((p for p in self.opcoes if p not in fora),
                     key=lambda p: -self._solo[p])
        dono = _titulares(self.slots, abertos, jog, self.posicoes)
        grupos = {i: ([dono[i]] if i in dono else []) for i in abertos}
        usados = set(dono.values())
        vals = {i: valor_sequencia(self._ops(g)) for i, g in grupos.items()}
        for p in jog:
            if p in usados:
                continue
            melhor, ganho = None, 1e-6
            for i, g in grupos.items():
                if not cabe(self.posicoes(p), self.slots[i]):
                    continue
                d = valor_sequencia(self._ops(g + [p])) - vals[i]
                if d > ganho:
                    melhor, ganho = i, d
            if melhor is not None:
                grupos[melhor].append(p)
                vals[melhor] += ganho
        pl = Plano(sum(vals.values()), grupos)
        self._memo[chave] = pl
        return pl

    def _ops(self, pids):
        return [o for p in pids for o in self.opcoes.get(p, [])]

    def limiar(self, abertos, i: int, pid: str, fora: frozenset = frozenset()) -> float:
        """Placar a partir do qual travar `pid` no slot i compensa."""
        com = self.plano(abertos, fora).valor
        sem = self.plano([x for x in abertos if x != i], fora | {pid}).valor
        return com - sem


# ==========================================================================
# Decisao de travar
# ==========================================================================
@dataclass
class Espera:
    """O que vale segurar o placar travavel em vez de travar ja.

    Segurar custa o mesmo que travar (o slot fica parado com ele) e guarda
    a escolha: depois dos jogos de outros, trava ele, trava em outro slot,
    ou solta o slot se o resto da semana pedir.
    """
    ganho: float               # valor esperado de esperar sobre travar ja no slot atual
    ate: datetime | None       # inicio do ultimo jogo que informa (decide depois dele)
    p_trava: float             # chance de, nesse ponto, ainda travar ele


@dataclass
class Trava:
    i: int
    slot: str
    pid: str
    pontos: float
    limiar: float              # no slot de destino
    travar: bool
    destino: int | None = None          # onde travar (None = onde ele esta)
    limiar_atual: float | None = None   # no slot onde ele esta
    segurar: bool = False
    espera: Espera | None = None
    parada: int | None = None           # segurado: slot onde fica (a escalacao escolhe)

    @property
    def margem(self) -> float:
        return self.pontos - self.limiar

    @property
    def j(self) -> int:
        return self.i if self.destino is None else self.destino

    @property
    def mover(self) -> bool:
        return self.j != self.i


def decidir_travas(slots, abertos, futuro: dict[str, list[Opcao]], posicoes,
                   candidatos: list[tuple[int, str, float]], mover: bool = True,
                   esperar: bool = False, ganho_espera: float = 0.5,
                   ganho_mover: float = 1.0, n_sim: int = 200) -> list[Trava]:
    """Decide cada (slot, jogador, placar) que pode ser travado agora.

    Varios na mesma noite interagem: travar o primeiro fecha um slot e muda
    o limiar do segundo. Resolve em sequencia, sempre pela maior margem.

    EM QUAL SLOT. Com `mover`, o placar pode ser travado em qualquer slot
    aberto em que ele cabe, nao so onde esta: o melhor e o de MENOR limiar,
    o que o resto da semana menos sente (o C, se sobra pivo; o UTIL, se
    ninguem mais precisa dele). Slot de outro candidato da noite fica fora.
    So muda de slot se ganhar `ganho_mover`: a escalacao do dia ja poe cada
    um no slot certo, e diferenca menor e ruido do plano (backtest 2025:
    escolher o slot sem margem nao rendeu nada).

    SEGURAR. Com `esperar`, quem ja passou do limiar pode esperar: o placar
    vale ate o proximo jogo dele, e jogos de outros antes disso podem mudar
    a conta (outro explode e fecha o UTIL; o G vira o slot que o resto da
    semana disputa). Segurar custa o mesmo que travar — o slot fica parado
    com ele —, entao so ganha. `simular_espera` mede quanto; se passa de
    `ganho_espera`, segura e decide depois. Abaixo do limiar nao segura:
    parar o slot como rede de seguranca perdeu no backtest de 2025.
    """
    pl = Planejador(slots, futuro, posicoes)
    ab = list(abertos)
    fora: frozenset = frozenset()
    pend = list(candidatos)
    out: list[Trava] = []

    def destinos(i, pid, ab, ocupados):
        return [j for j in ab if j == i or (mover and j not in ocupados
                                            and cabe(posicoes(pid), slots[j]))]

    while pend:
        avals = []
        for (i, pid, pts) in pend:
            ocupados = {c[0] for c in pend if c[1] != pid}
            lims = {j: pl.limiar(ab, j, pid, fora) for j in destinos(i, pid, ab, ocupados)}
            # empate fica onde esta: mover e um passo a mais no app
            j = min(lims, key=lambda x: (round(lims[x], 2), x != i))
            if lims[i] - lims[j] < ganho_mover:
                j = i
            avals.append((pts - lims[j], i, pid, pts, j, lims[j], lims[i]))
        avals.sort(key=lambda a: -a[0])
        m, i, pid, pts, j, lim, lim_i = avals[0]
        out.append(Trava(i, slots[i], pid, pts, lim, m >= 0,
                         destino=j if j != i else None, limiar_atual=lim_i))
        pend = [c for c in pend if c[1] != pid]
        if m >= 0:
            ab = [x for x in ab if x != j]
            fora = fora | {pid}

    if esperar:
        for t in out:
            if not t.travar:
                continue
            # o resto da noite como decidido; so este volta pra mesa
            esp = simular_espera(slots, sorted(set(ab) | {t.j}), t.i, t.pid, t.pontos,
                                 futuro, posicoes, fora - {t.pid}, mover, n_sim)
            t.espera = esp
            # relativo a travar ja onde ele esta; mover agora ja ganharia isto
            mover_ja = t.limiar_atual - t.limiar
            if esp.ate is not None and esp.ganho > mover_ja + ganho_espera:
                t.travar, t.segurar = False, True
    return out


def simular_espera(slots, abertos, i: int, pid: str, pts: float,
                   futuro: dict[str, list[Opcao]], posicoes, fora: frozenset = frozenset(),
                   mover: bool = True, n: int = 200, seed: int = 7) -> Espera:
    """Valor de segurar `pid` (placar `pts`, no slot i) e decidir depois.

    O prazo e o proximo jogo dele (sem proximo, o fim da semana). Ate la,
    o slot i fica parado com ele, como se travado; os outros slots seguem a
    escalacao do bot dia a dia, sorteando os placares, e travam quando
    passam do limiar. Em cada ponto de decisao (fim de um dia com jogo que
    informa) o melhor entre travar em i, travar em outro slot ou soltar i
    e comparado com travar em i. O ganho e o do melhor ponto FIXO — o bot
    reavalia de hora em hora, entao o valor real e pelo menos esse.
    """
    import random

    proximo = min((o.dia for o in futuro.get(pid, [])), default=None)
    outros = {p: v for p, v in futuro.items() if p != pid and p not in fora and v}
    dias = sorted({o.dia for v in outros.values() for o in v
                   if proximo is None or o.dia < proximo})
    if not dias or i not in abertos:
        return Espera(0.0, None, 1.0)

    memo_esc: dict = {}
    memo_dec: dict = {}

    def escala(k, ab, fo):
        if (k, ab, fo) not in memo_esc:
            d = dias[k]
            hoje = {p: o for p, v in outros.items() if p not in fo for o in v if o.dia == d}
            fut = {p: [o for o in v if o.dia > d] for p, v in outros.items() if p not in fo}
            e = escalar_dia(slots, list(ab), {}, hoje, fut, posicoes, [None] * len(slots))
            memo_esc[(k, ab, fo)] = [(j, hoje[p], e.limiar[j]) for j, p in e.hoje.items()]
        return memo_esc[(k, ab, fo)]

    def decide(k, ab, fo):
        """(ganho sobre travar em i, travou ele) depois dos jogos do dia k."""
        if (k, ab, fo) not in memo_dec:
            ops = {p: [o for o in v if o.dia > dias[k]] for p, v in futuro.items()
                   if p not in fo and p not in fora}
            pl = Planejador(slots, ops, posicoes)
            abi = sorted(set(ab) | {i})
            solta = pl.plano(abi, fo).valor
            trava = {j: pts + pl.plano([x for x in abi if x != j], fo | {pid}).valor
                     for j in abi if j == i or (mover and cabe(posicoes(pid), slots[j]))}
            melhor = max(trava.values())
            memo_dec[(k, ab, fo)] = (max(solta, melhor) - trava[i], melhor >= solta)
        return memo_dec[(k, ab, fo)]

    rng = random.Random(seed)
    ganho = [0.0] * len(dias)
    trava = [0] * len(dias)
    ab0 = tuple(x for x in abertos if x != i)
    for _ in range(n):
        ab, fo = ab0, frozenset()
        for k in range(len(dias)):
            for j, o, lim in escala(k, ab, fo):
                if rng.random() < o.p and rng.gauss(o.mu, o.sd) >= lim:
                    ab = tuple(x for x in ab if x != j)
                    fo = fo | {o.pid}
            g, tr = decide(k, ab, fo)
            ganho[k] += g
            trava[k] += tr
    k = max(range(len(dias)), key=lambda x: ganho[x])
    ultimo = max(o.inicio for v in outros.values() for o in v if o.dia == dias[k])
    return Espera(ganho[k] / n, ultimo, trava[k] / n)


# ==========================================================================
# Escalacao do dia
# ==========================================================================
@dataclass
class Escalacao:
    lineup: list[str | None]          # slot -> jogador (so os slots titulares)
    hoje: dict[int, str]              # slots com quem joga hoje
    ganho: dict[int, float]           # valor esperado de cada bilhete
    limiar: dict[int, float]          # placar que manda travar
    valor_semana: float
    segurados: dict[int, str] = field(default_factory=dict)   # slot -> quem esta parado nele


def escalar_dia(slots: list[str], abertos: list[int], congelados: dict[int, str],
                hoje: dict[str, Opcao], futuro: dict[str, list[Opcao]], posicoes,
                atual: list[str | None]) -> Escalacao:
    """Quem ocupa cada slot aberto HOJE.

    So joga hoje quem tem jogo hoje que ainda nao comecou (`hoje`). O resto
    da semana (`futuro`) define o limiar de cada slot. Colocar alguem num
    slot hoje e um bilhete gratis: se ele passar do limiar, trava; se nao,
    passa e o slot continua aberto. O ganho do bilhete e E[(X - limiar)+].

    `congelados` sao slots abertos cujo ocupante ja comecou o jogo de hoje:
    ninguem mexe neles ate amanha.
    """
    pl = Planejador(slots, futuro, posicoes)
    livres = [i for i in abertos if i not in congelados]
    lim: dict[tuple[str, int], float] = {}

    def ganho(pid, i):
        if i not in livres:
            return None
        o = hoje[pid]
        if (pid, i) not in lim:
            lim[(pid, i)] = pl.limiar(abertos, i, pid)
        t = lim[(pid, i)]
        return o.p * e_excesso(o.mu, o.sd, t)

    jog = [p for p in hoje if p not in congelados.values()]
    assign, total = otimizar(slots, jog, ganho, posicoes,
                             fixos={i: p for i, p in congelados.items()})

    hoje_sl = {i: p for i, p in enumerate(assign) if p and i in livres}
    hoje_sl = _tarde_no_flexivel(slots, hoje_sl, hoje, posicoes, ganho)

    # O resto dos slots: quem ja estava fica, se nao foi usado em outro lugar.
    usados = set(hoje_sl.values()) | set(congelados.values())
    lineup: list[str | None] = []
    for i, s in enumerate(slots):
        if i in hoje_sl:
            lineup.append(hoje_sl[i])
        elif i in congelados:
            lineup.append(congelados[i])
        else:
            a = atual[i] if i < len(atual) else None
            if a and a not in usados and (i not in abertos or cabe(posicoes(a), s)):
                lineup.append(a)
                usados.add(a)
            else:
                lineup.append(None)

    base = pl.plano(abertos).valor
    g = {i: ganho(p, i) or 0.0 for i, p in hoje_sl.items()}
    return Escalacao(lineup, hoje_sl, g,
                     {i: lim.get((p, i), pl.limiar(abertos, i, p)) for i, p in hoje_sl.items()},
                     base + sum(g.values()))


def _tarde_no_flexivel(slots, hoje_sl: dict[int, str], hoje: dict[str, Opcao],
                       posicoes, ganho, tolerancia: float = 0.1) -> dict[int, str]:
    """Quem joga mais tarde vai pro slot mais flexivel, se custar quase nada.

    Se o jogo das 22h30 for cancelado pra alguem (descartado no aquecimento),
    o slot dele ainda pode receber outro do banco — mas so se o slot aceitar
    muita gente. Um pivo das 22h30 no UTIL deixa qualquer reserva entrar; no
    C, so outro pivo. E a mesma regra do FLEX da NFL.
    """
    out = dict(hoje_sl)
    mudou = True
    while mudou:
        mudou = False
        for i in list(out):
            for j in list(out):
                a, b = out[i], out[j]
                if i == j or flexibilidade(slots[i]) <= flexibilidade(slots[j]):
                    continue
                # i e mais flexivel que j; queremos o mais TARDE em i
                if hoje[b].inicio <= hoje[a].inicio:
                    continue
                if not (cabe(posicoes(b), slots[i]) and cabe(posicoes(a), slots[j])):
                    continue
                antes = (ganho(a, i) or 0) + (ganho(b, j) or 0)
                depois = (ganho(b, i) or 0) + (ganho(a, j) or 0)
                if depois >= antes - tolerancia:
                    out[i], out[j] = b, a
                    mudou = True
    return out


def valor_bilhetes(slots: list[str], abertos: list[int], congelados: dict[int, str],
                   hoje: dict[str, Opcao], futuro: dict[str, list[Opcao]], posicoes,
                   lineup: list[str | None]) -> float:
    """Valor esperado dos jogos de hoje com uma escalacao DADA (a atual).

    E a regua pra dizer quanto a troca sugerida ganha: mesma conta do
    `escalar_dia`, so que sem escolher ninguem.
    """
    pl = Planejador(slots, futuro, posicoes)
    tot = 0.0
    for i in abertos:
        p = lineup[i] if i < len(lineup) else None
        if i in congelados or p not in hoje or not cabe(posicoes(p), slots[i]):
            continue
        o = hoje[p]
        tot += o.p * e_excesso(o.mu, o.sd, pl.limiar(abertos, i, p))
    return tot
