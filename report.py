"""Formatacao dos relatorios. Um lugar so, pra todos os runners.

Regras de layout, aprendidas apanhando:

  * HTML, nunca Markdown. Um `_` solto em `pre_draft` derrubava a mensagem
    inteira com 400 e o relatorio chegava sem formatacao nenhuma.
  * Negrito SO no que voce vai clicar/agir. Se tudo e negrito, nada e.
  * Uma ideia por linha. Numero entre parenteses dentro de parenteses e
    onde o relatorio vira sopa.
  * Numero em portugues usa virgula decimal.
"""

from __future__ import annotations

from clients import esc

POS_ICON: dict[str, str] = {}


def n(v, casas: int = 1, sinal: bool = False) -> str:
    """Numero com virgula decimal."""
    if v is None:
        return "—"
    s = f"{v:+.{casas}f}" if sinal else f"{v:.{casas}f}"
    return s.replace(".", ",")


ICONE_TITULO = {"DIA": "☀️", "LOCK-IN": "🔐", "FIM DA NOITE": "🌙", "TROCAS": "🔁", "DRAFT": "🏀"}
SEPARADOR = "━━━━━━━━━━━━━━━"


def titulo(kind: str, liga: str, sub: str = "") -> str:
    icone = ICONE_TITULO.get(kind, "🏀")
    L = [f"{icone} <b>{esc(kind)}</b> · {esc(liga)}"]
    if sub:
        L.append(f"<i>📅 {esc(sub)}</i>")
    return "\n".join(L)


def secao(icone: str, texto: str, nota: str = "") -> list[str]:
    """Cabecalho de secao: linha em branco, separador, titulo em caixa alta."""
    L = ["", SEPARADOR, f"{icone} <b>{esc(texto.upper())}</b>"]
    if nota:
        L.append(f"<i>{esc(nota)}</i>")
    L.append("")
    return L


def jogador(nome: str, pos: str, time: str, negrito: bool = True) -> str:
    icon = POS_ICON.get(pos, "")
    nm = f"<b>{esc(nome)}</b>" if negrito else esc(nome)
    return f"{icon} {nm} <i>{esc(pos)} {esc(time)}</i>".strip()


def bloco(linhas: list[str]) -> str:
    """Junta as linhas. Linha vazia vira UM espaco entre blocos (vazias
    seguidas, ou no comeco e no fim, somem); None some."""
    out: list[str] = []
    for x in linhas:
        if x is None:
            continue
        if x == "" and (not out or out[-1] == ""):
            continue
        out.append(x)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def rodape(avisos: list[str]) -> str:
    if not avisos:
        return ""
    return "\n\n<i>" + esc(" · ".join(avisos)) + "</i>"
