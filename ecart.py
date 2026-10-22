"""O resto do jogo de quem esta em quadra, medido (backtest_ecart.py -> ecart.json).

Tres tabelas, de 3 temporadas de play-by-play (2023-2025, ~91 mil
jogador-jogo):

  * PARTE: quanto do jogo de cada um ainda falta. Nao e a fracao do relogio:
    a estrela faz 27% dos pontos no 1o quarto e so 23% no 4o (o 4o tem
    garbage time e descanso), o terrao faz 39% nos ultimos 6 minutos;
  * FATOR do placar: no inicio do 4o com 25-29 de diferenca a estrela faz
    0,41x o normal no resto do jogo, com 0-2 faz 1,22x; o banco, o
    contrario (1,93x e 0,70x). Ganhando ou perdendo, igual;
  * RELOGIO de parede: o 2o tempo anda ~3,1 minutos reais por minuto de
    jogo, nao 2,6; os 3 minutos finais de jogo apertado levam ~12 minutos.

Sem o ecart.json, volta pro uniforme (o resto do jogo pela fracao do relogio).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ARQ = Path(__file__).resolve().parent / "ecart.json"


@lru_cache(maxsize=1)
def tabelas() -> dict | None:
    try:
        return json.loads(ARQ.read_text(encoding="utf-8"))
    except Exception:
        return None


def _interp(tab: dict[str, float], falta: float) -> float | None:
    """Interpola linearmente pelos minutos que faltam (chaves em texto)."""
    pts = sorted((float(k), v) for k, v in tab.items() if v is not None)
    if not pts:
        return None
    if falta <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= falta <= x1:
            return y0 + (y1 - y0) * (falta - x0) / (x1 - x0)
    return pts[-1][1]


def faixa_minutos(minutos: float | None, t: dict) -> str:
    """Faixa pelo minuto medio dele (estrela 32+, titular, rotacao...)."""
    if minutos is None:
        return "titular 26-32"
    return max(((k, v) for k, v in t["tiers"].items() if minutos >= v), key=lambda kv: kv[1],
               default=("terrão <10", 0))[0]


def faixa_placar(margem: int, t: dict) -> str:
    d = abs(int(margem))
    for f in t["faixas"]:
        lo, hi = (int(x) for x in f.replace("+", "-999").split("-"))
        if lo <= d <= hi:
            return f
    return t["faixas"][-1]


def faixa_exp(anos: int | None, t: dict) -> str | None:
    if anos is None:
        return None
    return next((e for e, (lo, hi) in t.get("exp", {}).items() if lo <= anos <= hi), None)


def resto(falta_min: float, minutos: float | None = None, margem: int | None = None,
          anos: int | None = None) -> float:
    """Fracao da media do jogo que ele ainda deve fazer. `anos`: temporadas
    de NBA antes desta (0 = calouro): o calouro titular nao senta no
    blowout, o veterano senta."""
    falta = min(max(falta_min, 0.0), 48.0)
    t = tabelas()
    if not t:
        return falta / 48.0
    tier = faixa_minutos(minutos, t)
    parte = _interp(t["parte"][tier], falta)
    if parte is None:
        parte = falta / 48.0
    fator = 1.0
    if margem is not None and falta < 48:
        f = faixa_placar(margem, t)
        e = (faixa_exp(anos, t) if t.get("usar_exp") and falta <= t.get("exp_ate_min", 12)
             else None)
        fonte = t["fator_exp"][tier][e] if e else t["fator"][tier]
        tab = {k: v.get(f) for k, v in fonte.items()}
        tab["48"] = 1.0                  # antes do jogo o placar nao diz nada
        fator = _interp(tab, falta) or 1.0
    return parte * fator


def relogio(falta_min: float, margem: int | None, antes_do_intervalo: bool) -> tuple[float, float]:
    """(mediana, p90) dos minutos REAIS ate o fim do jogo."""
    t = tabelas()
    falta = min(max(falta_min, 0.0), 48.0)
    intervalo = (t or {}).get("intervalo", 15.0) if antes_do_intervalo else 0.0
    if not t:
        return 2.6 * falta + intervalo, 3.2 * falta + intervalo
    d = abs(margem or 0)
    rot = "0-5" if d <= 5 else "6-14" if d <= 14 else "15+"
    tab = {"0": [0.0, 1.0], **(t["relogio"].get(rot) or {})}
    p50 = _interp({k: v[0] for k, v in tab.items()}, falta)
    p90 = _interp({k: v[1] for k, v in tab.items()}, falta)
    if p50 is None:
        return 2.6 * falta + intervalo, 3.2 * falta + intervalo
    # no 1o quarto (acima do maior ponto medido) segue no ritmo do 1o tempo
    maior = max(float(k) for k in tab)
    if falta > maior:
        p50 += 2.6 * (falta - maior)
        p90 += 2.9 * (falta - maior)
    return p50 + intervalo, p90 + intervalo
