"""Board de draft: quando e, e quem pegar.

Roda antes e DURANTE o draft. Duas coisas:

  * a data do draft da liga (do Sleeper), com status e formato;
  * os melhores disponiveis por VOR no formato Lock-In, ja descontando quem
    ja foi escolhido.

O ranking de partida e o que existe pra NBA sem chave: a projecao de
temporada do Sleeper (a linha estatistica que ele espera, pontuada aqui
pela regra da liga) e o ADP do Sleeper como termometro do mercado. O valor
NAO e a media: e quanto o jogador rende por semana num slot do Lock-In,
que paga variancia e cobra semana perdida (veja valor.py).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clients import CACHE, esc
from lineup import ELEGIVEIS, otimizar, starting_slots
from contexte import (base_parser, build, ja_avisado, marcar_avisado,
                 run_for_each_league)
from report import bloco, jogador, n, rodape, titulo
from valor import tabela


def draft_info(ctx) -> list[dict]:
    """Drafts da liga, com data ja convertida."""
    try:
        drafts = ctx.sl.drafts()
    except Exception as e:
        ctx.warnings.append(f"draft indisponivel ({e})")
        return []
    out = []
    for d in drafts:
        st = d.get("start_time")
        s = d.get("settings") or {}
        out.append({
            "id": d.get("draft_id"),
            "ordem": d.get("draft_order") or {},
            "status": d.get("status"),
            "type": d.get("type"),
            # start_time vem em MILISSEGUNDOS; sem a divisao vira ano 57000.
            # E com fuso explicito: sem ele a hora do draft saia no fuso da
            # maquina — ET na nuvem, BRT aqui — e a janela do gate mudava de
            # tamanho dependendo de onde o bot rodou.
            "quando": datetime.fromtimestamp(st / 1000, timezone.utc) if st else None,
            "rounds": s.get("rounds"),
            "teams": s.get("teams"),
            "timer": s.get("pick_timer"),
            # 3RR: a rodada `reversal_round` repete o sentido da anterior em
            # vez de virar (0 = snake puro).
            "reversao": s.get("reversal_round") or 0,
            # Pausa automatica de toda noite, em minutos do dia em UTC. A API
            # nao diz o fuso; UTC e o que bate com a liga de 2026 (pausa de
            # 60 a 660 = 22h-8h BRT, e o draft marcado pras 8h BRT).
            "pausa": ((s.get("autopause_start_time"), s.get("autopause_end_time"))
                      if s.get("autopause_enabled") else None),
            # quando o ultimo pick saiu: e quando o relogio do pick da vez comecou
            "ultimo_pick": (datetime.fromtimestamp(d["last_picked"] / 1000, timezone.utc)
                            if d.get("last_picked") else None),
        })
    return out


# Um draft de 4h por pick e um de 2 minutos precisam de coisas opostas.
# Acima deste limite (em minutos por pick) da tempo de ler Telegram e agir;
# abaixo, nao da — a mensagem tem que chegar ANTES.
LENTO_MIN = 30

# Alerta que repete e alerta que ninguem le. Sem marcar o que ja saiu, o
# mesmo "faltam 2 picks" sairia a cada passada do agendador ate voce
# escolher. O arquivo vive no cache que o GitHub Actions restaura entre
# execucoes; se ele sumir, o pior que acontece e um aviso repetido — muito
# melhor que o silencio que este arquivo existe pra evitar.
AVISOS = CACHE / "draft_avisos.json"
# A API nao da o horario de CADA pick, so o do ultimo (`last_picked`). O
# vigia olha de minuto em minuto e anota {picks feitos: horario do ultimo}:
# e daqui que sai o ritmo por rodada.
RITMO = CACHE / "draft_ritmo.json"


def lento(draft: dict) -> bool:
    """Draft de relogio longo — o que aceita mensagem no meio.

    `pick_timer` vem em SEGUNDOS. Zero e "sem relogio nenhum": o draft mais
    lento que existe, e nao o mais rapido, que era como a conta antiga o
    classificava (0/60 = 0 minutos por pick).
    """
    timer = draft.get("timer") or 0
    return timer == 0 or timer / 60 >= LENTO_MIN


# Quanta antecedencia o aviso tem que dar, em MINUTOS. Nao em picks: dois
# picks numa liga sao 17 minutos e noutra sao 8 horas. E o agendador do
# GitHub atrasa ate 59 minutos, entao aviso de 17 minutos chega junto com a
# sua vez — que foi exatamente o que aconteceu em 03/09/2026.
ANTECEDENCIA_MIN = 45

# Teto de picks de aviso. Sem ele, uma liga que despachou 40 picks em dois
# minutos (autopick em massa) daria um limiar de centenas de picks e o bot
# avisaria o draft inteiro.
LIMIAR_MAX = 10


def minutos_pausados(pausa, ini, fim) -> float:
    """Quanto de [ini, fim] caiu dentro da pausa de toda noite (minutos UTC)."""
    if not pausa or None in pausa or fim <= ini:
        return 0.0
    a, b = pausa
    janelas = [(a, b)] if a < b else [(a, 24 * 60), (0, b)]
    tot = 0.0
    dia = datetime(ini.year, ini.month, ini.day, tzinfo=timezone.utc)
    while dia <= fim:
        for x, y in janelas:
            j0, j1 = dia + timedelta(minutes=x), dia + timedelta(minutes=y)
            tot += max(0.0, (min(fim, j1) - max(ini, j0)).total_seconds() / 60)
        dia += timedelta(days=1)
    return tot


def em_pausa(pausa, t) -> bool:
    if not pausa or None in pausa:
        return False
    a, b = pausa
    m = t.hour * 60 + t.minute + t.second / 60
    return a <= m < b if a < b else (m >= a or m < b)


def avancar(ini, minutos: float, pausa):
    """O instante em que `minutos` de relogio ATIVO terminam, a partir de
    `ini`, pulando a pausa de toda noite."""
    t, resta = ini, float(minutos)
    passo = timedelta(minutes=1)
    # anda de minuto em minuto so dentro/perto da pausa; fora dela, em blocos
    while resta > 1e-9:
        if em_pausa(pausa, t):
            t += passo
            continue
        bloco = min(resta, 30.0)
        fim = t + timedelta(minutes=bloco)
        if pausa and None not in pausa and minutos_pausados(pausa, t, fim) > 0:
            bloco = 1.0 if resta >= 1 else resta
            fim = t + timedelta(minutes=bloco)
        t, resta = fim, resta - bloco
    return t


def previsao_fim(draft: dict, feitos: int, agora, min_ate: float, min_depois: float,
                 rodada_troca: int = 11):
    """Quando o draft acaba se cada pick usar o tempo inteiro: `min_ate`
    minutos por pick ate a rodada `rodada_troca - 1`, `min_depois` dali em
    diante. O pick da vez ja gastou o que correu desde o ultimo pick."""
    times, rodadas = draft.get("teams"), draft.get("rounds")
    if not times or not rodadas:
        return None
    total = times * rodadas
    if feitos >= total:
        return None

    def tempo(k):  # k = numero do pick, 1-based
        return min_depois if (k - 1) // times + 1 >= rodada_troca else min_ate

    inicio_vez = draft.get("ultimo_pick") or draft.get("quando") or agora
    gasto = max(0.0, (agora - inicio_vez).total_seconds() / 60
                - minutos_pausados(draft.get("pausa"), inicio_vez, agora))
    minutos = max(0.0, tempo(feitos + 1) - gasto)
    minutos += sum(tempo(k) for k in range(feitos + 2, total + 1))
    return avancar(agora, minutos, draft.get("pausa"))


def anotar_pick(draft: dict, feitos: int) -> dict:
    """Guarda o horario do ultimo pick visto e devolve {feitos: datetime}."""
    try:
        tudo = json.loads(RITMO.read_text(encoding="utf-8"))
    except Exception:
        tudo = {}
    obs = tudo.setdefault(str(draft["id"]), {})
    if draft.get("quando"):
        obs.setdefault("0", draft["quando"].timestamp())
    if feitos and draft.get("ultimo_pick") and str(feitos) not in obs:
        obs[str(feitos)] = draft["ultimo_pick"].timestamp()
        try:
            RITMO.write_text(json.dumps(tudo), encoding="utf-8")
        except OSError as e:
            print(f"(nao gravei o ritmo: {e})")
    return {int(k): datetime.fromtimestamp(v, timezone.utc) for k, v in obs.items()}


def ritmo_por_rodada(obs: dict, times: int, pausa) -> dict[int, float]:
    """{rodada: minutos ativos por pick}. Entre duas anotacoes, o tempo se
    divide igual pelos picks do meio (o vigia pode ter perdido algum)."""
    pts = sorted(obs.items())
    por_pick: dict[int, float] = {}
    for (n0, t0), (n1, t1) in zip(pts, pts[1:]):
        if n1 <= n0:
            continue
        m = max(0.0, (t1 - t0).total_seconds() / 60 - minutos_pausados(pausa, t0, t1))
        for k in range(n0 + 1, n1 + 1):
            por_pick[k] = m / (n1 - n0)
    rod: dict[int, list[float]] = {}
    for k, m in por_pick.items():
        rod.setdefault((k - 1) // times + 1, []).append(m)
    # rodada com menos de 1/3 dos picks medidos ainda nao diz nada
    return {r: sum(v) / len(v) for r, v in sorted(rod.items())
            if len(v) >= max(1, times // 3)}


def ritmo(draft: dict, feitos: int, agora) -> float | None:
    """Minutos por pick MEDIDOS neste draft. None se ainda nao da pra saber.

    O `pick_timer` nao serve: ele e o teto, nao o ritmo. Numa liga de NFL o teto
    e de 4 horas e o ritmo medido foi de 8 minutos — 30 vezes menor.

    A pausa da noite sai da conta: no segundo dia, 10h parado divididas
    pelos picks davam um ritmo de meia hora num draft de 3 minutos, e o
    aviso encolhia pro piso de 2 picks.
    """
    quando = draft.get("quando")
    if not quando or not feitos:
        return None
    minutos = ((agora - quando).total_seconds() / 60
               - minutos_pausados(draft.get("pausa"), quando, agora))
    return minutos / feitos if minutos > 0 else None


def limiar(draft: dict, feitos: int, agora, piso: int = 2) -> int:
    """Com quantos picks de antecedencia avisar, no ritmo medido.

    `piso` e o que vale enquanto nao ha ritmo (draft sem data, primeiro pick)
    — e tambem o minimo: mesmo num draft lentissimo o aviso sai com 2 picks.
    """
    r = ritmo(draft, feitos, agora)
    if not r:
        return piso
    return max(piso, min(LIMIAR_MAX, int(ANTECEDENCIA_MIN / r)))


def _avisado(chave: str) -> bool:
    return ja_avisado(AVISOS, chave)


def _marcar(chave: str) -> None:
    marcar_avisado(AVISOS, chave)


def minha_vez(ctx, draft: dict, feitos: int) -> dict | None:
    """Onde eu estou na fila. None se a ordem ainda nao foi sorteada.

    Snake: em rodada impar a ordem e 1..n; em par ela inverte. Sem isso, a
    conta erra em toda rodada par — que e metade do draft.
    Com 3RR (`reversao` = 3) a rodada 3 repete o sentido da 2 e dali em
    diante tudo troca de paridade. Ignorar isso em 2026 mandou "faltam 12
    picks" com o pick 94 ja no relogio.
    """
    ordem = (draft.get("ordem") or {})
    slot = ordem.get(str(ctx.sl.user_id))
    times, rodadas = draft.get("teams"), draft.get("rounds")
    if not slot or not times or not rodadas:
        return None

    rev = int(draft.get("reversao") or 0)
    meus = []
    for r in range(1, int(rodadas) + 1):
        base = (r - 1) * times
        invertida = (r % 2 == 0) != (rev > 1 and r >= rev)
        meus.append(base + (times - slot + 1 if invertida else slot))

    proximo = next((p for p in meus if p > feitos), None)
    return {"slot": slot, "picks": meus, "proximo": proximo,
            "faltam": (proximo - feitos - 1) if proximo else None,
            "rodada": (meus.index(proximo) + 1) if proximo else None}


def picks(ctx, draft_id: str) -> list[dict]:
    """Picks do draft, na ordem.

    E daqui que sai TAMBEM o meu elenco. Nao da pra usar o roster da liga:
    durante o draft ele vem vazio (`players: null` — conferido na Sleeper em
    03/09/2026 com um draft de NFL ja rolando). Quem decide o que
    mostrar com base num elenco vazio recomenda kicker na primeira rodada.
    """
    if not draft_id:
        return []
    try:
        return ctx.sl.draft_picks(draft_id)
    except Exception as e:
        ctx.warnings.append(f"picks do draft indisponiveis ({e})")
        return []


def escolhidos(ps: list[dict]) -> set[str]:
    """player_ids que ja sairam do board."""
    return {str(p.get("player_id")) for p in ps if p.get("player_id")}


def meus_jogadores(ctx, ps: list[dict], slot: int | None = None) -> list[str]:
    """player_ids que EU ja escolhi.

    `picked_by` e o dono do pick e resolve troca de pick; mas vem vazio no
    autopick, e ai o `draft_slot` (a minha vaga na ordem) desempata. Um sem
    o outro erra um caso cada.
    """
    uid = str(ctx.sl.user_id)
    out = []
    for p in ps:
        dono = str(p.get("picked_by") or "")
        meu = dono == uid or (not dono and slot and p.get("draft_slot") == slot)
        if meu and p.get("player_id"):
            out.append(str(p["player_id"]))
    return out


def falta(ctx, meus: list[str]) -> list[str]:
    """Slots titulares que o meu elenco ainda nao consegue preencher.

    Na NBA a pergunta nao e "quantos de cada posicao" — um ala listado como
    SF/PF cobre dois slots. E se existe uma escalacao legal: a atribuicao
    exata responde, e o que sobra vazio e o que falta.
    """
    slots = starting_slots(ctx.sl.roster_positions())
    assign, _ = otimizar(slots, meus, lambda p, i: 1.0, ctx.posicoes)
    return [s for s, a in zip(slots, assign) if a is None]


def foco(faltando: list[str], restam: int | None) -> set[str] | None:
    """Posicoes que o board deve mostrar. None = todas.

    So estreita quando os picks que sobram nao cobrem mais o que falta: ai
    acabou o melhor-disponivel, cada pick restante ja tem dono. Antes disso
    filtrar por necessidade e o erro classico — o VOR ja cobra o preco de
    cada posicao.
    """
    if faltando and restam is not None and restam <= len(faltando):
        aceitas = set()
        for s in faltando:
            aceitas |= ELEGIVEIS[s]
        return aceitas
    return None


def board(ctx, taken: set[str], limit: int = 25,
          posicoes: set[str] | None = None) -> list[dict]:
    """Melhores disponiveis por VOR semanal no Lock-In."""
    rows = ctx._cache.get("tabela")
    if rows is None:
        from modelo import carregar_params
        P = carregar_params()
        rows = tabela(ctx, lambda mu: max(2.0, P["sd_a"] + P["sd_b"] * mu))
        notas = (ctx.cfg.get("draft") or {}).get("notas") or {}
        for r in rows:
            r["nota"] = notas.get(ctx.name(r["pid"]))
        ctx._cache["tabela"] = rows
    out = [r for r in rows if r["pid"] not in taken
           and (posicoes is None or set(r["posicoes"]) & posicoes)]
    return out[:limit]


def _sinais(r: dict) -> list[str]:
    """O que o ranking do mercado (ADP) nao mostra e o Lock-In paga."""
    s = []
    if r.get("rank_adp") and r["rank"] - r["rank_adp"] <= -12:
        s.append(f"🟢 mercado dorme: ADP {r['rank_adp'] - r['rank']} posições depois")
    elif r.get("rank_adp") and r["rank"] - r["rank_adp"] >= 12:
        s.append(f"🔴 mercado paga caro: ADP {r['rank'] - r['rank_adp']} posições antes")
    if r["mu"] and r["sd"] / r["mu"] >= 0.45 and r["mu"] >= 15:
        s.append("🎢 oscila muito: bom pra travar")
    if r.get("tend") is not None and abs(r["tend"]) >= 4:
        seta = "📈" if r["tend"] > 0 else "📉"
        s.append(f"{seta} terminou 2025 com {r['tend']:+.0f} min "
                 f"({'+3' if r['tend'] > 0 else '-6,5'}% no valor)")
    if r.get("status") in ("Out", "IR"):
        ret = r.get("retorno")
        quando = f" ate {ret[8:10]}/{ret[5:7]}/{ret[2:4]}" if ret else ""
        quanto = (f"{r['fora']:.0%} da temporada fora" if r.get("fora") is not None
                  else "sem prazo: conto 1/4 da temporada fora")
        s.append(f"🚑 {r['status']}{quando}" + (f" ({r['lesao']})" if r.get("lesao") else "")
                 + f": {quanto}")
    if r.get("disp") == 0:
        s.append("🩹 nao jogou em 2025 (volta de lesao longa)")
    elif r.get("disp") is not None and r["disp"] < 0.75:
        s.append(f"🩹 jogou {r['disp']:.0%} das semanas em 2025")
    c = r.get("carga")
    if c and c[0] >= 20 and c[2] >= 5:
        normal, seg = c[1] / c[0], c[3] / c[2]
        if normal < 0.82 or seg < 0.6:
            s.append(f"🛌 controle de carga: jogou {normal:.0%} dos jogos e {seg:.0%} dos b2b "
                     "nas semanas ativas")
    if r["fonte"] == "2025":
        s.append("❔ sem projecao: media de 2025")
    if r.get("time_ant") and r.get("team") and r["time_ant"] != r["team"]:
        s.append(f"🔁 time novo: {r['time_ant']} → {r['team']}")
    if r.get("min_ant") and r.get("min_proj") and abs(r["min_proj"] - r["min_ant"]) >= 4:
        s.append(f"{'⬆️ papel maior' if r['min_proj'] > r['min_ant'] else '⬇️ papel menor'}: "
                 f"{r['min_ant']:.0f} → {r['min_proj']:.0f} min projetados")
    if r.get("nota"):
        s.append(f"📝 {r['nota']}")
    return s


SEPARADOR = "━━━━━━━━━━━━━━━"


def render(ctx, drafts, rows, taken, vez=None, por_posicao=False, evento="",
           meus=None, faltando=None, focado=False, eta=None, so_pos=None,
           compacto=False) -> str:
    d = drafts[0] if drafts else {}
    quando = (f"{ctx.hora(d['quando'], '%d/%m %H:%M')}" if d.get("quando")
              else "sem data marcada")
    sub = (f"{quando} · {d.get('type', '?')} · {d.get('rounds', '?')} rodadas · "
           f"{d.get('teams', '?')} times")
    L = [f"🏀 <b>DRAFT</b> · {esc(ctx.league_name)}",
         f"<i>📅 {esc(sub)}</i>"]
    if d:
        L.append(f"<i>⏳ {esc(d.get('status'))} · {len(taken)} picks feitos</i>")
    if evento == "comecou":
        L += ["", "🚨 <b>O DRAFT COMEÇOU</b> 🚨"]
    if vez:
        L.append("")
        if vez.get("faltam") == 0:
            L.append("🔔🔔 <b>É A SUA VEZ</b> 🔔🔔")
            L.append(f"<i>pick {vez['proximo']} · rodada {vez['rodada']}</i>")
        elif vez.get("faltam") is not None:
            quanto = f" · ⏱️ <b>~{eta:.0f} min</b>" if eta else ""
            L.append(f"⏳ <b>Faltam {vez['faltam']} picks</b> pra sua vez{quanto}")
            L.append(f"<i>pick {vez['proximo']} · rodada {vez['rodada']}</i>")
        else:
            L.append("<i>✅ seus picks acabaram</i>")

    if meus or faltando:
        L.append("")
    if meus:
        L.append("👥 <b>Seu elenco</b>")
        L.append("<i>" + esc(" · ".join(
            f"{ctx.name(p).split()[-1]} {ctx.pos(p)}" for p in meus)) + "</i>")
    if faltando:
        L.append("🧩 <b>Falta preencher:</b> <code>" + esc(" ".join(faltando)) + "</code>")

    L += ["", SEPARADOR]
    L.append("🏆 <b>MELHORES DISPONÍVEIS</b>")
    if focado:
        L.append("<i>🎯 só o que ainda falta</i>")
    if so_pos:
        L.append(f"<i>🎯 só {esc('/'.join(sorted(so_pos)))}</i>")
    L.append("<i>VOR = pts/semana acima da reposição da posição</i>")
    L.append("")
    medalha = {1: "🥇", 2: "🥈", 3: "🥉"}

    def linha(k, r):
        adp = f" · ADP {r['adp']:.0f}" if r.get("adp") else ""
        if compacto:
            L.append(f"{k}. {esc(r['nome'])} <i>{esc(r['pos'])} {esc(r['team'])}</i> "
                     f"<b>{n(r['vor'], 1, True)}</b>{esc(adp)}")
            return
        L.append(f"{medalha.get(k, f'<b>{k}.</b>')} <b>{esc(r['nome'])}</b> "
                 f"<i>{esc(r['pos'])} · {esc(r['team'])}</i>")
        rk = f"nosso #{r['rank']}" + (f" · ADP #{r['rank_adp']}" if r.get("rank_adp") else "")
        L.append(f"      💎 <b>{n(r['vor'], 1, True)}</b> VOR · {n(r['semana'])} pts/sem · "
                 f"{n(r['mu'])} pts/jogo")
        L.append(f"      🏷️ <b>{esc(rk)}</b>")
        for sinal in _sinais(r):
            L.append(f"      <i>{esc(sinal)}</i>")
        L.append("")

    for k, r in enumerate(rows, 1):
        linha(k, r)

    # Os que voce fixou (draft.fixados) aparecem sempre no fim, com a posicao
    # que eles tem no board inteiro, enquanto ninguem pegar.
    fixados = (ctx.cfg.get("draft") or {}).get("fixados") or []
    if fixados:
        mostrados = {r["pid"] for r in rows}
        todos = board(ctx, taken, limit=10 ** 6)
        fila = [(k, r) for k, r in enumerate(todos, 1)
                if r["nome"] in fixados and r["pid"] not in mostrados]
        if fila:
            L += [SEPARADOR, "📌 <b>DE OLHO</b>", ""]
            for k, r in fila:
                linha(k, r)

    if por_posicao:
        L += [SEPARADOR, "🗂️ <b>MELHORES DE CADA POSIÇÃO</b>"]
        for pos in ("PG", "SG", "SF", "PF", "C"):
            fila = [r for r in rows if pos in r["posicoes"]][:6]
            if fila:
                L.append(f"<b>{pos}</b> <i>" + esc(" · ".join(
                    f"{r['nome'].split()[-1]} {n(r['vor'], 1, True)}" for r in fila)) + "</i>")
    return bloco(L) + rodape(ctx.warnings)


def main_with(args, config=None) -> int:
    ctx = build(args, config)
    drafts = draft_info(ctx)
    d = next((x for x in drafts if x["status"] != "complete"), None)
    if not d:
        print(f"[{ctx.rotulo}] nenhum draft em aberto")
        return 0

    ps = picks(ctx, d["id"])
    taken = escolhidos(ps)
    vez = minha_vez(ctx, d, len(taken))
    devagar = lento(d)
    meus = meus_jogadores(ctx, ps, vez["slot"] if vez else None)
    # Quantos picks ainda sao meus. Sem a ordem sorteada nao da pra saber, e
    # ai o board nao estreita: `None` e "nao sei", nao "zero".
    restam = (sum(1 for k in vez["picks"] if k > len(taken)) if vez else None)
    faltando = falta(ctx, meus)
    posicoes = foco(faltando, restam)
    # `--pos` e uma PERGUNTA ("quem sobrou de RB?"), nao uma sugestao de
    # estrategia — por isso passa por cima do foco automatico em vez de
    # se somar a ele.
    so_pos = {p.upper() for p in (getattr(args, "pos", None) or [])} or None
    if so_pos:
        posicoes = so_pos
    # O vigia pergunta a cada liga se vale ficar no ar. Sem `--vigiar-em`,
    # qualquer draft aberto segura (e o disparo manual, que voce arma na mao
    # e quer que fique). Com ele, so segura quando o MEU pick esta a caminho
    # — que e a unica janela em que um minuto de diferenca muda alguma
    # coisa. Fora dela o processo sai e nao queima maquina.
    # UM numero decide as duas coisas: com quanta antecedencia falar e ate
    # quando ficar no ar. Se o vigia saisse antes da janela do aviso, o
    # aviso dependeria de o agendador entregar na hora exata — que e
    # exatamente o que ele nao faz.
    aviso = limiar(d, len(taken), ctx.agora,
                   int(getattr(args, "avisar_em", 2) or 2))
    dentro = (devagar and vez and vez.get("faltam") is not None
              and vez["faltam"] <= aviso)
    # Draft andando (ou a meia hora de comecar): o vigia fica no ar mesmo
    # longe da sua vez. Cada passada do cron segura ~50 min e a proxima
    # entra na fila atras dela, entao a vigilia e continua — e um atraso do
    # agendador do GitHub nao vira um pick perdido.
    perto_do_inicio = (d["status"] == "pre_draft" and d.get("quando") is not None
                       and (d["quando"] - ctx.agora).total_seconds() <= 1800)
    ativo = devagar and (d["status"] == "drafting" or perto_do_inicio)
    if not getattr(args, "so_perto", False) or dentro or ativo:
        args._vale_ficar = True

    # Quantos jogadores mandar e QUANDO mandar dependem do ritmo do draft.
    # Lento: da tempo de ler e agir, entao manda quando chegar perto da sua
    # vez, com uma lista curta e acionavel.
    # Ao vivo: entre picks voce nao le Telegram. A mensagem e uma COLA que
    # chega antes, longa e por posicao.
    limite = getattr(args, "limit", None)
    if not limite:
        limite = (ctx.cfg.get("draft") or {}).get("limite") or (20 if devagar else 60)

    obs = anotar_pick(d, len(taken)) if not getattr(args, "dry_run", False) else {}
    if getattr(args, "gate", False):
        comissario(ctx, d, len(taken), args, obs)

    evento = chave = ""
    if getattr(args, "gate", False):
        evento, chave, motivo = _evento(ctx, d, vez, devagar, args, aviso)
        if not evento:
            print(f"[{ctx.rotulo}] {motivo}")
            return 0

    rows = board(ctx, taken, limite, posicoes)
    r = ritmo(d, len(taken), ctx.agora)
    eta = (vez["faltam"] * r) if (r and vez
                                  and vez.get("faltam")) else None
    ctx.notify(render(ctx, drafts, rows, taken, vez,
                      # a cola por posicao nao tem o que dizer quando a
                      # lista inteira ja saiu filtrada por posicao.
                      por_posicao=not devagar and not so_pos,
                      evento=evento, meus=meus,
                      faltando=faltando, eta=eta, so_pos=so_pos,
                      compacto=bool(getattr(args, "compacto", False)),
                      focado=bool(posicoes and not so_pos)))
    # Marcar num --dry-run apagaria o alerta de verdade: o proximo ciclo
    # veria "ja avisei" de uma mensagem que ninguem recebeu.
    if chave and not getattr(args, "dry_run", False):
        _marcar(chave)
        # "comecou" e "e a sua vez" no mesmo minuto sao a mesma mensagem duas
        # vezes: a que acabou de sair ja mostra a sua vez.
        if (evento == "comecou" and vez and vez.get("faltam") is not None
                and vez["faltam"] <= aviso):
            estado = "agora" if vez["faltam"] == 0 else "perto"
            _marcar(f"{d['id']}:vez:{vez['proximo']}:{estado}")
    return 0


# Voce e o comissario: a partir da rodada 11 o tempo de pick cai pra 30 min,
# e quem muda isso e voce, na mao, no Sleeper. O bot so lembra.
RODADA_TROCA = 11
MIN_DEPOIS_DA_TROCA = 30
HORAS_PREVISAO = (8, 12, 16, 20)


def comissario(ctx, d, feitos: int, args, obs: dict | None = None) -> None:
    """A previsao de fim do draft (8h, 12h, 16h e 20h); e o alarme da rodada 11."""
    if d["status"] not in ("drafting", "paused") or not d.get("teams"):
        return
    seco = getattr(args, "dry_run", False)
    timer_min = (d.get("timer") or 0) / 60
    rodada = feitos // d["teams"] + 1

    # ALARME: rodada 11 no relogio e o tempo ainda acima de 30 min. Repete de
    # hora em hora ate o tempo mudar (o bot le o pick_timer do draft).
    if rodada >= RODADA_TROCA and timer_min > MIN_DEPOIS_DA_TROCA:
        chave = f"{d['id']}:r11:{ctx.agora:%Y%m%d%H}"
        if not _avisado(chave):
            L = ["🚨🚨🚨🚨🚨🚨🚨🚨",
                 f"<b>RODADA {rodada} CHEGOU!</b>",
                 "",
                 f"<b>⚠️ MUDE O TEMPO DE PICK PRA {MIN_DEPOIS_DA_TROCA} MINUTOS AGORA ⚠️</b>",
                 "",
                 f"<i>hoje está em {timer_min:.0f} min. Configurações do draft no Sleeper "
                 "(você é o comissário). Eu repito de hora em hora ate mudar.</i>",
                 "🚨🚨🚨🚨🚨🚨🚨🚨"]
            if ctx.notify(bloco(L)) and not seco:
                _marcar(chave)

    # PREVISAO: nos horarios do seu fuso em HORAS_PREVISAO. Se o cron atrasar,
    # sai na primeira passada depois do horario (uma vez por horario).
    hora = int(ctx.hora(ctx.agora, "%H"))
    passou = [h for h in HORAS_PREVISAO if h <= hora]
    if not passou:
        return
    chave = f"{d['id']}:previsao:{ctx.hora(ctx.agora, '%Y%m%d')}:{passou[-1]}"
    if _avisado(chave):
        return
    ate = timer_min if rodada < RODADA_TROCA else MIN_DEPOIS_DA_TROCA
    fim_max = previsao_fim(d, feitos, ctx.agora, ate, MIN_DEPOIS_DA_TROCA, RODADA_TROCA)
    if not fim_max:
        return
    total = d["teams"] * d["rounds"]
    # media por pick ate o ULTIMO pick (o da vez ainda nao terminou)
    r = None
    if feitos and d.get("quando") and d.get("ultimo_pick"):
        ativo = ((d["ultimo_pick"] - d["quando"]).total_seconds() / 60
                 - minutos_pausados(d.get("pausa"), d["quando"], d["ultimo_pick"]))
        r = ativo / feitos if ativo > 0 else None
    fim_ritmo = None
    if r:
        corre = max(0.0, (ctx.agora - d["ultimo_pick"]).total_seconds() / 60
                    - minutos_pausados(d.get("pausa"), d["ultimo_pick"], ctx.agora))
        fim_ritmo = avancar(ctx.agora, max(0.0, r - corre) + r * (total - feitos - 1),
                            d.get("pausa"))
    rodadas = ritmo_por_rodada(obs or {}, d["teams"], d.get("pausa"))
    troca_pick = (RODADA_TROCA - 1) * d["teams"]
    L = ["🔮 <b>PREVISÃO DO FIM DO DRAFT</b>",
         f"<i>📅 {ctx.hora(ctx.agora, '%a %d/%m %H:%M')} · pick {feitos + 1} de {total} · "
         f"rodada {rodada}</i>",
         "",
         "⏳ <b>Pior caso</b> <i>(todo mundo no tempo máximo)</i>",
         f"      🏁 <b>{ctx.hora(fim_max, '%a %d/%m %H:%M')}</b>",
         f"      <i>{ate:.0f} min por pick até a rodada {RODADA_TROCA - 1}, "
         f"{MIN_DEPOIS_DA_TROCA} min da {RODADA_TROCA}a em diante, pausa da noite fora</i>"]
    if fim_ritmo:
        L += ["",
              "📊 <b>Pela média de tempo por pick até agora</b>",
              f"      🏁 <b>{ctx.hora(fim_ritmo, '%a %d/%m %H:%M')}</b>",
              f"      <i>~{r:.0f} min por pick em {feitos} picks, pausa da noite fora</i>"]
    if len(rodadas) >= 1:
        partes = []
        for k, (rod, m) in enumerate(rodadas.items()):
            seta = ""
            if k:
                ant = list(rodadas.values())[k - 1]
                seta = " ↗️" if m > ant * 1.1 else " ↘️" if m < ant * 0.9 else " ➡️"
            atual = "*" if rod == rodada else ""
            partes.append(f"R{rod}{atual} {m:.0f}min{seta}")
        L += ["", "⏱️ <b>Média por rodada</b>", "      " + esc(" · ".join(partes))]
        vals = list(rodadas.values())
        if len(vals) >= 2:
            a, b = vals[-2], vals[-1]
            tend = ("📈 <b>subindo</b>" if b > a * 1.1 else "📉 <b>caindo</b>"
                    if b < a * 0.9 else "➡️ <b>estável</b>")
            L.append(f"      tendência: {tend} <i>(última rodada vs anterior; * = em andamento)</i>")
    if feitos < troca_pick:
        chega = previsao_fim(dict(d, rounds=RODADA_TROCA - 1), feitos, ctx.agora,
                             ate, MIN_DEPOIS_DA_TROCA, RODADA_TROCA)
        if chega:
            L += ["",
                  f"🔔 <b>Rodada {RODADA_TROCA}</b> (hora de mudar o tempo) chega no máximo "
                  f"<b>{ctx.hora(chega, '%a %d/%m %H:%M')}</b>"]
    if ctx.notify(bloco(L)) and not seco:
        _marcar(chave)


def _evento(ctx, d, vez, devagar, args, aviso: int = 2) -> tuple[str, str, str]:
    """Que alerta este estado do draft merece.

    Devolve (evento, chave, motivo). Evento vazio = fica quieto, e `motivo`
    diz por que. A `chave` e o que impede o mesmo alerta de sair de novo na
    proxima passada do agendador.

    `aviso` e com quantos picks de antecedencia falar, ja calculado pelo
    ritmo medido do draft.

    Lento: quando a sua vez esta chegando — voce tem horas pra decidir.
    Ao vivo: uma vez, algumas horas ANTES de comecar. Durante o draft de 2min
    por pick, mandar mensagem e so barulho.
    E, nos dois ritmos, quando o draft COMECA.
    """
    if d["status"] not in ("pre_draft", "drafting", "paused"):
        return "", "", "draft encerrado"

    # Comecou. Vale pros dois ritmos e nao depende de data marcada: a liga
    # que combina o horario no grupo (start_time nulo) nunca entrava em
    # janela nenhuma, e por isso nunca recebia nada.
    if d["status"] in ("drafting", "paused"):
        chave = f"{d['id']}:comecou"
        if not _avisado(chave):
            return "comecou", chave, ""

    if devagar:
        if vez is None:
            return "", "", "ordem do draft ainda nao sorteada"
        faltam = vez.get("faltam")
        if faltam is None:
            return "", "", "seus picks acabaram"
        if faltam <= aviso:
            # Dois estados, duas mensagens: "prepara" e "e agora". Uma chave
            # so por pick daria o aviso de "faltam 2" e silencio na hora H,
            # que e a hora que importa. Alem desses dois, a chave cala o
            # resto — o agendador passa varias vezes antes de voce escolher.
            estado = "agora" if faltam == 0 else "perto"
            chave = f"{d['id']}:vez:{vez['proximo']}:{estado}"
            if not _avisado(chave):
                return "vez", chave, ""
            return "", "", f"ja avisei do pick {vez['proximo']} ({estado})"
        return "", "", f"faltam {faltam} picks pra sua vez (aviso em {aviso})"

    # ao vivo: janela antes do inicio
    quando = d.get("quando")
    if not quando:
        return "", "", "draft ao vivo sem data marcada (espero ele comecar)"
    horas = (quando - ctx.agora).total_seconds() / 3600
    lo, hi = 1.0, float(getattr(args, "antes_de", 6) or 6)
    if lo <= horas <= hi:
        chave = f"{d['id']}:cola"
        if not _avisado(chave):
            return "cola", chave, ""
        return "", "", "a cola do draft ao vivo ja foi"
    return "", "", f"faltam {horas:.1f}h pro draft ao vivo (janela {lo}-{hi}h)"


def vigia(args) -> int:
    """Fica de olho enquanto o draft anda.

    O agendador de hora em hora resolve "nao chegou mensagem"; nao resolve
    "chegou depois de eu perder o pick". Entre duas passadas, um draft de 10
    times anda uma rodada inteira — e o aviso de "faltam 2 picks" vira
    noticia velha. Aqui o mesmo processo fica no ar olhando de minuto em
    minuto, e so custa maquina enquanto existe draft acontecendo.
    """
    fim = time.monotonic() + float(args.watch) * 60
    intervalo = max(10.0, float(getattr(args, "intervalo", 60) or 60))
    rc = 0
    while True:
        args._vale_ficar = False
        rc |= run_for_each_league(args, main_with)
        if not getattr(args, "_vale_ficar", False):
            print("nada a vigiar agora; encerrando a vigilia")
            return rc
        if time.monotonic() + intervalo >= fim:
            print("fim do tempo de vigilia")
            return rc
        time.sleep(intervalo)


def _parser():
    ap = base_parser("Board de draft: data, formato e melhores disponiveis por VOR no Lock-In.")
    ap.add_argument("--limit", type=int, default=None,
                    help="quantos jogadores listar (default: 20 no draft "
                         "lento, 60 no ao vivo)")
    ap.add_argument("--avisar-em", type=int, default=2, metavar="PICKS",
                    help=f"draft lento: PISO de antecedencia em picks "
                         f"(default 2). Acima disso quem manda e o ritmo "
                         f"medido: o aviso sai ~{ANTECEDENCIA_MIN}min antes "
                         f"da sua vez")
    ap.add_argument("--antes-de", type=float, default=6,
                    help="draft ao vivo: manda a cola ate N horas antes (default 6)")
    ap.add_argument("--watch", type=float, default=0, metavar="MIN",
                    help="fica vigiando por N minutos, olhando o draft a cada "
                         "--intervalo (default: roda uma vez e sai)")
    ap.add_argument("--so-perto", action="store_true",
                    help="modo vigia: so fica no ar dentro da janela de "
                         "aviso (default: fica enquanto houver draft aberto)")
    ap.add_argument("--intervalo", type=float, default=60, metavar="SEG",
                    help="modo vigia: de quantos em quantos segundos olhar "
                         "(default 60)")
    ap.add_argument("--pos", nargs="+", metavar="POS",
                    help="lista so estas posicoes (ex: --pos C). Passa por "
                         "cima do foco automatico: quem pergunta por uma "
                         "posicao quer a posicao, nao a recomendacao")
    ap.add_argument("--compacto", action="store_true",
                    help="uma linha por jogador, sem detalhe. E o "
                         "formato de lista longa: um top 200 no formato "
                         "normal nao cabe em rolagem nenhuma")
    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.watch:
        return vigia(args)
    return run_for_each_league(args, main_with)


if __name__ == "__main__":
    sys.exit(main())
