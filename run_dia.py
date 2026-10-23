"""Relatorio do dia: a escalacao ideal, o mercado e quem esta subindo.

Sai uma vez por dia (de manha, no seu fuso). A escalacao de hora em hora
cuida do que trava hoje a noite; aqui e o que pede calma:

  * a semana: o que ja travou, quanto cada slot aberto ainda deve render;
  * ADDS: os livres que melhoram o time contando a semana E o resto da
    temporada (o `run_escalacao` so olha quem joga hoje);
  * SUBINDO: livres com minutos em alta, com a causa — ganho organico fica
    ~80% no backtest, ganho porque o titular esta fora fica enquanto ele
    estiver fora, e some quando ele volta;
  * seu elenco: quem esta subindo ou caindo de papel;
  * CLAIMS: dispensados pela liga nos ultimos dias (ficam em waiver) que
    melhoram o seu titular de temporada, com quem sai no lugar;
    ADDS e CLAIMS saem no FIM DA NOITE (run_escalacao, depois do ultimo
    jogo); aqui so se a mensagem da noite nao saiu;
  * TROCAS, ate o prazo da liga: a lista inteira na segunda e, nos outros
    dias, so a proposta que ainda nao foi sugerida.

    python run_dia.py --dry-run
"""

from __future__ import annotations

import sys
from dataclasses import replace

from datetime import date, timedelta

from agenda import hoje_et
from clients import CACHE, esc
from lineup import starting_slots
from contexte import base_parser, build, ja_avisado, marcar_avisado, run_for_each_league
from report import bloco, n, rodape, secao, titulo
import enquete
from run_escalacao import AVISOS as AVISOS_NOITE
from run_escalacao import (em_waiver, escalacao_hoje, linhas_lineup, linhas_mudancas,
                           linhas_pickups, linhas_travados, pickups, prazo_mudancas,
                           travas)
from semana import estado
from trocas import AVISOS as AVISOS_TROCAS
from trocas import calcular as calcular_trocas
from trocas import chave_proposta
from trocas import linhas as linhas_trocas
from trocas import Plano
from previsao_waiver import VIES_LIVRE

AVISOS = CACHE / "dia_avisos.json"


def proximo_dia(ctx, est) -> str | None:
    """O proximo dia desta semana em que alguem do elenco joga (hoje incluso,
    se ainda ha jogo seu por comecar)."""
    usados = est.usados()
    return min((j.data for p in est.elenco if p not in usados
                for j in est.restantes(p, ctx.agora)), default=None)


def sem_jogo_hoje(ctx, dia, mud, hoje, segurados=()) -> list[str]:
    """Dia sem jogo seu: so o proximo dia de jogo e se a escalacao ja serve."""
    if not dia or not hoje:
        return ["", "😴 <b>Nenhum jogo seu até o fim da semana.</b>"]
    primeiro = min(o.inicio for o in hoje.values())
    quando = ctx.hora(primeiro, "%a %d/%m")
    L = ["", f"😴 <b>Hoje não tem jogo seu.</b> O próximo é <b>{quando}</b>, "
             f"a partir das {ctx.hora(primeiro, '%H:%M')}."]
    if not mud:
        L.append(f"✅ A escalação já está ideal pra {quando}. Não se preocupe: "
                 "no dia eu mando a escalação completa.")
        return L
    prazo = prazo_mudancas(mud, hoje)
    L.append("")
    ate = f"até {ctx.hora(prazo, '%H:%M')}" if prazo else "antes do jogo"
    L.append(f"🔄 Pra {quando}, mexa {ate}:")
    L += linhas_mudancas(ctx, mud, hoje, segurados)
    L.append("")
    L.append("<i>Sem pressa: no dia eu mando a escalação completa de novo.</i>")
    return L


def proximo_jogo_do_time(ctx, pid: str):
    t, hoje = ctx.team(pid), hoje_et(ctx.agora)
    return min((j for j in ctx.agenda.jogos if t in j.times and j.data >= hoje
                and not j.comecou(ctx.agora)), key=lambda j: j.data, default=None)


def lesao(ctx, pid: str, st: str) -> tuple[str, str]:
    """(icone, texto): quando ele volta e se isso custa algum jogo.

    Quem volta antes do proximo jogo do time nao perde nada: sem ambulancia.
    """
    ret = (ctx.retorno(pid) or "")[:10]
    prox = proximo_jogo_do_time(ctx, pid)
    if not ret:
        return "🚑", f"{st} · sem prazo de volta"
    volta = f"volta {ret[8:10]}/{ret[5:7]}"
    if prox and ret <= prox.data:
        return "🩹", f"{st} · {volta}, antes do próximo jogo ({ctx.hora(prox.inicio, '%a %d/%m')})"
    t, hoje = ctx.team(pid), hoje_et(ctx.agora)
    perde = sum(1 for j in ctx.agenda.jogos if t in j.times and hoje <= j.data < ret)
    return "🚑", f"{st} · {volta} · perde {perde} jogo{'s' if perde != 1 else ''}"


def subindo(ctx, candidatos: list[str], limite: int = 5) -> list[str]:
    rows = []
    for p in candidatos:
        b = ctx.modelo.base(p)
        if b["n"] < 8:
            continue
        d = b["R"] - b["B"]
        if d >= 4 and b["mu"] >= 12:
            rows.append((d, p))
    rows.sort(reverse=True)
    L = []
    for d, p in rows[:limite]:
        L.append(f"🔹 <b>{esc(ctx.name(p))}</b> <i>{esc(ctx.pos(p))} · {esc(ctx.team(p))}</i>")
        L.append(f"      📊 {n(ctx.modelo.base(p)['mu'])} pts/jogo")
        L.append(f"      <i>📈 {esc(ctx.modelo.tendencia(p))}</i>")
        L.append("")
    return L


def meu_elenco(ctx, est) -> list[str]:
    L = []
    for p in est.elenco + est.ir:
        t = ctx.modelo.tendencia(p)
        st = ctx.modelo.status(p)
        icone = "📉" if "caindo" in (t or "") else "📈"
        if st:
            icone, st = lesao(ctx, p, st)
        if t or st:
            extra = " · ".join(x for x in (st, t) if x)
            L.append(f"{icone} <b>{esc(ctx.name(p))}</b>")
            L.append(f"      <i>{esc(extra)}</i>")
    return L


def claims(ctx, est, ids: set[str], limite: int = 3) -> list[dict]:
    """Dispensados em waiver que melhoram o titular de temporada.

    Mesma conta das trocas: o plano da semana do Lock-In, com banco, na
    media do horizonte. Quem sai e o que menos custa; com vaga no elenco,
    ninguem sai.
    Claim nao e livre: na prioridade rolante ele te manda pro fim da fila,
    entao o corte (`ganho_minimo_claim`) e mais alto que o do add. O ganho
    ja desconta o que o livre escolhido costuma render abaixo do previsto
    (VIES_LIVRE).
    """
    cfg = ctx.cfg.get("waivers") or {}
    minimo = float(cfg.get("ganho_minimo_claim", 2.0))
    semanas = int(cfg.get("horizonte_semanas") or 6)
    nunca = set(cfg.get("never_drop") or [])
    slots = starting_slots(ctx.sl.roster_positions())
    cands = sorted((p for p in ids if ctx.posicoes(p)),
                   key=lambda p: -ctx.modelo.base(p)["mu"])[:8]
    if not cands:
        return []
    plano = Plano(ctx, slots, semanas)
    base = plano.valor(est.elenco)
    vaga = len(est.elenco) < len([s for s in ctx.sl.roster_positions() if s != "IR"])
    saem = [None] if vaga else [p for p in est.elenco if ctx.name(p) not in nunca]
    quem = {}
    nomes = ctx.sl.team_names()
    for w in {ctx.week, ctx.week - 1}:
        try:
            for t in ctx.sl.transactions(w) if w >= 1 else []:
                for pid, rid in (t.get("drops") or {}).items():
                    quem[pid] = nomes.get(rid, f"time {rid}")
        except Exception:
            pass
    out = []
    for f in cands:
        melhor = None
        for d in saem:
            novo = [p for p in est.elenco if p != d] + [f]
            g = plano.valor(novo) - base - VIES_LIVRE
            if melhor is None or g > melhor["ganho"]:
                melhor = {"add": f, "drop": d, "ganho": g, "de": quem.get(f, "?")}
        if melhor and melhor["ganho"] >= minimo:
            out.append(melhor)
    out.sort(key=lambda r: -r["ganho"])
    return out[:limite]


def linhas_claims(ctx, cl: list[dict]) -> list[str]:
    if not cl:
        return []
    st = ctx.sl.settings()
    meu = ctx.sl.my_roster().get("settings") or {}
    if int(st.get("waiver_type") or 0) == 2:
        sobra = int(st.get("waiver_budget") or 0) - int(meu.get("waiver_budget_used") or 0)
        custo = f"FAAB: sobram ${sobra}"
    else:
        custo = (f"você é o {meu.get('waiver_position', '?')}º da fila; "
                 "o claim te manda pro fim")
    L = secao("📝", "Claims de waiver", custo)
    for r in cl:
        f, d = r["add"], r["drop"]
        saida = f"dispensando {esc(ctx.name(d))}" if d else "na vaga livre"
        L.append(f"🟡 <b>{esc(ctx.name(f))}</b> <i>{esc(ctx.pos(f))} · {esc(ctx.team(f))}</i>")
        L.append(f"      🔁 {saida}")
        origem = (f"saiu do {esc(r['de'])}" if r["de"] != "?"
                  else "em waiver porque o jogo dele já começou")
        L.append(f"      📊 <b>{n(r['ganho'], 1, True)}</b> pts/semana · <i>{origem}</i>")
        L.append("")
    return L


def relatorio_waiver(ctx, est, abertos, congelados, usados, jogos_noite=()) -> list[str]:
    """Adds (livres, entram na hora) e claims (em waiver). Quem e livre e
    jogou na rodada da noite foi pra waiver quando o jogo comecou: vira
    claim, nao add."""
    times = {t for j in jogos_noite for t in j.times if j.comecou(ctx.agora)}
    livres = ctx.sl.free_agents(ctx.pool)
    waiver = em_waiver(ctx) | {p for p in livres if ctx.team(p) in times}
    fa = [p for p in livres if p not in waiver]
    L = []
    picks = pickups(ctx, est, abertos, congelados, usados, fa, limite=4) if abertos else []
    if picks:
        L += linhas_pickups(ctx, picks, "Adds", "➕", "livres: entram na hora · semana + temporada")
    L += linhas_claims(ctx, claims(ctx, est, waiver))
    if not L and jogos_noite:
        L += secao("📝", "Waiver")
        L.append("<i>nenhum livre nem claim melhora o time agora</i>")
    return L


def trocas_da_semana(ctx) -> tuple[list[str], list[str]]:
    """(linhas, chaves das propostas mostradas), ate o prazo. Segunda-feira
    (dia em que a semana do Lock-In comeca) vem a lista inteira; nos outros
    dias so a proposta que nunca foi sugerida (uma lesao, um livre que
    alguem pegou), e nada se nao tiver novidade."""
    cfg = ctx.cfg.get("trocas") or {}
    segunda = int(ctx.hora(ctx.agora, "%w")) == int(cfg.get("dia_semana", 1))
    if not segunda and not cfg.get("novidades", True):
        return [], []
    if ctx.week > int(ctx.sl.settings().get("trade_deadline") or 99):
        return [], []
    try:
        tops, _, semanas = calcular_trocas(ctx)
    except Exception as e:
        ctx.warnings.append(f"trocas nao calculadas ({e})")
        return [], []
    if not segunda:
        tops = [t for t in tops if not ja_avisado(AVISOS_TROCAS, chave_proposta(t))]
    rotulo = "Trocas" if segunda else ("Trocas novas" if len(tops) > 1 else "Troca nova")
    return linhas_trocas(ctx, tops, semanas, rotulo), [chave_proposta(t) for t in tops]


def main_with(args, config=None) -> int:
    ctx = build(args, config)
    hoje = ctx.hora(ctx.agora, "%Y-%m-%d")
    hora_envio = int((ctx.cfg.get("dia") or {}).get("hora", 10))
    chave = f"dia:{hoje}"
    if getattr(args, "gate", False):
        if int(ctx.hora(ctx.agora, "%H")) < hora_envio:
            print(f"[{ctx.rotulo}] relatorio do dia sai a partir das {hora_envio}h")
            return 0
        if ja_avisado(AVISOS, chave):
            print(f"[{ctx.rotulo}] relatorio do dia ja saiu")
            return 0

    enquete.sincronizar(ctx.cfg, avisar=not ctx.args.dry_run)
    est = estado(ctx)
    if not est.elenco:
        # Antes do draft o elenco e vazio e todo livre "melhora o time".
        print(f"[{ctx.rotulo}] elenco vazio (o draft ainda nao aconteceu)")
        return 0
    decs = travas(ctx, est)
    # A escalacao e a do proximo dia de jogo seu: hoje, ou o primeiro dia com
    # jogo (entre hoje e ele ninguem joga, entao nada trava no meio).
    dia = proximo_dia(ctx, est)
    alvo = replace(est, hoje=dia) if dia else est
    e, mud, ganho, hoje_op, futuro, abertos, congelados, usados = escalacao_hoje(ctx, alvo, decs)
    fa = ctx.sl.free_agents(ctx.pool)

    L = [titulo("DIA", ctx.league_name, ctx.hora(ctx.agora, "%a %d/%m"))]
    jogo_hoje = dia == est.hoje and bool(hoje_op)
    if est.travados or est.pendentes or jogo_hoje:
        nota = f"{len(est.travados)}/{len(est.slots)} travados"
        if est.abertos and dia:
            nota += f" · os slots abertos ainda devem render ~{n(e.valor_semana)}"
        L += secao("📊", f"Semana {est.week}", nota)
        L += linhas_travados(ctx, est)
    if jogo_hoje:
        L += linhas_lineup(ctx, alvo, e, mud, ganho, hoje_op, decs)
    elif est.abertos:
        L += sem_jogo_hoje(ctx, dia, mud, hoje_op, set(e.segurados.values()))

    # Adds e claims vem no fim da noite; aqui so se a da noite nao saiu
    ontem = (date.fromisoformat(hoje_et(ctx.agora)) - timedelta(days=1)).isoformat()
    if not ja_avisado(AVISOS_NOITE, f"noite:{ontem}"):
        L += relatorio_waiver(ctx, est, abertos, congelados, usados)
    sub = subindo(ctx, fa)
    if sub:
        L += secao("📈", "Livres subindo de papel")
        L += sub
    meus = meu_elenco(ctx, est)
    if meus:
        L += secao("👥", "Seu elenco")
        L += meus
    tr, propostas = trocas_da_semana(ctx)
    if tr:
        L += tr
    ok = ctx.notify(bloco(L) + rodape(ctx.warnings))
    if ok:
        marcar_avisado(AVISOS, chave)
        for k in propostas:
            marcar_avisado(AVISOS_TROCAS, k)
    return 0


def main(argv=None) -> int:
    args = base_parser(__doc__.splitlines()[0]).parse_args(argv)
    return run_for_each_league(args, main_with)


if __name__ == "__main__":
    sys.exit(main())
