"""Testes offline: pontuacao, slots, parada otima, plano, leitura de trava.

    python -m pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agenda import Jogo
from lineup import cabe, elenco_minimo_ok, otimizar, starting_slots
from lockin import (Opcao, Planejador, decidir_travas, e_max, escalar_dia,
                    valor_semanal, valor_sequencia)
from modelo import carregar_params, prever_de_log
from scoring import pontos

REGRAS = {"ast": 1.0, "blk": 2.0, "bonus_pt_40p": 2.0, "bonus_pt_50p": 2.0,
          "dd": 1.0, "ff": -2.0, "pts": 0.5, "reb": 1.0, "stl": 2.0, "td": 2.0,
          "tf": -2.0, "to": -1.0, "tpm": 0.5}
SLOTS = ["PG", "SG", "G", "SF", "PF", "F", "C", "UTIL"]
T0 = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)


def _o(pid, dia, mu, sd=6.0, p=1.0):
    return Opcao(pid, f"2026-10-{26 + dia:02d}", T0 + timedelta(days=dia), mu, sd, p)


# -- pontuacao -------------------------------------------------------------
def test_duplo_duplo_e_bonus_cumulativo():
    # 51 pts, 10 reb: 25,5 + 10 + dd 1 + bonus 40 (2) + bonus 50 (2)
    assert pontos({"pts": 51, "reb": 10}, REGRAS) == pytest.approx(40.5)


def test_triplo_duplo_paga_dd_e_td():
    s = {"pts": 10, "reb": 10, "ast": 10}
    assert pontos(s, REGRAS) == pytest.approx(5 + 10 + 10 + 1 + 2)


def test_projecao_respeita_dd_fracionario():
    assert pontos({"pts": 20, "dd": 0.5}, REGRAS) == pytest.approx(10.5)


# -- slots -------------------------------------------------------------------
def test_elegibilidade_dos_slots():
    assert cabe(["PG"], "G") and cabe(["SG"], "G") and not cabe(["SF"], "G")
    assert cabe(["PF"], "F") and cabe(["C"], "UTIL") and not cabe(["C"], "F")


def test_starting_slots_ignora_banco():
    assert starting_slots(SLOTS + ["BN", "BN", "IR"]) == SLOTS


def test_otimizar_e_exato_com_posicao_cruzada():
    # o PG/SF tem que ir pro SF, senao o SF fica vazio
    pos = {"a": ["PG", "SF"], "b": ["PG"], "c": ["SG"], "d": ["PF"], "e": ["C"]}
    assign, total = otimizar(["PG", "SG", "SF", "PF", "C"], list(pos),
                             lambda p, i: 1.0, lambda p: pos[p])
    assert None not in assign and total == 5


def test_elenco_minimo():
    assert not elenco_minimo_ok([["PG"], ["SG"], ["SF"], ["PF"]], SLOTS)
    ok = [["PG"], ["SG"], ["PG"], ["SF"], ["PF"], ["SF"], ["C"], ["C"]]
    assert elenco_minimo_ok(ok, SLOTS)


# -- parada otima ---------------------------------------------------------
def test_e_max_limites():
    assert e_max(20, 0, 10) == 20
    assert e_max(20, 0, 30) == 30
    assert e_max(20, 6, -100) == pytest.approx(20, abs=1e-6)


def test_mais_jogos_vale_mais_e_variancia_vale_mais():
    assert valor_semanal(20, 6, 1, 4) > valor_semanal(20, 6, 1, 3) > valor_semanal(20, 6, 1, 1)
    assert valor_semanal(20, 9, 1, 3) > valor_semanal(20, 5, 1, 3)
    # um jogo so: e a media
    assert valor_semanal(20, 6, 1, 1) == pytest.approx(20, abs=0.01)


def test_mesmo_dia_conta_um_so():
    um = valor_sequencia([_o("a", 0, 20)])
    dois_mesmo_dia = valor_sequencia([_o("a", 0, 20), _o("b", 0, 15)])
    assert dois_mesmo_dia == pytest.approx(um)


def test_slot_sem_jogo_vale_zero():
    assert valor_sequencia([]) == 0.0


# -- decisao de travar ----------------------------------------------------
def _elenco():
    pos = {"pg": ["PG"], "sg": ["SG"], "g2": ["PG", "SG"], "sf": ["SF"],
           "pf": ["PF"], "f2": ["SF", "PF"], "c": ["C"], "u": ["C", "PF"]}
    fut = {p: [_o(p, 1, 20), _o(p, 3, 20), _o(p, 5, 20)] for p in pos}
    return pos, fut


def test_placar_alto_trava_baixo_passa():
    pos, fut = _elenco()
    alto = decidir_travas(SLOTS, list(range(8)), fut, lambda p: pos[p], [(0, "pg", 40.0)])
    baixo = decidir_travas(SLOTS, list(range(8)), fut, lambda p: pos[p], [(0, "pg", 8.0)])
    assert alto[0].travar and not baixo[0].travar
    assert 15 < alto[0].limiar < 35


def test_sem_jogo_futuro_trava_qualquer_coisa():
    pos, fut = _elenco()
    vazio = {p: [] for p in fut}
    d = decidir_travas(SLOTS, list(range(8)), vazio, lambda p: pos[p], [(0, "pg", 3.0)])
    assert d[0].travar and d[0].limiar == pytest.approx(0)


def test_reserva_que_joga_depois_sobe_o_limiar():
    """O substituto no banco e o 'outro entrar no lugar e fazer mais'."""
    fut = {"pg": [_o("pg", 1, 20), _o("pg", 3, 20)]}
    sem = Planejador(["PG"], fut, lambda p: ["PG"]).limiar([0], 0, "pg")
    fut2 = dict(fut, banco=[_o("banco", 2, 25), _o("banco", 4, 25)])
    com = Planejador(["PG"], fut2, lambda p: ["PG"]).limiar([0], 0, "pg")
    assert com > sem + 2


def test_util_custa_mais_que_c():
    """Travar no UTIL fecha o slot que todo mundo aceita: limiar maior."""
    pos = {"c1": ["C"], "c2": ["C"], "g": ["PG"]}
    fut = {"c1": [_o("c1", 2, 20)], "c2": [_o("c2", 3, 20)], "g": [_o("g", 2, 20)]}
    pl = Planejador(["C", "UTIL"], fut, lambda p: pos[p])
    assert pl.limiar([0, 1], 1, "c1") >= pl.limiar([0, 1], 0, "c1") - 1e-9


def test_escalacao_poe_quem_joga_hoje_nos_slots_abertos():
    pos = {"a": ["PG"], "b": ["PG", "SG"], "c": ["C"]}
    hoje = {"a": _o("a", 0, 20), "c": _o("c", 0, 25)}
    fut = {"a": [_o("a", 2, 20)], "b": [_o("b", 1, 22)], "c": [_o("c", 3, 25)]}
    e = escalar_dia(["PG", "SG", "C"], [0, 1, 2], {}, hoje, fut, lambda p: pos[p],
                    ["b", None, None])
    assert e.lineup[0] == "a" and e.lineup[2] == "c"
    assert all(g > 0 for g in e.ganho.values())


def test_chance_troca():
    """Troca = nao jogar ou nao passar do limiar."""
    from lockin import chance_troca
    o = Opcao("a", "d", datetime(2026, 1, 1, tzinfo=timezone.utc), 20, 8, 0.9)
    assert chance_troca(o, 0) == pytest.approx(0.1, abs=0.01)     # trave o que vier
    assert chance_troca(o, 20) == pytest.approx(1 - 0.9 * 0.5)    # limiar na media
    assert chance_troca(o, 30) > chance_troca(o, 20)


# -- modelo ------------------------------------------------------------------
def _jogos(mins, fp_por_min=1.0):
    return [SimpleNamespace(min=m, fp=m * fp_por_min, data=f"2025-11-{k + 1:02d}", team="X")
            for k, m in enumerate(mins)]


def test_ganho_de_minutos_persiste_parcialmente():
    P = carregar_params()
    js = _jogos([20] * 15 + [30] * 5)
    r = prever_de_log(js, P, "organico")
    assert 20 < r["min"] < 30
    volta = prever_de_log(js, P, "lesao_volta")
    assert volta["min"] <= r["min"] + 1e-9


def test_prior_no_comeco_da_temporada():
    P = carregar_params()
    r = prever_de_log(_jogos([10] * 3), P, "organico", prior_min=30, prior_taxa=1.0)
    assert r["min"] > 20


def test_saude_conta_desde_o_comeco_da_temporada_e_lesao_atual_pesa():
    from dados import Jogo
    from valor import disponibilidade, saude, semanas_temporada
    datas = [f"2025-11-{d:02d}" for d in (3, 10, 17, 24)] + ["2025-12-01", "2025-12-08"]
    liga = {"x": [Jogo(d, "", "", "", 0, 30, {}) for d in datas]}
    semanas = semanas_temporada(liga)
    voltou_tarde = [Jogo(d, "", "", "", 0, 30, {}) for d in datas[3:]]
    assert disponibilidade(voltou_tarde, semanas) == pytest.approx(0.5)
    assert disponibilidade([], semanas) == 0.0          # temporada inteira fora
    assert saude(0.0, None) < saude(1.0, None)
    assert saude(1.0, None) - saude(0.5, None) < 0.1    # historico pesa pouco
    assert saude(1.0, "Out") < saude(0.0, None)         # lesao atual pesa mais
    assert saude(1.0, "Out", fora=0.1) > saude(1.0, "Out")  # prazo curto pesa pouco


def test_controle_de_carga_conta_b2b_so_nas_semanas_ativas():
    from dados import Jogo as J, contagem_carga, taxa_carga
    datas = ["2025-11-03", "2025-11-04", "2025-11-06"]          # 04 e b2b
    L = {"todo": [J(d, "T", "", "", 0, 30, {}) for d in datas],
         "poupa": [J(d, "T", "", "", 0, 30, {}) for d in ("2025-11-03", "2025-11-06")]}
    c = contagem_carga(L)
    assert c["todo"] == [2, 2, 1, 1] and c["poupa"] == [2, 2, 1, 0]
    embiid = [60, 45, 12, 3]
    assert taxa_carga(embiid, True) < taxa_carga(embiid, False) < taxa_carga(None, False)


def test_prazo_de_volta_da_espn():
    from modelo import P_VOLTA, Modelo
    m = Modelo({}, {}, {}, {}, {"1": {"team": "T", "injury_status": "Out"}},
               P={"p_falta": {}}, lesoes={"1": {"retorno": "2026-11-10"}}, hoje="2026-11-01")
    jogo = lambda d: SimpleNamespace(data=d)
    assert m.p_jogar("1", jogo("2026-11-08")) == 0.0
    assert m.p_jogar("1", jogo("2026-11-12")) == pytest.approx(P_VOLTA * m.carga("1", jogo("2026-11-12")))
    m.hoje = "2026-11-15"     # prazo passou e ele segue Out: vale o status
    assert m.p_jogar("1", jogo("2026-11-16")) == 0.0


# -- agenda ------------------------------------------------------------------
def test_status_da_espn_nao_vale_antes_do_tip():
    j = Jogo("1", "2026-10-26", 2, "DEN", "LAL", tip=T0, estado="post")
    assert not j.comecou(T0 - timedelta(hours=1))
    assert not j.acabou(T0 + timedelta(minutes=30))
    assert j.acabou(T0 + timedelta(hours=2))


# -- leitura de trava (semana.py) ------------------------------------------
def _ctx_falso(starters_points, agora):
    import semana
    jogos = {
        "a": [Jogo("1", "2026-10-26", 2, "DEN", "X", tip=T0),
              Jogo("2", "2026-10-28", 2, "DEN", "X", tip=T0 + timedelta(days=2))],
        "b": [Jogo("3", "2026-10-26", 2, "LAL", "X", tip=T0),
              Jogo("4", "2026-10-29", 2, "LAL", "X", tip=T0 + timedelta(days=3))],
    }
    time = {"a": "DEN", "b": "LAL"}
    logs = {"a": [SimpleNamespace(data="2026-10-26", fp=30.0)],
            "b": [SimpleNamespace(data="2026-10-26", fp=12.0)]}
    sl = SimpleNamespace(
        my_roster=lambda: {"roster_id": 1, "players": ["a", "b"], "starters": ["a", "b"]},
        roster_positions=lambda: ["PG", "C", "BN"],
        matchups=lambda w: [{"roster_id": 1, "starters": ["a", "b"],
                             "starters_points": starters_points}])
    ctx = SimpleNamespace(
        agora=agora, week=2, sl=sl, logs=logs, warnings=[],
        posicoes=lambda p: ["PG", "C"], team=lambda p: time[p],
        agenda=SimpleNamespace(jogos_do_time=lambda t, w: jogos["a" if t == "DEN" else "b"]))
    return semana, ctx


def test_placar_de_jogo_encerrado_e_trava(monkeypatch):
    semana, ctx = _ctx_falso([30.0, 0.0], T0 + timedelta(hours=4))
    monkeypatch.setattr(semana, "decisoes", lambda: {})
    est = semana.estado(ctx)
    assert est.travados == {0: ("a", 30.0)} and est.abertos == [1]


def test_provisorio_depois_de_passe_fica_aberto(monkeypatch):
    semana, ctx = _ctx_falso([30.0, 0.0], T0 + timedelta(hours=4))
    monkeypatch.setattr(semana, "decisoes", lambda: {"2:a:2026-10-26": "passar"})
    assert semana.estado(ctx).abertos == [0, 1]


def test_placar_ao_vivo_nao_e_trava(monkeypatch):
    # jogo em andamento: o placar nao bate com jogo ENCERRADO
    semana, ctx = _ctx_falso([0.0, 7.5], T0 + timedelta(hours=1))
    monkeypatch.setattr(semana, "decisoes", lambda: {})
    assert semana.estado(ctx).abertos == [0, 1]


# -- draft -------------------------------------------------------------------
def test_ritmo_do_draft_desconta_a_pausa_da_noite():
    from run_draft import minutos_pausados, ritmo
    ini = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)        # 8h BRT
    agora = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)      # dia seguinte, 9h BRT
    pausa = (60, 660)                                               # 22h-8h BRT
    assert minutos_pausados(pausa, ini, agora) == 600
    d = {"quando": ini, "pausa": pausa}
    # 25h no relogio, 15h de draft de verdade, 300 picks: 3 min por pick
    assert ritmo(d, 300, agora) == pytest.approx(3.0)
    assert ritmo({"quando": ini}, 300, agora) == pytest.approx(5.0)


def test_troca_so_passa_quando_os_dois_lados_aguentam():
    from trocas import buscar, melhores, valor_time
    pos = {"a": ["C"], "b": ["C"], "x": ["PG"], "y": ["PG"], "z": ["C"]}.get
    slots = ["PG", "C"]
    v = {"a": 30, "b": 10, "x": 5, "y": 25, "z": 12}
    elencos = {1: ["a", "b", "x"], 2: ["y", "z"]}
    assert valor_time(slots, elencos[1], v, pos) == 35        # banco vale zero
    achadas, testadas = buscar(slots, elencos, 1, v, pos, 2.0, 1.0)
    assert testadas == 3 * 2 + 3 * 2 + 3 * 1                  # 1x1, 2x1, 1x2
    # x pelo y me da +20, mas o titular deles cai de 37 pra 17: nao passa
    assert not any(t["manda"] == ["x"] and t["recebe"] == ["y"] for t in achadas)
    assert all(t["deles"] >= -1.0 and t["ganho"] >= 2.0 for t in achadas)
    tops = melhores([{"rid": 2, "ganho": 5, "deles": 0}, {"rid": 2, "ganho": 9, "deles": 0},
                     {"rid": 3, "ganho": 4, "deles": 1}], 3)
    assert [(t["rid"], t["ganho"]) for t in tops] == [(2, 9), (3, 4)]  # uma por time
    # o "nunca trocar" fica no titular mas nao sai
    a2, _ = buscar(slots, elencos, 1, v, pos, float("-inf"), float("inf"), frozenset({"a"}))
    assert a2 and all("a" not in t["manda"] for t in a2)


def test_horario_guardado_vale_quando_a_espn_cai(monkeypatch, tmp_path):
    import json
    import requests
    import clients
    monkeypatch.setattr(clients, "CACHE", tmp_path)
    velho = [{"tip": "2026-10-22T02:00Z", "home": "LAL", "away": "GS",
              "state": "in", "completed": False}]
    (tmp_path / "espn_2026-10-21.json").write_text(json.dumps(velho), encoding="utf-8")

    def cai(url, *a, **k):
        raise requests.ConnectionError("fora do ar")
    monkeypatch.setattr(clients, "_get_json", cai)
    out = clients.espn_dia("2026-10-21", 0.0)                  # ttl 0: tenta baixar
    assert out[0]["tip"] == "2026-10-22T02:00Z"
    assert out[0]["state"] == ""                               # status velho nao vale
    with pytest.raises(requests.RequestException):
        clients.espn_dia("2026-10-23", 0.0)                    # nada guardado: erro


def test_elenco_cheio_dispensa_o_pior_do_banco():
    from trocas import cortar_excesso
    pos = {"a": ["PG"], "b": ["C"], "c": ["C"], "d": ["C"], "n": ["PG"]}.get
    v = {"a": 30, "b": 20, "c": 8, "d": 3, "n": 1}
    el, sai = cortar_excesso(["a", "b", "c", "d", "n"], 4, set(), v, ["PG", "C"], pos,
                             frozenset({"n"}))
    assert sai == "d" and "n" in el                             # quem chegou fica
    el, sai = cortar_excesso(["a", "b", "c", "d"], 4, set(), v, ["PG", "C"], pos)
    assert sai is None                                          # cabe: ninguem sai
    el, sai = cortar_excesso(["a", "b", "c", "d", "n"], 4, {"d"}, v, ["PG", "C"], pos)
    assert sai is None                                          # IR nao ocupa vaga


def test_projecao_corrigida_por_anos_de_liga():
    from valor import projecao_ajustada
    J = SimpleNamespace
    ant = [J(data="2025-12-01", fp=30.0)] * 25
    assert projecao_ajustada(12.0, 0, []) == pytest.approx(12 * 1.132)     # novato de baixo: sobe
    assert projecao_ajustada(20.0, 0, []) == 20.0                          # escolha alta: sem vies
    assert projecao_ajustada(20.0, 1, ant) == pytest.approx(20 * 0.937)    # 2o ano bom: salto menor
    assert projecao_ajustada(15.0, 1, ant) == 15.0
    assert projecao_ajustada(20.0, 5, ant) == pytest.approx(0.65 * 20 * 0.981 + 0.35 * 30)
    assert projecao_ajustada(20.0, 5, ant[:10]) == pytest.approx(20 * 0.981)  # poucos jogos
    assert projecao_ajustada(20.0, 5, ant[:10], time_novo=True) == pytest.approx(20 * 0.981 * 0.955)
    assert projecao_ajustada(20.0, 5, ant[:10], pivo=True) == pytest.approx(20 * 0.981 * 0.955)
    assert projecao_ajustada(26.0, 5, ant[:10], pivo=True) == pytest.approx(26 * 0.981)
    assert projecao_ajustada(20.0, 5, ant[:10], tend=-6) == pytest.approx(20 * 0.981 * 0.935)
    assert projecao_ajustada(20.0, 5, ant[:10], tend=5) == pytest.approx(20 * 0.981 * 1.034)


def test_vor_conta_que_o_fiasco_se_corta():
    from valor import vor_com_corte
    assert vor_com_corte(40.0, 20.0, 5) == pytest.approx(20.0, abs=1e-3)   # craque: igual
    perto = vor_com_corte(21.0, 20.0, 5)
    assert perto > 1.0                                                     # opcao de estourar
    assert vor_com_corte(21.0, 20.0, 1) > perto                            # 2o ano oscila mais
    assert vor_com_corte(21.0, 20.0, 5, mu=12.0) > perto                    # projecao baixa oscila mais
    assert vor_com_corte(15.0, 20.0, 5) >= 0.0                             # abaixo: ~zero, nunca negativo


def test_saude_recalibrada_e_oscilacao_propria():
    from valor import saude, sd_proprio
    assert saude(1.0, None) == pytest.approx(0.874)
    assert saude(0.5, None) == pytest.approx(0.8345)
    assert saude(0.0, None) == pytest.approx(0.70)                  # ano inteiro fora
    J = SimpleNamespace
    calmo = [J(data="2025-12-01", fp=20.0 + (1 if k % 2 else -1)) for k in range(30)]
    sd_de = lambda m: 6.0
    assert sd_proprio(6.0, calmo, sd_de) < 6.0                      # quem oscila pouco, oscila menos
    assert sd_proprio(6.0, calmo[:10], sd_de) == 6.0                # poucos jogos: so a media


def test_previsao_do_fim_do_draft_pula_a_pausa_e_troca_o_tempo():
    from run_draft import avancar, em_pausa, previsao_fim
    pausa = (60, 660)                                   # 01:00-11:00 UTC
    t0 = datetime(2026, 10, 14, 23, 0, tzinfo=timezone.utc)
    assert em_pausa(pausa, datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc))
    assert not em_pausa(pausa, t0)
    # 3h de relogio ativo a partir das 23h: 2h ate 01h, pausa, 1h depois das 11h
    assert avancar(t0, 180, pausa) == datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    d = {"teams": 2, "rounds": 3, "pausa": None, "ultimo_pick": t0, "quando": t0}
    # 2 feitos: picks 3 e 4 (rodada 2) a 60, picks 5 e 6 (rodada 3) a 30 = 180 min
    fim = previsao_fim(d, 2, t0, 60, 30, rodada_troca=3)
    assert fim == t0 + timedelta(minutes=180)
    # o pick da vez ja correu 20 min: sobram 40 dele
    assert previsao_fim(d, 2, t0 + timedelta(minutes=20), 60, 30, 3) == fim


def test_ritmo_por_rodada_divide_o_tempo_entre_anotacoes():
    from run_draft import ritmo_por_rodada
    t0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    obs = {0: t0, 2: t0 + timedelta(minutes=20), 4: t0 + timedelta(minutes=80)}
    r = ritmo_por_rodada(obs, 2, None)               # 2 times: rodada = 2 picks
    assert r == {1: pytest.approx(10.0), 2: pytest.approx(30.0)}
    # anotacao perdida no meio: o tempo se divide igual
    r = ritmo_por_rodada({0: t0, 4: t0 + timedelta(minutes=80)}, 2, None)
    assert r == {1: pytest.approx(20.0), 2: pytest.approx(20.0)}


def test_status_fora_em_qualquer_fonte_e_fora():
    from modelo import Modelo
    pool = {"1": {"injury_status": "Questionable"}, "2": {"injury_status": "Out"}, "3": {}}
    les = {"1": {"status": "Out"}, "2": {"status": "Day-To-Day"}, "3": {"status": "Day-To-Day"}}
    m = Modelo({}, {}, {}, {}, pool, P={"p_falta": {}}, lesoes=les)
    assert m.status("1") == "Out"           # a ESPN, mais fresca, ja diz Out
    assert m.status("2") == "Out"           # o Sleeper diz Out: vale
    assert m.status("3") == "DTD"


def test_noticia_le_o_jogo_certo():
    from modelo import ler_noticia, noticia_do_jogo
    assert ler_noticia("Sengun (rest) isn't expected to play in Sunday's game") == "nao joga"
    assert ler_noticia("Brown (ankle) is expected to play Tuesday") == "joga"
    assert ler_noticia("Knueppel is a game-time decision for Friday") == "duvida"
    assert ler_noticia("Jones remains limited to non-contact work") is None
    J = SimpleNamespace
    terca = J(data="2026-10-27")                                      # terca-feira
    les = {"nota": "Allen won't play in Tuesday's game vs DAL", "data": "2026-10-27T18:00Z"}
    assert noticia_do_jogo(les, terca) == "nao joga"
    assert noticia_do_jogo(les, J(data="2026-10-29")) is None         # outro dia da semana
    velha = dict(les, data="2026-10-20T18:00Z")
    assert noticia_do_jogo(velha, terca) is None                      # noticia de outra semana


def test_horizonte_do_waiver_puxa_o_quente_pra_media():
    from modelo import Modelo
    # 10 jogos de 10 pts e os ultimos 5 de 22: o proximo jogo sobe bem mais
    # que as proximas 6 semanas (o salto recente some com o tempo)
    js = [SimpleNamespace(min=m, fp=f, data=f"2025-11-{k + 1:02d}", team="X")
          for k, (m, f) in enumerate([(20, 10)] * 10 + [(30, 22)] * 5)]
    datas = [f"2025-11-{k + 1:02d}" for k in range(25)]
    m = Modelo({"1": js}, {}, {}, {}, {"1": {"team": "X"}}, P=carregar_params(),
               datas_time={"X": datas}, hoje="2025-11-16")
    h = m.horizonte("1")
    assert h is not None and 10 < h < m.base("1")["mu"]
    # poucos jogos no time ou titular de verdade: fora da faixa medida
    curto = Modelo({"1": js[:4]}, {}, {}, {}, {"1": {"team": "X"}}, P=carregar_params(),
                   datas_time={"X": datas}, hoje="2025-11-05")
    assert curto.horizonte("1") is None
    estrela = [SimpleNamespace(min=36, fp=45, data=j.data, team="X") for j in js]
    m2 = Modelo({"1": estrela}, {}, {}, {}, {"1": {"team": "X"}}, P=carregar_params(),
                datas_time={"X": datas}, hoje="2025-11-16")
    assert m2.horizonte("1") is None


def test_minha_vez_com_reversao_na_terceira_rodada():
    from run_draft import minha_vez
    ctx = SimpleNamespace(sl=SimpleNamespace(user_id="u"))
    d = {"ordem": {"u": 10}, "teams": 12, "rounds": 12}
    assert minha_vez(ctx, d, 0)["picks"][:4] == [10, 15, 34, 39]          # snake puro
    # 3RR: a 3 repete o sentido da 2 (os picks de verdade da liga de 2026)
    v = minha_vez(ctx, dict(d, reversao=3), 93)
    assert v["picks"][:9] == [10, 15, 27, 46, 51, 70, 75, 94, 99]
    assert v["proximo"] == 94 and v["faltam"] == 0
