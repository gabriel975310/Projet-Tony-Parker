"""A noite inteira, de hora em hora: travar, escalar, pegar.

Tres decisoes, todas pelo mesmo plano da semana (lockin.py):

  1. TRAVAR OU PASSAR — pra cada titular cujo jogo acabou e ainda nao foi
     travado. Vale ate o proximo jogo dele comecar. Passar e escolher que a
     semana ainda rende mais naquele slot: com ele mesmo nos proximos jogos
     ou com outro que entra no lugar.
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
from lockin import chance_troca, decidir_travas, escalar_dia, valor_bilhetes
from contexte import base_parser, build, ja_avisado, marcar_avisado, run_for_each_league
from report import bloco, n, rodape, secao, titulo
from semana import (estado, jogos_hoje, opcoes_futuras, registrar_decisao,
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
    return decidir_travas(est.slots, est.abertos, futuro, ctx.posicoes, cands)


def _quem_segura(ctx, est, i: int, pid: str) -> str:
    """Quem mais pode ocupar o slot i esta semana (pra explicar o PASSE)."""
    nomes = []
    for p in est.elenco:
        if p in est.usados() or not cabe(ctx.posicoes(p), est.slots[i]):
            continue
        prox = est.restantes(p, ctx.agora)
        if prox:
            nomes.append((prox[0].inicio, f"{ctx.name(p).split()[-1]} {ctx.hora(prox[0].inicio, '%a')}"))
    nomes.sort()
    return ", ".join(x for _, x in nomes[:4])


def linhas_travas(ctx, est, decs) -> list[str]:
    L = []
    for t in decs:
        nome = esc(ctx.name(t.pid))
        prox = est.restantes(t.pid, ctx.agora)
        prazo = (f"até {ctx.hora(prox[0].inicio)}" if prox
                 else "até o fim da semana")
        duvida = abs(t.margem) < float((ctx.cfg.get("lock") or {}).get("margem_duvida", 1.5))
        if t.travar:
            L.append(f"✅ <b>TRAVE {nome}</b> no <code>{t.slot}</code>")
            L.append(f"      🎯 <b>{n(t.pontos)}</b> pts · <i>esperar rende ~{n(t.limiar)}</i>")
        else:
            L.append(f"⏭ <b>PASSE {nome}</b> no <code>{t.slot}</code>")
            L.append(f"      🎯 {n(t.pontos)} pts · o slot ainda rende <b>~{n(t.limiar)}</b>")
            quem = _quem_segura(ctx, est, t.i, t.pid)
            if quem:
                L.append(f"      <i>👥 quem ainda pode ocupar: {esc(quem)}</i>")
        L.append(f"      <i>⏰ {esc(prazo)}" + (" · 🪙 cara ou coroa" if duvida else "") + "</i>")
        L.append("")
    return L


# ==========================================================================
# 2. escalacao de hoje
# ==========================================================================
def escalacao_hoje(ctx, est, decs):
    """(Escalacao, mudancas, ganho, valor_atual, congelados, abertos, usados)."""
    travou = {t.i: t.pid for t in decs if t.travar}
    abertos = [i for i in est.abertos if i not in travou]
    usados = est.usados() | set(travou.values())
    livres = [p for p in est.elenco if p not in usados]
    # ocupante com jogo em andamento: o slot dele so mexe amanha
    congelados = {i: est.atual[i] for i in abertos
                  if est.atual[i] and est.em_andamento(est.atual[i], ctx.agora)}
    hoje = jogos_hoje(ctx, est, livres)
    futuro = opcoes_futuras(ctx, est, livres, so_depois_de_hoje=True)
    e = escalar_dia(est.slots, abertos, congelados, hoje, futuro, ctx.posicoes, est.atual)
    atual_val = valor_bilhetes(est.slots, abertos, congelados, hoje, futuro,
                               ctx.posicoes, est.atual)
    # so interessa trocar slot aberto em que alguem de hoje entra ou sai
    mud = [(i, s, a, b) for (i, s, a, b) in diff(est.slots, est.atual, e.lineup)
           if i in abertos and i not in congelados and (a in hoje or b in hoje)]
    ganho = sum(e.ganho.values()) - atual_val
    return e, mud, ganho, hoje, futuro, abertos, congelados, usados


def prazo_mudancas(mud, hoje):
    """O primeiro tip-off entre quem entra e quem sai: mexa antes disso."""
    prazos = [hoje[p].inicio for (_, _, a, b) in mud for p in (a, b) if p in hoje]
    return min(prazos, default=None)


def linhas_mudancas(ctx, mud, hoje) -> list[str]:
    L = []
    for (i, slot, a, b) in mud:
        sai = esc(ctx.name(a)) if a else "vazio"
        if b:
            o = hoje.get(b)
            hora = f" <i>{ctx.hora(o.inicio, '%H:%M')}</i>" if o else ""
            L.append(f"🔄 <code>{slot}</code> ❌ {sai} → ✅ <b>{esc(ctx.name(b))}</b>{hora}")
        else:
            L.append(f"🔄 <code>{slot}</code> ❌ {sai} <i>(ninguém joga nesse dia nesse slot)</i>")
    return L


def linha_troca(ctx, o, limiar: float) -> str:
    """Quando e com que chance o slot volta a pedir troca: no fim do jogo."""
    pct = round(100 * chance_troca(o, limiar))
    if limiar < 0.5:
        return f"      🔁 <i>trave o que vier; só troca se ele não jogar ({pct}%)</i>"
    fim = ctx.hora(o.inicio + DURACAO, "%H:%M")
    return f"      🔁 <i>{pct}% de chance de trocar quando o jogo acabar (~{fim})</i>"


def linhas_escalacao(ctx, est, e, mud, ganho, hoje) -> list[str]:
    L = []
    prazo = prazo_mudancas(mud, hoje)
    L += secao("🏀", "Escalação de hoje",
               f"⏰ mexa até {ctx.hora(prazo, '%H:%M')}" if prazo else "")
    L += linhas_mudancas(ctx, mud, hoje)
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
    for i, slot in enumerate(est.slots):
        if i in est.travados:
            p, pts = est.travados[i]
            L.append(f"🔒 <code>{slot}</code> {esc(ctx.name(p))} <b>{n(pts)}</b> <i>travado</i>")
            continue
        p = e.lineup[i] if i < len(e.lineup) else None
        if p and p in hoje:
            o = hoje[p]
            lim = e.limiar.get(i)
            if lim is None:
                quanto = ""
            elif lim >= 0.5:
                quanto = f" · trave se fizer <b>{n(lim)}+</b>"
            else:
                quanto = " · <b>trave o que vier</b>"
            troca = f" <i>(sai {esc(ctx.name(sai[i]))})</i>" if sai.get(i) else ""
            marca = "🔄" if i in sai else "✅"
            L.append(f"{marca} <code>{slot}</code> <b>{esc(ctx.name(p))}</b>{troca}")
            L.append(f"      ⏰ <i>{ctx.hora(o.inicio, '%H:%M')}</i> · média {n(o.mu)}{quanto}")
            if lim is not None:
                L.append(linha_troca(ctx, o, lim))
            jogo = next((j for j in est.jogos.get(p, []) if j.data == o.dia), None)
            L += noticia(ctx, p, jogo)
        else:
            quem = esc(ctx.name(p)) if p else "vazio"
            L.append(f"💤 <code>{slot}</code> {quem} <i>· não joga hoje</i>")
    L.append("")
    if sai:
        L.append(f"🔄 = mexer no Sleeper · 📊 <i>ganho esperado <b>{n(ganho, 1, True)}</b> na semana</i>")
    L.append("<i>ℹ️ ao longo do dia eu aviso se algo mudar e, quando cada jogo acabar, "
             "se é pra travar ou trocar</i>")
    return L


def linhas_bilhetes(ctx, est, e, hoje) -> list[str]:
    """Pra cada slot que joga hoje: a partir de quanto travar."""
    L = []
    for i, p in sorted(e.hoje.items(), key=lambda kv: hoje[kv[1]].inicio):
        o = hoje[p]
        quanto = (f"trave se fizer <b>{n(e.limiar[i])}+</b>" if e.limiar[i] >= 0.5
                  else "<b>trave o que vier</b> (última chance do slot)")
        L.append(f"🏀 <b>{esc(ctx.name(p))}</b> <i>{esc(ctx.pos(p))}</i> no <code>{est.slots[i]}</code>"
                 f" · <i>{ctx.hora(o.inicio, '%H:%M')}</i>")
        L.append(f"      🎯 {quanto} <i>(média {n(o.mu)})</i>")
        L.append(linha_troca(ctx, o, e.limiar[i]))
        jogo = next((j for j in est.jogos.get(p, []) if j.data == o.dia), None)
        L += noticia(ctx, p, jogo)
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
def painel(ctx, est) -> list[str]:
    travados = len(est.travados)
    L = ["", f"📊 <b>Semana {est.week}</b> · {travados}/{len(est.slots)} slots travados"]
    for i, (p, pts) in sorted(est.travados.items()):
        L.append(f"🔒 <code>{est.slots[i]}</code> {esc(ctx.name(p))} <b>{n(pts)}</b>")
    return L


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

    # --- o que e novo (modo agendado) --------------------------------------
    gate = bool(getattr(args, "gate", False))
    novos_trava = []
    for t in decs:
        j = est.ultimo_encerrado(t.pid, ctx.agora)
        chave = f"trava:{est.week}:{t.pid}:{j.data if j else '?'}"
        if not gate or not ja_avisado(AVISOS, chave):
            novos_trava.append((t, chave, j))

    proximo_tip = min((o.inicio for o in hoje.values()), default=None)
    perto = proximo_tip is not None and proximo_tip - ctx.agora <= timedelta(hours=JANELA_ESCALACAO_H)
    assinatura = hashlib.sha1(repr(sorted((i, b) for i, _, _, b in mud)).encode()).hexdigest()[:10]
    chave_esc = f"escala:{est.hoje}:{assinatura}"
    manda_esc = bool(mud) and ganho >= minimo and (
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
        L += linhas_travas(ctx, est, [t for t, _, _ in novos_trava])
    if manda_esc:
        L += linhas_escalacao(ctx, est, e, mud, ganho, hoje)
    if e.hoje and (manda_esc or not gate):
        L += secao("🎯", "Hoje: trave a partir de")
        L += linhas_bilhetes(ctx, est, e, hoje)
    if picks_novos:
        L += linhas_pickups(ctx, picks_novos)
    msg = bloco(L) + rodape(ctx.warnings)
    ok = ctx.notify(msg)

    if ok:
        for t, chave, j in novos_trava:
            marcar_avisado(AVISOS, chave)
            if j:
                registrar_decisao(est.week, t.pid, j.data, "travar" if t.travar else "passar")
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
