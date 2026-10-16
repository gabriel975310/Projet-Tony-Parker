"""Montagem compartilhada pelos runners: config, clientes, calendario, modelo.

Existe pra que a escalacao da noite, o relatorio do dia e o draft olhem pro
MESMO estado (mesmo log, mesma projecao, mesmo relogio) e pra que a politica
de degradacao seja uma so: fonte que cai vira aviso no rodape, nunca um
crash no meio da noite com jogador pra travar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from agenda import Agenda
from clients import Sleeper
from names import display_name, posicoes

HERE = Path(__file__).resolve().parent

# No GitHub Actions o log e PUBLICO (repo publico). Nada que identifique a
# liga ou o dono sai nele: nem a mensagem, nem o nome da liga, nem o chat.
# Username e liga vem de secrets, que o Actions ja mascara no log.
PUBLICO = bool(os.environ.get("GITHUB_ACTIONS"))

# O relatorio tem seta e emoji. No Windows o console abre em cp1252 e um
# print() com "→" levanta UnicodeEncodeError — o bot morreria DEPOIS de ja ter
# feito todo o trabalho, na hora de mostrar o resultado.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def ja_avisado(arquivo: Path, chave: str) -> bool:
    try:
        return chave in json.loads(arquivo.read_text(encoding="utf-8"))
    except Exception:
        return False


def marcar_avisado(arquivo: Path, chave: str) -> None:
    try:
        vistos = json.loads(arquivo.read_text(encoding="utf-8"))
    except Exception:
        vistos = {}
    vistos[chave] = int(time.time())
    # Poda de 30 dias: sem ela o arquivo ganharia uma linha por aviso, pra sempre.
    corte = time.time() - 30 * 86400
    vistos = {k: v for k, v in vistos.items() if v >= corte}
    try:
        arquivo.parent.mkdir(exist_ok=True)
        arquivo.write_text(json.dumps(vistos), encoding="utf-8")
    except OSError as e:
        print(f"(nao consegui gravar {arquivo.name}: {e})")


def ativos(roster: dict) -> list[str]:
    """Os jogadores que ocupam vaga no elenco ATIVO.

    Quem esta no IR (`reserve`) nao pode ser escalado e dispensar ele NAO
    abre espaco pra um add — a vaga que ele libera e a do IR. Foi um conselho
    impossivel de executar no bot da NFL em 16/09/2026.
    """
    fora = set(roster.get("reserve") or []) | set(roster.get("taxi") or [])
    return [p for p in (roster.get("players") or []) if p not in fora]


def all_configs(paths=None) -> list[Path]:
    found = sorted(p for p in HERE.glob("config*.yaml")
                   if not p.name.startswith(".") and not p.name.startswith("config.local"))
    if paths:
        pedidos = [Path(p) for p in (paths if isinstance(paths, (list, tuple)) else [paths])]
        faltam = [str(p) for p in pedidos if not p.exists()]
        if faltam:
            raise SystemExit(
                f"config nao encontrado: {', '.join(faltam)}\n"
                f"configs nesta pasta: {', '.join(p.name for p in found) or 'nenhum'}")
        return pedidos
    return found or [HERE / "config.yaml"]


def load_config(path: str | Path | None = None) -> dict:
    """O config.yaml do repo nao tem quem e voce nem qual a liga. Isso vem,
    nesta ordem, de SLEEPER_USERNAME / SLEEPER_LEAGUE_ID (os secrets do
    Actions) ou do `config.local.yaml` ao lado (fora do git)."""
    p = Path(path) if path else HERE / "config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    sl = cfg.setdefault("sleeper", {})
    local = p.parent / "config.local.yaml"
    if p.name == "config.yaml" and local.exists():
        for k, v in ((yaml.safe_load(local.read_text(encoding="utf-8")) or {})
                     .get("sleeper") or {}).items():
            if v not in (None, ""):
                sl[k] = v
    for k, env in (("username", "SLEEPER_USERNAME"), ("league_id", "SLEEPER_LEAGUE_ID")):
        if os.environ.get(env):
            sl[k] = os.environ[env]
    if str(cfg.get("sleeper", {}).get("username", "")).upper() in {"", "SEU_USERNAME"}:
        raise SystemExit(
            "Falta o username do Sleeper: SLEEPER_USERNAME ou sleeper.username\n"
            f"em {p.parent / 'config.local.yaml'}.\n"
            "E o username do Sleeper (o do @), nao o display name.")
    return cfg


def base_parser(desc: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--config", nargs="*", default=None,
                    help="config(s) a rodar. Sem isso, roda todo config*.yaml da pasta")
    ap.add_argument("--week", type=int, default=None,
                    help="forca a semana da liga (default: a semana de hoje)")
    ap.add_argument("--now", default=None,
                    help="finge que agora e este instante (ISO, horario de "
                         "Brasilia se sem fuso). E como se testa a noite")
    ap.add_argument("--dry-run", action="store_true",
                    help="nao manda no Telegram, so imprime")
    ap.add_argument("--gate", action="store_true",
                    help="so manda se houver algo novo a decidir (modo agendado)")
    ap.add_argument("--player-ttl", type=float, default=1.0,
                    help="idade maxima (h) do cache de jogadores do Sleeper")
    return ap


def run_for_each_league(args, runner) -> int:
    """Roda `runner(args, config)` pra cada liga. Erro numa nao cala a outra."""
    rc = 0
    for cfg_path in all_configs(getattr(args, "config", None)):
        try:
            rc |= runner(args, cfg_path)
        except SystemExit as e:
            print(f"[{cfg_path.name}] {e}")
            rc |= 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[{cfg_path.name}] falhou: {type(e).__name__}: {e}")
            rc |= 1
    return rc


def agora_de(args, tz: ZoneInfo) -> datetime:
    raw = getattr(args, "now", None)
    if raw:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


@dataclass
class Context:
    cfg: dict
    args: argparse.Namespace
    sl: Sleeper
    pool: dict
    agenda: Agenda
    season: int
    regras: dict
    agora: datetime          # UTC
    tz: ZoneInfo             # fuso das mensagens
    warnings: list[str] = field(default_factory=list)
    _cache: dict = field(default_factory=dict)

    # -- jogador -----------------------------------------------------------
    def name(self, pid: str) -> str:
        return display_name(self.pool.get(pid) or {"player_id": pid})

    def posicoes(self, pid: str) -> list[str]:
        return posicoes(self.pool.get(pid) or {})

    def pos(self, pid: str) -> str:
        return "/".join(self.posicoes(pid))

    def team(self, pid: str) -> str:
        return (self.pool.get(pid) or {}).get("team") or ""

    def status(self, pid: str) -> str | None:
        return (self.pool.get(pid) or {}).get("injury_status")

    # -- liga ----------------------------------------------------------------
    @property
    def rotulo(self) -> str:
        """Nome da liga pro LOG: no Actions (publico), so 'liga'."""
        return "liga" if PUBLICO else self.league_name

    @property
    def league_name(self) -> str:
        try:
            return (self.sl.league().get("name") or "").strip() or "liga"
        except Exception:
            return "liga"

    @property
    def week(self) -> int:
        if getattr(self.args, "week", None):
            return int(self.args.week)
        return self.agenda.semana_atual() or 1

    def hora(self, dt: datetime | None, fmt: str = "%a %H:%M") -> str:
        if dt is None:
            return "?"
        dias = {"Mon": "seg", "Tue": "ter", "Wed": "qua", "Thu": "qui",
                "Fri": "sex", "Sat": "sab", "Sun": "dom"}
        s = dt.astimezone(self.tz).strftime(fmt)
        for en, pt in dias.items():
            s = s.replace(en, pt)
        return s

    # -- dados (preguicosos: o draft nao precisa de log nenhum) -------------
    def _lazy(self, chave, fazer):
        if chave not in self._cache:
            self._cache[chave] = fazer()
        return self._cache[chave]

    @property
    def logs(self) -> dict:
        """Log desta temporada, das semanas que ja comecaram."""
        from dados import logs

        def fazer():
            semanas = range(1, self.week + 1)
            try:
                return logs(self.sl, self.season, self.regras, semanas, self.agenda,
                            self.agora.date())
            except Exception as e:
                self.warnings.append(f"log da temporada indisponivel ({e})")
                return {}
        return self._lazy("logs", fazer)

    @property
    def anterior(self) -> dict:
        from dados import logs

        def fazer():
            try:
                return logs(self.sl, self.season - 1, self.regras)
            except Exception as e:
                self.warnings.append(f"log da temporada passada indisponivel ({e})")
                return {}
        return self._lazy("anterior", fazer)

    @property
    def proj_temp(self) -> dict:
        """{pid: {"fp", "min", "adp"}} — projecao de temporada do Sleeper."""
        from scoring import minutos, pontos

        def fazer():
            out = {}
            try:
                for r in self.sl.projecao_temporada(self.season):
                    st = r.get("stats") or {}
                    if not st.get("pts"):
                        adp = st.get("adp_std")
                        if adp and adp < 999:
                            out[str(r["player_id"])] = {"fp": 0.0, "min": 0.0, "adp": adp}
                        continue
                    out[str(r["player_id"])] = {
                        "fp": pontos(st, self.regras), "min": minutos(st),
                        "adp": st.get("adp_std") if (st.get("adp_std") or 999) < 999 else None}
            except Exception as e:
                self.warnings.append(f"projecao de temporada indisponivel ({e})")
            return out
        return self._lazy("proj_temp", fazer)

    def proj_jogo(self, week: int) -> dict:
        from dados import projecoes_jogo

        def fazer():
            try:
                # a projecao do jogo muda quando sai lesao (titular fora sobe
                # os minutos dos outros): 15 min, como o resto da noite
                return projecoes_jogo(self.sl, self.season, week, self.regras, ttl_hours=0.25)
            except Exception as e:
                self.warnings.append(f"projecao por jogo indisponivel ({e})")
                return {}
        return self._lazy(("proj_jogo", week), fazer)

    @property
    def proj_passado(self) -> dict:
        """Projecao jogo a jogo das semanas ja encerradas desta temporada.
        Semana que passou nao muda: guarda por uma semana."""
        from dados import projecoes_jogo

        def fazer():
            out: dict = {}
            for w in range(1, self.week):
                try:
                    out.update(projecoes_jogo(self.sl, self.season, w, self.regras,
                                              ttl_hours=24 * 7))
                except Exception as e:
                    self.warnings.append(f"projecao da semana {w} indisponivel ({e})")
                    break
            return out
        return self._lazy("proj_passado", fazer)

    @property
    def lesoes(self) -> dict:
        """{pid: lesao da ESPN} — com o prazo de volta (clients.espn_lesoes).
        A ESPN nao tem o id do Sleeper: casa por nome, e o time desempata."""
        from clients import espn_lesoes
        from names import normalize, team_code

        def fazer():
            try:
                lista = espn_lesoes(ttl_hours=0.25)
            except Exception as e:
                self.warnings.append(f"lesoes da ESPN indisponiveis ({e})")
                return {}
            idx: dict[str, list[str]] = {}
            for pid, p in self.pool.items():
                if p.get("team"):
                    idx.setdefault(normalize(p.get("full_name")), []).append(pid)
            out = {}
            for les in lista:
                cand = idx.get(normalize(les.get("nome")), [])
                if len(cand) > 1:
                    cand = [c for c in cand if self.team(c) == team_code(les.get("team"))]
                if len(cand) == 1:
                    out[cand[0]] = les
            return out
        return self._lazy("lesoes", fazer)

    def retorno(self, pid: str) -> str | None:
        return (self.lesoes.get(pid) or {}).get("retorno")

    @property
    def carga(self) -> dict:
        """Jogos por semana ativa, temporada passada + esta (dados.contagem_carga)."""
        from dados import contagem_carga

        def fazer():
            out = {}
            for L in (self.anterior, self.logs):
                for pid, c in contagem_carga(L).items():
                    out[pid] = [a + b for a, b in zip(out.get(pid, [0, 0, 0, 0]), c)]
            return out
        return self._lazy("carga", fazer)

    @property
    def modelo(self):
        from agenda import hoje_et
        from modelo import Modelo

        def fazer():
            pj = dict(self.proj_jogo(self.week))
            pj.update(self.proj_jogo(self.week + 1))
            datas: dict[str, set] = {}
            for j in self.agenda.jogos:
                for t in (j.home, j.away):
                    datas.setdefault(t, set()).add(j.data)
            return Modelo(self.logs, self.anterior, self.proj_temp, pj, self.pool,
                          lesoes=self.lesoes, carga=self.carga,
                          datas_time={t: sorted(v) for t, v in datas.items()},
                          hoje=hoje_et(self.agora), proj_passado=self.proj_passado)
        return self._lazy("modelo", fazer)

    def notify(self, msg: str) -> bool:
        """Imprime e manda. Devolve se o Telegram aceitou: quem marca 'ja
        avisado' precisa saber, senao uma falha de envio cala o aviso."""
        from clients import telegram
        if PUBLICO and not self.args.dry_run:
            print(f"[liga] mensagem de {len(msg.splitlines())} linhas")
        else:
            print(msg)
        if self.args.dry_run:
            print("\n(dry-run: nao enviado)")
            return False
        tg = self.cfg.get("telegram") or {}
        return telegram(msg, tg.get("bot_token", ""), tg.get("chat_id", ""))


def build(args: argparse.Namespace, config: str | Path | None = None) -> Context:
    cfg = load_config(config)
    season = int(cfg["sleeper"]["season"])
    sl = Sleeper(cfg["sleeper"]["username"], season, cfg["sleeper"].get("league_id", ""))
    tz = ZoneInfo(cfg.get("fuso") or "America/Sao_Paulo")
    agora = agora_de(args, tz)
    ag = Agenda(sl, season, agora)
    return Context(cfg=cfg, args=args, sl=sl,
                   pool=sl.players(getattr(args, "player_ttl", 1.0)),
                   agenda=ag, season=season, regras=sl.scoring_settings(),
                   agora=agora, tz=tz)
