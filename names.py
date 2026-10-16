"""Nome, time e posicao de jogador da NBA.

Tudo no bot vem do Sleeper e casa por `player_id`: nao existe join por nome
com fonte externa. O que sobra aqui e apresentacao (nome legivel) e a ponte
de sigla de time com a ESPN, que e de onde sai o horario de cada jogo.
"""

from __future__ import annotations

import re
import unicodedata

POSICOES = ("PG", "SG", "SF", "PF", "C")

# A ESPN abrevia 6 times diferente do Sleeper. Sem isso, o horario do jogo do
# Warriors nunca casa e o bot acha que o Curry nao joga.
ESPN_PARA_SLEEPER = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS",
                     "UTAH": "UTA", "WSH": "WAS"}


def normalize(name: str | None) -> str:
    """'Nikola Jokić' -> 'nikola jokic'. Pra busca por nome na linha de comando."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def team_code(team: str | None) -> str:
    t = (team or "").upper().strip()
    return ESPN_PARA_SLEEPER.get(t, t)


def posicoes(player: dict) -> list[str]:
    """As posicoes em que o jogador pode ser escalado, na ordem do Sleeper.

    E `fantasy_positions`, nao `position`: o Sleeper diz que o Jokic e C, mas
    um ala-pivo listado como ['C', 'PF'] entra nos dois slots, e e isso que
    a escalacao usa. Time (DEF) nao existe na NBA de fantasy e fica de fora.
    """
    fp = [p for p in (player.get("fantasy_positions") or []) if p in POSICOES]
    if not fp and player.get("position") in POSICOES:
        fp = [player["position"]]
    return fp


def display_name(player: dict) -> str:
    nome = (player.get("full_name")
            or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip())
    return nome or str(player.get("player_id", "?"))
