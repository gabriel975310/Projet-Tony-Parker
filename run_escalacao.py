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

Rode de hora em hora com --gate: so sai mensagem quando ha decisao nova.
Sem --gate (na mao), mostra o painel inteiro da semana.

    python run_escalacao.py --dry-run
    python run_escalacao.py --dry-run --now "2026-10-22T23:30"
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta

from agenda import DURACAO
from clients import CACHE, esc
from lineup import cabe, diff
from lockin import Planejador, chance_troca, decidir_travas, escalar_dia, valor_bilhetes
from contexte import base_parser, build, ja_avisado, marcar_avisado, run_for_each_league
import enquete
from report import bloco, n, rodape, secao, titulo
from semana import (decisoes, estado, jogos_hoje, opcoes_futuras, registrar_decisao,
                    valor_temporada)

AVISOS = CACHE / "escalacao_avisos.json"

# Ate quantas horas antes do primeiro tip-off a escalacao do dia sai. A
# mensagem de manha seria velha a noite (GTD vira Out perto do jogo); a
# reavaliacao de hora em hora so reenvia se a recomendacao MUDAR.
JANELA_ESCALACAO_H = 6


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


def quem_ocupa(ctx, est, decs) -> dict[str, str]:
    """{quem PASSA: quem o plano da semana poe no slot dele}.

    Nao e todo mundo que cabe no slot: o plano da cada jogador ao slot em
    que ele mais rende, entao quem provavelmente trava em outro lugar (o
    Giddey no G, o Acuff no UTIL) nao aparece como reserva do SG.
    """
    fechados = {t.j for t in decs if t.travar} | {t.i for t in decs if t.segurar}
    fora = frozenset(t.pid for t in decs if t.travar or t.segurar)
    ab = [x for x in est.abertos if x not in fechados]
    pl = Planejador(est.slots, opcoes_futuras(ctx, est, est.elenco), ctx.posicoes)
    out = {}
    for t in decs:
        if t.travar or t.segurar or t.i not in ab:
            continue
        grupo = pl.plano(ab, fora | {t.pid}).grupos.get(t.i) or []
        nomes = []
        for p in grupo:
            prox = est.restantes(p, ctx.agora)
            if prox:
                nomes.append((prox[0].inicio, f"{ctx.name(p).split()[-1]} {ctx.hora(prox[0].inicio, '%a')}"))
        out[t.pid] = ", ".join(x for _, x in sorted(nomes))
    return out


def linhas_travas(ctx, est, decs, ocupa: dict[str, str] | None = None) -> list[str]:
    L = []
    for t in decs:
        nome = esc(ctx.name(t.pid))
        prox = est.restantes(t.pid, ctx.agora)
        prazo = (f"até {ctx.hora(prox[0].inicio)}" if prox
                 else "até o fim da semana")
        duvida = not t.segurar and abs(t.margem) < float((ctx.cfg.get("lock") or {}).get("margem_duvida", 1.5))
        destino = est.slots[t.j]
        if t.segurar:
            esp = t.espera
            h = t.i if t.parada is None else t.parada
            L.append(f"⏸ <b>SEGURE {nome}</b> no <code>{est.slots[h]}</code> · não trave ainda")
            if h != t.i:
                L.append(f"      🧭 <i>mova do <code>{t.slot}</code> pro <code>{est.slots[h]}</code>: "
                         f"é o slot que hoje e o resto da semana menos precisam</i>")
            L.append(f"      🎯 {n(t.pontos)} pts · já passou do limiar (~{n(t.limiar)}), "
                     f"mas ver os outros jogarem antes vale <b>{n(esp.ganho, 1, True)}</b>")
            L.append(f"      🔭 <i>decido depois dos jogos de {ctx.hora(esp.ate, '%a')}: "
                     f"{round(100 * esp.p_trava)}% de chance de travar ele</i>")
            L.append("      ⚠️ <i>não tire ele do slot, senão o placar se perde</i>")
        elif t.travar:
            L.append(f"✅ <b>TRAVE {nome}</b> no <code>{destino}</code>")
            L.append(f"      🎯 <b>{n(t.pontos)}</b> pts · <i>esperar rende ~{n(t.limiar)}</i>")
            if t.mover:
                L.append(f"      🧭 <i>mova do <code>{t.slot}</code> pro <code>{destino}</code> antes: "
                         f"lá o resto da semana perde ~{n(t.limiar)}, no {t.slot} ~{n(t.limiar_atual)}</i>")
                plano_b = ("trave no " + t.slot if t.pontos >= t.limiar_atual else "passe")
                L.append(f"      <i>(se o Sleeper não deixar mover, {plano_b})</i>")
        else:
            L.append(f"⏭ <b>PASSE {nome}</b> no <code>{t.slot}</code>")
            L.append(f"      🎯 {n(t.pontos)} pts · o slot ainda rende <b>~{n(t.limiar)}</b>")
            quem = (ocupa or {}).get(t.pid)
            if quem:
                L.append(f"      <i>👥 quem deve ocupar o {t.slot}: {esc(quem)}</i>")
        L.append(f"      <i>⏰ {esc(prazo)}" + (" · 🪙 cara ou coroa" if duvida else "") + "</i>")
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


def reserva_tarde(ctx, slot: str, o, hoje, titulares) -> str | None:
    """Quem do banco cabe no slot e joga depois que o jogo `o` acaba.

    Sem ninguem assim, nao ha troca na mesma noite: o slot so e refeito na
    escalacao do dia seguinte.
    """
    fim = o.inicio + DURACAO
    cands = [p for p, op in hoje.items() if p not in titulares and op.inicio >= fim
             and cabe(ctx.posicoes(p), slot)]
    return max(cands, key=lambda p: hoje[p].mu, default=None)


def linha_troca(ctx, o, limiar: float, reserva: str | None = None, hoje=None) -> str:
    """O que acontece se ele nao passar do limiar, e com que chance."""
    pct = round(100 * chance_troca(o, limiar))
    if reserva and hoje:
        r = hoje[reserva]
        quando = (f"troque por <b>{esc(ctx.name(reserva))}</b> quando o jogo acabar "
                  f"(~{ctx.hora(o.inicio + DURACAO, '%H:%M')}; ele joga {ctx.hora(r.inicio, '%H:%M')})")
    else:
        quando = "não trave: o slot volta pra escalação de amanhã"
    return f"      🔁 <i>senão, {quando} · {pct}% de chance</i>"


def linhas_escalacao(ctx, est, e, mud, ganho, hoje) -> list[str]:
    L = []
    prazo = prazo_mudancas(mud, hoje)
    L += secao("🏀", "Escalação de hoje",
               f"⏰ mexa até {ctx.hora(prazo, '%H:%M')}" if prazo else "")
    L += linhas_mudancas(ctx, mud, hoje, set(e.segurados.values()))
    L.append("")
    L.append(f"📊 <i>ganho esperado <b>{n(ganho, 1, True)}</b> na semana</i>")
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


def linhas_lineup(ctx, est, e, mud, ganho, hoje) -> list[str]:
    """A escalacao ideal do dia, slot por slot, com a hora e a chance de cada
    um pedir troca: pro relatorio da manha."""
    sai = {i: a for (i, _, a, _) in mud}
    prazo = prazo_mudancas(mud, hoje)
    if sai and prazo:
        nota = f"🔄 {len(sai)} troca(s) no Sleeper até {ctx.hora(prazo, '%H:%M')}"
    else:
        nota = "✅ o Sleeper já está com a escalação ideal"
    L = secao("🏀", "Escalação ideal de hoje", nota)
    # Os travados ja estao no resumo da semana; aqui so os slots abertos,
    # quem joga hoje primeiro e quem nao joga numa linha so.
    titulares = set(e.hoje.values())
    fora = []
    for i, slot in enumerate(est.slots):
        if i in est.travados:
            continue
        p = e.lineup[i] if i < len(e.lineup) else None
        if i in e.segurados:
            j = est.ultimo_encerrado(p, ctx.agora)
            pts = est.placar.get((p, j.data)) if j else None
            troca = f" <i>(sai {esc(ctx.name(sai[i]))})</i>" if sai.get(i) else ""
            L.append(f"⏸ <code>{slot}</code> <b>{esc(ctx.name(p))}</b> {n(pts)}{troca} "
                     f"· <i>segurado, não trave ainda</i>")
            continue
        if not (p and p in hoje):
            fora.append(f"<code>{slot}</code> {esc(ctx.name(p)) if p else 'vazio'}")
            continue
        o = hoje[p]
        lim = e.limiar.get(i)
        troca = f" <i>(sai {esc(ctx.name(sai[i]))})</i>" if sai.get(i) else ""
        marca = "🔄" if i in sai else "✅"
        L.append(f"{marca} <code>{slot}</code> <b>{esc(ctx.name(p))}</b> · "
                 f"<i>{ctx.hora(o.inicio, '%H:%M')}</i>{troca}")
        if lim is None:
            L.append(f"      📊 média {n(o.mu)}")
        elif lim < 0.5:
            L.append(f"      🎯 <b>trave o que vier</b> <i>(média {n(o.mu)})</i>")
        else:
            L.append(f"      🎯 trave se fizer <b>{n(lim)}+</b> <i>(média {n(o.mu)})</i>")
            L.append(linha_troca(ctx, o, lim, reserva_tarde(ctx, slot, o, hoje, titulares), hoje))
        jogo = next((j for j in est.jogos.get(p, []) if j.data == o.dia), None)
        L += noticia(ctx, p, jogo)
    if fora:
        L.append("")
        L.append("💤 <i>não jogam hoje:</i> " + " · ".join(fora))
    L.append("")
    if sai:
        L.append(f"🔄 = mexer no Sleeper · 📊 <i>ganho esperado <b>{n(ganho, 1, True)}</b> na semana</i>")
    L.append("<i>ℹ️ quando cada jogo acabar eu aviso se é pra travar</i>")
    return L


def linhas_bilhetes(ctx, est, e, hoje) -> list[str]:
    """Pra cada slot que joga hoje: a partir de quanto travar."""
    L = []
    for i, p in sorted(e.hoje.items(), key=lambda kv: hoje[kv[1]].inicio):
        o = hoje[p]
        L.append(f"🏀 <code>{est.slots[i]}</code> <b>{esc(ctx.name(p))}</b>"
                 f" · <i>{ctx.hora(o.inicio, '%H:%M')}</i>")
        if e.limiar[i] >= 0.5:
            L.append(f"      🎯 trave se fizer <b>{n(e.limiar[i])}+</b> <i>(média {n(o.mu)})</i>")
            L.append(f"      📉 <i>{round(100 * chance_troca(o, e.limiar[i]))}% de chance de não chegar</i>")
        else:
            L.append(f"      🎯 <b>trave o que vier</b> <i>(última chance do slot; média {n(o.mu)})</i>")
        jogo = next((j for j in est.jogos.get(p, []) if j.data == o.dia), None)
        L += noticia(ctx, p, jogo)
        L.append("")
    return L


def linhas_trocas_rodada(ctx, est, e, hoje) -> list[str]:
    """Quem do banco entra no lugar de quem, se o titular nao chegar no alvo.

    Agrupa por quem entra: o mesmo reserva pode cobrir dois slots, e ai
    entra no primeiro que ficar livre.
    """
    titulares = set(e.hoje.values())
    entra: dict[str, list[int]] = {}
    sem = []
    for i, p in sorted(e.hoje.items(), key=lambda kv: hoje[kv[1]].inicio):
        if e.limiar[i] < 0.5:
            continue
        r = reserva_tarde(ctx, est.slots[i], hoje[p], hoje, titulares)
        if r:
            entra.setdefault(r, []).append(i)
        else:
            sem.append(i)
    if not entra and not sem:
        return []
    L = secao("🔁", "Trocas durante a rodada", "se o titular não chegar no alvo: não trave e troque")
    for r, slots in sorted(entra.items(), key=lambda kv: hoje[kv[0]].inicio):
        L.append(f"✅ <b>{esc(ctx.name(r))}</b> <i>{esc(ctx.pos(r))} · joga "
                 f"{ctx.hora(hoje[r].inicio, '%H:%M')}</i>")
        for k, i in enumerate(slots):
            o = hoje[e.hoje[i]]
            ou = "ou " if k else ""
            L.append(f"      {ou}entra no <code>{est.slots[i]}</code> no lugar de "
                     f"{esc(ctx.name(e.hoje[i]))} <i>(acaba ~{ctx.hora(o.inicio + DURACAO, '%H:%M')})</i>")
        L.append("")
    if sem:
        quem = " · ".join(f"<code>{est.slots[i]}</code> {esc(ctx.name(e.hoje[i]))}" for i in sem)
        L.append(f"🚫 {quem}")
        L.append("      <i>ninguém do banco joga depois: se não chegar, não trave "
                 "e o slot volta pra escalação de amanhã</i>")
    return L


def linhas_folga(ctx, est, e, hoje) -> list[str]:
    """Quem nao joga hoje: titulares de slot aberto e banco, com os dias que
    ainda jogam na semana. Travado nao entra: o slot dele ja acabou."""
    def prox(p):
        js = [j for j in est.restantes(p, ctx.agora) if j.data > est.hoje]
        return ", ".join(ctx.hora(j.inicio, "%a") for j in js) or "sem jogo na semana"

    escalados = {p for p in e.lineup if p} | est.usados()
    tit = [f"<code>{est.slots[i]}</code> {esc(ctx.name(e.lineup[i]))} <i>({prox(e.lineup[i])})</i>"
           for i in est.abertos
           if i < len(e.lineup) and e.lineup[i] and e.lineup[i] not in hoje
           and i not in e.segurados]
    banco = [f"{esc(ctx.name(p))} <i>({prox(p)})</i>" for p in est.elenco
             if p not in escalados and p not in hoje]
    if not tit and not banco:
        return []
    L = secao("💤", "Não jogam hoje")
    if tit:
        L.append("<b>Titulares</b>")
        L += [f"      {x}" for x in tit]
        L.append("")
    if banco:
        L.append("<b>Banco</b>")
        L += [f"      {x}" for x in banco]
    return L


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
    proximas semanas menos o que o dispensado renderia.
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
            temp = peso * (ros_f - (0 if como != "dispensa" else ros[d]))
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
def linhas_travados(ctx, est) -> list[str]:
    """Travados (os provados e os que voce confirmou) e os talvez-travados."""
    L = [f"🔒 <code>{est.slots[i]}</code> {esc(ctx.name(p))} <b>{n(pts)}</b>"
         for i, (p, pts) in sorted(est.travados.items())]
    for i, (p, pts, _) in sorted(est.pendentes.items()):
        L.append(f"❓ <code>{est.slots[i]}</code> {esc(ctx.name(p))} {n(pts)} "
                 f"<i>· travou? marque na enquete</i>")
    if est.pendentes:
        L.append("")
        L.append("<i>ℹ️ sem voto, conto o ❓ como aberto</i>")
    return L


def painel(ctx, est) -> list[str]:
    L = secao("📊", f"Semana {est.week}",
              f"{len(est.travados)}/{len(est.slots)} slots travados")
    return L + linhas_travados(ctx, est)


def mandar_enquete(ctx, est, decs) -> None:
    """Pergunta quem dos talvez-travados voce travou: so depois da rodada
    daquele jogo (a decisao ja foi tomada) e uma vez por jogo."""
    ja = enquete.perguntadas()
    dec = decisoes()
    agora = {t.pid: ("SEGURAR" if t.segurar else "TRAVAR" if t.travar else "PASSAR")
             for t in decs}
    ordem = {"travar": 3, "segurar": 2, None: 1, "passar": 0}
    itens = []
    for i, (p, pts, data) in est.pendentes.items():
        chave = f"{est.week}:{p}:{data}"
        if data >= est.hoje or chave in ja:
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

    As chances de jogar por status (GTD 50%, Questionable 55%...) sao chute:
    nem o Sleeper nem a ESPN guardam historico de status. Com o ultimo status
    visto antes de cada tip-off, mais o log de quem jogou, da pra medir essas
    chances ao longo da temporada.
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
    enquete.sincronizar(ctx.cfg)
    est = estado(ctx)
    if not est.elenco:
        # Antes do draft o elenco e vazio e todo livre "melhora o time".
        print(f"[{ctx.rotulo}] elenco vazio (o draft ainda nao aconteceu)")
        return 0
    decs = travas(ctx, est)
    mandar_enquete(ctx, est, decs)
    e, mud, ganho, hoje, futuro, abertos, congelados, usados = escalacao_hoje(ctx, est, decs)

    minimo = float((ctx.cfg.get("escalacao") or {}).get("ganho_minimo", 0.5))
    waiver = em_waiver(ctx)
    fa = [p for p in ctx.sl.free_agents(ctx.pool) if p not in waiver]
    fa_hoje = [p for p in fa if any(j.data == est.hoje and not j.comecou(ctx.agora)
                                    for j in ctx.agenda.jogos_do_time(ctx.team(p), ctx.week))]
    picks = pickups(ctx, est, abertos, congelados, usados, fa_hoje) if abertos else []

    # --- o que e novo (modo agendado) --------------------------------------
    gate = bool(getattr(args, "gate", False))
    novos_trava = []
    for t in decs:
        j = est.ultimo_encerrado(t.pid, ctx.agora)
        acao = "segurar" if t.segurar else f"travar{t.j}" if t.travar else "passar"
        chave = f"trava:{est.week}:{t.pid}:{j.data if j else '?'}:{acao}"
        if not gate or not ja_avisado(AVISOS, chave):
            novos_trava.append((t, chave, j))

    proximo_tip = min((o.inicio for o in hoje.values()), default=None)
    perto = proximo_tip is not None and proximo_tip - ctx.agora <= timedelta(hours=JANELA_ESCALACAO_H)
    assinatura = hashlib.sha1(repr(sorted((i, b) for i, _, _, b in mud)).encode()).hexdigest()[:10]
    chave_esc = f"escala:{est.hoje}:{assinatura}"
    estaciona = any(b in e.segurados.values() for _, _, _, b in mud)
    manda_esc = bool(mud) and (ganho >= minimo or estaciona) and (
        not gate or (perto and not ja_avisado(AVISOS, chave_esc)))
    chaves_pick = [f"add:{est.hoje}:{r['add']}:{r['drop']}" for r in picks]
    picks_novos = [r for r, k in zip(picks, chaves_pick)
                   if not gate or (perto and not ja_avisado(AVISOS, k))]

    if gate and not (novos_trava or manda_esc or picks_novos):
        print(f"[{ctx.rotulo}] nada novo a decidir "
              f"(travados {len(est.travados)}/{len(est.slots)}, "
              f"proximo tip {ctx.hora(proximo_tip)})")
        return 0

    L = [titulo("LOCK-IN", ctx.league_name, ctx.hora(ctx.agora, "%a %d/%m %H:%M"))]
    L += painel(ctx, est)
    if novos_trava:
        L += secao("🔐", "Travar ou passar")
        L += linhas_travas(ctx, est, [t for t, _, _ in novos_trava], quem_ocupa(ctx, est, decs))
    if manda_esc:
        L += linhas_escalacao(ctx, est, e, mud, ganho, hoje)
    if e.hoje and (manda_esc or not gate):
        L += secao("🎯", "Jogam hoje", "a partir de quanto travar")
        L += linhas_bilhetes(ctx, est, e, hoje)
        L += linhas_trocas_rodada(ctx, est, e, hoje)
    if hoje and (manda_esc or not gate):
        L += linhas_folga(ctx, est, e, hoje)
    if picks_novos:
        L += linhas_pickups(ctx, picks_novos)
    msg = bloco(L) + rodape(ctx.warnings)
    ok = ctx.notify(msg)

    if ok:
        for t, chave, j in novos_trava:
            marcar_avisado(AVISOS, chave)
            if j:
                registrar_decisao(est.week, t.pid, j.data, "segurar" if t.segurar
                                  else "travar" if t.travar else "passar")
        if manda_esc:
            marcar_avisado(AVISOS, chave_esc)
        for r in picks_novos:
            marcar_avisado(AVISOS, f"add:{est.hoje}:{r['add']}:{r['drop']}")
    return 0


def main(argv=None) -> int:
    args = base_parser(__doc__.splitlines()[0]).parse_args(argv)
    return run_for_each_league(args, main_with)


if __name__ == "__main__":
    sys.exit(main())
