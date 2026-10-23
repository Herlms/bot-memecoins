#!/usr/bin/env python3
"""
Bot de PAPER TRADING de memecoins na Solana — VERSÃO 2 — SÓ SIMULAÇÃO.
Não toca em dinheiro real nem em carteiras.

O que mudou em relação à v1 (que perdeu ~25%):
  - Já não persegue subidas: compra RECUOS em tokens que estão em tendência de alta.
  - Só tokens com mais de 24h de vida, liquidez alta e liquidez proporcional ao valor
    de mercado (menos rugs e menos quedas de 80% entre verificações).
  - Descobre tokens pelos "trending" da GeckoTerminal (atividade real), não só pelos
    destaques pagos da DexScreener.
  - Bloqueia clones (mesmo nome, endereço diferente) e repetições pelo nome.
  - Filtro de mercado: não compra se a própria SOL estiver a cair.
  - Stop mais curto, stop que sobe para +4% depois de +12% (cobre os custos),
    e deixa os ganhadores correr mais (trailing).
  - Pausa de 6h depois de 3 perdas seguidas.
  - Relatório com ganho médio, perda média e profit factor.

Uso:
  python3 bot.py ciclo       -> um ciclo e sai (GitHub Actions)
  python3 bot.py             -> corre em ciclo contínuo (num servidor)
  python3 bot.py relatorio   -> mostra os resultados
  python3 bot.py reset       -> apaga a simulação v2 e recomeça

Só precisa de Python 3.8+ (sem bibliotecas extra).
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

VERSAO = 2

# ======================= CONFIGURAÇÃO =======================
CONFIG = {
    # Carteira virtual
    "saldo_inicial_usd": 1000.0,
    "tamanho_posicao_pct": 5.0,
    "max_posicoes": 4,
    "taxa_pct": 0.5,
    "slippage_base_pct": 1.0,

    # Qualidade do token
    "liquidez_min_usd": 100_000,
    "racio_liquidez_fdv_min": 0.04,   # liquidez >= 4% do valor de mercado
    "volume_24h_min_usd": 300_000,
    "txns_24h_min": 1500,
    "idade_min_horas": 24,            # já sobreviveu ao 1.º dia
    "idade_max_horas": 24 * 30,
    "fdv_min_usd": 1_000_000,
    "fdv_max_usd": 60_000_000,

    # Sinal de entrada: tendência de alta + recuo + a estabilizar
    "subida_24h_min_pct": 5.0,
    "subida_6h_min_pct": 0.0,
    "recuo_1h_min_pct": -12.0,        # caiu na última hora entre -12%...
    "recuo_1h_max_pct": -2.0,         # ...e -2%
    "m5_min_pct": 0.0,                # e nos últimos 5 min já parou de cair
    "m5_max_pct": 4.0,
    "racio_compras_vendas_1h_min": 1.0,

    # Filtro de mercado (SOL)
    "sol_1h_min_pct": -1.5,
    "sol_24h_min_pct": -6.0,

    # Saídas
    "stop_loss_pct": -12.0,
    "protecao_ativa_pct": 12.0,       # depois de +12%...
    "protecao_stop_pct": 4.0,         # ...o stop sobe para +4% (cobre os custos)
    "trailing_ativa_pct": 20.0,
    "trailing_queda_pct": 12.0,
    "take_profit_pct": 60.0,
    "estagnado_horas": 8,             # ao fim de 8h...
    "estagnado_min_pct": 3.0,         # ...se não ganhou 3%, sai
    "tempo_max_horas": 24,
    "liquidez_alerta_pct": 40.0,      # liquidez caiu 40% -> sai já
    "liquidez_rug_pct": 70.0,         # caiu 70% -> rug, venda péssima

    # Segurança
    "perda_diaria_max_pct": 6.0,
    "perdas_seguidas_max": 3,
    "pausa_horas": 6,
    "cooldown_horas": 24,

    # Tempos (modo contínuo)
    "intervalo_posicoes_s": 60,
    "intervalo_procura_s": 300,
}
# ============================================================

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
SOL = "So11111111111111111111111111111111111111112"
EXCLUIR = {
    SOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

PASTA = os.path.dirname(os.path.abspath(__file__))
F_ESTADO = os.path.join(PASTA, "estado.json")
F_TRADES = os.path.join(PASTA, "trades.csv")
F_LOG = os.path.join(PASTA, "bot.log")
F_RELATORIO = os.path.join(PASTA, "RELATORIO.md")
F_PARAR = os.path.join(PASTA, "PARAR")


def log(msg):
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linha, flush=True)
    with open(F_LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def obter(url):
    for _ in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "paper-bot/2.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log("Limite de pedidos atingido, a esperar 20s...")
                time.sleep(20)
            else:
                log(f"Erro HTTP {e.code} em {url[:80]}")
                time.sleep(3)
        except Exception as e:
            log(f"Erro de rede: {e}")
            time.sleep(3)
    return None


def num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def liquidez(par):
    return num((par.get("liquidity") or {}).get("usd"))


def simbolo(par):
    return ((par.get("baseToken") or {}).get("symbol") or "?").strip()


# ---------------------- Dados de mercado ----------------------
def melhores_pares(enderecos):
    pares, ok = {}, set()
    for i in range(0, len(enderecos), 30):
        lote = enderecos[i:i + 30]
        dados = obter(f"{DEX}/tokens/v1/solana/{','.join(lote)}")
        if not isinstance(dados, list):
            continue
        ok.update(lote)
        for par in dados:
            if par.get("chainId") != "solana":
                continue
            end = (par.get("baseToken") or {}).get("address")
            if end not in lote:
                continue
            if end not in pares or liquidez(par) > liquidez(pares[end]):
                pares[end] = par
    return pares, ok


def candidatos():
    ends = []

    def junta(a):
        if a and a not in EXCLUIR and a not in ends:
            ends.append(a)

    # GeckoTerminal: pools em tendência e com mais volume (atividade real)
    for url in (f"{GECKO}/networks/solana/trending_pools?page=1",
                f"{GECKO}/networks/solana/trending_pools?page=2",
                f"{GECKO}/networks/solana/pools?sort=h24_volume_usd_desc&page=1"):
        dados = obter(url)
        for p in (dados or {}).get("data", []) if isinstance(dados, dict) else []:
            tid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if tid.startswith("solana_"):
                junta(tid[len("solana_"):])
    # DexScreener: perfis e mais promovidos (só como fonte extra)
    for ep in ("/token-profiles/latest/v1", "/token-boosts/top/v1"):
        dados = obter(DEX + ep)
        if isinstance(dados, list):
            for t in dados:
                if t.get("chainId") == "solana":
                    junta(t.get("tokenAddress"))
    return ends


def mercado_ok():
    """Não compra se a SOL estiver a cair (arrasta as memecoins todas)."""
    pares, _ = melhores_pares([SOL])
    par = pares.get(SOL)
    if not par:
        return False, "sem dados da SOL"
    v = par.get("priceChange") or {}
    h1, h24 = num(v.get("h1")), num(v.get("h24"))
    if h1 < CONFIG["sol_1h_min_pct"] or h24 < CONFIG["sol_24h_min_pct"]:
        return False, f"SOL a cair (1h {h1:+.1f}%, 24h {h24:+.1f}%)"
    return True, f"SOL ok (1h {h1:+.1f}%, 24h {h24:+.1f}%)"


def passa_filtros(par):
    c = CONFIG
    liq = liquidez(par)
    fdv = num(par.get("fdv") or par.get("marketCap"))
    vol = par.get("volume") or {}
    var = par.get("priceChange") or {}
    tx = par.get("txns") or {}
    tx24 = tx.get("h24") or {}
    tx1 = tx.get("h1") or {}
    criado = num(par.get("pairCreatedAt"))
    idade_h = (time.time() * 1000 - criado) / 3_600_000 if criado else -1
    m5, h1, h6, h24 = (num(var.get(k)) for k in ("m5", "h1", "h6", "h24"))
    compras1, vendas1 = num(tx1.get("buys")), num(tx1.get("sells"))

    testes = [
        (num(par.get("priceUsd")) > 0, "sem preço"),
        (liq >= c["liquidez_min_usd"], "liquidez baixa"),
        (c["fdv_min_usd"] <= fdv <= c["fdv_max_usd"], "FDV fora"),
        (fdv > 0 and liq / fdv >= c["racio_liquidez_fdv_min"], "liquidez fina p/ o FDV"),
        (num(vol.get("h24")) >= c["volume_24h_min_usd"], "volume baixo"),
        (num(tx24.get("buys")) + num(tx24.get("sells")) >= c["txns_24h_min"], "poucas transações"),
        (c["idade_min_horas"] <= idade_h <= c["idade_max_horas"], "idade fora"),
        (h24 >= c["subida_24h_min_pct"] and h6 >= c["subida_6h_min_pct"], "sem tendência"),
        (c["recuo_1h_min_pct"] <= h1 <= c["recuo_1h_max_pct"], "sem recuo"),
        (c["m5_min_pct"] <= m5 <= c["m5_max_pct"], "ainda a cair / já disparou"),
        (compras1 >= vendas1 * c["racio_compras_vendas_1h_min"], "pressão vendedora"),
    ]
    for ok, motivo in testes:
        if not ok:
            return False, motivo
    return True, "ok"


# ---------------------- Estado ----------------------
def estado_novo():
    return {
        "versao": VERSAO,
        "caixa": CONFIG["saldo_inicial_usd"],
        "posicoes": {},
        "fechados": [],
        "ultimo_trade": {},
        "ultimo_simbolo": {},
        "dia": "",
        "valor_inicio_dia": CONFIG["saldo_inicial_usd"],
        "pnl_dia": 0.0,
        "perdas_seguidas": 0,
        "pausa_ate": 0,
        "inicio": datetime.now().isoformat(timespec="seconds"),
    }


def arquivar_v1():
    """Guarda os ficheiros da versão anterior com sufixo _v1 e começa do zero."""
    for f in (F_ESTADO, F_TRADES, F_RELATORIO, F_LOG):
        if os.path.exists(f):
            base, ext = os.path.splitext(f)
            destino = f"{base}_v1{ext}"
            if not os.path.exists(destino):
                os.replace(f, destino)
            else:
                os.remove(f)


def carregar():
    if os.path.exists(F_ESTADO):
        with open(F_ESTADO, encoding="utf-8") as f:
            e = json.load(f)
        if e.get("versao") == VERSAO:
            for k, v in estado_novo().items():
                e.setdefault(k, v)
            return e
        arquivar_v1()
        log("Simulação da v1 arquivada (ficheiros *_v1). A v2 começa com $1000.")
    return estado_novo()


def guardar(estado):
    tmp = F_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)
    os.replace(tmp, F_ESTADO)


def valor_total(e):
    return e["caixa"] + sum(p["qtd"] * p["preco_atual"] for p in e["posicoes"].values())


def novo_dia(e):
    hoje = datetime.now().strftime("%Y-%m-%d")
    if e["dia"] != hoje:
        e["dia"] = hoje
        e["valor_inicio_dia"] = valor_total(e)
        e["pnl_dia"] = 0.0
        log(f"Novo dia. Carteira: ${e['valor_inicio_dia']:.2f}")


def bloqueio(e):
    if e["pnl_dia"] <= -CONFIG["perda_diaria_max_pct"] / 100 * e["valor_inicio_dia"]:
        return "perda diária máxima atingida"
    if time.time() < e["pausa_ate"]:
        falta = (e["pausa_ate"] - time.time()) / 3600
        return f"pausa após perdas seguidas ({falta:.1f}h restantes)"
    return None


def slippage(valor, liq):
    return min(CONFIG["slippage_base_pct"] / 100 + (valor / liq if liq > 0 else 0.5), 0.5)


# ---------------------- Operações simuladas ----------------------
def comprar(e, end, par):
    preco, liq = num(par["priceUsd"]), liquidez(par)
    valor = min(valor_total(e) * CONFIG["tamanho_posicao_pct"] / 100, e["caixa"])
    if valor < 1:
        return
    pago = preco * (1 + slippage(valor, liq))
    qtd = valor * (1 - CONFIG["taxa_pct"] / 100) / pago
    sim = simbolo(par)
    e["caixa"] -= valor
    e["posicoes"][end] = {
        "simbolo": sim, "url": par.get("url", ""), "qtd": qtd, "custo": valor,
        "preco_entrada": preco, "preco_atual": preco, "preco_max": preco,
        "stop": CONFIG["stop_loss_pct"], "liq_entrada": liq,
        "entrada_ts": time.time(), "falhas": 0,
    }
    e["ultimo_trade"][end] = time.time()
    e["ultimo_simbolo"][sim.lower()] = time.time()
    log(f"COMPRA {sim} | ${valor:.2f} a ${preco:.8g}")


def vender(e, end, preco, motivo, liq=0.0, fator=None):
    pos = e["posicoes"].pop(end)
    preco_ef = preco * fator if fator is not None else preco * (1 - slippage(pos["qtd"] * preco, liq))
    recebido = pos["qtd"] * preco_ef * (1 - CONFIG["taxa_pct"] / 100)
    pnl = recebido - pos["custo"]
    e["caixa"] += recebido
    e["pnl_dia"] += pnl
    if pnl < 0:
        e["perdas_seguidas"] += 1
        if e["perdas_seguidas"] >= CONFIG["perdas_seguidas_max"]:
            e["pausa_ate"] = time.time() + CONFIG["pausa_horas"] * 3600
            e["perdas_seguidas"] = 0
            log(f"{CONFIG['perdas_seguidas_max']} perdas seguidas — pausa de {CONFIG['pausa_horas']}h.")
    else:
        e["perdas_seguidas"] = 0
    t = {
        "data_saida": datetime.now().isoformat(timespec="seconds"),
        "simbolo": pos["simbolo"], "token": end,
        "custo_usd": round(pos["custo"], 2), "recebido_usd": round(recebido, 2),
        "pnl_usd": round(pnl, 2), "pnl_pct": round(pnl / pos["custo"] * 100, 2),
        "horas": round((time.time() - pos["entrada_ts"]) / 3600, 2),
        "motivo": motivo, "url": pos["url"],
    }
    e["fechados"].append(t)
    novo = not os.path.exists(F_TRADES)
    with open(F_TRADES, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(t))
        if novo:
            w.writeheader()
        w.writerow(t)
    log(f"VENDA {pos['simbolo']} | {motivo} | P&L ${pnl:+.2f} ({t['pnl_pct']:+.1f}%)")


def gerir_posicoes(e):
    ends = list(e["posicoes"])
    if not ends:
        return
    c = CONFIG
    pares, ok = melhores_pares(ends)
    for end in ends:
        if end not in ok:
            continue
        pos = e["posicoes"][end]
        par = pares.get(end)
        if not par or num(par.get("priceUsd")) <= 0:
            pos["falhas"] += 1
            if pos["falhas"] >= 3:
                vender(e, end, 0.0, "par desapareceu (rug?)", fator=0)
            continue
        pos["falhas"] = 0
        preco, liq = num(par["priceUsd"]), liquidez(par)
        pos["preco_atual"] = preco
        pos["preco_max"] = max(pos["preco_max"], preco)
        var = (preco / pos["preco_entrada"] - 1) * 100
        pico = (pos["preco_max"] / pos["preco_entrada"] - 1) * 100
        queda_pico = (preco / pos["preco_max"] - 1) * 100
        horas = (time.time() - pos["entrada_ts"]) / 3600
        queda_liq = (1 - liq / pos["liq_entrada"]) * 100 if pos["liq_entrada"] else 0

        if pico >= c["protecao_ativa_pct"]:
            pos["stop"] = max(pos["stop"], c["protecao_stop_pct"])

        if queda_liq >= c["liquidez_rug_pct"]:
            vender(e, end, preco, "liquidez colapsou (rug?)", fator=0.3)
        elif queda_liq >= c["liquidez_alerta_pct"]:
            vender(e, end, preco, "liquidez a fugir", liq)
        elif var >= c["take_profit_pct"]:
            vender(e, end, preco, "take profit", liq)
        elif var <= pos["stop"]:
            vender(e, end, preco, "stop protegido" if pos["stop"] > 0 else "stop loss", liq)
        elif pico >= c["trailing_ativa_pct"] and queda_pico <= -c["trailing_queda_pct"]:
            vender(e, end, preco, "trailing stop", liq)
        elif horas >= c["estagnado_horas"] and var < c["estagnado_min_pct"]:
            vender(e, end, preco, "estagnado", liq)
        elif horas >= c["tempo_max_horas"]:
            vender(e, end, preco, "tempo máximo", liq)


def procurar_entradas(e):
    motivo = bloqueio(e)
    if motivo:
        log(f"Sem novas compras: {motivo}.")
        return
    vagas = CONFIG["max_posicoes"] - len(e["posicoes"])
    if vagas <= 0:
        return
    ok, info = mercado_ok()
    if not ok:
        log(f"Sem novas compras: {info}.")
        return
    agora = time.time()
    cd = CONFIG["cooldown_horas"] * 3600
    ends = [x for x in candidatos()
            if x not in e["posicoes"] and agora - e["ultimo_trade"].get(x, 0) > cd]
    pares, _ = melhores_pares(ends)

    # Anti-clones: por cada nome, só conta o token com mais liquidez
    por_nome = {}
    for end, par in pares.items():
        n = simbolo(par).lower()
        if n not in por_nome or liquidez(par) > liquidez(pares[por_nome[n]]):
            por_nome[n] = end
    em_carteira = {p["simbolo"].lower() for p in e["posicoes"].values()}

    bons, rejeicoes = [], {}
    for n, end in por_nome.items():
        if n in em_carteira or agora - e["ultimo_simbolo"].get(n, 0) <= cd:
            continue
        passou, m = passa_filtros(pares[end])
        if passou:
            bons.append((end, pares[end]))
        else:
            rejeicoes[m] = rejeicoes.get(m, 0) + 1
    bons.sort(key=lambda ep: liquidez(ep[1]), reverse=True)
    for end, par in bons[:vagas]:
        comprar(e, end, par)
    top = ", ".join(f"{m}: {n}" for m, n in sorted(rejeicoes.items(), key=lambda x: -x[1])[:4])
    log(f"Procura ({info}): {len(por_nome)} tokens, {len(bons)} passaram. "
        f"Rejeições: {top or '-'} | Carteira ${valor_total(e):.2f}")


# ---------------------- Relatório ----------------------
def texto_relatorio(e):
    f = e["fechados"]
    total, ini = valor_total(e), CONFIG["saldo_inicial_usd"]
    l = [f"# Relatório da simulação — v{VERSAO}", "",
         f"Atualizado: {datetime.now():%Y-%m-%d %H:%M} (UTC) · Início: {e.get('inicio', '?')}", "",
         f"**Valor da carteira: ${total:.2f}** ({(total / ini - 1) * 100:+.2f}% vs ${ini:.0f})", "",
         f"Caixa: ${e['caixa']:.2f} · Posições abertas: {len(e['posicoes'])}", ""]
    b = bloqueio(e)
    if b:
        l += [f"⚠️ Sem novas compras: {b}.", ""]
    if f:
        g = [t for t in f if t["pnl_usd"] > 0]
        p = [t for t in f if t["pnl_usd"] <= 0]
        soma_g, soma_p = sum(t["pnl_usd"] for t in g), -sum(t["pnl_usd"] for t in p)
        pf = f"{soma_g / soma_p:.2f}" if soma_p > 0 else "∞"
        mg = sum(t["pnl_pct"] for t in g) / len(g) if g else 0
        mp = sum(t["pnl_pct"] for t in p) / len(p) if p else 0
        motivos = {}
        for t in f:
            motivos[t["motivo"]] = motivos.get(t["motivo"], 0) + 1
        l += [f"Trades fechados: {len(f)} · Ganhadores: {len(g)} ({len(g) / len(f) * 100:.0f}%)", "",
              f"P&L realizado: ${soma_g - soma_p:+.2f} · Média por trade: "
              f"{sum(t['pnl_pct'] for t in f) / len(f):+.1f}%", "",
              f"Ganho médio: {mg:+.1f}% · Perda média: {mp:+.1f}% · "
              f"Profit factor: {pf} (acima de 1 = ganha dinheiro)", "",
              "Saídas: " + ", ".join(f"{m}: {n}" for m, n in motivos.items()), "",
              "## Últimos 10 trades", ""]
        for t in f[-10:][::-1]:
            l.append(f"- {t['data_saida']} · {t['simbolo']} · {t['pnl_pct']:+.1f}% · {t['motivo']}")
        l.append("")
    else:
        l += ["Ainda sem trades fechados. A v2 é muito mais seletiva: é normal passar horas sem comprar.", ""]
    if e["posicoes"]:
        l += ["## Posições abertas", ""]
        for p in e["posicoes"].values():
            v = (p["preco_atual"] / p["preco_entrada"] - 1) * 100
            l.append(f"- {p['simbolo']} {v:+.1f}% (custo ${p['custo']:.2f}, stop {p['stop']:+.0f}%)")
    return "\n".join(l) + "\n"


# ---------------------- Comandos ----------------------
def um_ciclo(e):
    novo_dia(e)
    gerir_posicoes(e)
    procurar_entradas(e)


def ciclo():
    if os.path.exists(F_PARAR):
        print("Ficheiro PARAR existe — ciclo ignorado.")
        return
    e = carregar()
    try:
        um_ciclo(e)
    except Exception as ex:
        log(f"Erro no ciclo: {ex}")
    guardar(e)
    with open(F_RELATORIO, "w", encoding="utf-8") as f:
        f.write(texto_relatorio(e))


def correr():
    e = carregar()
    log("Bot v2 iniciado — MODO SIMULAÇÃO.")
    ultima = 0.0
    try:
        while not os.path.exists(F_PARAR):
            try:
                novo_dia(e)
                gerir_posicoes(e)
                if time.time() - ultima >= CONFIG["intervalo_procura_s"]:
                    procurar_entradas(e)
                    ultima = time.time()
                guardar(e)
                with open(F_RELATORIO, "w", encoding="utf-8") as f:
                    f.write(texto_relatorio(e))
            except Exception as ex:
                log(f"Erro no ciclo: {ex}")
            time.sleep(CONFIG["intervalo_posicoes_s"])
    except KeyboardInterrupt:
        pass
    guardar(e)


def relatorio():
    print(texto_relatorio(carregar()))


def reset():
    for f in (F_ESTADO, F_TRADES, F_RELATORIO):
        if os.path.exists(f):
            os.remove(f)
    print("Simulação v2 apagada.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "correr"
    {"correr": correr, "ciclo": ciclo, "relatorio": relatorio, "reset": reset}.get(cmd, correr)()
