"""O modelo proprio: quanto cada jogador deve fazer no PROXIMO jogo.

Ranking de NBA quase nao existe fora de draft, e os que existem sao de
categoria (9-cat), nao de pontos. Entao o bot projeta sozinho, a partir do
log jogo a jogo, com a mesma decomposicao que a gente usava na NFL pra
snaps:

    pontos = MINUTOS x EFICIENCIA (pontos de fantasy por minuto)

porque as duas metades se comportam diferente. Minuto e papel: muda de um
dia pro outro quando um titular se machuca, e o ganho PERSISTE ou nao
conforme a causa. Eficiencia e talento: oscila muito de jogo pra jogo e
regride forte pra media. Medir as duas juntas (so "pontos nos ultimos 5")
mistura sinal com ruido.

Cada metade e:   base + beta x (recente - base)

  * base     = media dele na temporada, sem os ultimos 5 jogos;
  * recente  = media dos ultimos 5;
  * beta     = quanto do desvio recente sobrevive. MEDIDO em 2021-2025 pelo
               `backtest_momentum.py`, e separado pela causa do ganho de
               minutos (organico, titular ainda fora, titular de volta).

Por cima disso entra a projecao jogo a jogo do Sleeper, que sabe do
adversario e de quem foi descartado pro jogo. A mistura tambem e medida.

Tudo isso vira, pra cada jogo: media (mu), desvio (sd) e chance de jogar.
E e com esses tres numeros que a decisao de travar e feita (lockin.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from dados import b2b, taxa_carga
from names import posicoes
from previsao_jogo import grupo_pos, indices, prever
from previsao_waiver import MIN_JOGOS, TETO, prever as prever_horizonte

HERE = Path(__file__).resolve().parent

# Valores de partida. O `momentum.json` (gerado pelo backtest) sobrepoe.
PARAMS_PADRAO = {
    "beta_min": {"organico": 0.5, "lesao_segue": 0.7, "lesao_volta": 0.2},
    "beta_eficiencia": 0.15,
    "peso_modelo": 0.5,
    "sd_a": 4.0, "sd_b": 0.25,
    "p_falta": {"jogou_ultimo": 0.06, "faltou_ultimo": 0.6,
                "jogou_ultimo_b2b": 0.08, "faltou_ultimo_b2b": 0.6},
    # quantos jogos de "prior" valem a base, no comeco da temporada
    "k_prior_jogos": 8,
    "k_prior_minutos": 250.0,
}

JANELA = 5
ROTACAO = 20.0
LIMIAR_VAGO = 8.0

# Status do Sleeper na NBA -> chance de jogar. NAO e medido: o Sleeper nao
# guarda historico de status, entao nao ha com o que calibrar. Sao pontos de
# partida conservadores; o que importa e Out = 0 e GTD bem abaixo de 1.
P_STATUS = {
    "Out": 0.0, "IR": 0.0, "Sus": 0.0, "NA": 0.0, "DNR": 0.0, "COV": 0.0,
    "Doubtful": 0.2, "GTD": 0.5, "Questionable": 0.55, "DTD": 0.7,
    "Probable": 0.9,
}
FORA = {k for k, v in P_STATUS.items() if v == 0.0}
# Status da ESPN -> o do Sleeper.
ESPN_STATUS = {"Out": "Out", "Day-To-Day": "DTD", "Questionable": "Questionable",
               "Doubtful": "Doubtful", "Probable": "Probable", "Suspension": "Sus"}
# Primeiro jogo depois do prazo de volta da imprensa: o prazo escorrega.
# Chute declarado, sem historico de prazos pra medir.
P_VOLTA = 0.75

# A NOTICIA do jogo (RotoWire, via ESPN) diz mais que o status: "Questionable"
# que "is expected to play" e "Questionable" que "is unlikely to play" sao
# jogadores diferentes. Ela fala de um jogo especifico ("won't play in
# Tuesday's game"), entao so vale pro jogo daquele dia. As chances abaixo sao
# chute declarado; o bot anota status e noticia antes de cada jogo
# (run_escalacao.registrar_status) pra medir isso na temporada.
NAO_JOGA = ("won't play", "will not play", "ruled out", "is out for", "will be out",
            "won't suit up", "will not suit up", "isn't expected to play",
            "is not expected to play", "not expected to play", "will be rested",
            "will rest", "will miss", "unlikely to play", "is unlikely", "doubtful",
            "has been downgraded to out", "remains out", "will sit out", "won't be available")
JOGA = ("expected to play", "will play", "plans to play", "is available", "will be available",
        "has been cleared", "cleared to play", "upgraded to available", "will start",
        "in the starting lineup", "probable", "is good to go", "will return", "set to return")
DUVIDA = ("game-time decision", "questionable", "uncertain", "doesn't know", "day-to-day")
P_NOTICIA = {"nao joga": 0.05, "joga": 0.9, "duvida": None}
DIAS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def ler_noticia(texto: str | None) -> str | None:
    """'nao joga' | 'joga' | 'duvida' | None (a noticia nao fala do jogo)."""
    t = (texto or "").lower()
    if not t:
        return None
    if any(k in t for k in NAO_JOGA):        # antes: "isn't expected to play"
        return "nao joga"
    if any(k in t for k in JOGA):
        return "joga"
    if any(k in t for k in DUVIDA):
        return "duvida"
    return None


def noticia_do_jogo(les: dict | None, jogo) -> str | None:
    """A leitura da noticia, se ela e sobre ESTE jogo: escrita ate 2 dias
    antes dele e, se cita um dia da semana, citando o dia do jogo."""
    if not les or jogo is None or not les.get("nota"):
        return None
    from datetime import date
    d = date.fromisoformat(jogo.data)
    quando = (les.get("data") or "")[:10]
    if not quando or not ((d.toordinal() - 2) <= date.fromisoformat(quando).toordinal() <= d.toordinal()):
        return None
    t = les["nota"].lower()
    citados = [x for x in DIAS if x in t]
    if citados and DIAS[d.weekday()] not in citados:
        return None
    return ler_noticia(t)


def carregar_params(path: Path | None = None) -> dict:
    p = path or HERE / "momentum.json"
    P = json.loads(json.dumps(PARAMS_PADRAO))
    try:
        rel = json.loads(p.read_text(encoding="utf-8")) or {}
        P.update(rel.get("params") or {})
        P["persistencia"] = rel.get("persistencia") or {}
    except Exception:
        pass
    return P


def _media(xs):
    return sum(xs) / len(xs) if xs else 0.0


def prever_de_log(prior: list, P: dict, causa: str = "organico",
                  prior_min: float | None = None,
                  prior_taxa: float | None = None) -> dict:
    """Media prevista do proximo jogo a partir dos jogos ja jogados.

    `prior_*` entram no comeco da temporada, quando 5 jogos nao sao base de
    nada: valem como `k_prior_jogos` jogos (minutos) e `k_prior_minutos`
    minutos (eficiencia) de evidencia.
    """
    if not prior:
        m, t = prior_min or 0.0, prior_taxa or 0.0
        return {"mu": m * t, "min": m, "taxa": t, "B": m, "R": m, "Br": t, "Rr": t}
    base, rec = (prior[:-JANELA], prior[-JANELA:]) if len(prior) > JANELA else (prior, prior)
    B = _media([j.min for j in base])
    R = _media([j.min for j in rec])
    bm = sum(j.min for j in base)
    Br = sum(j.fp for j in base) / bm if bm else 0.0
    rm = sum(j.min for j in rec)
    Rr = sum(j.fp for j in rec) / rm if rm else Br

    if prior_min is not None:
        k = P["k_prior_jogos"]
        B = (len(base) * B + k * prior_min) / (len(base) + k)
    if prior_taxa is not None:
        k = P["k_prior_minutos"]
        Br = (bm * Br + k * prior_taxa) / (bm + k)
    if len(prior) <= JANELA:
        # Ate 5 jogos, "recente" e "base" sao os mesmos jogos: nao existe
        # tendencia pra medir, so a base ja encolhida pro prior.
        R, Rr = B, Br

    beta = (P.get("beta_min") or {}).get(causa, 0.5)
    m = max(0.0, B + beta * (R - B))
    t = max(0.0, Br + P["beta_eficiencia"] * (Rr - Br))
    return {"mu": m * t, "min": m, "taxa": t, "B": B, "R": R, "Br": Br, "Rr": Rr}


@dataclass
class Previsao:
    mu: float
    sd: float
    p: float                 # chance de jogar
    nota: str = ""           # por que (tendencia, status, projecao)
    detalhe: dict = field(default_factory=dict, repr=False)


class Modelo:
    """Previsao por jogador e por jogo, com o contexto da temporada atual.

    `logs` = log desta temporada; `anterior` = log da passada (prior);
    `proj_temp` = projecao de temporada do Sleeper {pid: {"fp","min"}};
    `proj_jogo` = projecao por jogo {(pid, data): {"fp","min"}}.
    """

    def __init__(self, logs: dict, anterior: dict, proj_temp: dict,
                 proj_jogo: dict, pool: dict, P: dict | None = None,
                 lesoes: dict | None = None, carga: dict | None = None,
                 datas_time: dict | None = None, hoje: str | None = None,
                 proj_passado: dict | None = None):
        self.logs, self.anterior = logs, anterior
        self.proj_temp, self.proj_jogo = proj_temp, proj_jogo
        self.pool = pool
        # `lesoes` = {pid: lesao da ESPN, com `retorno`}; `carga` = contagem
        # de jogos por semana ativa (dados.contagem_carga); `datas_time` = o
        # calendario {time: datas}, pra saber o que e back-to-back no futuro.
        self.lesoes = lesoes or {}
        self._carga = carga or {}
        self.datas_time = datas_time or {}
        self.hoje = hoje
        # projecao jogo a jogo das semanas que JA passaram: e contra ela que
        # se mede quem vem acima do Sleeper e quanto cada defesa cede
        self.proj_passado = proj_passado or {}
        self._idx = None
        self.P = P or carregar_params()
        self._base: dict[str, dict] = {}
        self._horiz: dict[str, float | None] = {}
        self._vagos = None

    # -- causa do ganho de minutos ------------------------------------------
    def _rotacao_fora(self) -> dict[str, float]:
        """{time: minutos/jogo da rotacao que esta FORA agora (status)}."""
        if self._vagos is None:
            self._vagos = {}
            for pid, js in self.logs.items():
                p = self.pool.get(pid) or {}
                if not js or p.get("injury_status") not in FORA:
                    continue
                m = _media([j.min for j in js])
                if len(js) >= 5 and m >= ROTACAO:
                    t = p.get("team") or js[-1].team
                    self._vagos[t] = self._vagos.get(t, 0.0) + m
        return self._vagos

    def _times(self):
        """Por time, uma vez so: datas de jogo e a rotacao (quem tem 5+ jogos
        com 20+ min de media), com as datas em que cada um jogou."""
        if getattr(self, "_por_time", None) is None:
            datas: dict[str, set] = {}
            jogos: dict[tuple[str, str], list] = {}
            for q, qs in self.logs.items():
                for j in qs:
                    datas.setdefault(j.team, set()).add(j.data)
                    jogos.setdefault((j.team, q), []).append(j)
            rot: dict[str, dict] = {}
            for (t, q), qs in jogos.items():
                m = _media([j.min for j in qs])
                if len(qs) >= 5 and m >= ROTACAO:
                    rot.setdefault(t, {})[q] = (m, {j.data for j in qs},
                                                max(j.data for j in qs))
            self._por_time = ({t: sorted(v) for t, v in datas.items()}, rot)
        return self._por_time

    def causa(self, pid: str) -> str:
        """Mesma classe do backtest, mas com o que se sabe AGORA: quem da
        rotacao do time esta fora pelo relatorio de lesao."""
        js = self.logs.get(pid) or []
        if len(js) < JANELA + 3:
            return "organico"
        time = js[-1].team
        rot = {q: v for q, v in self._times()[1].get(time, {}).items() if q != pid}

        def vago(ds):
            ds = list(ds)
            if not ds:
                return 0.0
            return sum(sum(m for (m, dq, ult) in rot.values()
                           if d not in dq and d <= ult) for d in ds) / len(ds)
        # Variacao, nao nivel: o que ficou vago A MAIS na janela recente em
        # relacao a base dele (backtest_momentum.py explica o porque).
        v_base = vago(j.data for j in js[:-JANELA] if j.team == time)
        v_rec = vago(j.data for j in js[-JANELA:]) - v_base
        if v_rec < LIMIAR_VAGO:
            return "organico"
        v_agora = self._rotacao_fora().get(time, 0.0) - v_base
        return "lesao_segue" if v_agora >= 0.6 * v_rec else "lesao_volta"

    # -- previsao ------------------------------------------------------------
    def _priors(self, pid: str) -> tuple[float | None, float | None]:
        pt = self.proj_temp.get(pid)
        if pt and pt.get("min"):
            return pt["min"], pt["fp"] / pt["min"]
        ant = self.anterior.get(pid) or []
        if len(ant) >= 10:
            mins = sum(j.min for j in ant)
            return mins / len(ant), sum(j.fp for j in ant) / mins
        return None, None

    def base(self, pid: str) -> dict:
        """Previsao generica do proximo jogo (sem olhar adversario)."""
        if pid not in self._base:
            js = self.logs.get(pid) or []
            c = self.causa(pid)
            pm, pt = self._priors(pid)
            # Com 10+ jogos o prior so atrapalha: e o regime do backtest.
            if len(js) >= 10:
                pm = pt = None
            b = prever_de_log(js, self.P, c, pm, pt)
            b["causa"], b["n"] = c, len(js)
            self._base[pid] = b
        return self._base[pid]

    def sd(self, mu: float) -> float:
        return max(2.0, self.P["sd_a"] + self.P["sd_b"] * mu)

    def status(self, pid: str) -> str | None:
        """Status de jogo. Duas fontes: o Sleeper (dump de jogadores, que a
        Sleeper pede pra baixar pouco: 1x por hora aqui) e a ESPN (a cada 15
        min). Fora em qualquer uma = fora; senao vale a ESPN, que e a mais
        fresca na noite do jogo (GTD vira Out meia hora antes do tip-off)."""
        sl = (self.pool.get(pid) or {}).get("injury_status") or None
        es = ESPN_STATUS.get((self.lesoes.get(pid) or {}).get("status") or "") or None
        for st in (sl, es):
            if st in FORA:
                return st
        return es or sl

    def carga(self, pid: str, jogo=None) -> float:
        """Chance de jogar um jogo do time numa semana em que ele esta ativo:
        o controle de carga DELE (dados.taxa_carga), b2b ou nao."""
        eh = None
        if jogo is not None:
            time = (self.pool.get(pid) or {}).get("team")
            eh = b2b(self.datas_time.get(time) or (), jogo.data)
        return taxa_carga(self._carga.get(pid), eh)

    def p_jogar(self, pid: str, jogo=None) -> float:
        les = self.lesoes.get(pid)
        leitura = noticia_do_jogo(les, jogo)
        if leitura and P_NOTICIA.get(leitura) is not None:
            return P_NOTICIA[leitura] * (1.0 if leitura == "nao joga" else self.carga(pid, jogo))
        st = self.status(pid)
        ret = (self.lesoes.get(pid) or {}).get("retorno")
        if st in P_STATUS and ret and jogo is not None:
            # Antes do prazo de volta vale o status; depois, a volta e incerta.
            # Prazo que ja passou com ele ainda fora nao diz nada: vale o status.
            if jogo.data >= ret[:10] and (self.hoje is None or ret[:10] > self.hoje):
                return P_VOLTA * self.carga(pid, jogo)
            return P_STATUS[st]
        if st in P_STATUS:
            return P_STATUS[st]
        js = self.logs.get(pid) or []
        pf = self.P.get("p_falta") or {}
        # faltou o ultimo jogo do time? lesao costuma durar mais de um dia
        if js and jogo is not None and self._faltou_ultimo(pid, js[-1].data, jogo.data):
            return 1.0 - pf.get("faltou_ultimo", 0.6)
        return self.carga(pid, jogo)

    def _faltou_ultimo(self, pid, ultima_data, data_jogo) -> bool:
        time = (self.pool.get(pid) or {}).get("team")
        antes = [d for d in self._times()[0].get(time, []) if d < data_jogo]
        return bool(antes) and antes[-1] != ultima_data

    def indices(self) -> tuple[dict, dict]:
        """(temporada de cada jogador vs Sleeper, quanto cada defesa cede)."""
        if self._idx is None:
            proj = dict(self.proj_passado)
            proj.update(self.proj_jogo)
            self._idx = indices(self.logs, proj,
                                lambda q: posicoes(self.pool.get(q) or {}), self.hoje)
        return self._idx

    def descanso(self, time: str | None, data: str) -> int | None:
        """Dias desde o jogo anterior do time (1 = segunda noite de b2b)."""
        from datetime import date
        antes = [d for d in (self.datas_time.get(time) or ()) if d < data]
        if not antes:
            return None
        return (date.fromisoformat(data) - date.fromisoformat(max(antes))).days

    def horizonte(self, pid: str) -> float | None:
        """Pontos por jogo DO TIME nas proximas ~6 semanas (previsao_waiver),
        ou None fora da faixa medida (poucos jogos no time, titular de
        verdade). E o numero do waiver: o proximo jogo repetido por 6
        semanas superestima quem esta quente."""
        if pid in self._horiz:
            return self._horiz[pid]
        self._horiz[pid] = None
        js = self.logs.get(pid) or []
        time = (self.pool.get(pid) or {}).get("team")
        hoje = str(self.hoje or "")
        datas = [d for d in (self.datas_time.get(time) or ()) if d < hoje] if hoje else []
        no_time = [j for j in js if j.team == time]
        if len(no_time) < MIN_JOGOS or not datas:
            return None
        jogou = {j.data for j in no_time}
        ult10 = datas[-10:]
        pres10 = sum(d in jogou for d in ult10) / len(ult10)
        desde = [d for d in datas if d >= no_time[0].data]
        pres = min(1.0, len(no_time) / max(1, len(desde)))
        fp = [j.fp for j in js]
        S = _media(fp)
        if S * pres10 > TETO:
            return None
        prox = next((d for d in sorted(self.datas_time.get(time) or ()) if d >= hoje), None)
        sp = self.proj_jogo.get((pid, prox)) if prox else None
        ant = self.anterior.get(pid) or []
        v = prever_horizonte(S, _media(fp[-5:]), _media(fp[-10:]), self.base(pid)["mu"],
                             pres10, pres, sp["fp"] if sp else None,
                             _media([j.fp for j in ant]) if len(ant) >= 10 else None)
        self._horiz[pid] = v
        return v

    def jogo_futuro(self, pid: str, jogo) -> Previsao:
        """Previsao de um jogo de semana futura: a do jogo, com a media trocada
        pela do horizonte quando ele esta na faixa do waiver (a chance de
        jogar ja esta dentro dela, entao a media e dividida pela carga)."""
        pr = self.jogo(pid, jogo)
        h = self.horizonte(pid)
        if h is None:
            return pr
        mu = h / max(self.carga(pid, jogo), 0.05)
        return Previsao(mu, self.sd(mu), pr.p, pr.nota, {**pr.detalhe, "horizonte": h})

    def jogo(self, pid: str, jogo) -> Previsao:
        """Previsao pra UM jogo especifico (`agenda.Jogo`): a projecao do
        Sleeper corrigida pelo que foi medido (`previsao_jogo.py`)."""
        b = self.base(pid)
        mu = b["mu"]
        notas = []
        sp = self.proj_jogo.get((pid, jogo.data)) if jogo is not None else None
        if sp is not None:
            time = (self.pool.get(pid) or {}).get("team")
            temp, defesa = self.indices()
            joga = time in jogo.times
            opp = jogo.adversario(time) if joga else None
            g = grupo_pos(posicoes(self.pool.get(pid) or {}))
            modelo = mu if (b["n"] > 0 or mu > 0) else None
            mu = prever(sp["fp"], modelo, temp.get(pid),
                        defesa.get((opp, g)) if opp else None,
                        (time == jogo.home) if joga else None,
                        self.descanso(time, jogo.data))
        p = self.p_jogar(pid, jogo)
        if sp is not None and sp.get("min", 1) <= 0:
            p = min(p, 0.1)
            notas.append("Sleeper nao projeta minutos")
        st = self.status(pid)
        if st:
            ret = (self.lesoes.get(pid) or {}).get("retorno")
            notas.append(f"{st} ate {ret[8:10]}/{ret[5:7]}" if ret and st in FORA else st)
        leitura = noticia_do_jogo(self.lesoes.get(pid), jogo)
        if leitura:
            notas.append(f"noticia: {leitura}")
        return Previsao(mu, self.sd(mu), p, " · ".join(notas),
                        {"base": b, "sleeper": sp})

    def tendencia(self, pid: str) -> str:
        """Uma frase sobre o momento: minutos subindo/caindo e por que."""
        b = self.base(pid)
        if b["n"] < JANELA + 3:
            return ""
        d = b["R"] - b["B"]
        if abs(d) < 3:
            return ""
        beta = (self.P.get("beta_min") or {}).get(b["causa"], 0.5)
        # Salto grande se comporta diferente da media: +6 min organico fica
        # 81%, nao 61% (backtest_momentum). A frase usa o numero da faixa.
        pc = (self.P.get("persistencia") or {}).get(b["causa"]) or {}
        faixa = pc.get("subiu6_fica") if d >= 6 else pc.get("caiu6_fica") if d <= -6 else None
        if faixa is not None and faixa == faixa:
            beta = faixa
        porque = {"organico": "ganho proprio",
                  "lesao_segue": "titular do time segue fora",
                  "lesao_volta": "titular do time voltou"}[b["causa"]]
        seta = "subindo" if d > 0 else "caindo"
        return (f"minutos {seta} {d:+.0f} ({b['B']:.0f}→{b['R']:.0f}); "
                f"{porque}, fica ~{beta:.0%}")
