"""A noite inteira, de hora em hora: travar, escalar, pegar.

Tres decisoes, todas pelo mesmo plano da semana (lockin.py):

  1. TRAVAR OU PASSAR — pra cada titular cujo jogo acabou e ainda nao foi
     travado. Vale ate o proximo jogo dele comecar. Passar e escolher que a
     semana ainda rende mais naquele slot: com ele mesmo nos proximos jogos
     ou com outro que entra no lugar. SEGURAR e deixar o placar travavel
     ate perto do prazo, quando os jogos dos outros antes disso podem mudar
     a conta. E o slot pode ser outro: o que menos custa ao resto da semana.
  2. ESCALACAO DE HOJE — quem ocupa cada slot aberto nos jogos que ainda
     nao comecaram. Slot cujo ocupante jogou cedo e PASSOU fica livre de
     novo na mesma noite: da pra por quem joga as 22h30 no lugar.
  3. PEGAR AGORA — livre que joga HOJE, antes do tip-off dele (depois do
     jogo comecar ele vai pra waiver), entrando num slot aberto no lugar de
     quem menos faz falta. Cobra o resto da temporada de quem sai.

Rode de 15 em 15 minutos com --gate. So sai mensagem quando ha algo pra
fazer AGORA no app:

  * SUBSTITUICAO: um reserva que joga hoje pode entrar no slot de quem ja
    jogou ou esta em quadra. Sai pelo menos 15 minutos antes do tip-off do
    reserva, com o placar ao vivo de cada titular em que ele cabe;
  * a escalacao do dia (ou uma mudanca nela) e livre pra pegar antes do jogo;
  * FIM DA NOITE: quando o ultimo jogo da rodada acaba, todas as travas da
    noite de uma vez, o relatorio de waiver e a enquete.

Trava sozinha nao manda mensagem: o placar vale ate o proximo jogo do
jogador, entao espera o fim da noite. Toda mensagem traz a lista inteira de
travas ate ali, com 🆕 no que entrou desde a ultima.
Sem --gate (na mao), mostra o painel inteiro da semana.

    python run_escalacao.py --dry-run
    python run_escalacao.py --dry-run --now "2026-10-22T23:30"
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta

from agenda import DURACAO, ET
from clients import CACHE, esc
from lineup import cabe, diff
from lockin import (Planejador, chance_superar, chance_troca, decidir_travas, e_excesso,
                    escalar_dia, p_acima, projecao_ao_vivo, valor_bilhetes)
from contexte import base_parser, build, ja_avisado, marcar_avisado, run_for_each_league
import enquete
from report import bloco, n, rodape, secao, titulo
from previsao_waiver import VIES_LIVRE
from semana import (decisoes, estado, jogos_hoje, opcoes_futuras, registrar_decisao,
                    semanas_horizonte, valor_temporada)

AVISOS = CACHE / "escalacao_avisos.json"

# Ate quantas horas antes do primeiro tip-off a escalacao do dia sai. A
# mensagem de manha seria velha a noite (GTD vira Out perto do jogo); a
# reavaliacao de hora em hora so reenvia se a recomendacao MUDAR.
JANELA_ESCALACAO_H = 6

# A substituicao sai quando faltam ate tantos minutos pro tip-off do reserva.
# Com uma passada a cada 15 minutos, a primeira dentro da janela cai entre 35
# e 20 minutos antes: sobra pelo menos 15 pra mexer no app.
JANELA_TROCA_MIN = 35

# A escalacao do dia sai uma vez so. Se ate tantos minutos antes do prazo a
# troca ainda nao foi feita no Sleeper, sai um lembrete (o KAT ficou no banco
# no jogo das 20h de 23/10/2026: a troca estava avisada desde a manha). De
# 15 em 15 minutos, o primeiro cai entre 45 e 30 minutos antes; se um
# disparo falhar, o seguinte ainda chega com 15.
JANELA_LEMBRETE_MIN = 45

SUFIXOS = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def sobrenome(nome: str) -> str:
    """"Darius Acuff Jr." -> "Acuff" (nao "Jr.")."""
    partes = nome.split()
    while len(partes) > 1 and partes[-1].lower() in SUFIXOS:
        partes.pop()
    return partes[-1] if partes else nome


def dia_rel(ctx, dt) -> str:
    """"hoje 20:00", "amanhã 17:30", "dom 19:00"."""
    d = (dt.astimezone(ctx.tz).date() - ctx.agora.astimezone(ctx.tz).date()).days
    dia = {0: "hoje", 1: "amanhã"}.get(d) or ctx.hora(dt, "%a")
    return f"{dia} {ctx.hora(dt, '%H:%M')}"


# ==========================================================================
# 1. travar ou passar
# ==========================================================================
def candidatos_trava(ctx, est) -> list[tuple[int, str, float]]:
    """(slot, jogador, placar) que da pra travar AGORA."""
    out = []
    usados = est.usados()
    for i in est.abertos:
        p = est.atual[i]
        if not p or p in usados or est.em_andamento(p, ctx.agora):
            continue
        j = est.ultimo_encerrado(p, ctx.agora)
        if not j:
            continue
        # o proximo jogo dele ja comecou: aquele placar nao esta mais disponivel
        if any(x.comecou(ctx.agora) for x in est.jogos.get(p, []) if x.data > j.data):
            continue
        pts = est.placar.get((p, j.data))
        if pts is None:
            # Sem linha dele, mas com linha de outro do mesmo jogo: ele nao
            # entrou em quadra (DNP). Nao ha o que travar, o slot segue aberto.
            # Sem linha de NINGUEM do jogo: a estatistica ainda nao saiu.
            jogo_tem_stats = any(x.data == j.data and x.team == ctx.team(p)
                                 for js in ctx.logs.values() for x in js[-3:])
            if not jogo_tem_stats:
                ctx.stats_pendentes = True
                ctx.warnings.append(f"{ctx.name(p)} acabou de jogar e a estatistica "
                                    f"ainda nao saiu; decido na proxima passada")
            continue
        out.append((i, p, pts))
    return out


def travas(ctx, est):
    cands = candidatos_trava(ctx, est)
    if not cands:
        return []
    # so jogo que ainda nao comecou e futuro: o que acabou de acabar sai
    futuro = opcoes_futuras(ctx, est, est.elenco)
    lk = ctx.cfg.get("lock") or {}
    return decidir_travas(est.slots, est.abertos, futuro, ctx.posicoes, cands,
                          mover=bool(lk.get("mover_slot", True)), esperar=True,
                          ganho_espera=float(lk.get("ganho_minimo_espera", 0.5)),
                          ganho_mover=float(lk.get("ganho_minimo_mover", 1.0)))


def quem_ocupa(ctx, est, decs) -> dict[str, tuple[str, float]]:
    """{jogador: (quem o plano da semana poe no slot se ele PASSAR, chance de
    o slot terminar acima do placar dele)}.

    Nao e todo mundo que cabe no slot: o plano da cada jogador ao slot em
    que ele mais rende, entao quem provavelmente trava em outro lugar (o
    Giddey no G, o Acuff no UTIL) nao aparece como reserva do SG. O proprio
    jogador entra, com os proximos jogos dele: passar e esperar por ele
    tambem.
    """
    fechados = {t.j for t in decs if t.travar} | {t.i for t in decs if t.segurar}
    fora = frozenset(t.pid for t in decs if t.travar or t.segurar)
    futuro = opcoes_futuras(ctx, est, est.elenco)
    pl = Planejador(est.slots, futuro, ctx.posicoes)
    out = {}
    for t in decs:
        if t.segurar:
            continue
        slot = t.j if t.travar else t.i
        ab = [x for x in est.abertos if x not in fechados or x == slot]
        grupo = pl.plano(ab, fora - {t.pid}).grupos.get(slot) or []
        nomes, ops = [], []
        for p in grupo:
            prox = est.restantes(p, ctx.agora)
            ops += futuro.get(p, [])
            if prox:
                nomes.append((prox[0].inicio, f"{sobrenome(ctx.name(p))} "
                                              f"{dia_rel(ctx, prox[0].inicio).split()[0]}"))
        if nomes:
            out[t.pid] = (", ".join(x for _, x in sorted(nomes)), chance_superar(ops, t.pontos))
    return out


def linhas_travas(ctx, est, decs, ocupa: dict[str, tuple[str, float]] | None = None,
                  novos=frozenset()) -> list[str]:
    """TRAVE e SEGURE um por um (pedem acao no app); PASSE junto no fim,
    numa linha cada: passar e nao fazer nada."""
    L, passes = [], []
    for t in decs:
        nome = esc(ctx.name(t.pid))
        novo = "🆕 " if t.pid in novos else ""
        quem, chance = (ocupa or {}).get(t.pid) or ("", 0.0)
        prox = est.restantes(t.pid, ctx.agora)
        prazo = (f"até {dia_rel(ctx, prox[0].inicio)}, quando ele joga de novo" if prox
                 else "até o fim da semana")
        duvida = not t.segurar and abs(t.margem) < float((ctx.cfg.get("lock") or {}).get("margem_duvida", 1.5))
        destino = est.slots[t.j]
        if not t.segurar and not t.travar:
            passes.append((t, nome, novo, duvida))
            continue
        if t.segurar:
            esp = t.espera
            h = t.i if t.parada is None else t.parada
            L.append(f"{novo}⏸ <b>SEGURE {nome}</b> no <code>{est.slots[h]}</code> · não trave ainda")
            if h != t.i:
                L.append(f"      🧭 <i>mova do <code>{t.slot}</code> pro <code>{est.slots[h]}</code>: "
                         f"é o slot que hoje e o resto da semana menos precisam</i>")
            L.append(f"      🎯 {n(t.pontos)} pts · já passou do limiar (~{n(t.limiar)}), "
                     f"mas ver os outros jogarem antes vale <b>{n(esp.ganho, 1, True)}</b>")
            L.append(f"      🔭 <i>decido depois dos jogos de {ctx.hora(esp.ate, '%a')}: "
                     f"{round(100 * esp.p_trava)}% de chance de travar ele</i>")
            L.append("      ⚠️ <i>não tire ele do slot, senão o placar se perde</i>")
        else:
            j = est.ultimo_encerrado(t.pid, ctx.agora)
            de = f" <i>(jogo de {ctx.hora(j.inicio, '%a')})</i>" if j else ""
            # 👉, nao 🔒: o cadeado fechado e so de quem ja travou (28/10/2026)
            L.append(f"{novo}👉 <b>TRAVE {nome}</b> no <code>{destino}</code> · "
                     f"<b>{n(t.pontos)}</b> pts{de}")
            # Sem nomes: qualquer um que cabe no slot pode entrar nele; o
            # plano so diz quem provavelmente entra (23/10/2026).
            if quem:
                L.append(f"      <i>se esperar, o {destino} rende ~{n(t.limiar)} no resto da semana: "
                         f"{'só ' if chance < 0.5 else ''}{round(100 * chance)}% de chance "
                         f"de passar de {n(t.pontos)}</i>")
            else:
                L.append(f"      <i>esperar rende ~{n(t.limiar)}</i>")
            if t.mover:
                L.append(f"      🧭 <i>mova do <code>{t.slot}</code> pro <code>{destino}</code> antes: "
                         f"lá o resto da semana perde ~{n(t.limiar)}, no {t.slot} ~{n(t.limiar_atual)}</i>")
                plano_b = ("trave no " + t.slot if t.pontos >= t.limiar_atual else "não trave")
                L.append(f"      <i>(se o Sleeper não deixar mover, {plano_b})</i>")
        L.append(f"      <i>⏰ {esc(prazo)}" + (" · 🪙 cara ou coroa" if duvida else "") + "</i>")
        L.append("")
    if passes:
        L.append("⏭ <b>NÃO TRAVE</b> <i>· não precisa fazer nada</i>")
        for t, nome, novo, duvida in passes:
            L.append(f"      {novo}<code>{t.slot}</code> {nome} {n(t.pontos)} "
                     f"<i>· o slot ainda rende ~{n(t.limiar)}"
                     + (" · 🪙 cara ou coroa" if duvida else "") + "</i>")
        L.append("      <i>é o placar do último jogo deles; sem travar, o slot "
                 "continua aberto pros próximos jogos</i>")
        L.append("")
    return L


# ==========================================================================
# 2. escalacao de hoje
# ==========================================================================
def escalacao_hoje(ctx, est, decs):
    """(Escalacao, mudancas, ganho, valor_atual, congelados, abertos, usados)."""
    travou = {t.j: t.pid for t in decs if t.travar}
    segura = [t for t in decs if t.segurar]
    base = [i for i in est.abertos if i not in travou]
    novos = set(travou.values()) | {t.pid for t in segura}
    usados = est.usados() | novos
    livres = [p for p in est.elenco if p not in usados]
    # ocupante com jogo em andamento: o slot dele so mexe amanha
    congelados = {i: est.atual[i] for i in base
                  if est.atual[i] and est.em_andamento(est.atual[i], ctx.agora)}
    hoje = jogos_hoje(ctx, est, livres)
    futuro = opcoes_futuras(ctx, est, livres, so_depois_de_hoje=True)
    # quem acabou de travar ou esta segurado nao fica onde estava por inercia
    atual = [None if p in novos else p for p in est.atual]

    remanejar = float((ctx.cfg.get("escalacao") or {}).get("ganho_minimo_remanejar", 2.0))

    def escala(parados: dict[int, str]):
        ab = [i for i in base if i not in parados]
        return escalar_dia(est.slots, ab, congelados, hoje, futuro, ctx.posicoes, atual,
                           remanejar=remanejar)

    parados = estacionar(ctx, est, segura, base, congelados, escala)
    abertos = [i for i in base if i not in parados]
    e = escala(parados)
    _poe_parados(ctx, est, e, parados, travou)
    atual_val = valor_bilhetes(est.slots, abertos, congelados, hoje, futuro,
                               ctx.posicoes, est.atual)
    # so interessa trocar slot aberto em que alguem de hoje entra ou sai, ou
    # estacionar um segurado
    seg = set(parados.values())
    mud = [(i, s, a, b) for (i, s, a, b) in diff(est.slots, est.atual, e.lineup)
           if i in base and i not in congelados
           and (a in hoje or b in hoje or a in seg or b in seg)]
    ganho = sum(e.ganho.values()) - atual_val
    return e, mud, ganho, hoje, futuro, abertos, congelados, usados


def estacionar(ctx, est, segura, base, congelados, escala) -> dict[int, str]:
    """Em qual slot cada segurado fica parado: {slot: jogador}.

    Segurado nao joga, mas ocupa um slot ate travar ou soltar. O melhor e o
    que a semana menos sente fechado: os bilhetes de hoje mais o plano do
    resto. Na hora de travar ele ainda pode ir pra outro slot. Empate fica
    onde esta.
    """
    if not segura:
        return {}
    from itertools import product
    opcoes = [[h for h in base if h not in congelados
               and cabe(ctx.posicoes(t.pid), est.slots[h])] or [t.i] for t in segura]
    melhor, chave = {}, None
    for combo in product(*opcoes):
        if len(set(combo)) < len(combo):
            continue
        parados = dict(zip(combo, (t.pid for t in segura)))
        v = escala(parados).valor_semana
        k = (round(v, 2), sum(h == t.i for h, t in zip(combo, segura)))
        if chave is None or k > chave:
            melhor, chave = parados, k
    for t in segura:
        t.parada = next(h for h, p in melhor.items() if p == t.pid)
    return melhor


def _poe_parados(ctx, est, e, parados: dict[int, str], travou: dict[int, str]) -> None:
    """Escalacao final com quem trava e quem segura no slot certo; quem
    estava no slot de um segurado vai pro slot que ele deixou, se couber."""
    e.segurados = dict(parados)
    for h, p in list(parados.items()) + list(travou.items()):
        sai = e.lineup[h]
        e.lineup[h] = p
        for k, q in enumerate(e.lineup):
            if q == p and k != h:
                cabe_ali = (sai and sai not in e.lineup and k not in est.travados
                            and cabe(ctx.posicoes(sai), est.slots[k]))
                e.lineup[k] = sai if cabe_ali else None


def prazo_mudancas(mud, hoje):
    """O primeiro tip-off entre quem entra e quem sai: mexa antes disso."""
    prazos = [hoje[p].inicio for (_, _, a, b) in mud for p in (a, b) if p in hoje]
    return min(prazos, default=None)


def linhas_mudancas(ctx, mud, hoje, segurados=()) -> list[str]:
    L = []
    for (i, slot, a, b) in mud:
        sai = esc(ctx.name(a)) if a else "vazio"
        if b:
            o = hoje.get(b)
            hora = (f" <i>{ctx.hora(o.inicio, '%H:%M')}</i>" if o
                    else " <i>⏸ segurado, não trave</i>" if b in segurados else "")
            L.append(f"🔄 <code>{slot}</code> ❌ {sai} → ✅ <b>{esc(ctx.name(b))}</b>{hora}")
        else:
            L.append(f"🔄 <code>{slot}</code> ❌ {sai} <i>(ninguém joga nesse dia nesse slot)</i>")
    return L


def linhas_lembrete(ctx, mud, hoje) -> list[str]:
    L = secao("⏰", "Falta mexer no Sleeper", "a escalação ainda está como antes")
    for (i, slot, a, b) in mud:
        sai = f"tire {esc(ctx.name(a))}, " if a else ""
        entra = f"ponha <b>{esc(ctx.name(b))}</b>" if b else "deixe vazio"
        L.append(f"      • <code>{slot}</code>: {sai}{entra} · "
                 f"<b>até {ctx.hora(prazo_mudancas([(i, slot, a, b)], hoje), '%H:%M')}</b>")
    L.append("")
    return L


def linhas_plano_dia(ctx, est, e, mud, hoje, decs=(), titulo_txt="Escalação de hoje"
                     ) -> list[str]:
    """O plano do dia em topicos: o que mexer agora e o alvo de cada um.
    Mesma forma de manha e de hora em hora. Quem entra no lugar de quem nao
    chegar sai na hora (substituicoes), com o placar de verdade: de manha
    "troque o Giddey pelo Acuff" so confunde.

    Quem esta pra travar o placar de um jogo anterior nao entra: o jogo de
    hoje dele nao conta, e o lembrete fica no painel da semana.
    """
    L = secao("🏀", titulo_txt)
    trava = {t.pid for t in decs if t.travar}
    prazo = prazo_mudancas(mud, hoje)
    if mud:
        ate = f"até {ctx.hora(prazo, '%H:%M')}" if prazo else "antes do jogo"
        L.append(f"🔄 <b>Mexa no Sleeper {ate}:</b>")
        for (i, slot, a, b) in mud:
            sai = f"tire {esc(ctx.name(a))}, " if a else ""
            entra = f"ponha <b>{esc(ctx.name(b))}</b>" if b else "deixe vazio"
            L.append(f"      • <code>{slot}</code>: {sai}{entra}")
    else:
        L.append("✅ <b>Nada pra mexer:</b> o Sleeper já está com a escalação certa.")
    L.append("")

    jogam = sorted(e.hoje.items(), key=lambda kv: hoje[kv[1]].inicio)
    if jogam:
        L.append("🎯 <b>Jogam hoje</b> <i>· trave se fizer (chance de chegar)</i>")
        for i, p in jogam:
            o, lim = hoje[p], e.limiar.get(i)
            hora = ctx.hora(o.inicio, "%H:%M")
            if lim is None:
                alvo = f"média {n(o.mu)}"
            elif lim < 0.5:
                alvo = "<b>trave o que vier</b>"
            else:
                alvo = (f"<b>{n(lim)}+</b> "
                        f"({round(100 * (1 - chance_troca(o, lim)))}%)")
            L.append(f"      • <code>{est.slots[i]}</code> {esc(ctx.name(p))} "
                     f"<i>{hora}</i> → {alvo}")
            jogo = next((j for j in est.jogos.get(p, []) if j.data == o.dia), None)
            L += [f"         {x.strip()}" for x in noticia(ctx, p, jogo)]
        L.append("")

    if any((e.limiar.get(i) or 0) >= 0.5 for i in e.hoje):
        L.append("<i>ℹ️ fez menos que o alvo? não trave. Se alguém do banco puder "
                 "entrar no lugar, eu aviso na hora.</i>")
        L.append("")

    fora = [f"<code>{est.slots[i]}</code> {esc(ctx.name(e.lineup[i])) if e.lineup[i] else 'vazio'}"
            for i in est.abertos
            if i < len(e.lineup) and i not in e.hoje and i not in e.segurados
            and e.lineup[i] not in trava]
    if fora:
        L.append("💤 <i>não jogam hoje:</i> " + " · ".join(fora))
    for i, p in e.segurados.items():
        L.append(f"⏸ <code>{est.slots[i]}</code> {esc(ctx.name(p))} · <i>segurado, não trave ainda</i>")
    return L


def noticia(ctx, pid: str, jogo=None) -> list[str]:
    """A noticia de lesao do jogador, recuada, se ele tem status."""
    les = ctx.lesoes.get(pid) or {}
    st = ctx.modelo.status(pid)
    if not st or not les.get("nota"):
        return []
    from modelo import noticia_do_jogo
    leitura = noticia_do_jogo(les, jogo)
    marca = {"nao joga": "🔴", "joga": "🟢", "duvida": "🟡"}.get(leitura or "", "📰")
    txt = les["nota"]
    txt = txt if len(txt) <= 170 else txt[:167] + "..."
    return [f"      {marca} <i>{esc(st)}: {esc(txt)}</i>"]


# ==========================================================================
# 2b. substituicao no meio da noite
# ==========================================================================
def _relogio(j, agora) -> str:
    """"faltam 4:12 no 4º", "intervalo", ou "faltam ~20 min de jogo" sem o
    relogio da ESPN (que e regressivo, como o da TV)."""
    if j.estado == "in" and j.periodo and j.relogio is not None:
        mm, ss = divmod(int(j.relogio), 60)
        quarto = f"{j.periodo}º" if j.periodo <= 4 else "prorrogação"
        if j.periodo == 2 and j.relogio <= 0:
            return "intervalo"
        return f"faltam {mm}:{ss:02d} no {quarto}"
    return f"faltam ~{round(j.restante_min(agora))} min de jogo"


def substituicoes(ctx, est, decs, hoje, e, minimo: float) -> list[dict]:
    """Reserva que joga hoje e pode entrar no slot de quem ja foi pra quadra.

    Pra cada reserva com tip-off nos proximos `JANELA_TROCA_MIN` minutos,
    olha cada slot aberto em que ele cabe e cujo titular ja jogou hoje ou
    esta jogando:
      * acabou e o bot manda PASSAR: o slot ja esta livre, troque agora;
      * em quadra: o placar final dele e previsto AO VIVO (o que ja fez + a
        media no tempo que falta), e a chance de o slot abrir e a de ele
        fechar abaixo do limiar. So conta se o jogo dele deve acabar antes
        do tip-off do reserva: titular em quadra nao sai do slot.
    O reserva vai pro slot de maior ganho esperado (chance de abrir x o que
    ele rende la), um reserva por slot.
    """
    fechados = ({t.j for t in decs if t.travar}
                | {t.i if t.parada is None else t.parada for t in decs if t.segurar})
    fora = frozenset(t.pid for t in decs if t.travar or t.segurar)
    ab = [i for i in est.abertos if i not in fechados]
    passa = {t.pid: t for t in decs if not t.travar and not t.segurar}
    trava = {t.j if t.travar else (t.i if t.parada is None else t.parada): t
             for t in decs if t.travar or t.segurar}
    escalados = set(e.hoje.values()) | {p for p in est.atual if p}
    banco = sorted((p for p, o in hoje.items() if p not in escalados
                    and timedelta(0) < o.inicio - ctx.agora <= timedelta(minutes=JANELA_TROCA_MIN)),
                   key=lambda p: hoje[p].inicio)
    if not banco:
        return []
    pl = Planejador(est.slots, opcoes_futuras(ctx, est, est.elenco), ctx.posicoes)
    out, ocupados = [], set()
    for b in banco:
        ob = hoje[b]
        opcoes = []
        for i in est.abertos:
            if not cabe(ctx.posicoes(b), est.slots[i]):
                continue
            a = est.atual[i]
            if i in trava:
                t = trava[i]
                opcoes.append({"i": i, "a": t.pid, "viavel": False,
                               "txt": f"{n(t.pontos)} · {'trave' if t.travar else 'segure'}: fica com ele"})
                continue
            jogo_a = next((j for j in est.jogos.get(a, []) if a and j.data == ob.dia
                           and j.comecou(ctx.agora)), None)
            if not jogo_a:
                continue          # nao joga hoje, ou joga depois: e a escalacao do dia
            lim = pl.limiar(ab, i, a, fora)
            if jogo_a.acabou(ctx.agora):
                t = passa.get(a)
                feito = t.pontos if t else est.placar.get((a, jogo_a.data))
                if t is None and feito is not None:
                    continue      # tem placar e nao tem decisao: ja travado
                p_abre, fim, fim90, prev = 1.0, ctx.agora, ctx.agora, feito
                txt = (f"acabou com {n(feito)} · passe" if feito is not None
                       else "acabou sem jogar")
            else:
                pr = ctx.modelo.jogo(a, jogo_a)
                feito = est.feito_ao_vivo(a, jogo_a, ctx.agora)
                falta = jogo_a.restante_min(ctx.agora)
                ult = [j.min for j in (ctx.logs.get(a) or [])[-15:] if j.data < jogo_a.data]
                anos = (ctx.pool.get(a) or {}).get("years_exp")
                prev, sd = projecao_ao_vivo(feito, pr.mu, pr.sd, falta,
                                            sum(ult) / len(ult) if ult else None, jogo_a.margem(),
                                            int(anos) if anos is not None else None)
                p_abre = 1 - p_acima(prev, sd, lim)
                fim = jogo_a.fim_previsto(ctx.agora)
                fim90 = jogo_a.fim_previsto(ctx.agora, p90=True)
                sit = jogo_a.situacao(ctx.team(a), ctx.agora)
                txt = (f"{n(feito)} agora, {_relogio(jogo_a, ctx.agora)}"
                       + (f" · {sit}" if sit else "")
                       + f" → fecha ~{n(prev)} · {round(100 * (1 - p_abre))}% de chance de "
                       f"chegar em {n(lim)}")
            viavel = fim <= ob.inicio and i in ab and i not in ocupados
            if not jogo_a.acabou(ctx.agora) and fim > ob.inicio:
                txt += f" · acaba ~{ctx.hora(fim, '%H:%M')}, não dá tempo"
            g = ob.p * e_excesso(ob.mu, ob.sd, pl.limiar(ab, i, b, fora))
            opcoes.append({"i": i, "a": a, "viavel": viavel, "txt": txt, "ganho": p_abre * g,
                           "p_abre": p_abre, "feito": feito, "prev": prev, "lim": lim,
                           "fim": fim, "fim90": fim90, "acabou": jogo_a.acabou(ctx.agora),
                           "jogo": jogo_a})
        boas = [o for o in opcoes if o["viavel"] and o["ganho"] >= minimo]
        if not boas:
            continue
        melhor = max(boas, key=lambda o: o["ganho"])
        ocupados.add(melhor["i"])
        out.append({"b": b, "o": ob, **melhor,
                    "outras": [o for o in opcoes if o is not melhor]})
    return out


def linhas_substituicoes(ctx, est, subs) -> list[str]:
    L = secao("🔁", "Substituição", "o reserva entra no lugar de quem já foi pra quadra")
    for s in subs:
        b, a, slot, ob = s["b"], s["a"], est.slots[s["i"]], s["o"]
        nb, na = esc(ctx.name(b)), esc(ctx.name(a))
        L.append(f"✅ <b>{nb}</b> entra no <code>{slot}</code> no lugar de <b>{na}</b>")
        L.append(f"      ⏰ <i>{sobrenome(nb)} joga {ctx.hora(ob.inicio, '%H:%M')}</i>")
        if s["acabou"]:
            L.append(f"      🔄 <b>troque agora</b> <i>({s['txt']})</i>")
        else:
            L.append(f"      🔄 <i>troque quando o jogo do {sobrenome(na)} acabar "
                     f"(~{ctx.hora(s['fim'], '%H:%M')}), antes das {ctx.hora(ob.inicio, '%H:%M')}</i>")
            if s["fim90"] > ob.inicio:
                L.append(f"      ⏳ <i>jogo assim às vezes vai até ~{ctx.hora(s['fim90'], '%H:%M')}: "
                         f"pode não dar tempo</i>")
            L.append(f"      🏀 <i>{sobrenome(na)} tem {n(s['feito'])}, {_relogio(s['jogo'], ctx.agora)} · "
                     f"deve fechar ~{n(s['prev'])}</i>")
            sit = s["jogo"].situacao(ctx.team(a), ctx.agora)
            if sit:
                L.append(f"      <i>{esc(sit)}</i>")
            L.append(f"      🎯 <i><b>{round(100 * (1 - s['p_abre']))}%</b> de chance de o {sobrenome(na)} "
                     f"chegar em {n(s['lim'])}: se chegar, não troque, trave ele</i>")
        L.append(f"      📊 <i>ganho esperado da troca <b>{n(s['ganho'], 1, True)}</b></i>")
        if s["outras"]:
            L.append(f"      👀 <i>onde mais o {sobrenome(nb)} cabe:</i>")
            for o in s["outras"]:
                L.append(f"      <code>{est.slots[o['i']]}</code> {esc(ctx.name(o['a']))} · "
                         f"<i>{esc(o['txt'])}</i>")
        L.append("")
    return L


# ==========================================================================
# 2c. fim da noite
# ==========================================================================
def noite(ctx) -> tuple[str, list]:
    """(data, jogos) da rodada da noite. O dia americano vira a meia-noite
    ET, mas o ultimo jogo acaba la pela 1h ET: a rodada so vira as 8h ET."""
    d = (ctx.agora.astimezone(ET) - timedelta(hours=8)).date().isoformat()
    return d, ctx.agenda.do_dia(d)


def noite_fechada(ctx, jogos) -> bool:
    """Todo jogo da rodada acabou e a estatistica dos seus ja saiu (ou ja
    passou uma hora do fim: aviso com o que tem)."""
    if not jogos or not all(j.acabou(ctx.agora) for j in jogos):
        return False
    fim = max(j.inicio for j in jogos) + DURACAO
    return not getattr(ctx, "stats_pendentes", False) or ctx.agora - fim > timedelta(hours=1)


# ==========================================================================
# 3. pegar agora (livre que joga hoje)
# ==========================================================================
def em_waiver(ctx) -> set[str]:
    """Dispensados ha menos de `waiver_clear_days`: esses pedem claim, nao
    entram na hora. Sem isso o bot mandaria pegar quem o app nao deixa."""
    dias = float(ctx.sl.settings().get("waiver_clear_days") or 2)
    corte = (ctx.agora - timedelta(days=dias)).timestamp() * 1000
    out = set()
    for w in {ctx.week, ctx.week - 1}:
        if w < 1:
            continue
        try:
            for t in ctx.sl.transactions(w):
                if (t.get("status") == "complete" and (t.get("status_updated") or 0) >= corte):
                    out |= set((t.get("drops") or {}).keys())
        except Exception:
            pass
    return out


def pickups(ctx, est, abertos, congelados, usados, candidatos: list[str],
            limite: int = 3) -> list[dict]:
    """Os melhores (add, drop) pelo ganho na SEMANA + diferenca de temporada.

    Ganho na semana = valor da semana (planos + bilhetes de hoje) com o
    elenco novo menos com o atual. Temporada = o que o livre rende nas
    proximas semanas menos o que o dispensado renderia, menos o que o livre
    escolhido costuma render abaixo do previsto (VIES_LIVRE por semana).
    """
    cfg = ctx.cfg.get("waivers") or {}
    peso = float(cfg.get("peso_temporada", 1.0))
    minimo = float(cfg.get("ganho_minimo", 2.0))
    nunca = set(cfg.get("never_drop") or [])

    def valor(elenco):
        livres = [p for p in elenco if p not in usados]
        hoje = jogos_hoje(ctx, est, livres)
        fut = opcoes_futuras(ctx, est, livres, so_depois_de_hoje=True)
        e = escalar_dia(est.slots, abertos, congelados, hoje, fut, ctx.posicoes, est.atual)
        return e.valor_semana

    base = valor(est.elenco)
    # quem pode sair: nao travado, sem jogo em andamento, fora da lista
    saem = [p for p in est.elenco if p not in usados and p not in congelados.values()
            and ctx.name(p) not in nunca]
    ros = {p: valor_temporada(ctx, p) for p in saem}
    saem = sorted(saem, key=lambda p: ros[p])[:5]

    # vaga livre (elenco curto) ou IR vazio com alguem Out: entra sem sair ninguem
    vaga = len(est.elenco) < len([s for s in ctx.sl.roster_positions() if s != "IR"])
    ir_livre = len(est.ir) < int(ctx.sl.settings().get("reserve_slots") or 0)
    pro_ir = [p for p in saem if ir_livre and ctx.status(p) in ("Out", "Sus")]

    # pre-filtro barato: os candidatos de maior media
    cands = sorted(candidatos, key=lambda p: -ctx.modelo.base(p)["mu"])[:8]
    vies = VIES_LIVRE * semanas_horizonte(ctx)
    out = []
    for f in cands:
        ros_f = valor_temporada(ctx, f)
        opcoes_saida = [(None, "vaga livre")] if vaga else []
        opcoes_saida += [(d, "IR") for d in pro_ir]
        opcoes_saida += [(d, "dispensa") for d in saem]
        melhor = None
        for d, como in opcoes_saida:
            novo = [p for p in est.elenco if p != d] + [f]
            sem = valor(novo) - base
            temp = peso * (ros_f - (0 if como != "dispensa" else ros[d]) - vies)
            tot = sem + temp
            if melhor is None or tot > melhor["total"]:
                melhor = {"add": f, "drop": d, "como": como, "semana": sem,
                          "temporada": temp, "total": tot}
        if melhor and melhor["semana"] >= minimo and melhor["total"] >= minimo:
            out.append(melhor)
    out.sort(key=lambda r: -r["total"])
    return out[:limite]


def linhas_pickups(ctx, picks, titulo_txt="Pegar agora", icone="➕",
                   nota="") -> list[str]:
    L = secao(icone, titulo_txt, nota)
    cabecalho = len(L)
    for r in picks:
        f, d = r["add"], r["drop"]
        jogo = [j for j in ctx.agenda.jogos_do_time(ctx.team(f), ctx.week)
                if not j.comecou(ctx.agora)]
        quando = f", joga {ctx.hora(jogo[0].inicio)}" if jogo else ""
        if r["como"] == "dispensa":
            saida = f"dispensando {esc(ctx.name(d))}"
        elif r["como"] == "IR":
            saida = f"movendo {esc(ctx.name(d))} pro IR"
        else:
            saida = "na vaga livre"
        L.append(f"🟢 <b>{esc(ctx.name(f))}</b> <i>{esc(ctx.pos(f))} · {esc(ctx.team(f))}"
                 f"{esc(quando)}</i>")
        L.append(f"      🔁 {saida}")
        L.append(f"      📊 semana <b>{n(r['semana'], 1, True)}</b> · temporada "
                 f"{n(r['temporada'], 1, True)}")
        t = ctx.modelo.tendencia(f)
        if t:
            L.append(f"      <i>📈 {esc(t)}</i>")
        L.append("")
    if len(L) == cabecalho:
        return []
    L.append("<i>ℹ️ livre entra na hora; depois que o jogo dele começa, ele vai pra waiver</i>")
    return L


# ==========================================================================
def linhas_travados(ctx, est, decs=()) -> list[str]:
    """Travados (os provados e os que voce confirmou) e os talvez-travados.
    O talvez-travado que eu mandei travar leva o prazo: e o unico lembrete
    dele depois da noite em que jogou."""
    L = [f"🔒 <code>{est.slots[i]}</code> {esc(ctx.name(p))} <b>{n(pts)}</b>"
         for i, (p, pts) in sorted(est.travados.items())]
    trava = {t.pid for t in decs if t.travar}
    for i, (p, pts, _) in sorted(est.pendentes.items()):
        prox = est.restantes(p, ctx.agora)
        if p in trava and prox:
            nota = f"se ainda não travou, trave até {dia_rel(ctx, prox[0].inicio)}"
        else:
            nota = "travou? marque na enquete"
        L.append(f"❓ <code>{est.slots[i]}</code> {esc(ctx.name(p))} {n(pts)} <i>· {nota}</i>")
    if est.pendentes:
        L.append("")
        L.append("<i>ℹ️ sem voto, conto o ❓ como aberto</i>")
    return L


def painel(ctx, est, decs=()) -> list[str]:
    L = secao("📊", f"Semana {est.week}",
              f"{len(est.travados)}/{len(est.slots)} slots travados")
    return L + linhas_travados(ctx, est, decs)


def chave_escalacao(est, mud) -> str:
    """A escalacao do dia ja mandada (de manha ou de hora em hora) nao volta
    enquanto nao mudar."""
    assinatura = hashlib.sha1(repr(sorted((i, b) for i, _, _, b in mud)).encode()).hexdigest()[:10]
    return f"escala:{est.hoje}:{assinatura}"


def recentes(ctx, est, decs):
    """Travar ou passar so de quem jogou na ultima noite (ou hoje): o placar
    de antes ja foi decidido na mensagem daquela noite."""
    ontem = (date.fromisoformat(est.hoje) - timedelta(days=1)).isoformat()
    out = []
    for t in decs:
        j = est.ultimo_encerrado(t.pid, ctx.agora)
        if j and j.data >= ontem:
            out.append(t)
    return out


def mandar_enquete(ctx, est, decs, ate: str) -> None:
    """Pergunta quem dos talvez-travados voce travou: uma enquete so, depois
    que a rodada daquele jogo fechou (`ate`: a ultima noite fechada) e a
    mensagem do fim da noite saiu, e uma vez por jogo."""
    ja = enquete.perguntadas()
    dec = decisoes()
    agora = {t.pid: ("SEGURAR" if t.segurar else "TRAVAR" if t.travar else "PASSAR")
             for t in decs}
    ordem = {"travar": 3, "segurar": 2, None: 1, "passar": 0}
    # so quem ja estava pendente na passada anterior (15-30 min antes)
    firmes = enquete.firmes({f"{est.week}:{p}:{data}" for p, _, data in est.pendentes.values()
                             if data <= ate})
    itens = []
    for i, (p, pts, data) in est.pendentes.items():
        chave = f"{est.week}:{p}:{data}"
        if chave not in firmes or chave in ja:
            continue
        antes = dec.get(chave)
        if antes:
            rec = f"recomendei {antes.upper()}"
        elif p in agora:
            rec = f"agora eu diria {agora[p]}"
        else:
            rec = "sem recomendação"
        chance = ordem.get(antes or (agora.get(p) or "").lower() or None, 1)
        itens.append((chance, pts, chave,
                      f"{ctx.name(p)} · {est.slots[i]} · {n(pts)} pts · {rec}"))
    if not itens:
        return
    itens.sort(key=lambda x: (-x[0], -x[1]))
    pergunta = (f"Semana {est.week}: quem destes você TRAVOU? O Sleeper não me mostra "
                f"até o próximo jogo deles. Marque todos (ou '{enquete.NENHUM}').")
    if ctx.args.dry_run:
        print("\n(enquete, dry-run: nao enviada)\n" + pergunta)
        for *_, t in itens:
            print(f"  [ ] {t}")
        return
    if enquete.enviar(ctx.cfg, pergunta, [(c, t) for *_, c, t in itens]):
        print(f"[{ctx.rotulo}] enquete de trava: {len(itens)} jogador(es)")


STATUS_LOG = CACHE / "status_log.json"


def registrar_status(ctx) -> None:
    """Anota o status de cada jogador de rotacao antes do jogo dele.

    As chances de jogar por status vem dos relatorios oficiais da NBA
    (backtest_statut.py). Este log guarda o status que o BOT viu (Sleeper e
    ESPN, que nao sao o relatorio) e a noticia, pra medir as duas coisas na
    temporada.
    """
    try:
        from agenda import hoje_et
        hoje = hoje_et(ctx.agora)
        jogos = [j for j in ctx.agenda.do_dia(hoje) if not j.comecou(ctx.agora)]
        if not jogos:
            return
        times = {t for j in jogos for t in j.times}
        try:
            log = json.loads(STATUS_LOG.read_text(encoding="utf-8"))
        except Exception:
            log = {}
        from modelo import noticia_do_jogo
        dia = log.setdefault(hoje, {})
        jogo_do = {t: j for j in jogos for t in j.times}
        for pid, js in ctx.logs.items():
            t = ctx.team(pid)
            if len(js) >= 5 and t in times:
                leitura = noticia_do_jogo(ctx.lesoes.get(pid), jogo_do[t])
                dia[pid] = [ctx.modelo.status(pid) or "ok", leitura]
        for d in sorted(log)[:-240]:
            del log[d]
        STATUS_LOG.write_text(json.dumps(log), encoding="utf-8")
    except Exception as e:
        print(f"(nao anotei os status: {e})")


def main_with(args, config=None) -> int:
    ctx = build(args, config)
    if not ctx.sl.lock_in():
        ctx.warnings.append("esta liga NAO esta no modo Lock-In; a logica de trava nao se aplica")
    registrar_status(ctx)
    enquete.sincronizar(ctx.cfg, avisar=not ctx.args.dry_run)
    est = estado(ctx)
    if not est.elenco:
        # Antes do draft o elenco e vazio e todo livre "melhora o time".
        print(f"[{ctx.rotulo}] elenco vazio (o draft ainda nao aconteceu)")
        return 0
    decs = travas(ctx, est)
    e, mud, ganho, hoje, futuro, abertos, congelados, usados = escalacao_hoje(ctx, est, decs)

    minimo = float((ctx.cfg.get("escalacao") or {}).get("ganho_minimo", 0.5))
    waiver = em_waiver(ctx)
    fa = [p for p in ctx.sl.free_agents(ctx.pool) if p not in waiver]
    fa_hoje = [p for p in fa if any(j.data == est.hoje and not j.comecou(ctx.agora)
                                    for j in ctx.agenda.jogos_do_time(ctx.team(p), ctx.week))]
    picks = pickups(ctx, est, abertos, congelados, usados, fa_hoje) if abertos else []
    subs = substituicoes(ctx, est, decs, hoje, e, minimo)

    # --- o que e novo (modo agendado) --------------------------------------
    gate = bool(getattr(args, "gate", False))
    dia_noite, jogos_noite = noite(ctx)
    fechada = noite_fechada(ctx, jogos_noite)
    chave_noite = f"noite:{dia_noite}"
    manda_noite = fechada and (not gate or not ja_avisado(AVISOS, chave_noite))
    ate = dia_noite if fechada else (date.fromisoformat(dia_noite) - timedelta(days=1)).isoformat()

    chaves_trava = {}
    for t in decs:
        j = est.ultimo_encerrado(t.pid, ctx.agora)
        acao = "segurar" if t.segurar else f"travar{t.j}" if t.travar else "passar"
        chaves_trava[t.pid] = (f"trava:{est.week}:{t.pid}:{j.data if j else '?'}:{acao}", j)
    novos = {p for p, (k, _) in chaves_trava.items() if not ja_avisado(AVISOS, k)}
    # Trava espera o fim da noite (vale ate o proximo jogo dele), menos a
    # que pede mover pro slot de quem ainda joga hoje: essa e agora.
    urgentes = [t for t in decs if t.pid in novos and t.travar and t.mover
                and est.atual[t.j] in hoje]

    proximo_tip = min((o.inicio for o in hoje.values()), default=None)
    perto = proximo_tip is not None and proximo_tip - ctx.agora <= timedelta(hours=JANELA_ESCALACAO_H)
    entra = lambda i, b: f"entra:{est.hoje}:{i}:{b}"
    # troca que a substituicao ja avisou nao volta como escalacao nova
    mud_novo = [m for m in mud if not (m[3] and ja_avisado(AVISOS, entra(m[0], m[3])))]
    chave_esc = chave_escalacao(est, mud)
    estaciona = any(b in e.segurados.values() for _, _, _, b in mud)
    manda_esc = bool(mud_novo) and (ganho >= minimo or estaciona) and (
        not gate or (perto and not ja_avisado(AVISOS, chave_esc)))
    lembra = lambda i, b: f"lembra:{est.hoje}:{i}:{b}"

    def em_cima(m) -> bool:
        pz = prazo_mudancas([m], hoje)
        return pz is not None and timedelta(0) < pz - ctx.agora <= timedelta(minutes=JANELA_LEMBRETE_MIN)
    # o Sleeper ainda nao tem a troca e o prazo dela esta perto
    lembretes = [m for m in mud if gate and (ganho >= minimo or estaciona) and em_cima(m)
                 and not ja_avisado(AVISOS, lembra(m[0], m[3]))]
    subs_novas = [x for x in subs if not gate or not ja_avisado(AVISOS, entra(x["i"], x["b"]))]
    chaves_pick = [f"add:{est.hoje}:{r['add']}:{r['drop']}" for r in picks]
    picks_novos = [r for r, k in zip(picks, chaves_pick)
                   if not gate or (perto and not ja_avisado(AVISOS, k))]

    if gate and not (subs_novas or manda_esc or lembretes or picks_novos or urgentes or manda_noite):
        print(f"[{ctx.rotulo}] nada novo a decidir "
              f"(travados {len(est.travados)}/{len(est.slots)}, {len(novos)} trava(s) "
              f"esperando o fim da noite, proximo tip {ctx.hora(proximo_tip)})")
        mandar_enquete(ctx, est, decs, ate)
        return 0

    tipo = ("FIM DA NOITE" if manda_noite else "LEMBRETE"
            if lembretes and not (subs_novas or manda_esc or picks_novos or urgentes) else "LOCK-IN")
    L = [titulo(tipo, ctx.league_name, ctx.hora(ctx.agora, "%a %d/%m %H:%M"))]
    # O painel da semana sai no fim da noite (e na mao); no meio do dia a
    # mensagem e so o que mudou.
    if manda_noite or not gate:
        L += painel(ctx, est, decs)
    # o lembrete sozinho e so o que mexer: as travas esperam o fim da noite
    rec = recentes(ctx, est, decs) if tipo != "LEMBRETE" else []
    if rec:
        # o que entrou desde a ultima mensagem vai primeiro, com 🆕
        marca = novos & {t.pid for t in rec}
        marca = marca if len(marca) < len(rec) else frozenset()
        L += secao("🔐", "Travar ou passar", "🆕 = novo desde a última mensagem" if marca else "")
        L += linhas_travas(ctx, est, sorted(rec, key=lambda t: t.pid not in marca),
                           quem_ocupa(ctx, est, decs), marca)
    if subs_novas:
        L += linhas_substituicoes(ctx, est, subs_novas)
    if lembretes and not manda_esc:
        # a escalacao nova ja diz "mexa ate"; sem ela, o lembrete vai primeiro
        L[1:1] = linhas_lembrete(ctx, lembretes, hoje)
    if e.hoje and (manda_esc or not gate):
        L += linhas_plano_dia(ctx, est, e, mud, hoje, decs,
                              "Escalação de hoje (mudou)" if gate else "Escalação de hoje")
    if picks_novos:
        L += linhas_pickups(ctx, picks_novos)
    if manda_noite:
        from run_dia import relatorio_waiver
        L += relatorio_waiver(ctx, est, abertos, congelados, usados, jogos_noite)
    msg = bloco(L) + rodape(ctx.warnings)
    ok = ctx.notify(msg)

    if ok:
        for t in decs if tipo != "LEMBRETE" else ():
            chave, j = chaves_trava[t.pid]
            marcar_avisado(AVISOS, chave)
            if j:
                registrar_decisao(est.week, t.pid, j.data, "segurar" if t.segurar
                                  else "travar" if t.travar else "passar")
        if manda_esc:
            marcar_avisado(AVISOS, chave_esc)
            for i, _, _, b in mud:
                if b:
                    marcar_avisado(AVISOS, entra(i, b))
        for i, _, _, b in lembretes:
            marcar_avisado(AVISOS, lembra(i, b))
        for x in subs_novas:
            marcar_avisado(AVISOS, entra(x["i"], x["b"]))
        for r in picks_novos:
            marcar_avisado(AVISOS, f"add:{est.hoje}:{r['add']}:{r['drop']}")
        if manda_noite:
            marcar_avisado(AVISOS, chave_noite)
    mandar_enquete(ctx, est, decs, ate)
    return 0


def main(argv=None) -> int:
    args = base_parser(__doc__.splitlines()[0]).parse_args(argv)
    return run_for_each_league(args, main_with)


if __name__ == "__main__":
    sys.exit(main())
