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
"""

from __future__ import annotations

import json
import os

import requests

from clients import CACHE

ARQ = CACHE / "enquetes_trava.json"
NENHUM = "nenhum desses"


def _ler() -> dict:
    try:
        d = json.loads(ARQ.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("enquetes", {})     # poll_id -> {"chaves": [...], "votos": [...]}
    d.setdefault("respostas", {})    # "week:pid:data" -> true/false
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


def sincronizar(cfg) -> None:
    """Le os votos novos. Nao confirma o offset do getUpdates: o Telegram
    guarda 24h, e outra copia do bot (local x Actions) ainda precisa ler."""
    token, _ = _tg(cfg)
    if not token:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"timeout": 0}, timeout=20)
        ups = r.json().get("result") or [] if r.ok else []
    except Exception as e:
        print(f"(nao li a enquete: {e})")
        return
    d = _ler()
    mudou = False
    for u in ups:
        a = u.get("poll_answer")
        e = d["enquetes"].get((a or {}).get("poll_id"))
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
            else:
                # voto retirado: volta a nao saber
                d["respostas"].pop(chave, None)
    if mudou:
        _gravar(d)


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
    d["enquetes"][poll["id"]] = {"chaves": [c for c, _ in itens], "votos": None}
    _gravar(d)
    return True
