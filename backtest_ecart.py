"""Placar no meio do jogo x minutos e pontos: o quanto o blowout senta o titular.

O bot preve o placar final de quem esta em quadra (substituicao no meio da
noite). Este backtest mede, com o play-by-play da ESPN, o que a diferenca de
placar em cada momento do jogo faz com o resto do jogo de cada tipo de
jogador (titular, rotacao, banco, terrao): minutos e pontos de fantasy a
partir dali, comparados com o que ele costuma fazer naquele trecho.

Tambem mede o relogio de parede (quanto tempo real o resto do jogo leva),
que diz se da tempo de por o reserva no slot antes do tip-off dele.

    python backtest_ecart.py baixar        # play-by-play de 2024 e 2025 (~10 min)
    python backtest_ecart.py               # as tabelas
"""

from __future__ import annotations

import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

from clients import CACHE

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PBP = CACHE / "pbp"
HOST = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
TEMPORADAS = {2023: ("2023-10-24", "2024-04-14"), 2024: ("2024-10-22", "2025-04-13"),
              2025: ("2025-10-21", "2026-04-12")}


# ==========================================================================
# download
# ==========================================================================
def _enxuto(s: dict) -> dict:
    """So o que a analise usa: ~10x menor que o resumo da ESPN."""
    plays = [{"t": (p.get("type") or {}).get("text", ""), "x": p.get("text", ""),
              "h": p.get("homeScore"), "a": p.get("awayScore"),
              "q": (p.get("period") or {}).get("number"),
              "c": (p.get("clock") or {}).get("displayValue"),
              "tm": (p.get("team") or {}).get("id"),
              "p": [(x.get("athlete") or {}).get("id") for x in p.get("participants") or []],
              "sc": p.get("scoringPlay"), "v": p.get("scoreValue"), "w": p.get("wallclock")}
             for p in s.get("plays") or []]
    times = []
    for t in (s.get("boxscore") or {}).get("players") or []:
        st = (t.get("statistics") or [{}])[0]
        times.append({"id": (t.get("team") or {}).get("id"),
                      "lab": st.get("labels"),
                      "at": [{"id": a["athlete"]["id"], "nome": a["athlete"].get("displayName"),
                              "tit": a.get("starter"), "dnp": a.get("didNotPlay"),
                              "mot": a.get("reason"), "st": a.get("stats")}
                             for a in st.get("athletes") or []]})
    comp = ((s.get("header") or {}).get("competitions") or [{}])[0]
    lados = {c.get("homeAway"): c.get("id") for c in comp.get("competitors") or []}
    return {"plays": plays, "times": times, "casa": lados.get("home"), "fora": lados.get("away")}


def baixar(season: int) -> None:
    ini, fim = (date.fromisoformat(x) for x in TEMPORADAS[season])
    pasta = PBP / str(season)
    pasta.mkdir(parents=True, exist_ok=True)
    ids = []
    d = ini
    while d <= fim:
        try:
            sb = requests.get(f"{HOST}/scoreboard", params={"dates": d.strftime("%Y%m%d")},
                              timeout=30).json()
            ids += [e["id"] for e in sb.get("events") or []
                    if (e.get("season") or {}).get("type") == 2]
        except Exception as e:
            print(f"(placar de {d}: {e})")
        d += timedelta(days=1)
    falta = [i for i in ids if not (pasta / f"{i}.json.gz").exists()]
    print(f"{season}: {len(ids)} jogos, {len(falta)} pra baixar")

    def um(ev):
        for _ in range(3):
            try:
                s = requests.get(f"{HOST}/summary", params={"event": ev}, timeout=40).json()
                (pasta / f"{ev}.json.gz").write_bytes(
                    gzip.compress(json.dumps(_enxuto(s)).encode()))
                return
            except Exception:
                continue
        print(f"(jogo {ev} falhou)")

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(um, falta))


ATLETAS = PBP / "atletas.json"


def baixar_atletas() -> None:
    """Estreia, draft e nascimento de todo mundo que aparece nos jogos."""
    try:
        feito = json.loads(ATLETAS.read_text(encoding="utf-8"))
    except Exception:
        feito = {}
    ids = set()
    for s in TEMPORADAS:
        for _, g in jogos(s):
            ids |= {a["id"] for t in g["times"] for a in t["at"]}
    falta = sorted(ids - set(feito))
    print(f"atletas: {len(ids)}, {len(falta)} pra baixar")

    def um(i):
        try:
            a = requests.get("https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
                             f"nba/athletes/{i}", timeout=40).json()
            d = a.get("draft") or {}
            return i, {"estreia": a.get("debutYear"), "draft": d.get("year"),
                       "pick": d.get("selection"), "rodada": d.get("round"),
                       "nasc": (a.get("dateOfBirth") or "")[:10], "nome": a.get("displayName")}
        except Exception:
            return i, None

    with ThreadPoolExecutor(8) as ex:
        for i, v in ex.map(um, falta):
            if v:
                feito[i] = v
    ATLETAS.write_text(json.dumps(feito), encoding="utf-8")


def jogos(season: int):
    for f in sorted((PBP / str(season)).glob("*.json.gz")):
        yield f.stem.split(".")[0], json.loads(gzip.decompress(f.read_bytes()))


# ==========================================================================
# play-by-play -> quem estava em quadra e quanto fez, segundo a segundo
# ==========================================================================
# Pontuacao da liga sem os bonus (duplo-duplo, 40 pontos): os bonus sao do
# jogo inteiro e nao tem "trecho".
FP = {"pts": 0.5, "tpm": 0.5, "reb": 1.0, "ast": 1.0, "stl": 2.0, "blk": 2.0, "to": -1.0}
REGULAMENTAR = 48 * 60


def segundos(q: int, clock: str | None) -> float:
    """Segundos de jogo corridos ate este lance (prorrogacao de 5 min)."""
    base = 720 * min(q - 1, 4) + 300 * max(q - 5, 0)
    dur = 720 if q <= 4 else 300
    c = clock or "0"
    if ":" in c:
        m, s = c.split(":")
        resta = int(m) * 60 + float(s)
    else:
        resta = float(c)
    return base + dur - resta


def _ws(w: str | None):
    from datetime import datetime
    return datetime.fromisoformat(w.replace("Z", "+00:00")) if w else None


def _antes(texto: str, nome: str, palavra: str) -> bool:
    i, j = texto.find(nome), texto.find(palavra)
    return i >= 0 and j >= 0 and i < j


class Jogo:
    """Um jogo lido: quem jogou, em que trechos, e cada ponto de fantasy."""

    def __init__(self, g: dict):
        self.casa, self.fora = g["casa"], g["fora"]
        self.time, self.nome, self.box, self.status, self.motivo = {}, {}, {}, {}, {}
        tit: dict[str, set] = {}
        for t in g["times"]:
            lab = t.get("lab") or []
            for a in t["at"]:
                pid = a["id"]
                self.time[pid], self.nome[pid] = t["id"], a.get("nome") or pid
                mot = (a.get("mot") or "").upper()
                if a.get("dnp"):
                    # so o "coach's decision" estava disponivel e ficou no banco
                    self.status[pid] = "cd" if "COACH" in mot else "fora"
                    self.motivo[pid] = mot
                else:
                    self.status[pid] = "jogou"
                    self.box[pid] = dict(zip(lab, a.get("st") or []))
                if a.get("tit"):
                    tit.setdefault(t["id"], set()).add(pid)
        self.plays = [p for p in g["plays"] if p.get("q")]
        self.eventos: list[tuple[float, str, float]] = []      # (seg, pid, fp)
        self.conta: dict[str, dict] = {}                         # validacao
        self.trechos: dict[str, list[list[float]]] = {}
        self.placar: list[tuple[float, int, int, object]] = []  # (seg, casa, fora, relogio)
        self.hora_q: dict[int, list] = {}                         # quarto -> horas dos lances
        self._ler(tit)

    # -- stats -------------------------------------------------------------
    def _soma(self, pid, k, v=1):
        if not pid:
            return
        self.conta.setdefault(pid, {}).setdefault(k, 0)
        self.conta[pid][k] += v

    def _ev(self, seg, pid, k, v=1):
        if pid and pid in self.time:
            self.eventos.append((seg, pid, FP[k] * v))
            self._soma(pid, k, v)

    def _stats(self, p, seg):
        t, x, ps = p["t"], p["x"] or "", [i for i in p["p"] if i]
        nome = lambda i: self.nome.get(i, "#")
        # o primeiro participante e o dono do lance; o segundo, quem deu a
        # assistencia ou roubou ("X bad pass (Y steals)")
        if p.get("sc") and ps:
            v = int(p.get("v") or 0)
            self._ev(seg, ps[0], "pts", v)
            if v == 3 and "free throw" not in t.lower():
                self._ev(seg, ps[0], "tpm")
            if "assist" in x and len(ps) > 1:
                self._ev(seg, ps[1], "ast")
        if "Rebound" in t and ps:
            self._ev(seg, ps[0], "reb")
        if "Turnover" in t and ps:
            self._ev(seg, ps[0], "to")
            if "steal" in x and len(ps) > 1:
                self._ev(seg, ps[1], "stl")
        if "blocks" in x:
            for i in ps:
                if _antes(x, nome(i), "blocks"):
                    self._ev(seg, i, "blk")
                    break

    # -- quem esta em quadra -----------------------------------------------
    def _ler(self, tit):
        por_q: dict[int, list] = {}
        for p in self.plays:
            por_q.setdefault(p["q"], []).append(p)
        quadra = {t: set(v) for t, v in tit.items()}
        for q in sorted(por_q):
            ini = 720 * min(q - 1, 4) + 300 * max(q - 5, 0)
            fim = ini + (720 if q <= 4 else 300)
            ps = por_q[q]
            if q > 1:
                quadra = self._inicio_do_quarto(ps, quadra)
            abertos = {pid: ini for t in quadra.values() for pid in t}
            for p in ps:
                seg = segundos(q, p.get("c"))
                self.placar.append((seg, p.get("h") or 0, p.get("a") or 0, _ws(p.get("w"))))
                if p.get("w"):
                    self.hora_q.setdefault(q, []).append(_ws(p["w"]))
                ids = [i for i in p["p"] if i]
                if p["t"] == "Substitution" and len(ids) >= 2:
                    entra, sai = ids[0], ids[1]
                    if not _antes(p["x"] or "", self.nome.get(entra, "#"), "enters"):
                        entra, sai = sai, entra
                    t = self.time.get(entra) or self.time.get(sai)
                    if t is None:
                        continue
                    if sai in abertos:
                        self.trechos.setdefault(sai, []).append([abertos.pop(sai), seg])
                    quadra.setdefault(t, set()).discard(sai)
                    quadra[t].add(entra)
                    abertos.setdefault(entra, seg)
                    continue
                self._stats(p, seg)
                for i in ids:
                    # lance de quem "nao estava" em quadra: o ESPN pulou a troca
                    t = self.time.get(i)
                    if t and i not in abertos and p["t"] not in ("Technical Foul",):
                        quadra.setdefault(t, set()).add(i)
                        abertos[i] = seg
            for pid, a in abertos.items():
                self.trechos.setdefault(pid, []).append([a, fim])
        self.fim = max((seg for seg, *_ in self.placar), default=REGULAMENTAR)

    def _inicio_do_quarto(self, ps, antes):
        """Quem comeca o quarto: quem aparece no quarto sem ter entrado nele,
        completado (ate 5 por time) com quem terminou o anterior e nao
        aparece."""
        primeiro: dict[str, str] = {}
        for p in ps:
            ids = [i for i in p["p"] if i]
            if p["t"] == "Substitution" and len(ids) >= 2:
                entra, sai = ids[0], ids[1]
                if not _antes(p["x"] or "", self.nome.get(entra, "#"), "enters"):
                    entra, sai = sai, entra
                primeiro.setdefault(entra, "entra")
                primeiro.setdefault(sai, "estava")
            else:
                for i in ids:
                    primeiro.setdefault(i, "estava")
        novo = {}
        for t in set(self.time.values()):
            q = {i for i, v in primeiro.items() if v == "estava" and self.time.get(i) == t}
            for i in sorted(antes.get(t, ())):
                if len(q) >= 5:
                    break
                if i not in primeiro:
                    q.add(i)
            novo[t] = q
        return novo

    # -- consultas ---------------------------------------------------------
    def minutos(self, pid, a=0.0, b=1e9) -> float:
        return sum(max(0.0, min(f, b) - max(i, a)) for i, f in self.trechos.get(pid, ())) / 60

    def fp(self, pid, a=0.0, b=1e9) -> float:
        if not hasattr(self, "_por"):
            self._por: dict[str, list] = {}
            for s, p, v in self.eventos:
                self._por.setdefault(p, []).append((s, v))
        return sum(v for s, v in self._por.get(pid, ()) if a <= s < b)

    def margem(self, seg) -> tuple[int, int]:
        """(casa, fora) no ultimo lance ate `seg`."""
        h = a = 0
        for s, hh, aa, _ in self.placar:
            if s > seg:
                break
            h, a = hh, aa
        return h, a

    def relogio(self, seg):
        """Hora real do primeiro lance a partir de `seg`."""
        return next((w for s, *_, w in self.placar if s >= seg and w), None)


# ==========================================================================
# validacao: o play-by-play bate com o boxscore?
# ==========================================================================
def validar(season: int, n: int = 300) -> None:
    pares = {"PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl", "BLK": "blk", "TO": "to"}
    erro = {k: [] for k in list(pares) + ["MIN"]}
    for k, (_, g) in enumerate(jogos(season)):
        if k >= n:
            break
        j = Jogo(g)
        for pid, box in j.box.items():
            c = j.conta.get(pid, {})
            for lab, kk in pares.items():
                try:
                    erro[lab].append(abs(int(box.get(lab) or 0) - c.get(kk, 0)))
                except ValueError:
                    pass
            try:
                erro["MIN"].append(abs(float(box.get("MIN") or 0) - j.minutos(pid)))
            except ValueError:
                pass
    print(f"\n## Validação {season} ({n} jogos): play-by-play x boxscore")
    print("| stat | erro médio | % exato | % erro ≤1 |\n|---|---|---|---|")
    for lab, v in erro.items():
        if v:
            print(f"| {lab} | {sum(v) / len(v):.2f} | {100 * sum(x < 0.5 for x in v) / len(v):.0f}% "
                  f"| {100 * sum(x <= 1 for x in v) / len(v):.0f}% |")


# ==========================================================================
# registros: cada jogador em cada jogo, em cada momento
# ==========================================================================
MOMENTOS = [(12, "início do 2º"), (24, "início do 3º"), (30, "6:00 do 3º"),
            (36, "início do 4º"), (39, "9:00 do 4º"), (42, "6:00 do 4º"), (45, "3:00 do 4º")]
FAIXAS = [(0, 2), (3, 5), (6, 8), (9, 11), (12, 14), (15, 17), (18, 20), (21, 24), (25, 29), (30, 99)]
TIERS = [("estrela 32+", 32, 99), ("titular 26-32", 26, 32), ("rotação 18-26", 18, 26),
         ("banco 10-18", 10, 18), ("terrão <10", 0, 10)]


def registros(season: int) -> tuple[list[dict], list[dict], list[dict]]:
    """(jogador-jogo, jogo). Jogador-jogo so de quem estava disponivel (jogou
    ou ficou no banco por decisao do tecnico): sem o terrao que nao entrou,
    o blowout pareceria so dar minuto."""
    rows, games, fora = [], [], []
    for gid, g in jogos(season):
        try:
            j = Jogo(g)
        except Exception as e:
            print(f"(jogo {gid}: {e})")
            continue
        if not j.placar or j.fim < REGULAMENTAR - 1:
            continue
        mg = {c: j.margem(c * 60) for c, _ in MOMENTOS}
        fh, fa = j.margem(1e9)
        w_fim = next((w for *_, w in reversed(j.placar) if w), None)
        w_q2 = max(j.hora_q[2]) if j.hora_q.get(2) else None
        w_q3 = min(j.hora_q.get(3)) if j.hora_q.get(3) else None
        w0 = next((w for *_, w in j.placar if w), None)
        if w0 is None:
            continue
        data = (w0 - timedelta(hours=6)).date().isoformat() if w0 else ""
        games.append({"g": gid, "data": data, "casa": j.casa, "fora": j.fora, "marg": {c: h - a for c, (h, a) in mg.items()}, "final": fh - fa,
                      "ot": j.fim > REGULAMENTAR + 1, "real": {c: (w_fim - j.relogio(c * 60)).total_seconds() / 60
                                                             if w_fim and j.relogio(c * 60) else None
                                                             for c, _ in MOMENTOS},
                      "intervalo": (w_q3 - w_q2).total_seconds() / 60 if w_q2 and w_q3 else None,
                      "jogo_min": j.fim / 60})
        for pid, st in j.status.items():
            if st == "fora":
                fora.append({"g": gid, "pid": pid, "season": season, "data": data,
                             "time": j.time[pid], "mot": j.motivo.get(pid, "")})
                continue
            lado = 1 if j.time[pid] == j.casa else -1
            r = {"g": gid, "pid": pid, "season": season, "data": data, "time": j.time[pid],
                 "min": j.minutos(pid), "fp": j.fp(pid),
                 "final": lado * (fh - fa), "ot": j.fim > REGULAMENTAR + 1}
            for c, _ in MOMENTOS:
                h, a = mg[c]
                r[c] = (lado * (h - a), j.minutos(pid, c * 60), j.fp(pid, c * 60), j.fp(pid, 0, c * 60))
            rows.append(r)
    return rows, games, fora


def com_base(rows: list[dict]) -> list[dict]:
    """Poe em cada linha a faixa do jogador (minutos quando joga) e o normal
    dele em cada trecho (media do trecho em todos os jogos dele)."""
    por: dict[str, list] = {}
    for r in rows:
        por.setdefault(r["pid"], []).append(r)
    out = []
    for pid, rs in por.items():
        jogou = [r["min"] for r in rs if r["min"] > 0]
        if len(rs) < 15 or len(jogou) < 8:
            continue
        mm = sum(jogou) / len(jogou)
        tier = next(t for t, lo, hi in TIERS if lo <= mm < hi)
        mu = sum(r["fp"] for r in rs) / len(rs)
        sd = (sum((r["fp"] - mu) ** 2 for r in rs) / len(rs)) ** 0.5
        jg = [r["fp"] for r in rs if r["min"] > 0]
        mu_j = sum(jg) / len(jg)
        sd_j = (sum((x - mu_j) ** 2 for x in jg) / len(jg)) ** 0.5
        base = {c: (sum(r[c][1] for r in rs) / len(rs), sum(r[c][2] for r in rs) / len(rs))
                for c, _ in MOMENTOS}
        for r in rs:
            out.append({**r, "tier": tier, "mm": mm, "mu": mu, "sd": sd, "base": base,
                        "mu_j": mu_j, "sd_j": sd_j})
    return out


def faixa(d: int) -> str:
    d = abs(d)
    lo, hi = next(f for f in FAIXAS if f[0] <= d <= f[1])
    return f"{lo}+" if hi == 99 else f"{lo}-{hi}"


# ==========================================================================
# tabelas
# ==========================================================================
def tabela_razao(rs, c, k, titulo):
    """Razao (feito / normal do jogador) no resto do jogo, por faixa de
    diferenca no momento c e faixa do jogador. k=1 minutos, k=2 pontos."""
    agg: dict = {}
    for r in rs:
        d, *v = r[c]
        key = (faixa(d), r["tier"])
        a = agg.setdefault(key, [0.0, 0.0, 0])
        a[0] += v[k - 1]
        a[1] += r["base"][c][k - 1]
        a[2] += 1
    print(f"\n### {titulo}")
    print("| diferença | " + " | ".join(t for t, *_ in TIERS) + " | n |")
    print("|---|" + "---|" * (len(TIERS) + 1))
    for lo, hi in FAIXAS:
        f = f"{lo}+" if hi == 99 else f"{lo}-{hi}"
        cel, tot = [], 0
        for t, *_ in TIERS:
            a = agg.get((f, t))
            if a and a[1] > 0 and a[2] >= 40:
                cel.append(f"{a[0] / a[1]:.2f}")
            else:
                cel.append("·")
            tot += a[2] if a else 0
        print(f"| {f} | " + " | ".join(cel) + f" | {tot} |")


def tabela_lado(rs, c):
    """Blowout: quem esta ganhando senta mais que quem esta perdendo?"""
    print(f"\n### Ganhando x perdendo, {dict(MOMENTOS)[c]} (pontos no resto / normal)")
    print("| diferença | lado | " + " | ".join(t for t, *_ in TIERS[:4]) + " |")
    print("|---|---|" + "---|" * 4)
    for f in [(0, 5), (6, 11), (12, 17), (18, 24), (25, 99)]:
        for lado, nome in ((1, "ganhando"), (-1, "perdendo")):
            cel = []
            for t, *_ in TIERS[:4]:
                a = b = 0.0
                k = 0
                for r in rs:
                    d = r[c][0]
                    if r["tier"] != t or not (f[0] <= abs(d) <= f[1]) or (d * lado <= 0 and d != 0):
                        continue
                    if d == 0 and lado == -1:
                        continue
                    a += r[c][2]
                    b += r["base"][c][1]
                    k += 1
                cel.append(f"{a / b:.2f}" if b > 0 and k >= 40 else "·")
            rot = f"{f[0]}+" if f[1] == 99 else f"{f[0]}-{f[1]}"
            print(f"| {rot} | {nome} | " + " | ".join(cel) + " |")


def tabela_virada(games):
    """Blowout no meio do jogo vira jogo apertado?"""
    print("\n### O placar do momento segura até o fim? (% dos jogos)")
    print("| momento | diferença | n | final ≤5 | final ≤9 | final ≥15 | prorrogação |")
    print("|---|---|---|---|---|---|---|")
    for c, nome in MOMENTOS:
        for f in [(0, 5), (6, 11), (12, 17), (18, 24), (25, 99)]:
            gs = [g for g in games if f[0] <= abs(g["marg"][c]) <= f[1]]
            if len(gs) < 20:
                continue
            p = lambda cond: 100 * sum(cond(g) for g in gs) / len(gs)
            rot = f"{f[0]}+" if f[1] == 99 else f"{f[0]}-{f[1]}"
            print(f"| {nome} | {rot} | {len(gs)} | {p(lambda g: abs(g['final']) <= 5):.0f}% "
                  f"| {p(lambda g: abs(g['final']) <= 9):.0f}% | {p(lambda g: abs(g['final']) >= 15):.0f}% "
                  f"| {p(lambda g: g['ot']):.1f}% |")


def _pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def tabela_relogio(games):
    """Quanto tempo real falta, por momento e placar: e o "da tempo?"."""
    print("\n### Relógio de parede: minutos reais até o fim do jogo")
    print("| momento | diferença | n | mediana | p10 | p90 | real por minuto de jogo | bot hoje |")
    print("|---|---|---|---|---|---|---|---|")
    for c, nome in MOMENTOS:
        falta = 48 - c
        bot = 2.6 * falta + (15 if falta > 24 else 0)
        for f in [(0, 5), (6, 14), (15, 99)]:
            v = [g["real"][c] for g in games if g["real"][c] and f[0] <= abs(g["marg"][c]) <= f[1]
                 and not g["ot"]]
            if len(v) < 20:
                continue
            rot = f"{f[0]}+" if f[1] == 99 else f"{f[0]}-{f[1]}"
            md = _pct(v, .5)
            print(f"| {nome} | {rot} | {len(v)} | {md:.0f} | {_pct(v, .1):.0f} | {_pct(v, .9):.0f} "
                  f"| {(md - (15 if falta > 24 else 0)) / falta:.2f} | {bot:.0f} |")
    iv = [g["intervalo"] for g in games if g["intervalo"]]
    print(f"\nIntervalo: mediana {_pct(iv, .5):.1f} min, p10 {_pct(iv, .1):.1f}, p90 {_pct(iv, .9):.1f}")


def tabela_projecao(rs):
    """A projecao ao vivo do bot (feito + media x fracao que falta) erra pra
    que lado, e o desvio que ela usa (sd x raiz da fracao) e o certo?"""
    print("\n### Projeção ao vivo: realizado / projetado no resto do jogo, e o desvio")
    print("| momento | faixa | n | resto real / projetado | desvio real / desvio do bot |")
    print("|---|---|---|---|---|")
    for c, nome in MOMENTOS:
        f = (48 - c) / 48
        for t, *_ in TIERS[:4]:
            v = [r for r in rs if r["tier"] == t and r["min"] > 0]
            if len(v) < 100:
                continue
            real = sum(r[c][2] for r in v)
            proj = sum(r["mu"] * f for r in v)
            res = [r[c][2] - r["mu"] * f for r in v]
            m = sum(res) / len(res)
            sd_real = (sum((x - m) ** 2 for x in res) / len(res)) ** 0.5
            sd_bot = (sum((r["sd"] * f ** 0.5) ** 2 for r in v) / len(v)) ** 0.5
            print(f"| {nome} | {t} | {len(v)} | {real / proj:.2f} | {sd_real / sd_bot:.2f} |")


def tabela_quartos(rs):
    """Como cada faixa divide os minutos pelos quartos (o bot assume igual)."""
    print("\n### Divisão do jogo de cada faixa (fração dos minutos e dos pontos por trecho)")
    cortes = [0, 12, 24, 36, 42, 48]
    nomes = ["1º", "2º", "3º", "4º até 6:00", "últimos 6:00"]
    print("| faixa | " + " | ".join(nomes) + " |\n|---|" + "---|" * len(nomes))
    for t, *_ in TIERS:
        v = [r for r in rs if r["tier"] == t]
        tot_m = sum(r["min"] for r in v) or 1
        cel = []
        for a, b in zip(cortes, cortes[1:]):
            ma = sum((r[a][1] if a else r["min"]) - (r[b][1] if b < 48 else 0) for r in v)
            cel.append(f"{100 * ma / tot_m:.0f}%")
        print(f"| {t} | " + " | ".join(cel) + " |")


def ajustar(rs, c, regra):
    """Fator por (faixa do jogador, classe) que multiplica o normal do resto."""
    agg: dict = {}
    for r in rs:
        k = (r["tier"], regra(r[c][0], c))
        a = agg.setdefault(k, [0.0, 0.0])
        a[0] += r[c][2]
        a[1] += r["base"][c][1]
    return {k: a / b if b > 0 else 1.0 for k, (a, b) in agg.items()}


def erro(rs, c, regra, fatores):
    return sum((r[c][2] - r["base"][c][1] * fatores.get((r["tier"], regra(r[c][0], c)), 1.0)) ** 2
               for r in rs) / max(len(rs), 1)


def tabela_regras(treino, teste):
    """Que corte de pau a pau / blowout explica melhor o resto do jogo?
    Ajusta em 2024, mede em 2025: % do erro quadratico que a regra tira em
    relacao a usar so o normal do jogador."""
    regras = {"nenhuma": lambda d, c: "-"}
    for a in (3, 5, 8):
        for b in (10, 12, 15, 18, 20, 25):
            regras[f"pau ≤{a}, blowout ≥{b}"] = (lambda a, b: lambda d, c:
                                                 "pau" if abs(d) <= a else "blow" if abs(d) >= b else "normal")(a, b)
    # pelo tempo que falta: a mesma diferenca pesa mais no fim
    for k in (2.0, 3.0, 4.0):
        regras[f"blowout se dif ≥ {k:g}·√(min que faltam)"] = (lambda k: lambda d, c:
            "blow" if abs(d) >= k * (48 - c) ** 0.5 else "pau" if abs(d) <= 5 else "normal")(k)
    regras["faixa fina (teto)"] = lambda d, c: faixa(d)
    print("\n### Qual corte explica melhor? (% do erro tirado, ajustado em 2024, medido em 2025)")
    cs = [c for c, _ in MOMENTOS if c >= 24]
    print("| regra | " + " | ".join(dict(MOMENTOS)[c] for c in cs) + " | média |")
    print("|---|" + "---|" * (len(cs) + 1))
    res = []
    for nome, regra in regras.items():
        ganhos = []
        for c in cs:
            e0 = erro(teste, c, regras["nenhuma"], ajustar(treino, c, regras["nenhuma"]))
            e1 = erro(teste, c, regra, ajustar(treino, c, regra))
            ganhos.append(100 * (1 - e1 / e0))
        res.append((sum(ganhos) / len(ganhos), nome, ganhos))
    for media, nome, ganhos in sorted(res, reverse=True)[:14]:
        print(f"| {nome} | " + " | ".join(f"{g:.1f}%" for g in ganhos) + f" | {media:.1f}% |")


# ==========================================================================
# experiencia, draft, fase da temporada
# ==========================================================================
EXP = [("calouro", 0, 0), ("2º-3º ano", 1, 2), ("4º-7º ano", 3, 6), ("veterano 8+", 7, 99)]
DRAFT = [("pick 1-10", 1, 10), ("pick 11-30", 11, 30), ("2ª rodada", 31, 60), ("não draftado", 0, 0)]
FASES = [("out-dez", (10, 11, 12)), ("jan-fev", (1, 2)), ("mar-abr", (3, 4))]


CAMPANHA = [("início (<10 jogos)", None, None), ("tank <35%", 0, .35), ("35-50%", .35, .5),
            ("50-65%", .5, .65), ("favorito 65%+", .65, 1.01)]


def campanhas(games) -> dict[tuple[str, str], str]:
    """{(jogo, time): faixa do aproveitamento do time ATE a vespera}."""
    out, reg = {}, {}
    for g in sorted(games, key=lambda g: (g["data"], g["g"])):
        for t in (g["casa"], g["fora"]):
            v, d = reg.get(t, (0, 0))
            if v + d < 10:
                out[(g["g"], t)] = CAMPANHA[0][0]
            else:
                pct = v / (v + d)
                out[(g["g"], t)] = next(n for n, lo, hi in CAMPANHA[1:] if lo <= pct < hi)
        vence = g["casa"] if g["final"] > 0 else g["fora"]
        for t in (g["casa"], g["fora"]):
            v, d = reg.get(t, (0, 0))
            reg[t] = (v + (t == vence), d + (t != vence))
    return out


def estreias() -> dict[str, int]:
    """{id ESPN: temporada de estreia}. A ESPN nao preenche a estreia de
    quase metade (os mais novos): vale o ano do draft (o draftado estreia na
    temporada do draft) e, pro nao draftado, o years_exp do Sleeper."""
    try:
        at = json.loads(ATLETAS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    sleeper = {}
    try:
        from clients import Sleeper
        from contexte import HERE, load_config
        cfg = load_config(HERE / "config.yaml")
        pool = Sleeper(cfg["sleeper"]["username"], 2026, cfg["sleeper"]["league_id"]).players(24 * 30)
        import re
        import unicodedata
        nz = lambda t: re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", t or "")
                              .encode("ascii", "ignore").decode().lower())
        for p in pool.values():
            if p.get("years_exp") is not None:
                sleeper[nz(f"{p.get('first_name', '')}{p.get('last_name', '')}")] = 2026 - int(p["years_exp"])
    except Exception as e:
        print(f"(sem o Sleeper pra estreia: {e})")
    import re
    import unicodedata
    out = {}
    for i, a in at.items():
        e = a.get("estreia") or a.get("draft")
        if not e:
            n = re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", a.get("nome") or "")
                       .encode("ascii", "ignore").decode().lower())
            e = sleeper.get(n)
        if e:
            out[i] = int(e)
    return out


def com_perfil(rs, camp=None):
    try:
        at = json.loads(ATLETAS.read_text(encoding="utf-8"))
    except Exception:
        at = {}
    est = estreias()
    for r in rs:
        a = at.get(r["pid"]) or {}
        e = max(0, r["season"] - est[r["pid"]]) if r["pid"] in est else None
        r["exp"] = next((n for n, lo, hi in EXP if e is not None and lo <= e <= hi), None)
        r["anos"] = e
        r["pick"] = (a.get("pick") or 0) if a else None
        nasc = a.get("nasc")
        r["idade"] = ((date.fromisoformat(r["data"]) - date.fromisoformat(nasc)).days / 365.25
                      if nasc and r.get("data") else None)
        pk = a.get("pick") or 0
        r["draft"] = next((n for n, lo, hi in DRAFT if lo <= pk <= hi), None) if a else None
        mes = int(r["data"][5:7]) if r.get("data") else 0
        r["fase"] = next((n for n, ms in FASES if mes in ms), None)
        r["camp"] = (camp or {}).get((r["g"], r.get("time")))
    return rs


def razao(rs, c, k=2, filtro=lambda r: True, dif=(0, 99)):
    a = b = 0.0
    n = 0
    for r in rs:
        if filtro(r) and dif[0] <= abs(r[c][0]) <= dif[1]:
            a += r[c][k]
            b += r["base"][c][k - 1]
            n += 1
    return (a / b if b > 0 else None), n


NORMAL, BLOW = (6, 14), (20, 99)


def _cel(rs, c, f, k=2, minimo=30):
    rn, nn = razao(rs, c, k, f, NORMAL)
    rb, nb = razao(rs, c, k, f, BLOW)
    if rn is None or rb is None or nb < minimo:
        return "·"
    return f"{rb / rn:.2f} <sub>n={nb}</sub>"


def tabela_experiencia(rs, c):
    """Efeito do blowout (resto do jogo no blowout / no jogo normal), por
    minutagem e experiencia. < 1 = senta no blowout; > 1 = ganha minuto."""
    print(f"\n### Experiência × minutagem: efeito do blowout (20+) sobre o normal (6-14), "
          f"{dict(MOMENTOS)[c]} — pontos de fantasy no resto do jogo")
    print("| faixa | " + " | ".join(n for n, *_ in EXP) + " |\n|---|" + "---|" * len(EXP))
    for t, *_ in TIERS:
        print(f"| {t} | " + " | ".join(_cel(rs, c, lambda r, t=t, e=e: r["tier"] == t and r["exp"] == e)
                                       for e, *_ in EXP) + " |")
    print(f"\n#### mesma tabela, em minutos")
    print("| faixa | " + " | ".join(n for n, *_ in EXP) + " |\n|---|" + "---|" * len(EXP))
    for t, *_ in TIERS:
        print(f"| {t} | " + " | ".join(_cel(rs, c, lambda r, t=t, e=e: r["tier"] == t and r["exp"] == e, 1)
                                       for e, *_ in EXP) + " |")


def tabela_lado_exp(rs, c):
    print(f"\n### Experiência × lado do blowout, {dict(MOMENTOS)[c]} (titular+rotação, pontos)")
    print("| experiência | ganhando | perdendo |\n|---|---|---|")
    for e, *_ in EXP:
        cel = []
        for lado in (1, -1):
            f = lambda r, e=e, lado=lado: (r["exp"] == e and r["tier"] in ("estrela 32+", "titular 26-32",
                                            "rotação 18-26") and r[c][0] * lado >= 0)
            cel.append(_cel(rs, c, f))
        print(f"| {e} | " + " | ".join(cel) + " |")


def tabela_draft(rs, c):
    print(f"\n### Jovens (até 3º ano) por pick do draft, {dict(MOMENTOS)[c]} (efeito do blowout, pontos)")
    print("| faixa | " + " | ".join(n for n, *_ in DRAFT) + " |\n|---|" + "---|" * len(DRAFT))
    for t, *_ in TIERS[:4]:
        print(f"| {t} | " + " | ".join(_cel(rs, c, lambda r, t=t, d=d: r["tier"] == t and r["draft"] == d
                                                and r["exp"] in ("calouro", "2º-3º ano"))
                                       for d, *_ in DRAFT) + " |")


def tabela_fase(rs, c):
    print(f"\n### Fase da temporada × experiência, {dict(MOMENTOS)[c]} (titular+rotação)")
    print("efeito do blowout sobre o normal; e, entre parênteses, os minutos do jogo inteiro "
          "naquela fase sobre a média da temporada dele")
    print("| experiência | " + " | ".join(n for n, _ in FASES) + " |\n|---|" + "---|" * len(FASES))
    for e, *_ in EXP:
        cel = []
        for fase, _ in FASES:
            f = lambda r, e=e, fase=fase: (r["exp"] == e and r["fase"] == fase
                                           and r["tier"] in ("estrela 32+", "titular 26-32", "rotação 18-26"))
            v = [r for r in rs if f(r)]
            tend = (sum(r["min"] for r in v) / sum(r["mm"] for r in v)) if v else None
            cel.append(_cel(rs, c, f) + (f" ({tend:.2f})" if tend else ""))
        print(f"| {e} | " + " | ".join(cel) + " |")


def tabela_campanha(rs, c):
    print(f"\n### Campanha do time × minutagem: efeito do blowout, {dict(MOMENTOS)[c]} (pontos)")
    print("| campanha | " + " | ".join(t for t, *_ in TIERS) + " |\n|---|" + "---|" * len(TIERS))
    for cp, *_ in CAMPANHA:
        print(f"| {cp} | " + " | ".join(_cel(rs, c, lambda r, t=t, cp=cp: r["tier"] == t and r["camp"] == cp)
                                        for t, *_ in TIERS) + " |")
    print(f"\n#### campanha × experiência (titular+rotação)")
    print("| campanha | " + " | ".join(e for e, *_ in EXP) + " |\n|---|" + "---|" * len(EXP))
    for cp, *_ in CAMPANHA:
        print(f"| {cp} | " + " | ".join(_cel(rs, c, lambda r, e=e, cp=cp: r["exp"] == e and r["camp"] == cp
                                                and r["tier"] in ("estrela 32+", "titular 26-32", "rotação 18-26"))
                                        for e, *_ in EXP) + " |")


# ==========================================================================
# a semana do Lock-In: jogar os jogos, media, e superar o primeiro placar
# ==========================================================================
def _semana(data: str) -> tuple:
    return date.fromisoformat(data).isocalendar()[:2]


def tabela_disponibilidade(rs, fora):
    """Nas semanas em que ele jogou ao menos uma vez, quantos jogos do time
    ele jogou (o bot usa 90% normal, 82% no b2b, fixo na temporada)."""
    tier = {(r["season"], r["pid"]): r for r in rs}
    sem: dict = {}
    for r in rs:
        k = (r["season"], r["pid"], _semana(r["data"]))
        a = sem.setdefault(k, [0, 0, r])
        a[0] += 1
        a[1] += r["min"] > 0
    motivos: dict = {}
    for f in fora:
        ref = tier.get((f["season"], f["pid"]))
        if not ref:
            continue
        k = (f["season"], f["pid"], _semana(f["data"]))
        a = sem.setdefault(k, [0, 0, {**ref, **f, "min": 0}])
        a[0] += 1
        m = f["mot"].split(" - ")[0] or "?"
        motivos[m] = motivos.get(m, 0) + 1
    print("\n### Motivos de quem ficou fora (ESPN)")
    for m, k in sorted(motivos.items(), key=lambda x: -x[1])[:8]:
        print(f"- {m}: {k}")

    def taxa(filtro):
        jg = tot = semanas = zeradas = 0
        for (s, pid, w), (n, joga, r) in sem.items():
            ref = tier.get((s, pid))
            if not ref or not filtro({**ref, "data": r["data"], "fase": r.get("fase"),
                                      "camp": r.get("camp")}):
                continue
            semanas += 1
            if joga == 0:
                zeradas += 1
                continue
            jg += joga
            tot += n
        if not semanas or semanas < 60:
            return "·"
        return f"{100 * jg / tot:.0f}% · {100 * zeradas / semanas:.0f}% <sub>n={semanas}</sub>"

    for nome, grupos, chave in (("minutagem", [t for t, *_ in TIERS], "tier"),
                                ("experiência", [e for e, *_ in EXP], "exp"),
                                ("campanha", [c for c, *_ in CAMPANHA], "camp")):
        print(f"\n### Jogos jogados nas semanas ativas · semanas inteiras fora — fase × {nome}")
        print(f"| {nome} | " + " | ".join(f for f, _ in FASES) + " |\n|---|" + "---|" * len(FASES))
        for g in grupos:
            print(f"| {g} | " + " | ".join(taxa(lambda r, g=g, f=f: r.get(chave) == g and r.get("fase") == f
                                                and r["tier"] != "terrão <10")
                                           for f, _ in FASES) + " |")


def tabela_media_fase(rs):
    """Media por jogo jogado na fase / media da temporada dele, e o desvio
    relativo (sd/media): o bot usa a media da temporada o ano inteiro."""
    for nome, grupos, chave in (("experiência", [e for e, *_ in EXP], "exp"),
                                ("campanha", [c for c, *_ in CAMPANHA], "camp"),
                                ("minutagem", [t for t, *_ in TIERS[:4]], "tier")):
        print(f"\n### Pontos por jogo na fase / média da temporada dele (e oscilação sd/média) — {nome}")
        print(f"| {nome} | " + " | ".join(f for f, _ in FASES) + " |\n|---|" + "---|" * len(FASES))
        for g in grupos:
            cel = []
            for f, _ in FASES:
                v = [r for r in rs if r.get(chave) == g and r["fase"] == f and r["min"] > 0
                     and r["tier"] != "terrão <10" and r["mu"] > 5]
                if len(v) < 200:
                    cel.append("·")
                    continue
                med = sum(r["fp"] for r in v) / sum(r["mu_j"] for r in v)
                cv = (sum((r["fp"] / r["mu_j"] - 1) ** 2 for r in v) / len(v)) ** 0.5
                cel.append(f"{med:.2f} · {cv:.2f}")
            print(f"| {g} | " + " | ".join(cel) + " |")


def tabela_superar(rs):
    """Semana com 2+ jogos: depois do primeiro, algum jogo seguinte passou
    dele? Observado x o que o modelo diz (Normal(media, sd) da temporada,
    jogos independentes). E o coracao do "passe e espere"."""
    from math import erf
    Phi = lambda z: 0.5 * (1 + erf(z / 2 ** 0.5))
    sem: dict = {}
    for r in rs:
        if r["min"] > 0 and r["tier"] != "terrão <10":
            sem.setdefault((r["season"], r["pid"], _semana(r["data"])), []).append(r)
    casos = []
    for js in sem.values():
        if len(js) < 2:
            continue
        js.sort(key=lambda r: r["data"])
        r0 = js[0]
        sd = max(r0["sd_j"], 1.0)
        bateu = any(r["fp"] > r0["fp"] for r in js[1:])
        prev = 1 - Phi((r0["fp"] - r0["mu_j"]) / sd) ** (len(js) - 1)
        casos.append((r0, bateu, prev))
    print("\n### Superar o primeiro placar da semana: observado x modelo")
    for nome, grupos, chave in (("fase", [f for f, _ in FASES], "fase"),
                                ("experiência", [e for e, *_ in EXP], "exp"),
                                ("campanha", [c for c, *_ in CAMPANHA], "camp")):
        print(f"\n| {nome} | n | observado | modelo |\n|---|---|---|---|")
        for g in grupos:
            v = [(b, p) for r, b, p in casos if r.get(chave) == g]
            if len(v) < 100:
                continue
            print(f"| {g} | {len(v)} | {100 * sum(b for b, _ in v) / len(v):.0f}% "
                  f"| {100 * sum(p for _, p in v) / len(v):.0f}% |")
    print("\n#### pelo primeiro placar (em desvios da média dele)")
    print("| primeiro jogo | n | observado | modelo |\n|---|---|---|---|")
    for lo, hi, rot in ((-9, -1, "bem abaixo (< -1 sd)"), (-1, 0, "abaixo"), (0, 1, "acima"),
                        (1, 9, "bem acima (> +1 sd)")):
        v = [(b, p) for r, b, p in casos if lo <= (r["fp"] - r["mu_j"]) / max(r["sd_j"], 1) < hi]
        if v:
            print(f"| {rot} | {len(v)} | {100 * sum(b for b, _ in v) / len(v):.0f}% "
                  f"| {100 * sum(p for _, p in v) / len(v):.0f}% |")


# ==========================================================================
# qual recorte da temporada explica mais?
# ==========================================================================
ALLSTAR = {2023: "2024-02-16", 2024: "2025-02-14", 2025: "2026-02-13"}
PRAZO = {2023: "2024-02-08", 2024: "2025-02-06", 2025: "2026-02-05"}


def _frac(r) -> float:
    ini, fim = TEMPORADAS[r["season"]]
    a, b, d = (date.fromisoformat(x) for x in (ini, fim, r["data"]))
    return min(max((d - a).days / max((b - a).days, 1), 0.0), 0.999)


def _dias_ao_fim(r) -> int:
    return (date.fromisoformat(TEMPORADAS[r["season"]][1]) - date.fromisoformat(r["data"])).days


CORTES = {
    "nenhum": lambda r: "-",
    "1ª quinzena x resto": lambda r: "ini" if (date.fromisoformat(r["data"]) - date.fromisoformat(
        TEMPORADAS[r["season"]][0])).days < 14 else "resto",
    "metades (All-Star)": lambda r: "antes" if r["data"] < ALLSTAR[r["season"]] else "depois",
    "prazo de trocas": lambda r: "antes" if r["data"] < PRAZO[r["season"]] else "depois",
    "reta final (3 sem.) x resto": lambda r: "fim" if _dias_ao_fim(r) < 21 else "resto",
    "prazo + reta final": lambda r: ("antes" if r["data"] < PRAZO[r["season"]]
                                     else "fim" if _dias_ao_fim(r) < 21 else "meio"),
    "3 fases (out-dez, jan-fev, mar-abr)": lambda r: r["fase"],
    "4 blocos iguais": lambda r: int(4 * _frac(r)),
    "6 blocos iguais": lambda r: int(6 * _frac(r)),
    "mensal": lambda r: r["data"][5:7],
}
GRUPOS = {
    "sem grupo": lambda r: "-",
    "experiência": lambda r: r.get("exp"),
    "campanha": lambda r: r.get("camp"),
    "experiência × campanha": lambda r: (r.get("exp"), r.get("camp")),
}


def _oos(rs, y, base, chave, minimo=30):
    """% do erro quadratico tirado, no teste (ultima temporada), por um fator
    medio por `chave` ajustado no treino (as outras). y e base: funcoes."""
    ult = max(r["season"] for r in rs)
    agg: dict = {}
    for r in rs:
        if r["season"] == ult:
            continue
        a = agg.setdefault(chave(r), [0.0, 0.0, 0])
        a[0] += y(r)
        a[1] += base(r)
        a[2] += 1
    fat = {k: a / b for k, (a, b, n) in agg.items() if b > 0 and n >= minimo}
    e0 = e1 = 0.0
    for r in rs:
        if r["season"] != ult:
            continue
        e0 += (y(r) - base(r)) ** 2
        e1 += (y(r) - base(r) * fat.get(chave(r), 1.0)) ** 2
    return 100 * (1 - e1 / e0) if e0 else 0.0


def _corte(v, limites, rotulos):
    """Faixa de v pelos limites superiores inclusivos."""
    if v is None:
        return None
    for lim, rot in zip(limites, rotulos):
        if v <= lim:
            return rot
    return rotulos[-1]


def _pk(lims, rots):
    # pick 0 = nao draftado
    return lambda r: None if r.get("pick") is None else ("ND" if r["pick"] == 0
                                                          else _corte(r["pick"], lims, rots))


PERFIS = {
    "exp: calouro x resto": lambda r: _corte(r.get("anos"), [0, 99], ["0", "1+"]),
    "exp: ≤3 x 4+": lambda r: _corte(r.get("anos"), [3, 99], ["≤3", "4+"]),
    "exp: ≤1 / 2-4 / 5+": lambda r: _corte(r.get("anos"), [1, 4, 99], ["≤1", "2-4", "5+"]),
    "exp: 0 / 1-2 / 3-6 / 7+": lambda r: _corte(r.get("anos"), [0, 2, 6, 99], ["0", "1-2", "3-6", "7+"]),
    "exp: 0/1/2/3/4-6/7-10/11+": lambda r: _corte(r.get("anos"), [0, 1, 2, 3, 6, 10, 99],
                                                  ["0", "1", "2", "3", "4-6", "7-10", "11+"]),
    "exp: ano a ano (até 15)": lambda r: None if r.get("anos") is None else min(r["anos"], 15),
    "pick: 1ª rodada x resto": _pk([30, 99], ["1ª", "2ª"]),
    "pick: loteria / 15-30 / 2ª / ND": _pk([14, 30, 99], ["1-14", "15-30", "2ª"]),
    "pick: top5 / 6-14 / 15-30 / 2ª / ND": _pk([5, 14, 30, 99], ["1-5", "6-14", "15-30", "2ª"]),
    "pick: top3 / 4-10 / 11-20 / 21-30 / 2ª / ND": _pk([3, 10, 20, 30, 99],
                                                       ["1-3", "4-10", "11-20", "21-30", "2ª"]),
    "idade: ≤21 / 22-24 / 25-28 / 29-32 / 33+": lambda r: _corte(r.get("idade"), [22, 25, 29, 33, 99],
                                                                ["≤21", "22-24", "25-28", "29-32", "33+"]),
}
PERFIS["exp (0/1-2/3-6/7+) × pick (loteria/15-30/2ª/ND)"] = (
    lambda r: (PERFIS["exp: 0 / 1-2 / 3-6 / 7+"](r), PERFIS["pick: loteria / 15-30 / 2ª / ND"](r)))
PERFIS["exp (≤1/2-4/5+) × idade"] = (
    lambda r: (PERFIS["exp: ≤1 / 2-4 / 5+"](r), PERFIS["idade: ≤21 / 22-24 / 25-28 / 29-32 / 33+"](r)))


def _alvos(rs, fora):
    """Os tres alvos: (linhas, y, base, chave propria do alvo)."""
    jog = [r for r in rs if r["min"] > 0 and r["tier"] != "terrão <10" and r["mu_j"] > 5]
    alvos = {
        "pontos por jogo jogado":
            (jog, lambda r: r["fp"], lambda r: r["mu_j"], lambda r: ()),
        "resto do jogo, início do 4º, com placar":
            ([r for r in rs if r["tier"] != "terrão <10"], lambda r: r[36][2],
             lambda r: r["base"][36][1], lambda r: (r["tier"], faixa(r[36][0]))),
        "resto do jogo, 6:00 do 4º, com placar":
            ([r for r in rs if r["tier"] != "terrão <10"], lambda r: r[42][2],
             lambda r: r["base"][42][1], lambda r: (r["tier"], faixa(r[42][0]))),
    }
    tier = {(r["season"], r["pid"]): r for r in rs}
    ativos = {(r["season"], r["pid"], _semana(r["data"])) for r in rs if r["min"] > 0}
    disp = []
    for r in rs + fora:
        ref = tier.get((r["season"], r["pid"]))
        if not ref or ref["tier"] == "terrão <10":
            continue
        if (r["season"], r["pid"], _semana(r["data"])) not in ativos:
            continue
        disp.append({**ref, "data": r["data"], "fase": r.get("fase"), "camp": r.get("camp"),
                     "jogou": float(r.get("min", 0) > 0)})
    media = sum(d["jogou"] for d in disp) / max(len(disp), 1)
    alvos["jogar nas semanas ativas"] = (disp, lambda r: r["jogou"], lambda r: media, lambda r: ())
    return alvos


def tabela_perfis(rs, fora):
    """Experiencia, pick e idade em varios recortes, sozinhos e com a fase."""
    alvos = _alvos(rs, fora)
    for cn in ("nenhum", "3 fases (out-dez, jan-fev, mar-abr)", "prazo + reta final"):
        cf = CORTES[cn]
        print(f"\n### Experiência / pick / idade — recorte da temporada: {cn}")
        print("% do erro tirado na temporada de teste, além do que o alvo já tem "
              "(minutagem e placar nos alvos do jogo)")
        print("| perfil | " + " | ".join(alvos) + " |\n|---|" + "---|" * len(alvos))
        refs = {k: _oos(d, y, b, lambda r, ex=ex: (ex(r), cf(r))) for k, (d, y, b, ex) in alvos.items()}
        linhas = []
        for pn, pf in PERFIS.items():
            ganhos = [_oos(d, y, b, lambda r, ex=ex, pf=pf: (ex(r), pf(r), cf(r))) - refs[k]
                      for k, (d, y, b, ex) in alvos.items()]
            linhas.append((sum(ganhos), pn, ganhos))
        for _, pn, ganhos in sorted(linhas, reverse=True):
            print(f"| {pn} | " + " | ".join(f"{g:+.2f}" for g in ganhos) + " |")


def tabela_cortes(rs, fora):
    jog = [r for r in rs if r["min"] > 0 and r["tier"] != "terrão <10" and r["mu_j"] > 5]
    alvos = {
        "pontos por jogo jogado (vs média da temporada dele)":
            (jog, lambda r: r["fp"], lambda r: r["mu_j"], lambda r: ()),
        "resto do jogo no início do 4º (vs normal do trecho, com placar)":
            ([r for r in rs if r["tier"] != "terrão <10"], lambda r: r[36][2],
             lambda r: r["base"][36][1], lambda r: (r["tier"], faixa(r[36][0]))),
    }
    # disponibilidade: jogou (1) ou nao (0) nos jogos das semanas ativas
    tier = {(r["season"], r["pid"]): r for r in rs}
    ativos = {(r["season"], r["pid"], _semana(r["data"])) for r in rs if r["min"] > 0}
    disp = []
    for r in rs + fora:
        ref = tier.get((r["season"], r["pid"]))
        if not ref or ref["tier"] == "terrão <10":
            continue
        if (r["season"], r["pid"], _semana(r["data"])) not in ativos:
            continue
        disp.append({**ref, "data": r["data"], "fase": r.get("fase"), "camp": r.get("camp"),
                     "jogou": float(r.get("min", 0) > 0)})
    media = sum(d["jogou"] for d in disp) / max(len(disp), 1)
    alvos["jogar o jogo nas semanas ativas (vs taxa geral)"] = (
        disp, lambda r: r["jogou"], lambda r: media, lambda r: ())
    for nome, (dados, y, base, extra) in alvos.items():
        print(f"\n### Recortes da temporada — {nome}")
        print("% do erro tirado na temporada de teste (ajustado nas outras). "
              "Cada célula: recorte × grupo; a referência é sem recorte e sem grupo")
        print("| recorte | " + " | ".join(GRUPOS) + " |\n|---|" + "---|" * len(GRUPOS))
        ref = _oos(dados, y, base, lambda r: extra(r))
        for cn, cf in CORTES.items():
            cel = [f"{_oos(dados, y, base, lambda r, cf=cf, gf=gf: (extra(r), gf(r), cf(r))) - ref:+.2f}"
                   for gf in GRUPOS.values()]
            print(f"| {cn} | " + " | ".join(cel) + " |")
        if ref:
            print(f"\n(o grupo do alvo sozinho já tira {ref:.1f}% do erro)")


# ==========================================================================
# o que o bot le: ecart.json
# ==========================================================================
SAIDA = Path(__file__).resolve().parent / "ecart.json"
TIERS_BOT = {"estrela 32+": 32, "titular 26-32": 26, "rotação 18-26": 18, "banco 10-18": 10,
             "terrão <10": 0}


def exportar(rs, gs) -> None:
    """Tabelas medidas pra projecao ao vivo do bot.

    parte[tier][min que faltam]: fracao dos pontos do jogo que ainda vem
      (o normal de cada um no trecho, sobre a media do jogo);
    fator[tier][min que faltam][faixa]: o placar no momento x o normal;
    relogio[faixa][min que faltam]: [mediana, p90] dos minutos reais ate o
      fim, sem o intervalo (que vem a parte).
    """
    out = {"tiers": TIERS_BOT, "faixas": [f"{lo}-{hi}" if hi < 99 else f"{lo}+" for lo, hi in FAIXAS],
           "parte": {}, "fator": {}, "relogio": {}, "n": len(rs)}
    for t in TIERS_BOT:
        v = [r for r in rs if r["tier"] == t and r["mu"] > 0]
        out["parte"][t] = {str(48 - c): round(sum(r["base"][c][1] for r in v) / sum(r["mu"] for r in v), 4)
                           for c, _ in MOMENTOS}
        out["parte"][t]["48"] = 1.0
        out["parte"][t]["0"] = 0.0
        out["fator"][t] = {}
        for c, _ in MOMENTOS:
            cel = {}
            for lo, hi in FAIXAS:
                f = f"{lo}-{hi}" if hi < 99 else f"{lo}+"
                a = b = 0.0
                n = 0
                for r in v:
                    if lo <= abs(r[c][0]) <= hi:
                        a += r[c][2]
                        b += r["base"][c][1]
                        n += 1
                # celula pequena encolhe pra 1 (o normal)
                cel[f] = round((a + 50 * (b / max(n, 1))) / (b + 50 * (b / max(n, 1))), 3) if n and b else 1.0
            out["fator"][t][str(48 - c)] = cel
    # experiencia: o calouro titular nao senta no blowout, o veterano senta.
    # Grava por faixa de experiencia (encolhida pro fator da faixa de
    # minutos) e liga so se melhorar fora da amostra.
    out["exp"] = {e: [lo, hi] for e, lo, hi in EXP}
    out["fator_exp"] = {}
    ganhos = []
    for c, _ in MOMENTOS:
        if c < 24:
            continue
        sem = _oos(rs, lambda r, c=c: r[c][2], lambda r, c=c: r["base"][c][1],
                   lambda r, c=c: (r["tier"], faixa(r[c][0])))
        com = _oos(rs, lambda r, c=c: r[c][2], lambda r, c=c: r["base"][c][1],
                   lambda r, c=c: (r["tier"], faixa(r[c][0]), r.get("exp")))
        ganhos.append(com - sem)
    out["ganho_exp_oos"] = [round(g, 2) for g in ganhos]
    # no 3o quarto piora, no 4o ajuda (2023-2025: -0,18 -0,05 | +0,22 +0,41
    # +0,30 +0,09): liga so no 4o quarto, onde a substituicao acontece
    q4 = ganhos[-4:]
    out["usar_exp"] = sum(q4) / len(q4) > 0.15
    out["exp_ate_min"] = 12
    print(f"\nexperiência no fator do placar, fora da amostra: {out['ganho_exp_oos']} "
          f"-> {'usa' if out['usar_exp'] else 'não usa'}")
    for t in TIERS_BOT:
        out["fator_exp"][t] = {}
        for e, *_ in EXP:
            v = [r for r in rs if r["tier"] == t and r.get("exp") == e and r["mu"] > 0]
            out["fator_exp"][t][e] = {}
            for c, _ in MOMENTOS:
                cel = {}
                for lo, hi in FAIXAS:
                    f = f"{lo}-{hi}" if hi < 99 else f"{lo}+"
                    a = b = 0.0
                    n = 0
                    for r in v:
                        if lo <= abs(r[c][0]) <= hi:
                            a += r[c][2]
                            b += r["base"][c][1]
                            n += 1
                    pai = out["fator"][t][str(48 - c)][f]
                    k = 50 * (b / max(n, 1)) if n else 0.0
                    cel[f] = round((a + k * pai) / (b + k), 3) if n and b else pai
                out["fator_exp"][t][e][str(48 - c)] = cel
    iv = [g["intervalo"] for g in gs if g["intervalo"]]
    out["intervalo"] = round(_pct(iv, .5), 1)
    for f in [(0, 5), (6, 14), (15, 99)]:
        rot = f"{f[0]}-{f[1]}" if f[1] < 99 else f"{f[0]}+"
        out["relogio"][rot] = {}
        for c, _ in MOMENTOS:
            v = [g["real"][c] for g in gs if g["real"][c] and f[0] <= abs(g["marg"][c]) <= f[1]
                 and not g["ot"]]
            if len(v) < 20:
                continue
            tira = out["intervalo"] if c < 24 else 0.0
            out["relogio"][rot][str(48 - c)] = [round(_pct(v, .5) - tira, 1), round(_pct(v, .9) - tira, 1)]
    SAIDA.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n(gravei {SAIDA.name})")


def main():
    dados = {}
    for s in TEMPORADAS:
        if not any((PBP / str(s)).glob("*.json.gz")):
            continue
        validar(s)
        rows, games, fora = registros(s)
        dados[s] = (com_base(rows), games, fora)
        print(f"\n{s}: {len(games)} jogos, {len(dados[s][0])} jogador-jogo")
    seasons = sorted(dados)
    gs = [g for s in seasons for g in dados[s][1]]
    camp = {k: v for s in seasons for k, v in campanhas(dados[s][1]).items()}
    rs = com_perfil([r for s in seasons for r in dados[s][0]], camp)
    fora = com_perfil([f for s in seasons for f in dados[s][2]], camp)
    print(f"\ntemporadas {seasons}: {len(gs)} jogos, {len(rs)} jogador-jogo")
    tabela_quartos(rs)
    for c, nome in MOMENTOS:
        tabela_razao(rs, c, 1, f"Minutos no resto do jogo / normal — diferença no {nome}")
        tabela_razao(rs, c, 2, f"Pontos de fantasy no resto do jogo / normal — diferença no {nome}")
    for c in (24, 36, 42):
        tabela_lado(rs, c)
    tabela_virada(gs)
    tabela_relogio(gs)
    tabela_projecao(rs)
    tabela_regras([r for r in rs if r["season"] != seasons[-1]],
                  [r for r in rs if r["season"] == seasons[-1]])
    for c in (24, 36, 42):
        tabela_experiencia(rs, c)
    tabela_lado_exp(rs, 36)
    tabela_draft(rs, 36)
    tabela_fase(rs, 36)
    tabela_campanha(rs, 36)
    tabela_disponibilidade(rs, fora)
    tabela_media_fase(rs)
    tabela_superar(rs)
    tabela_cortes(rs, fora)
    tabela_perfis(rs, fora)
    exportar(rs, gs)


if __name__ == "__main__":
    if sys.argv[1:2] == ["baixar"]:
        for s in TEMPORADAS:
            baixar(s)
        baixar_atletas()
    elif sys.argv[1:2] == ["atletas"]:
        baixar_atletas()
    elif sys.argv[1:2] == ["validar"]:
        validar(int(sys.argv[2]) if len(sys.argv) > 2 else 2025, 200)
    else:
        main()
