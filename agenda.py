"""Calendario da NBA com HORARIO de cada jogo.

Na NFL o lock era por janela (quinta, domingo 13h, 16h25, noite). Na NBA cada
jogador trava no tip-off do PROPRIO jogo, e numa noite comum sao 10 jogos
espalhados entre 19h e 22h30 ET. Tudo que o bot decide de hora em hora —
quem escalar, quem travar, quem pegar no meio da noite — depende de saber,
jogo a jogo, se ele ainda nao comecou, esta rolando ou acabou.

Duas fontes: o calendario do Sleeper (data, semana da liga, times) e o
scoreboard da ESPN (horario e status ao vivo). A semana vem do Sleeper porque
e ela que define o confronto: segunda a domingo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from clients import espn_dia
from names import team_code

ET = ZoneInfo("America/New_York")

# Quanto dura um jogo, do tip-off ao fim, pra quando a ESPN nao responde.
# Media da NBA fica em ~2h15; 2h30 erra pro lado seguro (achar que ainda nao
# acabou e so atrasa uma decisao de lock, nunca adianta uma errada).
DURACAO = timedelta(hours=2, minutes=30)

# Duracao tipica do tip-off ao fim: so pra estimar o tempo de jogo que falta
# quando a ESPN nao da o relogio. O "quando acaba" vem do ecart.py, medido.
DURACAO_REAL = timedelta(hours=2, minutes=15)

# Sem horario da ESPN, o jogo e assumido as 19h ET: o tip-off mais cedo de
# uma noite normal. Errar pra cedo faz o bot avisar antes, nunca depois.
TIP_PADRAO = (19, 0)


@dataclass
class Jogo:
    game_id: str
    data: str            # dia do jogo no calendario americano (AAAA-MM-DD)
    week: int
    home: str
    away: str
    tip: datetime | None = None   # UTC
    estado: str = ""              # "pre" | "in" | "post" (ESPN), "" = sem dado
    periodo: int = 0              # quarto em andamento (ESPN; 5+ = prorrogacao)
    relogio: float | None = None  # segundos que faltam no quarto (ESPN)
    pontos_casa: int | None = None   # placar ao vivo (ESPN)
    pontos_fora: int | None = None

    @property
    def times(self) -> tuple[str, str]:
        return self.home, self.away

    def adversario(self, time: str) -> str:
        return self.away if time == self.home else self.home

    @property
    def inicio(self) -> datetime:
        if self.tip:
            return self.tip
        y, m, d = (int(x) for x in self.data.split("-"))
        return datetime(y, m, d, *TIP_PADRAO, tzinfo=ET).astimezone(timezone.utc)

    # O relogio manda; o status da ESPN so refina DEPOIS do tip-off (jogo que
    # acabou cedo, prorrogacao que passou das 2h30). Ler o status antes do
    # horario quebrava o --now: num teste em data passada, a ESPN diz "post"
    # pra tudo e o bot achava que o jogo das 22h ja tinha acabado as 19h.
    def comecou(self, agora: datetime) -> bool:
        return self.inicio <= agora

    def acabou(self, agora: datetime) -> bool:
        if not self.comecou(agora):
            return False
        if self.estado == "post" and agora - self.inicio >= timedelta(hours=1, minutes=45):
            return True
        if self.estado == "in" and agora - self.inicio < timedelta(hours=4):
            return False
        return self.inicio + DURACAO <= agora

    def restante_min(self, agora: datetime) -> float:
        """Minutos de JOGO que faltam (de 48). Com o relogio da ESPN, exato;
        sem ele, pelo tempo de relogio de parede desde o tip-off."""
        if not self.comecou(agora):
            return 48.0
        if self.acabou(agora):
            return 0.0
        if self.estado == "in" and self.periodo and self.relogio is not None:
            return max(0, 4 - self.periodo) * 12 + max(self.relogio, 0.0) / 60
        corrido = (agora - self.inicio) / DURACAO_REAL
        return 48.0 * max(0.0, 1.0 - corrido)

    def tipo(self, agora: datetime) -> str | None:
        """"blowout", "pau a pau" ou "normal" pelo placar ao vivo."""
        if self.pontos_casa is None or self.pontos_fora is None or not self.comecou(agora):
            return None
        d = abs(self.pontos_casa - self.pontos_fora)
        if d >= 20 or (d >= 15 and self.restante_min(agora) <= 12):
            return "blowout"
        return "pau a pau" if d <= 5 else "normal"

    def situacao(self, time: str, agora: datetime) -> str | None:
        """"🔥 pau a pau · POR perde por 3": diz se o titular deve jogar o
        fim do jogo (pau a pau) ou sentar no banco (blowout)."""
        tipo = self.tipo(agora)
        if not tipo:
            return None
        meu, deles = ((self.pontos_casa, self.pontos_fora) if time == self.home
                      else (self.pontos_fora, self.pontos_casa))
        d = abs(meu - deles)
        tipo = {"blowout": "💤 blowout", "pau a pau": "🔥 pau a pau"}.get(tipo, tipo)
        lado = ("empatado" if d == 0 else
                f"{time} {'vence' if meu > deles else 'perde'} por {d}")
        return f"{tipo} · {lado} ({meu}-{deles})"

    def margem(self) -> int | None:
        if self.pontos_casa is None or self.pontos_fora is None:
            return None
        return abs(self.pontos_casa - self.pontos_fora)

    def fim_previsto(self, agora: datetime, p90: bool = False) -> datetime:
        """Quando o jogo deve acabar, pelo relogio de parede medido
        (ecart.py): o ritmo muda com o tempo que falta e com o placar (jogo
        apertado no fim leva o dobro). `p90`: o horario que so 1 em 10 jogos
        passa, pra dizer se da tempo."""
        from ecart import relogio
        if self.acabou(agora):
            return agora
        if not self.comecou(agora):
            m50, m90 = relogio(48.0, None, True)
            return self.inicio + timedelta(minutes=m90 if p90 else m50)
        falta = self.restante_min(agora)
        antes = self.periodo <= 2 if self.periodo else falta > 24
        m50, m90 = relogio(falta, self.margem(), antes)
        return agora + timedelta(minutes=m90 if p90 else m50)


def hoje_et(agora: datetime) -> str:
    return agora.astimezone(ET).date().isoformat()


class Agenda:
    def __init__(self, sl, season: int, agora: datetime | None = None):
        self.season = season
        self.agora = agora or datetime.now(timezone.utc)
        self.jogos: list[Jogo] = []
        for g in sl.calendario(season):
            if not g.get("date") or not g.get("week"):
                continue
            self.jogos.append(Jogo(str(g["game_id"]), g["date"], int(g["week"]),
                                   team_code(g["home"]), team_code(g["away"])))
        self._por_data: dict[str, list[Jogo]] = {}
        for j in self.jogos:
            self._por_data.setdefault(j.data, []).append(j)
        self._com_horario: set[str] = set()

    # -- horario -----------------------------------------------------------
    def _ttl(self, data: str) -> float:
        """Jogo passado nao muda; o de hoje muda de minuto em minuto (status)."""
        hoje = date.fromisoformat(hoje_et(self.agora))
        d = date.fromisoformat(data)
        if d < hoje - timedelta(days=1):
            return 24 * 365
        if d <= hoje:
            return 0.08          # ~5 minutos: e o status ao vivo
        return 6.0

    def _horarios(self, data: str) -> None:
        if data in self._com_horario:
            return
        self._com_horario.add(data)
        try:
            eventos = espn_dia(data, self._ttl(data))
        except Exception:
            return
        idx = {(team_code(e["home"]), team_code(e["away"])): e for e in eventos}
        for j in self._por_data.get(data, []):
            e = idx.get((j.home, j.away))
            if not e:
                continue
            if e.get("tip"):
                j.tip = datetime.fromisoformat(e["tip"].replace("Z", "+00:00"))
            j.estado = e.get("state") or ""
            j.periodo = int(e.get("period") or 0)
            j.relogio = e.get("clock")
            for lado in ("home", "away"):
                try:
                    v = int(e.get(f"{lado}_score"))
                except (TypeError, ValueError):
                    v = None
                setattr(j, "pontos_casa" if lado == "home" else "pontos_fora", v)

    def do_dia(self, data: str) -> list[Jogo]:
        self._horarios(data)
        return sorted(self._por_data.get(data, []), key=lambda j: j.inicio)

    # -- semana ------------------------------------------------------------
    def semana_de(self, data: str) -> int | None:
        """Semana da liga que contem esta data. Dia sem jogo (All-Star) cai na
        semana do proximo jogo, que e a que ainda esta pra ser decidida."""
        ds = sorted(self._por_data)
        for d in ds:
            if d >= data:
                return self._por_data[d][0].week
        return None

    def semana_atual(self) -> int | None:
        return self.semana_de(hoje_et(self.agora))

    def datas_da_semana(self, week: int) -> list[str]:
        return sorted({j.data for j in self.jogos if j.week == week})

    def jogos_do_time(self, time: str, week: int) -> list[Jogo]:
        out = []
        for d in self.datas_da_semana(week):
            for j in self._por_data.get(d, []):
                if time in j.times:
                    out.append(j)
        for d in {j.data for j in out}:
            self._horarios(d)
        return sorted(out, key=lambda j: j.inicio)

    def restantes(self, time: str, week: int, depois_de: datetime) -> list[Jogo]:
        """Jogos do time nesta semana que ainda NAO comecaram."""
        return [j for j in self.jogos_do_time(time, week) if not j.comecou(depois_de)]

    def jogos_por_semana(self, time: str, week: int) -> int:
        return sum(1 for j in self.jogos if j.week == week and time in j.times)
