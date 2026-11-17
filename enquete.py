"""Quem voce travou: a enquete do Telegram.

A API do Sleeper nao diz quem esta travado. No meio da semana o placar do
slot (`starters_points`) mostra o ultimo jogo do titular, travado ou nao; so
quando o PROXIMO jogo dele comeca da pra ver (o placar velho continua ali =
travado). Ate la, quem sabe e voce.

Entao, quando a rodada da noite fecha, o bot manda UMA enquete de multipla
escolha com todo titular que jogou e pode estar travado, do mais provavel ao
menos (pelo que ele recomendou), e le os votos nas passadas seguintes
(`getUpdates`). Sem voto, o slot conta como aberto: o bot continua mandando
travar ou passar, o que na pior hipotese repete uma decisao ja tomada.

Errou o voto, ou travou dias depois? Da pra retirar o voto na propria
enquete e votar de novo, ou mandar uma mensagem pro bot, a qualquer hora da
semana: "travei Queen", "travei o Queen ha 2 dias com 20,5", "travei Maxey
sabado", "nao travei Wilson" (varios separados por virgula, ou "travei
todos"). O nome e qualquer um do seu elenco; o jogo, o do dia ou do placar
citado. A cada leitura nova o bot responde com o que anotou, e com qual jogo.

Travado nao destrava: o que voce confirmou vale ate o fim da semana, mesmo
que o confronto mostre outro placar depois. So "nao travei Fulano" desfaz.
E o "nao" tambem vale: o placar nao prova trava de jogo que voce disse que
nao travou.

O getUpdates sem offset devolve as ultimas 24h, e nem sempre inteiras (5
updates numa chamada, 1 na seguinte, 24/10/2026). Reaplicar o voto velho
desfazia o novo; entao cada enquete guarda o update_id do ultimo voto lido e
so aceita um mais novo.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata

import requests

from clients import CACHE, esc

ARQ = CACHE / "enquetes_trava.json"
NENHUM = "nenhum desses"


def _ler() -> dict:
    try:
        d = json.loads(ARQ.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("enquetes", {})     # poll_id -> {"chaves": [...], "votos": [...], "ult": update_id}
    d.setdefault("respostas", {})    # "week:pid:data" -> true/false
    d.setdefault("lidas", [])        # update_id das mensagens de texto ja lidas
    d.setdefault("noites", [])       # "semana:data" das noites ja perguntadas
    return d


def _gravar(d: dict) -> None:
    try:
        ARQ.write_text(json.dumps(d), encoding="utf-8")
    except OSError as e:
        print(f"(nao gravei a enquete: {e})")


def respostas() -> dict[str, bool]:
    return _ler()["respostas"]


def perguntadas() -> set[str]:
    return {k for e in _ler()["enquetes"].values() for k in e["chaves"]}


def travados_semana(week: int, votos: dict[str, bool] | None = None) -> dict[str, str]:
    """{pid: data do jogo} de quem voce disse que travou nesta semana.

    O slot nao segura o placar travado: o Queen, travado com 20,0 (21/10),
    passou a mostrar 21,5 (23/10) na manha de 24/10, sem jogo no meio. Sem a
    memoria, o travado virava pendente e ganhava enquete de novo.

    So o voto, nao o placar: guardar a "prova" do placar travou o KAT a
    semana inteira (25/10/2026) com 27,0 de 20/10, um jogo em que voce disse
    que ele NAO travou, e depois do qual ele foi pro banco."""
    votos = respostas() if votos is None else votos
    out = {}
    for k, travou in votos.items():
        w, pid, data = k.split(":", 2)
        if travou and w == str(week):
            out.setdefault(pid, data)
    return out


def noite_perguntada(noite: str) -> bool:
    """Ja saiu a enquete desta noite ("semana:data")? Uma por noite, com todo
    mundo que jogou: cada jogador entrava quando o placar dele aparecia no
    confronto, e saiam duas enquetes com 15 minutos de diferenca (1 + 2
    jogadores em 07/11/2026, 3 + 1 em 08/11)."""
    return noite in _ler()["noites"]


def _esquecer(d: dict, chave: str) -> None:
    """"nao travei Fulano": desfaz a trava dele na semana inteira."""
    w, pid = chave.split(":")[:2]
    for k in d["respostas"]:
        if k.startswith(f"{w}:{pid}:"):
            d["respostas"][k] = False


def _tg(cfg) -> tuple[str, str]:
    tg = cfg.get("telegram") or {}
    token = os.path.expandvars(tg.get("bot_token", "") or "")
    chat = os.path.expandvars(tg.get("chat_id", "") or "")
    if not token or not chat or token.startswith("$"):
        return "", ""
    return token, chat


def _norm(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt.lower()).strip()


# "travei", "não travei", "destravei", em qualquer lugar da frase: "o Queen eu
# travei com 20" e "travei o Queen há 2 dias, com 20 pontos" (09/11/2026, o
# bot so entendia a frase que COMECAVA com o verbo e o nome exato da enquete)
VERBO = re.compile(r"\b(?:(nao|n)\s+)?(travei|tranquei|lockei|travado|travados|travou)\b"
                   r"|\b(destravei|passei)\b")
DIAS_PT = {"seg": 0, "segunda": 0, "ter": 1, "terca": 1, "qua": 2, "quarta": 2,
           "qui": 3, "quinta": 3, "sex": 4, "sexta": 4, "sab": 5, "sabado": 5,
           "dom": 6, "domingo": 6}
RELATIVO = {"hoje": 0, "ontem": 1, "anteontem": 2}
VAZIAS = {"o", "a", "os", "as", "com", "de", "do", "da", "dos", "das", "no", "na", "nos",
          "nas", "em", "pontos", "ponto", "pts", "pt", "ha", "faz", "dias", "dia", "jogo",
          "jogos", "que", "eu", "ele", "ja", "tambem", "so", "pra", "para", "um", "uma",
          "fez", "feito", "placar", "semana", "atras", "feira", "mas", "esse", "aquele",
          "jr", "sr", "ii", "iii", "iv", "nao", "sim", "ai", "la", "aqui", "quando"} \
    | set(DIAS_PT) | set(RELATIVO)
TOLERANCIA = 1.5        # "travei Queen com 20" casa com o jogo de 20,5


def _nome(texto_opcao: str) -> str:
    return texto_opcao.split(" · ")[0]


def _ler_pedaco(ped: str, hoje) -> tuple[list[str], str | None, float | None]:
    """"o queen ha 2 dias com 20,5" -> (["queen"], data, 20.5)."""
    from datetime import date, timedelta
    data = None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", ped)
    if m:
        try:
            d = date(hoje.year, int(m[2]), int(m[1]))
            data = (d if d <= hoje else d.replace(year=hoje.year - 1)).isoformat()
        except ValueError:
            pass
        ped = ped[:m.start()] + " " + ped[m.end():]
    m = re.search(r"\b(?:ha|faz)\s+(\d+)\s+dias?\b|\b(\d+)\s+dias?\s+atras\b", ped)
    if m:
        data = (hoje - timedelta(days=int(m[1] or m[2]))).isoformat()
        ped = ped[:m.start()] + " " + ped[m.end():]
    palavras = re.findall(r"[a-z]+", ped)
    for w in palavras:
        if w in RELATIVO:
            data = (hoje - timedelta(days=RELATIVO[w])).isoformat()
        elif w in DIAS_PT:
            data = (hoje - timedelta(days=(hoje.weekday() - DIAS_PT[w]) % 7)).isoformat()
    nums = re.findall(r"\d+(?:[.,]\d+)?", ped)
    num = float(nums[-1].replace(",", ".")) if nums else None
    return [w for w in palavras if w not in VAZIAS and len(w) >= 3], data, num


def _casar(tokens: list[str], elenco: dict) -> list[str]:
    """Os jogadores do elenco que o texto cita: nome ou sobrenome, o comeco
    dele com 4+ letras ("alex" -> Alexander-Walker) ou com erro de
    digitacao ("quen" -> Queen). Empate = todos os empatados."""
    from difflib import SequenceMatcher
    from names import normalize
    pontos = {}
    for pid, (nome, _) in elenco.items():
        nts = [t for t in normalize(nome).split() if len(t) >= 3]
        s = 0.0
        for t in tokens:
            melhor = 0.0
            for nt in nts:
                if t == nt:
                    melhor = max(melhor, 2.0)
                elif len(t) >= 4 and nt.startswith(t):
                    melhor = max(melhor, 1.5)
                elif SequenceMatcher(None, t, nt).ratio() >= 0.8:
                    melhor = max(melhor, 1.0)
            s += melhor
        if s:
            pontos[pid] = s
    if not pontos:
        return []
    top = max(pontos.values())
    return [p for p, s in pontos.items() if s == top]


def _dia(data: str) -> str:
    from datetime import date
    try:
        d = date.fromisoformat(data)
    except (TypeError, ValueError):
        return str(data)
    return f"{['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom'][d.weekday()]} {d:%d/%m}"


def _fmt(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _jogo(nome: str, jogos: list, data, num, padrao) -> tuple[str | None, str]:
    """(data do jogo, detalhe pra confirmar) ou (None, por que nao achou)."""
    if not jogos:
        return None, f"{nome}: sem jogo nesta semana"
    if data:
        js = [j for j in jogos if j[0] == data]
        if not js:
            feitos = ", ".join(_dia(j[0]) for j in jogos)
            return None, f"{nome} não jogou {_dia(data)} (jogou {feitos})"
        j = js[0]
    elif num is not None:
        j = min(jogos, key=lambda x: abs((x[1] if x[1] is not None else 1e9) - num))
        if j[1] is None or abs(j[1] - num) > TOLERANCIA:
            feitos = ", ".join(f"{_fmt(x[1])} {_dia(x[0])}" for x in jogos if x[1] is not None)
            return None, f"{nome} não fez {_fmt(num)} nesta semana ({feitos})"
    else:
        j = next((x for x in jogos if x[0] == padrao), jogos[-1])
    return j[0], (f"{_fmt(j[1])} · {_dia(j[0])}" if j[1] is not None else _dia(j[0]))


def _por_texto(d: dict, texto: str, elenco: dict | None = None, semana: int | None = None,
               hoje=None) -> tuple[list[tuple[str, str, bool, str, bool]], list[str]] | None:
    """"travei Queen há 2 dias com 20,5, não travei Wilson" ->
    ([(chave, nome, travou, detalhe, citou o jogo?), ...], o que nao achou).
    None se nao tiver "travei"/"não travei" na mensagem.

    `elenco` = {pid: (nome, [(data, pontos)] dos jogos encerrados da semana
    `semana`)}. Sem ele, so os nomes das enquetes. O jogo sai do dia citado
    ("ontem", "sábado", "há 2 dias", "07/11"), senao do placar citado
    (o jogo mais perto, ate 1,5 ponto), senao o da ultima enquete dele,
    senao o ultimo da semana."""
    from datetime import date
    t = _norm(texto).lstrip("/")
    achados = list(VERBO.finditer(t))
    if not achados:
        return None
    hoje = hoje or date.today()
    enquetes = [e for e in reversed(list(d["enquetes"].values())) if e.get("textos")]
    ultima_chave = {}                        # pid -> data da enquete mais nova dele
    for e in reversed(enquetes):
        for chave in e["chaves"]:
            w, pid, data = chave.split(":", 2)
            if semana is None or w == str(semana):
                ultima_chave[pid] = (w, data)
    if not elenco:
        # so o que as enquetes perguntaram
        elenco, semana_de = {}, {}
        for e in reversed(enquetes):
            for chave, txt in zip(e["chaves"], e["textos"]):
                w, pid, data = chave.split(":", 2)
                elenco[pid] = (_nome(txt), [(data, None)])
                semana_de[pid] = w
    else:
        semana_de = {pid: str(semana) for pid in elenco}

    feitos, faltou = [], []
    for k, m in enumerate(achados):
        travou = not (m.group(1) or m.group(3))
        fim = achados[k + 1].start() if k + 1 < len(achados) else len(t)
        trecho = t[m.end():fim]
        if k == 0:
            trecho = t[:m.start()] + " " + trecho       # "o Queen eu travei"
        pedacos = [x for x in re.split(r",(?!\d)|;|/(?!\d)|\s+e\s+|\s+mas\s+", trecho) if x.strip()]
        ultimo = None                                    # [pids, data, num] do pedaco anterior
        lidos = []
        for ped in pedacos:
            tokens, data, num = _ler_pedaco(ped, hoje)
            if "todos" in re.findall(r"[a-z]+", ped) and enquetes:
                lidos.append([[c.split(":")[1] for c in enquetes[0]["chaves"]], data, num, ped])
                ultimo = lidos[-1]
                continue
            pids = _casar(tokens, elenco) if tokens else []
            if not pids and ultimo is not None and (data or num is not None):
                # "travei o Queen ha 2 dias, com 20 pontos": o numero e dele
                ultimo[1] = ultimo[1] or data
                ultimo[2] = ultimo[2] if ultimo[2] is not None else num
                continue
            if not pids:
                if tokens:
                    faltou.append(ped.strip())
                continue
            lidos.append([pids, data, num, ped])
            ultimo = lidos[-1]
        for pids, data, num, ped in lidos:
            if len(pids) > 1 and num is None and data is None:
                nomes = " ou ".join(elenco[p][0] for p in pids[:3])
                faltou.append(f"{ped.strip()} ({nomes}?)")
                continue
            for pid in pids:
                nome, jogos = elenco[pid]
                padrao = (ultima_chave.get(pid) or (None, None))[1]
                quando, detalhe = _jogo(nome, jogos, data, num, padrao)
                if quando is None:
                    if len(pids) == 1:
                        faltou.append(detalhe)
                    continue
                w = semana_de.get(pid) or (ultima_chave.get(pid) or ("?",))[0]
                feitos.append((f"{w}:{pid}:{quando}", nome, travou, detalhe,
                               data is not None or num is not None))
    return feitos, faltou


def sincronizar(cfg, avisar: bool = False, elenco: dict | None = None,
                semana: int | None = None) -> list[tuple[str, bool]]:
    """Le os votos e as mensagens novas. Nao confirma o offset do getUpdates:
    o Telegram guarda 24h, e outra copia do bot (local x Actions) ainda
    precisa ler. Com `avisar`, responde no chat o que anotou.

    `elenco` e `semana` deixam a mensagem citar qualquer jogador seu e
    qualquer jogo da semana (`_por_texto`), nao so o que a enquete perguntou.

    Devolve [(nome, travado)] do que mudou."""
    token, chat = _tg(cfg)
    if not token:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"timeout": 0}, timeout=20)
        ups = r.json().get("result") or [] if r.ok else []
    except Exception as e:
        print(f"(nao li a enquete: {e})")
        return []
    d = _ler()
    nomes = {c: _nome(t) for e in d["enquetes"].values()
             for c, t in zip(e["chaves"], e.get("textos") or [])}
    mudou, anotado, faltou, ajuda = False, [], [], False
    detalhes: dict[str, str] = {}
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.get("fuso") or "America/Sao_Paulo")
    for u in sorted(ups, key=lambda u: u.get("update_id") or 0):
        a = u.get("poll_answer")
        msg = u.get("message") or {}
        if a:
            e = d["enquetes"].get(a.get("poll_id"))
            uid = u.get("update_id") or 0
            if not e or uid <= e.get("ult", -1):
                continue      # voto ja lido (ou mais velho que o lido)
            e["ult"] = uid
            mudou = True
            votos = sorted(a.get("option_ids") or [])
            if votos == e.get("votos"):
                continue
            e["votos"] = votos
            for k, chave in enumerate(e["chaves"]):
                if votos:
                    d["respostas"][chave] = k in votos
                    anotado.append((nomes.get(chave, chave), k in votos))
                else:
                    # voto retirado: volta a nao saber
                    d["respostas"].pop(chave, None)
        elif msg.get("text") and str((msg.get("chat") or {}).get("id")) == str(chat):
            if u.get("update_id") in d["lidas"]:
                continue
            d["lidas"] = (d["lidas"] + [u.get("update_id")])[-500:]
            mudou = True
            # "ontem" e "sabado" contam do dia em que voce escreveu
            quando = msg.get("date")
            hoje = datetime.fromtimestamp(quando, tz).date() if quando else None
            lido = _por_texto(d, msg["text"], elenco, semana, hoje)
            if lido is None:
                ajuda = True
                continue
            feitos, nao_achou = lido
            faltou += nao_achou
            for chave, nome, travou, detalhe, citou_jogo in feitos:
                w, pid = chave.split(":")[:2]
                if not travou:
                    if not citou_jogo:
                        _esquecer(d, chave)          # "nao travei Fulano": a semana toda
                else:
                    # um jogo travado por semana: os outros dele ficam abertos
                    for k in list(d["respostas"]):
                        if k.startswith(f"{w}:{pid}:") and k != chave:
                            d["respostas"][k] = False
                d["respostas"][chave] = travou
                anotado.append((nome, travou))
                detalhes[nome] = detalhe
    if mudou:
        _gravar(d)
    if avisar and (anotado or faltou or ajuda):
        _confirmar(token, chat, anotado, faltou, ajuda, detalhes)
    return anotado


def _confirmar(token, chat, anotado, faltou, ajuda, detalhes=None) -> None:
    from clients import telegram
    L = ["📥 <b>Anotei</b>"] if anotado else []
    vistos = {}
    for nome, travou in anotado:
        vistos[nome] = travou          # a ultima palavra vale
    det = lambda nome: f" <i>({esc(detalhes[nome])})</i>" if (detalhes or {}).get(nome) else ""
    # 🔓 e 🔒 lado a lado no celular parecem iguais (28/10/2026): o nao
    # travado vai com ❌, cada grupo numa linha
    sim = [esc(nome) + det(nome) for nome, t in vistos.items() if t]
    nao = [esc(nome) for nome, t in vistos.items() if not t]
    if sim:
        L.append(f"🔒 <b>travado:</b> {', '.join(sim)}")
    if nao:
        L.append(f"❌ <b>não travado:</b> {', '.join(nao)}")
    for f in faltou:
        L.append(f"❓ <i>não entendi: {esc(f)}</i>")
    if faltou or ajuda:
        L.append("<i>ℹ️ pra corrigir: \"travei Queen\", \"travei Queen com 20,5\", "
                 "\"travei Queen ontem\" (ou sábado, 07/11, há 2 dias), \"não travei Wilson\" "
                 "— vários separados por vírgula, ou \"travei todos\"</i>")
    telegram("\n".join(L), token, chat)


def enviar(cfg, pergunta: str, itens: list[tuple[str, str]], noite: str | None = None) -> bool:
    """itens: (chave "week:pid:data", texto da opcao), do mais ao menos
    provavel. Acrescenta "nenhum desses" (o Telegram nao aceita voto vazio).
    `noite` ("semana:data") fica marcada como perguntada."""
    token, chat = _tg(cfg)
    if not token or not itens:
        return False
    itens = itens[:11]
    opcoes = [{"text": t[:100]} for _, t in itens] + [{"text": NENHUM}]
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPoll", json={
            "chat_id": chat, "question": pergunta[:300], "options": opcoes,
            "is_anonymous": False, "allows_multiple_answers": True}, timeout=30)
        poll = (r.json().get("result") or {}).get("poll") if r.ok else None
    except Exception as e:
        print(f"(enquete falhou: {e})")
        return False
    if not poll:
        print(f"(enquete {r.status_code}: {r.text[:200]})")
        return False
    d = _ler()
    d["enquetes"][poll["id"]] = {"chaves": [c for c, _ in itens],
                                 "textos": [t for _, t in itens], "votos": None}
    if noite:
        d["noites"] = (d["noites"] + [noite])[-60:]
    _gravar(d)
    return True
