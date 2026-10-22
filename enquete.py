"""Quem voce travou: a enquete do Telegram.

A API do Sleeper nao diz quem esta travado. No meio da semana o placar do
slot (`starters_points`) mostra o ultimo jogo do titular, travado ou nao; so
quando o PROXIMO jogo dele comeca da pra ver (o placar velho continua ali =
travado). Ate la, quem sabe e voce.

Entao, na manha seguinte a rodada, o bot manda uma enquete de multipla
escolha com quem pode estar travado, do mais provavel ao menos (pelo que ele
recomendou), e le os votos nas passadas seguintes (`getUpdates`). Sem voto,
o slot conta como aberto: o bot continua mandando travar ou passar, o que
na pior hipotese repete uma decisao ja tomada.

Errou o voto? Da pra retirar o voto na propria enquete e votar de novo, ou
mandar uma mensagem pro bot: "travei Queen", "nao travei Wilson" (varios
separados por virgula, ou "travei todos"). A cada leitura nova o bot
responde com o que anotou.
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
    d.setdefault("enquetes", {})     # poll_id -> {"chaves": [...], "votos": [...]}
    d.setdefault("respostas", {})    # "week:pid:data" -> true/false
    d.setdefault("lidas", [])        # update_id das mensagens de texto ja lidas
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


COMANDO = re.compile(r"^/?(nao travei|n travei|destravei|passei|travei)\b[\s:,.-]*(.*)$")


def _nome(texto_opcao: str) -> str:
    return texto_opcao.split(" · ")[0]


def _por_texto(d: dict, texto: str) -> tuple[list[tuple[str, str, bool]], list[str]] | None:
    """"travei Queen, Wilson" -> ([(chave, nome, True), ...], nao achados).
    None se nao for um comando. Procura da enquete mais nova pra mais velha."""
    m = COMANDO.match(_norm(texto))
    if not m:
        return None
    travou = m.group(1) == "travei"
    pedacos = [x.strip() for x in re.split(r",|;|/|\se\s", m.group(2)) if x.strip()]
    enquetes = [e for e in reversed(list(d["enquetes"].values())) if e.get("textos")]
    feitos, faltou = [], []
    for ped in pedacos:
        achou = []
        for e in enquetes:
            for chave, t in zip(e["chaves"], e["textos"]):
                if ped == "todos" or ped in _norm(_nome(t)):
                    achou.append((chave, _nome(t), travou))
            if achou:
                break
        if achou:
            feitos += achou
        else:
            faltou.append(ped)
    return feitos, faltou


def sincronizar(cfg, avisar: bool = False) -> list[tuple[str, bool]]:
    """Le os votos e as mensagens novas. Nao confirma o offset do getUpdates:
    o Telegram guarda 24h, e outra copia do bot (local x Actions) ainda
    precisa ler. Com `avisar`, responde no chat o que anotou.

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
    for u in ups:
        a = u.get("poll_answer")
        msg = u.get("message") or {}
        if a:
            e = d["enquetes"].get(a.get("poll_id"))
            if not e:
                continue
            votos = sorted(a.get("option_ids") or [])
            if votos == e.get("votos"):
                continue
            e["votos"] = votos
            mudou = True
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
            lido = _por_texto(d, msg["text"])
            if lido is None:
                ajuda = True
                continue
            feitos, nao_achou = lido
            faltou += nao_achou
            for chave, nome, travou in feitos:
                d["respostas"][chave] = travou
                anotado.append((nome, travou))
    if mudou:
        _gravar(d)
    if avisar and (anotado or faltou or ajuda):
        _confirmar(token, chat, anotado, faltou, ajuda)
    return anotado


def _confirmar(token, chat, anotado, faltou, ajuda) -> None:
    from clients import telegram
    L = ["📥 <b>Anotei</b>"] if anotado else []
    vistos = {}
    for nome, travou in anotado:
        vistos[nome] = travou          # a ultima palavra vale
    L += [f"{'🔒' if t else '🔓'} {esc(nome)} · {'travado' if t else 'não travado'}"
          for nome, t in vistos.items()]
    if faltou:
        L.append(f"❓ <i>não achei na enquete: {esc(', '.join(faltou))}</i>")
    if ajuda:
        L.append("<i>ℹ️ pra corrigir a enquete: \"travei Queen\" ou \"não travei Wilson\" "
                 "(vários separados por vírgula, ou \"travei todos\")</i>")
    telegram("\n".join(L), token, chat)


def enviar(cfg, pergunta: str, itens: list[tuple[str, str]]) -> bool:
    """itens: (chave "week:pid:data", texto da opcao), do mais ao menos
    provavel. Acrescenta "nenhum desses" (o Telegram nao aceita voto vazio)."""
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
    _gravar(d)
    return True
