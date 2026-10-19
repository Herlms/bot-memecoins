#!/usr/bin/env python3
"""
Bot de PAPER TRADING de memecoins na Solana — SÓ SIMULAÇÃO.
Não toca em dinheiro real nem em carteiras. Usa dados públicos da DexScreener
(sem chave de API) e regista trades fictícios com taxas e derrapagem simuladas.

Uso:
  python3 bot_memecoins.py             -> corre o bot (Ctrl+C para parar)
  python3 bot_memecoins.py relatorio   -> mostra os resultados até agora
  python3 bot_memecoins.py ciclo       -> faz um só ciclo e sai (modo GitHub Actions)
  python3 bot_memecoins.py reset       -> apaga a simulação e começa do zero

Para parar à distância: cria um ficheiro chamado PARAR na mesma pasta
(ex.: `touch PARAR`). O bot fecha-se sozinho no ciclo seguinte.

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

# ======================= CONFIGURAÇÃO =======================
CONFIG = {
    # Carteira virtual
    "saldo_inicial_usd": 1000.0,
    "tamanho_posicao_pct": 5.0,       # % do valor total por trade
    "max_posicoes": 5,
    "taxa_pct": 0.5,                  # taxa por operação (DEX + rede), aprox.
    "slippage_base_pct": 1.0,         # derrapagem mínima por operação

    # Filtros de entrada (o que o bot considera "comprável")
    "liquidez_min_usd": 30_000,
    "volume_1h_min_usd": 20_000,
    "idade_min_horas": 1,             # evita tokens acabados de lançar
    "idade_max_horas": 72,
    "fdv_max_usd": 20_000_000,
    "subida_5m_min_pct": 1.0,         # momento positivo...
    "subida_5m_max_pct": 15.0,        # ...mas não já em foguete
    "compras_5m_min": 15,
    "racio_compras_vendas_min": 1.2,

    # Regras de saída
    "take_profit_pct": 40.0,
    "stop_loss_pct": -20.0,
    "trailing_ativa_pct": 20.0,       # depois de subir 20%...
    "trailing_queda_pct": 15.0,       # ...vende se cair 15% desde o pico
    "tempo_max_horas": 6,
    "queda_liquidez_rug_pct": 60.0,   # liquidez a cair isto = provável rug

    # Segurança
    "perda_diaria_max_pct": 10.0,     # pausa novas compras até amanhã
    "cooldown_horas": 12,             # não volta ao mesmo token tão cedo

    # Tempos
    "intervalo_posicoes_s": 60,
    "intervalo_procura_s": 300,
}
# ============================================================

API = "https://api.dexscreener.com"
PASTA = os.path.dirname(os.path.abspath(__file__))
F_ESTADO = os.path.join(PASTA, "estado.json")
F_TRADES = os.path.join(PASTA, "trades.csv")
F_LOG = os.path.join(PASTA, "bot.log")
F_PARAR = os.path.join(PASTA, "PARAR")


def log(msg):
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linha, flush=True)
    with open(F_LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def obter(url):
    """GET com até 3 tentativas. Devolve None se falhar."""
    for _ in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "paper-bot/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log("Limite de pedidos atingido, a esperar 30s...")
                time.sleep(30)
            else:
                log(f"Erro HTTP {e.code} em {url[:80]}")
                time.sleep(5)
        except Exception as e:
            log(f"Erro de rede: {e}")
            time.sleep(5)
    return None


def num(x, defeito=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return defeito


def liquidez(par):
    return num((par.get("liquidity") or {}).get("usd"))


# ---------------------- Dados de mercado ----------------------
def melhores_pares(enderecos):
    """Para cada token, devolve o par com mais liquidez.
    Devolve (pares, enderecos_consultados_com_sucesso)."""
    pares, ok = {}, set()
    for i in range(0, len(enderecos), 30):
        lote = enderecos[i:i + 30]
        dados = obter(f"{API}/tokens/v1/solana/{','.join(lote)}")
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
    """Tokens Solana recentes/promovidos na DexScreener."""
    ends = []
    for ep in ("/token-boosts/latest/v1", "/token-profiles/latest/v1", "/token-boosts/top/v1"):
        dados = obter(API + ep)
        if isinstance(dados, list):
            for t in dados:
                a = t.get("tokenAddress")
                if t.get("chainId") == "solana" and a and a not in ends:
                    ends.append(a)
    return ends


def passa_filtros(par):
    c = CONFIG
    liq = liquidez(par)
    vol1h = num((par.get("volume") or {}).get("h1"))
    var = par.get("priceChange") or {}
    m5, h1 = num(var.get("m5")), num(var.get("h1"))
    tx = (par.get("txns") or {}).get("m5") or {}
    compras, vendas = num(tx.get("buys")), num(tx.get("sells"))
    fdv = num(par.get("fdv") or par.get("marketCap"))
    criado = num(par.get("pairCreatedAt"))
    idade_h = (time.time() * 1000 - criado) / 3_600_000 if criado else -1

    if num(par.get("priceUsd")) <= 0:
        return False, "sem preço"
    if liq < c["liquidez_min_usd"]:
        return False, "liquidez baixa"
    if vol1h < c["volume_1h_min_usd"]:
        return False, "volume baixo"
    if not (c["idade_min_horas"] <= idade_h <= c["idade_max_horas"]):
        return False, "idade fora do intervalo"
    if fdv <= 0 or fdv > c["fdv_max_usd"]:
        return False, "FDV fora do intervalo"
    if not (c["subida_5m_min_pct"] <= m5 <= c["subida_5m_max_pct"]):
        return False, "momento 5m fora do intervalo"
    if h1 <= 0:
        return False, "1h negativa"
    if compras < c["compras_5m_min"]:
        return False, "poucas compras"
    if compras < vendas * c["racio_compras_vendas_min"]:
        return False, "pressão vendedora"
    return True, "ok"


# ---------------------- Estado ----------------------
def estado_novo():
    return {
        "caixa": CONFIG["saldo_inicial_usd"],
        "posicoes": {},
        "fechados": [],
        "ultimo_trade": {},
        "dia": "",
        "valor_inicio_dia": CONFIG["saldo_inicial_usd"],
        "pnl_dia": 0.0,
        "inicio": datetime.now().isoformat(timespec="seconds"),
    }


def carregar():
    if os.path.exists(F_ESTADO):
        with open(F_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return estado_novo()


def guardar(estado):
    tmp = F_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)
    os.replace(tmp, F_ESTADO)


def valor_total(estado):
    return estado["caixa"] + sum(p["qtd"] * p["preco_atual"] for p in estado["posicoes"].values())


def novo_dia(estado):
    hoje = datetime.now().strftime("%Y-%m-%d")
    if estado["dia"] != hoje:
        estado["dia"] = hoje
        estado["valor_inicio_dia"] = valor_total(estado)
        estado["pnl_dia"] = 0.0
        log(f"Novo dia. Valor da carteira: ${estado['valor_inicio_dia']:.2f}")


def pausado(estado):
    limite = -CONFIG["perda_diaria_max_pct"] / 100 * estado["valor_inicio_dia"]
    return estado["pnl_dia"] <= limite


def slippage(valor_usd, liq):
    s = CONFIG["slippage_base_pct"] / 100 + (valor_usd / liq if liq > 0 else 0.5)
    return min(s, 0.5)


# ---------------------- Operações simuladas ----------------------
def comprar(estado, end, par):
    preco = num(par["priceUsd"])
    liq = liquidez(par)
    valor = min(valor_total(estado) * CONFIG["tamanho_posicao_pct"] / 100, estado["caixa"])
    if valor < 1:
        return
    preco_pago = preco * (1 + slippage(valor, liq))
    qtd = valor * (1 - CONFIG["taxa_pct"] / 100) / preco_pago
    base = par.get("baseToken") or {}
    estado["caixa"] -= valor
    estado["posicoes"][end] = {
        "simbolo": base.get("symbol", "?"),
        "url": par.get("url", ""),
        "qtd": qtd,
        "custo": valor,
        "preco_entrada": preco,
        "preco_atual": preco,
        "preco_max": preco,
        "liq_entrada": liq,
        "entrada_ts": time.time(),
        "falhas": 0,
    }
    estado["ultimo_trade"][end] = time.time()
    log(f"COMPRA {base.get('symbol')} | ${valor:.2f} a ${preco:.8g} (pago ${preco_pago:.8g})")


def vender(estado, end, preco, motivo, liq=0.0, fator=None):
    pos = estado["posicoes"].pop(end)
    if fator is not None:          # execução forçada (rug / par desaparecido)
        preco_ef = preco * fator
    else:
        preco_ef = preco * (1 - slippage(pos["qtd"] * preco, liq))
    recebido = pos["qtd"] * preco_ef * (1 - CONFIG["taxa_pct"] / 100)
    pnl = recebido - pos["custo"]
    estado["caixa"] += recebido
    estado["pnl_dia"] += pnl
    horas = (time.time() - pos["entrada_ts"]) / 3600
    trade = {
        "data_saida": datetime.now().isoformat(timespec="seconds"),
        "simbolo": pos["simbolo"],
        "token": end,
        "custo_usd": round(pos["custo"], 2),
        "recebido_usd": round(recebido, 2),
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl / pos["custo"] * 100, 2),
        "horas": round(horas, 2),
        "motivo": motivo,
        "url": pos["url"],
    }
    estado["fechados"].append(trade)
    novo = not os.path.exists(F_TRADES)
    with open(F_TRADES, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(trade))
        if novo:
            w.writeheader()
        w.writerow(trade)
    log(f"VENDA {pos['simbolo']} | {motivo} | P&L ${pnl:+.2f} ({trade['pnl_pct']:+.1f}%)")


def gerir_posicoes(estado):
    ends = list(estado["posicoes"])
    if not ends:
        return
    c = CONFIG
    pares, ok = melhores_pares(ends)
    for end in ends:
        if end not in ok:
            continue                      # falha de rede: tenta no próximo ciclo
        pos = estado["posicoes"][end]
        par = pares.get(end)
        if not par or num(par.get("priceUsd")) <= 0:
            pos["falhas"] += 1
            if pos["falhas"] >= 3:
                vender(estado, end, 0.0, "par desapareceu (rug?)", fator=0)
            continue
        pos["falhas"] = 0
        preco, liq = num(par["priceUsd"]), liquidez(par)
        pos["preco_atual"] = preco
        pos["preco_max"] = max(pos["preco_max"], preco)

        var = (preco / pos["preco_entrada"] - 1) * 100
        subida_pico = (pos["preco_max"] / pos["preco_entrada"] - 1) * 100
        queda_pico = (preco / pos["preco_max"] - 1) * 100
        horas = (time.time() - pos["entrada_ts"]) / 3600
        queda_liq = (1 - liq / pos["liq_entrada"]) * 100 if pos["liq_entrada"] else 0

        if queda_liq >= c["queda_liquidez_rug_pct"]:
            vender(estado, end, preco, "liquidez colapsou (rug?)", fator=0.3)
        elif var >= c["take_profit_pct"]:
            vender(estado, end, preco, "take profit", liq)
        elif var <= c["stop_loss_pct"]:
            vender(estado, end, preco, "stop loss", liq)
        elif subida_pico >= c["trailing_ativa_pct"] and queda_pico <= -c["trailing_queda_pct"]:
            vender(estado, end, preco, "trailing stop", liq)
        elif horas >= c["tempo_max_horas"]:
            vender(estado, end, preco, "tempo máximo", liq)


def procurar_entradas(estado):
    if pausado(estado):
        log("Perda diária máxima atingida — sem novas compras até amanhã.")
        return
    vagas = CONFIG["max_posicoes"] - len(estado["posicoes"])
    if vagas <= 0:
        return
    agora = time.time()
    cooldown = CONFIG["cooldown_horas"] * 3600
    ends = [
        e for e in candidatos()
        if e not in estado["posicoes"] and agora - estado["ultimo_trade"].get(e, 0) > cooldown
    ]
    pares, _ = melhores_pares(ends)
    bons = [(e, p) for e, p in pares.items() if passa_filtros(p)[0]]
    bons.sort(key=lambda ep: num((ep[1].get("volume") or {}).get("h1")), reverse=True)
    for end, par in bons[:vagas]:
        comprar(estado, end, par)
    log(f"Procura: {len(ends)} tokens vistos, {len(bons)} passaram os filtros. "
        f"Carteira ${valor_total(estado):.2f} | caixa ${estado['caixa']:.2f} | "
        f"{len(estado['posicoes'])} posições")


# ---------------------- Comandos ----------------------
def correr():
    estado = carregar()
    log("Bot iniciado — MODO SIMULAÇÃO (sem dinheiro real).")
    ultima_procura = 0.0
    try:
        while True:
            if os.path.exists(F_PARAR):
                log("Ficheiro PARAR encontrado. A desligar.")
                os.remove(F_PARAR)
                break
            try:
                novo_dia(estado)
                gerir_posicoes(estado)
                if time.time() - ultima_procura >= CONFIG["intervalo_procura_s"]:
                    procurar_entradas(estado)
                    ultima_procura = time.time()
                guardar(estado)
            except Exception as e:
                log(f"Erro no ciclo: {e}")
            time.sleep(CONFIG["intervalo_posicoes_s"])
    except KeyboardInterrupt:
        log("Interrompido pelo utilizador.")
    guardar(estado)


def texto_relatorio(estado):
    fechados = estado["fechados"]
    total = valor_total(estado)
    inicial = CONFIG["saldo_inicial_usd"]
    l = ["# Relatório da simulação", "",
         f"Atualizado: {datetime.now():%Y-%m-%d %H:%M} (UTC no GitHub) · Início: {estado.get('inicio', '?')}", "",
         f"**Valor da carteira: ${total:.2f}** ({(total / inicial - 1) * 100:+.2f}% vs ${inicial:.0f})", "",
         f"Caixa: ${estado['caixa']:.2f} · Posições abertas: {len(estado['posicoes'])}", ""]
    if pausado(estado):
        l += ["⚠️ Perda diária máxima atingida — sem novas compras até amanhã.", ""]
    if fechados:
        ganhos = [t for t in fechados if t["pnl_usd"] > 0]
        pnl = sum(t["pnl_usd"] for t in fechados)
        melhor = max(fechados, key=lambda t: t["pnl_pct"])
        pior = min(fechados, key=lambda t: t["pnl_pct"])
        motivos = {}
        for t in fechados:
            motivos[t["motivo"]] = motivos.get(t["motivo"], 0) + 1
        l += [f"Trades fechados: {len(fechados)} · Ganhadores: {len(ganhos)} "
              f"({len(ganhos) / len(fechados) * 100:.0f}%)", "",
              f"P&L realizado: ${pnl:+.2f} · Média por trade: "
              f"{sum(t['pnl_pct'] for t in fechados) / len(fechados):+.1f}%", "",
              f"Melhor: {melhor['simbolo']} {melhor['pnl_pct']:+.1f}% · Pior: {pior['simbolo']} {pior['pnl_pct']:+.1f}%", "",
              "Saídas: " + ", ".join(f"{m}: {n}" for m, n in motivos.items()), "",
              "## Últimos 10 trades", ""]
        for t in fechados[-10:][::-1]:
            l.append(f"- {t['data_saida']} · {t['simbolo']} · {t['pnl_pct']:+.1f}% · {t['motivo']}")
        l.append("")
    else:
        l += ["Ainda sem trades fechados.", ""]
    if estado["posicoes"]:
        l += ["## Posições abertas", ""]
        for p in estado["posicoes"].values():
            var = (p["preco_atual"] / p["preco_entrada"] - 1) * 100
            l.append(f"- {p['simbolo']} {var:+.1f}% (custo ${p['custo']:.2f})")
    return "\n".join(l) + "\n"


def relatorio():
    print(texto_relatorio(carregar()))


def ciclo():
    """Um único ciclo (para correr agendado, ex. GitHub Actions)."""
    if os.path.exists(F_PARAR):
        print("Ficheiro PARAR existe — ciclo ignorado.")
        return
    estado = carregar()
    try:
        novo_dia(estado)
        gerir_posicoes(estado)
        procurar_entradas(estado)
    except Exception as e:
        log(f"Erro no ciclo: {e}")
    guardar(estado)
    with open(os.path.join(PASTA, "RELATORIO.md"), "w", encoding="utf-8") as f:
        f.write(texto_relatorio(estado))


def reset():
    for f in (F_ESTADO, F_TRADES):
        if os.path.exists(f):
            os.remove(f)
    print("Simulação apagada. Próxima execução começa do zero.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "correr"
    {"correr": correr, "ciclo": ciclo, "relatorio": relatorio, "reset": reset}.get(cmd, correr)()
