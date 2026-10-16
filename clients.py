"""Clientes de API. Sleeper e READ-ONLY por design — nao existe write oficial.

Tres fontes, todas sem chave:

  * `api.sleeper.app/v1`  — liga, elencos, confrontos, transacoes, jogadores;
  * `api.sleeper.com`     — estatistica e projecao JOGO A JOGO (uma linha por
    jogador por jogo, com data, adversario e minutos). E daqui que sai o
    modelo inteiro;
  * ESPN (scoreboard)     — o HORARIO de cada jogo. O calendario do Sleeper
    traz so a data, e na NBA a escalacao trava jogo a jogo.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from names import POSICOES, posicoes

# Cache proprio, separado do bot da NFL: os dois guardam o dump de jogadores,
# e um dump da NFL lido como NBA daria um elenco sem ninguem.
CACHE = Path.home() / ".cache_projet"
CACHE.mkdir(exist_ok=True)


def _cached(name: str, ttl_hours: float, fetch):
    p = CACHE / f"{name}.json"
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl_hours * 3600:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            p.unlink(missing_ok=True)  # cache corrompido nao pode matar o run
    data = fetch()
    p.write_text(json.dumps(data), encoding="utf-8")
    return data


# Uma conexao reaproveitada: no Windows cada handshake TLS novo custava ~0,8s,
# e uma passada da escalacao faz dezenas de pedidos.
_SESSAO = requests.Session()
_SESSAO.headers["User-Agent"] = "Mozilla/5.0"


def _get_json(url: str, tentativas: int = 3, espera: float = 1.5):
    """GET com repeticao. A Sleeper devolve 404 passageiro em recurso que
    existe (medido no bot da NFL: uma falha e depois 8 de 8 com 200)."""
    erro = None
    for k in range(tentativas):
        r = None
        try:
            r = _SESSAO.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            erro = e
            if r is not None and r.status_code not in (404, 429) and r.status_code < 500:
                raise
        except requests.RequestException as e:
            erro = e
        if k < tentativas - 1:
            time.sleep(espera * (k + 1))
    raise erro


# ==========================================================================
# Sleeper
# ==========================================================================
class Sleeper:
    BASE = "https://api.sleeper.app/v1"
    STATS = "https://api.sleeper.com"

    def __init__(self, username: str, season: int, league_id: str = ""):
        self.username, self.season = username, season
        self._league_id, self._user_id, self._league = league_id, None, None
        self._rosters = None

    def _get(self, path: str):
        return _get_json(f"{self.BASE}{path}")

    @property
    def user_id(self) -> str:
        if self._user_id is None:
            u = self._get(f"/user/{self.username}")
            if not u:
                raise RuntimeError(f"Usuario '{self.username}' nao existe no Sleeper. "
                                   "Use o username, nao o display name.")
            self._user_id = u["user_id"]
        return self._user_id

    @property
    def league_id(self) -> str:
        if not self._league_id:
            ls = self._get(f"/user/{self.user_id}/leagues/nba/{self.season}")
            if not ls:
                raise RuntimeError(f"Nenhuma liga NBA {self.season} para {self.username}")
            self._league_id = ls[0]["league_id"]
        return self._league_id

    def state(self):
        return self._get("/state/nba")

    def league(self):
        if self._league is None:
            self._league = self._get(f"/league/{self.league_id}")
        return self._league

    def rosters(self):
        # Uma passada de hora em hora pede os elencos de varios lugares
        # (escalacao, livres, drops). Uma vez por execucao basta.
        if self._rosters is None:
            self._rosters = self._get(f"/league/{self.league_id}/rosters") or []
        return self._rosters

    def matchups(self, week: int):
        return self._get(f"/league/{self.league_id}/matchups/{week}") or []

    def transactions(self, week: int):
        return self._get(f"/league/{self.league_id}/transactions/{week}") or []

    def users(self):
        return self._get(f"/league/{self.league_id}/users")

    def drafts(self):
        return self._get(f"/league/{self.league_id}/drafts") or []

    def draft_picks(self, draft_id: str):
        return self._get(f"/draft/{draft_id}/picks") or []

    def team_names(self) -> dict[int, str]:
        """{roster_id: nome do time}. Falhar aqui nao derruba relatorio."""
        try:
            donos = {u.get("user_id"): (((u.get("metadata") or {}).get("team_name")
                                         or u.get("display_name") or "").strip())
                     for u in (self.users() or [])}
        except Exception:
            donos = {}
        out: dict[int, str] = {}
        for r in (self.rosters() or []):
            rid = r.get("roster_id")
            out[rid] = donos.get(r.get("owner_id")) or f"time {rid}"
        return out

    def trending(self, kind="add", hours=24, limit=50):
        return self._get(
            f"/players/nba/trending/{kind}?lookback_hours={hours}&limit={limit}") or []

    def players(self, ttl_hours: float = 6):
        """Dump de jogadores (~3MB). E daqui que sai `injury_status`.

        TTL curto de proposito: na NBA o status muda de hora em hora no dia
        do jogo (GTD vira Out meia hora antes do tip-off), e a escalacao da
        noite le esse campo.
        """
        return _cached("sleeper_players_nba", ttl_hours,
                       lambda: self._get("/players/nba"))

    # --- estatistica e projecao jogo a jogo -------------------------------
    def linhas(self, tipo: str, season: int, week: int, ttl_hours: float):
        """Uma linha por jogador POR JOGO na semana `week`.

        `tipo` = "stats" (o que aconteceu) ou "projections" (o que o Sleeper
        projeta pra cada jogo, ja sabendo do adversario e de quem esta fora).
        Linha sem `stats` e jogador que nao entrou em quadra, e sai aqui.
        """
        def baixar():
            dados = _get_json(f"{self.STATS}/{tipo}/nba/{season}/{week}"
                              f"?season_type=regular")
            return [{k: x.get(k) for k in ("player_id", "date", "team", "opponent",
                                           "game_id", "stats")}
                    for x in (dados or []) if x.get("stats")]
        return _cached(f"{tipo}_{season}_{week}", ttl_hours, baixar)

    def projecao_temporada(self, season: int, ttl_hours: float = 12) -> list[dict]:
        """Projecao POR JOGO da temporada inteira + ADP, uma linha por jogador.

        E o ranking de draft que existe pra NBA sem chave: o Sleeper publica
        `adp_std` (o mercado) e a linha estatistica media que ele espera. Os
        pontos saem pela regra da liga, aqui no bot.
        """
        q = "&".join(f"position[]={p}" for p in POSICOES)

        def baixar():
            dados = _get_json(f"{self.STATS}/projections/nba/{season}"
                              f"?season_type=regular&{q}")
            return [{"player_id": x.get("player_id"), "stats": x.get("stats") or {}}
                    for x in (dados or [])]
        return _cached(f"proj_temporada_{season}", ttl_hours, baixar)

    def calendario(self, season: int, ttl_hours: float = 24) -> list[dict]:
        """Todos os jogos da temporada regular: data, semana, mandante, visitante."""
        def baixar():
            out = []
            for g in _get_json(f"https://api.sleeper.app/schedule/nba/regular/{season}") or []:
                h, a = g.get("home") or {}, g.get("away") or {}
                out.append({"game_id": g.get("game_id"), "date": g.get("date"),
                            "week": g.get("week"), "status": g.get("status"),
                            "home": h.get("team") if isinstance(h, dict) else h,
                            "away": a.get("team") if isinstance(a, dict) else a})
            return out
        return _cached(f"calendario_{season}", ttl_hours, baixar)

    # --- derivados --------------------------------------------------------
    def my_roster(self):
        for r in self.rosters():
            if r.get("owner_id") == self.user_id or self.user_id in (r.get("co_owners") or []):
                return r
        raise RuntimeError("Roster nao encontrado — confira o username.")

    def roster_positions(self) -> list[str]:
        return list(self.league().get("roster_positions") or [])

    def n_teams(self) -> int:
        return int(self.league().get("total_rosters") or len(self.rosters()))

    def scoring_settings(self) -> dict:
        return self.league().get("scoring_settings") or {}

    def settings(self) -> dict:
        return self.league().get("settings") or {}

    def lock_in(self) -> bool:
        """Liga no modo Lock-In: um jogo por jogador por semana, escolhido por
        voce. `game_mode` 1 e o da nossa liga, e a temporada 2025 inteira
        bate com isso (NOTES_LOCALES.md, *Lock-In: o que os dados de 2025 dizem*)."""
        return int(self.settings().get("game_mode") or 0) == 1

    def waiver_mode(self) -> str:
        wt = self.settings().get("waiver_type")
        return {0: "rolling", 1: "reverse", 2: "faab"}.get(wt, "unknown")

    def free_agents(self, pool: dict) -> list[str]:
        taken = set()
        for r in self.rosters():
            taken.update(r.get("players") or [])
            taken.update(r.get("taxi") or [])
            taken.update(r.get("reserve") or [])
        return [pid for pid, p in pool.items()
                if pid not in taken and p.get("team") and posicoes(p)]


# ==========================================================================
# ESPN — horario dos jogos
# ==========================================================================
# Dois hosts da ESPN com a mesma API e CDN diferente: quando um cai, o outro
# costuma estar de pe. O CDN da NBA devolve 403 fora do navegador (testado
# com User-Agent e Referer de navegador em 14/10/2026), e o TheSportsDB de
# graca corta a lista no meio da noite (6 de 11 jogos).
ESPN_HOSTS = ("https://site.api.espn.com", "https://site.web.api.espn.com")


def espn_dia(data: str, ttl_hours: float) -> list[dict]:
    """Jogos de um dia ('AAAA-MM-DD', no calendario americano) com horario em
    UTC e status. A ESPN e a unica fonte sem chave com o horario: o calendario
    do Sleeper so tem a data.

    Sem resposta dos dois hosts, vale o horario que ja estava guardado (tip-off
    quase nunca muda), mas SEM o status: um "in" de horas atras seguraria o
    jogo como em andamento. So sem nada guardado o jogo cai nas 19h ET."""
    def baixar():
        erro = None
        for host in ESPN_HOSTS:
            try:
                j = _get_json(f"{host}/apis/site/v2/sports/basketball/"
                              f"nba/scoreboard?dates={data.replace('-', '')}")
                break
            except requests.RequestException as e:
                erro = e
        else:
            raise erro
        out = []
        for ev in (j or {}).get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            times = {c.get("homeAway"): (c.get("team") or {}).get("abbreviation")
                     for c in comp.get("competitors") or []}
            st = ((ev.get("status") or {}).get("type") or {})
            out.append({"tip": ev.get("date"), "home": times.get("home"),
                        "away": times.get("away"), "state": st.get("state"),
                        "completed": bool(st.get("completed"))})
        return out
    try:
        return _cached(f"espn_{data}", ttl_hours, baixar)
    except requests.RequestException as erro:
        velho = CACHE / f"espn_{data}.json"
        try:
            guardado = json.loads(velho.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise erro from None
        return [dict(e, state="", completed=False) for e in guardado]


def espn_lesoes(ttl_hours: float = 1.0) -> list[dict]:
    """Lesionados com a DATA PREVISTA DE VOLTA e a noticia (RotoWire via ESPN).

    O Sleeper so diz Out/GTD, sem prazo: Out por uma noite e Out por tres
    meses ficavam iguais. A ESPN traz `returnDate` pra todo lesionado — e a
    estimativa da imprensa, nao a do time, mas e a melhor que existe sem chave.
    """
    def baixar():
        j = _get_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries")
        out = []
        for t in (j or {}).get("injuries") or []:
            for x in t.get("injuries") or []:
                a = x.get("athlete") or {}
                det = x.get("details") or {}
                out.append({"nome": a.get("displayName"),
                            "team": (a.get("team") or {}).get("abbreviation"),
                            "status": x.get("status"), "retorno": det.get("returnDate"),
                            "tipo": det.get("type"), "nota": x.get("shortComment"),
                            "data": x.get("date")})
        return out
    return _cached("espn_lesoes", ttl_hours, baixar)


# ==========================================================================
# Telegram
# ==========================================================================
LIMIT = 3800


def esc(text) -> str:
    """Escapa pro parse_mode HTML do Telegram.

    Trocamos Markdown legado por HTML depois de um bug feio: `pre_draft` tem
    UM underscore, que no Markdown abre um italico que nunca fecha. O Telegram
    rejeita a MENSAGEM INTEIRA com 400 e o relatorio caia pra texto puro, sem
    nenhuma formatacao. Escapar so nomes de jogador nao bastava — qualquer
    valor dinamico com `_` ou `*` derrubava tudo.

    Em HTML so 3 caracteres precisam de escape, e eles nunca aparecem em nome
    de jogador. Muito mais dificil de quebrar.
    """
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def b(text) -> str:
    return f"<b>{esc(text)}</b>"


def i(text) -> str:
    return f"<i>{esc(text)}</i>"


def _chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Quebra em limite de linha, nao no meio de uma palavra ou de um marcador."""
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out or [text]


def _destino(r) -> str:
    """Nome do chat que recebeu. Nao e o chat_id: id e segredo de repo e sai
    mascarado no log do Actions, entao nao responde 'foi pra mim?'."""
    try:
        c = (r.json().get("result") or {}).get("chat") or {}
        return (c.get("title") or c.get("first_name")
                or c.get("username") or "chat sem nome")
    except Exception:
        return "chat desconhecido"


def telegram(text: str, token: str, chat_id: str, quiet: bool = False) -> bool:
    """Manda a mensagem. Nunca levanta: falha de envio nao pode apagar o
    relatorio que ja foi impresso no stdout.

    Se o HTML for recusado (400), reenvia o chunk como texto puro em vez de
    perder a mensagem — mas isso agora e ultimo recurso, nao o caminho comum.

    Em caso de sucesso diz PRA QUEM entregou. Um relatorio que chega num
    chat que ninguem le e hoje indistinguivel de um relatorio entregue: os
    dois dao 200 e nenhum erro. Sem esta linha, "nao recebi nada" nao tem
    como ser respondido pelo log.
    """
    token = os.path.expandvars(token or "")
    chat_id = os.path.expandvars(chat_id or "")
    if not token or not chat_id or token.startswith("$"):
        if not quiet:
            print("(telegram nao configurado — so stdout)")
        return False
    ok, destino = True, None
    for chunk in _chunks(text):
        for mode in ("HTML", None):
            body = {"chat_id": chat_id, "text": chunk}
            if mode:
                body["parse_mode"] = mode
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json=body, timeout=30)
                if r.ok:
                    if destino is None:
                        destino = _destino(r)
                        if not quiet:
                            # no Actions o log e publico: o nome do chat fica fora
                            quem = "" if os.environ.get("GITHUB_ACTIONS") else f" para {destino}"
                            print(f"(telegram: entregue{quem})")
                    break
                if mode is None:
                    print(f"(telegram {r.status_code}: {r.text[:200]})")
                    ok = False
            except Exception as e:
                print(f"(telegram falhou: {e})")
                ok = False
                break
    return ok
