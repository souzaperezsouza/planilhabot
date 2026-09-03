import os
import io
import re
import logging
import threading
import psycopg2
import psycopg2.extras
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
import base64
import json
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters, CallbackQueryHandler
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def esc_md(texto):
    """Escapa caracteres especiais do Markdown legado do Telegram (_ * ` [) em texto
    vindo do usuário, evitando erro 'Can't parse entities'."""
    if texto is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(texto))

TOKEN         = "8790751046:AAFHpd7q4FmgUZrq3PA4eMb06fMyMaiL5lE"
DATABASE_URL  = os.environ.get("DATABASE_URL", "postgresql://db_setembro_user:6MjNh6d47Ve7Oiwz0hKDcFNmlofBLZ42@dpg-dac2doqjobas73carjs0-a.oregon-postgres.render.com/db_setembro")
BANCA_INICIAL = 5000

CASAS = [
    "Bet365","Betano","SportingBet","Betboo","Vaidebet","Betpix365","Lottu",
    "BETesporte","BetFair","Novibet","Esportivabet","Betnacional",
    "Vupi","Goldebet","Lotogreen","Pagol","Versus","Kto",
    "4Win","Donaldbet/Bet.bet","Vivasorte","Esportes Da Sorte","Stake",
    "Faz1Bet","EstrelaBet","Outra"
]

def get_casas_limitadas():
    """Retorna a lista de casas marcadas como limitadas (não aparecem nas opções de nova aposta)."""
    try:
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT casa FROM casas_limitadas ORDER BY casa")
                return [r[0] for r in cur.fetchall()]
    except Exception:
        return []

def adicionar_casa_limitada(casa):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO casas_limitadas (casa) VALUES (%s) ON CONFLICT (casa) DO NOTHING",
                (casa,)
            )
        conn.commit()

def remover_casa_limitada(casa):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM casas_limitadas WHERE casa=%s", (casa,))
        conn.commit()

def get_casas_ordenadas(n=13):
    """Retorna as n casas mais usadas no banco + 'Outra' no final, excluindo casas limitadas."""
    # Comparação case-insensitive: casas antigas no banco podem estar com grafia
    # diferente (ex: "Rei do Pitaco") da que foi salva em casas_limitadas
    # (ex: "Rei Do Pitaco", gerado por .title()), então comparar direto fazia
    # a casa "escapar" do filtro.
    limitadas_lower = set(c.lower() for c in get_casas_limitadas())
    try:
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT casa, COUNT(*) as total
                    FROM apostas
                    WHERE casa IS NOT NULL AND casa != ''
                    GROUP BY casa
                """)
                rows = cur.fetchall()
        # Agrupa por nome NORMALIZADO antes de ordenar: grafias diferentes da
        # mesma casa (ex: "BETesporte" vs "Betesporte") não podem competir por
        # espaço separadamente, senão a contagem real fica fragmentada e a
        # ordem por "casa que eu mais aposto" sai errada. Também não dá mais
        # LIMIT direto na query, porque cortar antes de filtrar as limitadas
        # deixava só sobras aleatórias (ordem do array CASAS) pra preencher
        # os espaços vagos, em vez das próximas casas mais usadas de verdade.
        contagem = {}
        for casa_raw, total in rows:
            if not casa_raw:
                continue
            nome = normalizar_casa(casa_raw)
            if nome == "Outra":
                continue
            contagem[nome] = contagem.get(nome, 0) + total
        ordenadas = sorted(contagem.items(), key=lambda x: x[1], reverse=True)
        top = [nome for nome, _ in ordenadas if nome.lower() not in limitadas_lower][:n]
        # Garante que casas conhecidas mas não no banco apareçam se necessário
        if len(top) < n:
            for c in CASAS:
                if c not in top and c != "Outra" and c.lower() not in limitadas_lower and len(top) < n:
                    top.append(c)
        return top + ["Outra"]
    except Exception:
        return [c for c in CASAS if c.lower() not in limitadas_lower]

# Mapeamento para normalizar casas antigas/variantes para o nome atual
MAPA_CASAS_GEMEAS = {


    # Vaidebet
    "vdb":            "Vaidebet",
    "Vdb":            "Vaidebet",
    "VDB":            "Vaidebet",
    # Rei do Pitaco
    "pitaco":         "Rei Do Pitaco",
    "Pitaco":         "Rei Do Pitaco",
    "Rei do Pitaco":  "Rei Do Pitaco",
    # Donaldbet/Bet.bet
    "donaldbet":                                      "Donaldbet/Bet.bet",
    "Donaldbet":                                      "Donaldbet/Bet.bet",
    "DonaldBet":                                      "Donaldbet/Bet.bet",
    "bet.bet":                                        "Donaldbet/Bet.bet",
    "Bet.bet":                                        "Donaldbet/Bet.bet",
    "BET.BET":                                        "Donaldbet/Bet.bet",
    "Betpontobet":                                    "Donaldbet/Bet.bet",
    "Betponto":                                       "Donaldbet/Bet.bet",
    "Donald":                                         "Donaldbet/Bet.bet",
    # variacao com maiuscula no Bet.Bet (como aparece no banco)
    "Donaldbet/Bet.Bet":                              "Donaldbet/Bet.bet",
    # link markdown que o Telegram salva como texto
    "Donaldbet/[Bet.bet](https://bet.bet/)":          "Donaldbet/Bet.bet",
    "Donaldbet/[Bet.Bet](https://bet.bet/)":          "Donaldbet/Bet.bet",

    # Bateubet (typos e variacoes com espaco)
    "Bateu Bet":       "Bateubet",
    "Bateu":           "Bateubet",
    "Bqteubet":        "Bateubet",
    "Bqtaubet":        "Bateubet",

    # Vaidebet (variacao com espaco)
    "Vai De Bet":      "Vaidebet",

    # Mc Games (variacao com espaco)
    "Mc Games":        "Mcgames",
    "MC Games":        "Mcgames",
    "McGames":         "Mcgames",

    # Bolsa De Apostas (singular)
    "Bolsa De Aposta": "Bolsa De Apostas",

    "rei do pitaco":   "Rei Do Pitaco",

    # Betpix365 (abreviacao)
    "betpix":          "Betpix365",
    "Betpix":          "Betpix365",
    "BetPix":          "Betpix365",
    "BETPIX":          "Betpix365",

    # BetFair (variacao de capitalizacao)
    "Betfair":         "BetFair",
    "betfair":         "BetFair",
    "BETFAIR":         "BetFair",

    # BETesporte (variacao de capitalizacao - grafia oficial é "BETesporte")
    "Betesporte":      "BETesporte",
    "betesporte":      "BETesporte",
    "BETESPORTE":      "BETesporte",

    # EstrelaBet (nome abreviado/digitado errado)
    "Estrela":         "EstrelaBet",
    "estrela":         "EstrelaBet",
    "estrelabet":      "EstrelaBet",

    # Esportes Da Sorte (abreviacao)
    "eds":             "Esportes Da Sorte",
    "Eds":             "Esportes Da Sorte",
    "EDS":             "Esportes Da Sorte",

    # Goldebet (grafia separada "Gol de Bet") - casa propria, distinta da Lotogreen
    "Gol de Bet":      "Goldebet",
    "gol de bet":      "Goldebet",
    "goldebet":        "Goldebet",
}

ESPORTES = ["⚽ Futebol","🏀 Basquete","🎾 Tênis","🏒 Hóquei",
            "🏈 Futebol Americano","⚾ Beisebol","🥊 MMA/Boxe","🏐 Vôlei",
            "🏎️ F1","🎮 Esports","🎯 Múltipla","Outro"]

def get_esportes_ordenados():
    """Retorna esportes ordenados por uso no banco + 'Outro' no final."""
    try:
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT esporte, COUNT(*) as total
                    FROM apostas
                    WHERE esporte IS NOT NULL AND esporte != ''
                    GROUP BY esporte
                    ORDER BY total DESC
                """)
                rows = cur.fetchall()
        # Normaliza e deduplica mantendo ordem por frequência
        seen = set()
        top = []
        for r in rows:
            if not r[0] or r[0] == "Outro": continue
            nome = normalizar_esporte(r[0])
            if nome not in seen:
                seen.add(nome)
                top.append(nome)
        for e in ESPORTES:
            if e not in seen and e != "Outro":
                top.append(e)
        return top + ["Outro"]
    except Exception:
        return ESPORTES

logging.basicConfig(level=logging.WARNING)

# Estados dos ConversationHandlers
DATA, HORARIO, DESCRICAO, ODD, STAKE, ESPORTE, CASA = range(7)
ATUALIZAR_ID, ATUALIZAR_RES                         = range(7, 9)
EDITAR_ID, EDITAR_CAMPO, EDITAR_VALOR, EDITAR_CASA  = range(9, 13)
EDITAR_PAGINA                                       = 13
MUDAR_UNIDADE_VALOR                                 = 14
RESULTADOS_MENU                                     = 15
GERAR_MENU                                          = 16
CONFIG_MENU, GASTOS_MENU, GASTO_DESCRICAO, GASTO_VALOR = range(17, 21)
LIMITACOES_MENU, LIMITACOES_ADD                         = range(21, 23)
DUPLICAR_DESC_AGUARDA                                    = 23
SEMANA_MES, SEMANA_SEMANA                                = 24, 25

CANCELAR_BTN = "❌ Cancelar"
VOLTAR_BTN   = "⬅️ Voltar"

# ── TECLADOS ──────────────────────────────────────────────────────────────────
def teclado_menu():
    modo_ia_label = "Modo IA: ON ✅" if get_modo_ia() else "Modo IA: OFF"
    return ReplyKeyboardMarkup([
        ["📝 Nova aposta",   "⏳ Ver pendentes"],
        ["📈 Resultados",    "✏️ Editar aposta"],
        ["📊 Gerar Resultados", "⚙️ Configurações"],
        [modo_ia_label],
    ], resize_keyboard=True)

def teclado_cancelar():
    return ReplyKeyboardMarkup([[CANCELAR_BTN]], resize_keyboard=True)

def teclado_cancelar_voltar():
    return ReplyKeyboardMarkup([[VOLTAR_BTN, CANCELAR_BTN]], resize_keyboard=True)

def teclado_resultados():
    return ReplyKeyboardMarkup([
        ["🏦 Por Casa", "🏅 Por Esporte"],
        ["📅 Por Mês",  "📆 Semana"],
        ["📊 Estatísticas"],
        ["🔙 Voltar"],
    ], resize_keyboard=True)

def teclado_gerar():
    return ReplyKeyboardMarkup([
        ["📊 Gerar Dashboard", "🌐 Dashboard HTML"],
        ["📂 Gerar Dados"],
        ["🔙 Voltar"],
    ], resize_keyboard=True)

def teclado_config():
    return ReplyKeyboardMarkup([
        ["💲 Mudar Unidade", "💸 Gastos"],
        ["🚫 Limitações"],
        ["🔙 Voltar"],
    ], resize_keyboard=True)

def teclado_limitacoes(casas_limitadas):
    linhas = [[c] for c in casas_limitadas]
    linhas.append(["➕ Adicionar Casa"])
    linhas.append(["🔙 Voltar"])
    return ReplyKeyboardMarkup(linhas, resize_keyboard=True)

def teclado_gastos():
    return ReplyKeyboardMarkup([
        ["➕ Novo Gasto", "📋 Ver Gastos"],
        ["🔙 Voltar"],
    ], resize_keyboard=True)

# ── BANCO DE DADOS ────────────────────────────────────────────────────────────
def conectar():
    return psycopg2.connect(DATABASE_URL)

def inicializar_db():
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS apostas (
                    id        SERIAL PRIMARY KEY,
                    data      DATE NOT NULL,
                    horario   VARCHAR(5),
                    descricao TEXT NOT NULL,
                    odd       NUMERIC(12,6) NOT NULL,
                    stake     NUMERIC(10,2) NOT NULL,
                    resultado VARCHAR(10) DEFAULT 'pendente',
                    casa      VARCHAR(50),
                    esporte   VARCHAR(50),
                    freebet   NUMERIC(10,2) DEFAULT 0,
                    unidade   NUMERIC(10,2) DEFAULT 50
                )
            """)
            cur.execute("ALTER TABLE apostas ADD COLUMN IF NOT EXISTS freebet NUMERIC(10,2) DEFAULT 0")
            cur.execute("ALTER TABLE apostas ALTER COLUMN odd TYPE NUMERIC(12,6)")
            cur.execute("ALTER TABLE apostas ADD COLUMN IF NOT EXISTS unidade NUMERIC(10,2) DEFAULT 50")
            cur.execute("ALTER TABLE apostas ADD COLUMN IF NOT EXISTS cashout_valor NUMERIC(10,2) DEFAULT 0")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS configuracoes (
                    chave  VARCHAR(50) PRIMARY KEY,
                    valor  TEXT NOT NULL
                )
            """)
            cur.execute("""
                INSERT INTO configuracoes (chave, valor) VALUES ('unidade_atual', '50')
                ON CONFLICT (chave) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO configuracoes (chave, valor) VALUES ('modo_ia', '0')
                ON CONFLICT (chave) DO NOTHING
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gastos (
                    id        SERIAL PRIMARY KEY,
                    data      DATE NOT NULL,
                    descricao TEXT NOT NULL,
                    valor     NUMERIC(10,2) NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS casas_limitadas (
                    casa VARCHAR(50) PRIMARY KEY
                )
            """)
        conn.commit()
    # Normaliza casas com nomes antigos/errados automaticamente a cada deploy
    migrar_casas_gemeas()

def carregar():
    with conectar() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM apostas ORDER BY data ASC, horario ASC, id ASC")
            return [dict(r) for r in cur.fetchall()]

def proximo_id_disponivel(cur):
    """Acha a menor 'tag' (ID) livre — de uma aposta que foi deletada, por exemplo.
    Se não houver nenhum buraco na sequência de IDs, retorna o próximo ID normal (max+1)."""
    cur.execute("""
        SELECT MIN(s.id)
        FROM generate_series(1, (SELECT COALESCE(MAX(id), 0) FROM apostas)) AS s(id)
        LEFT JOIN apostas a ON a.id = s.id
        WHERE a.id IS NULL
    """)
    gap = cur.fetchone()[0]
    if gap is not None:
        return gap
    cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM apostas")
    return cur.fetchone()[0]

def duplicar_aposta(id_orig: int):
    """Duplica a aposta id_orig: copia TODOS os campos exatamente (mesma data,
    resultado, odd, stake etc.) e insere como uma nova linha, usando o próximo
    ID disponível (aproveitando buracos de apostas deletadas, se houver).
    Retorna (aposta_original, novo_id) ou (None, None) se o ID não existir."""
    with conectar() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM apostas WHERE id = %s", (id_orig,))
            aposta = cur.fetchone()
    if not aposta:
        return None, None
    aposta = dict(aposta)

    with conectar() as conn:
        with conn.cursor() as cur:
            novo_id = proximo_id_disponivel(cur)
            cur.execute("""
                INSERT INTO apostas
                    (id, data, horario, descricao, odd, stake, resultado,
                     casa, esporte, freebet, unidade, cashout_valor)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                novo_id, aposta["data"], aposta.get("horario"), aposta["descricao"],
                aposta["odd"], aposta["stake"], aposta["resultado"], aposta.get("casa"),
                aposta.get("esporte"), aposta.get("freebet") or 0,
                aposta.get("unidade") or 50, aposta.get("cashout_valor") or 0,
            ))
            cur.execute("SELECT setval(pg_get_serial_sequence('apostas','id'), (SELECT MAX(id) FROM apostas))")
        conn.commit()
    return aposta, novo_id

def _texto_resumo_aposta(id_orig, aposta, novo_id, titulo):
    data_fmt = aposta["data"].strftime("%d/%m/%Y") if hasattr(aposta["data"], "strftime") else str(aposta["data"])[:10]
    hora_txt = f' às {aposta["horario"]}' if aposta.get("horario") else ""
    res_txt = {"ganhou": "Green ✅", "perdeu": "Red ❌", "void": "Void ↩️", "pendente": "Pendente ⏳"}.get(aposta["resultado"], aposta["resultado"])
    return (
        f"{titulo} #{id_orig} → *#{novo_id}*\n\n"
        f"📅 {data_fmt}{hora_txt}\n"
        f"🏅 {esc_md(aposta.get('esporte') or '')}\n🏷 {esc_md(aposta['descricao'])}\n"
        f"🔢 Odd: {float(aposta['odd']):g}\n💰 Stake: R$ {float(aposta['stake']):.2f}\n"
        f"🏦 Casa: {esc_md(aposta.get('casa') or '')}\n📌 Resultado: {res_txt}"
    )

async def duplicar_comando(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler global: qualquer mensagem 'duplicar <id>' cria uma cópia exata
    dessa aposta (mesma data, resultado, odd, stake etc.), independente de
    qual tela/fluxo o usuário está no momento. Também aceita
    'duplicar <id> casa <nome>' para duplicar trocando a casa de aposta."""
    texto_msg = update.message.text.strip()
    m_casa = re.match(r"(?i)^duplicar\s+(\d+)\s+casa\s+(.+)$", texto_msg)
    if m_casa:
        id_orig    = int(m_casa.group(1))
        nova_casa  = normalizar_casa(m_casa.group(2).strip())
        aposta, novo_id = duplicar_aposta(id_orig)
        if aposta is None:
            await update.message.reply_text(f"❌ Não achei nenhuma aposta #{id_orig}.")
            return
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE apostas SET casa = %s WHERE id = %s", (nova_casa, novo_id))
            conn.commit()
        aposta["casa"] = nova_casa
        texto = _texto_resumo_aposta(id_orig, aposta, novo_id, "✅ Aposta duplicada (casa trocada)")
        try:
            await update.message.reply_text(texto, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(texto.replace("*", ""))
        return

    m = re.match(r"(?i)^duplicar\s+(\d+)$", texto_msg)
    if not m:
        return
    id_orig = int(m.group(1))
    aposta, novo_id = duplicar_aposta(id_orig)
    if aposta is None:
        await update.message.reply_text(f"❌ Não achei nenhuma aposta #{id_orig}.")
        return

    texto = _texto_resumo_aposta(id_orig, aposta, novo_id, "✅ Aposta duplicada")
    try:
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(texto.replace("*", ""))

async def triplicar_comando(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler global: 'triplicar <id>' cria duas cópias extras da aposta
    (a original + 2 novas = 3 no total)."""
    m = re.match(r"(?i)^triplicar\s+(\d+)$", update.message.text.strip())
    if not m:
        return
    id_orig = int(m.group(1))
    aposta1, novo_id1 = duplicar_aposta(id_orig)
    if aposta1 is None:
        await update.message.reply_text(f"❌ Não achei nenhuma aposta #{id_orig}.")
        return
    aposta2, novo_id2 = duplicar_aposta(id_orig)

    data_fmt = aposta1["data"].strftime("%d/%m/%Y") if hasattr(aposta1["data"], "strftime") else str(aposta1["data"])[:10]
    hora_txt = f' às {aposta1["horario"]}' if aposta1.get("horario") else ""
    res_txt = {"ganhou": "Green ✅", "perdeu": "Red ❌", "void": "Void ↩️", "pendente": "Pendente ⏳"}.get(aposta1["resultado"], aposta1["resultado"])
    texto = (
        f"✅ Aposta #{id_orig} triplicada → *#{novo_id1}* e *#{novo_id2}*\n\n"
        f"📅 {data_fmt}{hora_txt}\n"
        f"🏅 {esc_md(aposta1.get('esporte') or '')}\n🏷 {esc_md(aposta1['descricao'])}\n"
        f"🔢 Odd: {float(aposta1['odd']):g}\n💰 Stake: R$ {float(aposta1['stake']):.2f} (x3)\n"
        f"🏦 Casa: {esc_md(aposta1.get('casa') or '')}\n📌 Resultado: {res_txt}"
    )
    try:
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(texto.replace("*", ""))

async def duplicar_descricao_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point de 'duplicar <id> descricao': pede a nova descrição antes
    de criar a cópia."""
    m = re.match(r"(?i)^duplicar\s+(\d+)\s+descricao$", update.message.text.strip())
    id_orig = int(m.group(1))
    with conectar() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM apostas WHERE id = %s", (id_orig,))
            aposta = cur.fetchone()
    if not aposta:
        await update.message.reply_text(f"❌ Não achei nenhuma aposta #{id_orig}.")
        return ConversationHandler.END
    ctx.user_data["duplicar_desc_id"] = id_orig
    await update.message.reply_text(
        f"🏷 Descrição atual: _{esc_md(aposta['descricao'])}_\n\nDigite a nova descrição para a cópia:",
        reply_markup=ReplyKeyboardMarkup([[CANCELAR_BTN]], resize_keyboard=True),
        parse_mode="Markdown"
    )
    return DUPLICAR_DESC_AGUARDA

async def duplicar_descricao_receber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == CANCELAR_BTN:
        ctx.user_data.pop("duplicar_desc_id", None)
        return await cancelar(update, ctx)
    id_orig = ctx.user_data.pop("duplicar_desc_id", None)
    if id_orig is None:
        return ConversationHandler.END
    nova_desc = update.message.text.strip()
    aposta, novo_id = duplicar_aposta(id_orig)
    if aposta is None:
        await update.message.reply_text(f"❌ Não achei nenhuma aposta #{id_orig}.")
        return ConversationHandler.END
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE apostas SET descricao = %s WHERE id = %s", (nova_desc, novo_id))
        conn.commit()
    aposta["descricao"] = nova_desc
    texto = _texto_resumo_aposta(id_orig, aposta, novo_id, "✅ Aposta duplicada (descrição trocada)")
    try:
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(texto.replace("*", ""))
    return ConversationHandler.END

async def marcar_multiplo_comando(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler global: 'green 12 34 56', 'red 12, 34 e 56' etc. — marca várias
    apostas de uma vez, sem precisar entrar em editar uma por uma."""
    texto = update.message.text.strip()
    m = re.match(r"(?i)^(green|red|void)\s+(.+)$", texto)
    if not m:
        return
    palavra   = m.group(1).lower()
    resultado = {"green": "ganhou", "red": "perdeu", "void": "void"}[palavra]
    ids = [int(x) for x in re.findall(r"\d+", m.group(2))]
    if not ids:
        return

    ok, nao_achadas = [], []
    lucro_soma = 0.0
    for id_alvo in ids:
        aposta = buscar_por_id(id_alvo)
        if not aposta:
            nao_achadas.append(id_alvo)
            continue
        atualizar_campo(id_alvo, "resultado", resultado)
        aposta["resultado"] = resultado
        lucro_soma += lucro_aposta(aposta)
        ok.append(id_alvo)

    emoji_conf = {"ganhou": "✅", "perdeu": "❌", "void": "↩️"}[resultado]
    label_res  = {"ganhou": "Green", "perdeu": "Red", "void": "Void"}[resultado]
    linhas = [f"{emoji_conf} *{len(ok)} apostas marcadas como {label_res}*"]
    if ok:
        linhas.append("IDs: " + ", ".join(f"#{i}" for i in ok))
        if resultado in ("ganhou", "perdeu"):
            sinal = "+" if lucro_soma >= 0 else ""
            linhas.append(f"💰 Impacto combinado: *{sinal}R$ {lucro_soma:.2f}*")
    if nao_achadas:
        linhas.append("⚠️ Não encontradas: " + ", ".join(f"#{i}" for i in nao_achadas))
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")

def inserir(a):
    unidade = get_unidade_atual()
    with conectar() as conn:
        with conn.cursor() as cur:
            novo_id = proximo_id_disponivel(cur)
            cur.execute("""
                INSERT INTO apostas (id,data,horario,descricao,odd,stake,resultado,casa,esporte,freebet,unidade)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (novo_id, a["data"],a["horario"],a["descricao"],a["odd"],a["stake"],
                  a["resultado"],a["casa"],a["esporte"],float(a.get("freebet") or 0),unidade))
            new_id = cur.fetchone()[0]
            # mantém a sequência do SERIAL sincronizada mesmo quando um buraco é preenchido,
            # pra nunca colidir se algum outro trecho de código usar o ID automático (DEFAULT)
            cur.execute("SELECT setval(pg_get_serial_sequence('apostas','id'), (SELECT MAX(id) FROM apostas))")
        conn.commit()
    return new_id

def atualizar_campo(id_aposta, campo, valor):
    if campo not in {"data","horario","descricao","odd","stake","resultado","casa","esporte","freebet","cashout_valor"}:
        return
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE apostas SET {campo}=%s WHERE id=%s", (valor, id_aposta))
        conn.commit()

def buscar_por_id(id_aposta):
    with conectar() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM apostas WHERE id=%s", (id_aposta,))
            row = cur.fetchone()
    return dict(row) if row else None

def deletar_aposta(id_aposta):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM apostas WHERE id=%s", (id_aposta,))
        conn.commit()

def get_unidade_atual():
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT valor FROM configuracoes WHERE chave='unidade_atual'")
            row = cur.fetchone()
    return float(row[0]) if row else 50.0

def get_modo_ia():
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT valor FROM configuracoes WHERE chave='modo_ia'")
            row = cur.fetchone()
    return (row[0] == "1") if row else False

def set_modo_ia(ligado: bool):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO configuracoes (chave, valor) VALUES ('modo_ia', %s)
                ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
            """, ("1" if ligado else "0",))
        conn.commit()

def migrar_casas_gemeas():
    """Atualiza no banco todas as apostas com casas antigas para os nomes com gêmeas."""
    with conectar() as conn:
        with conn.cursor() as cur:
            atualizadas = 0
            for nome_antigo, nome_novo in MAPA_CASAS_GEMEAS.items():
                cur.execute("UPDATE apostas SET casa=%s WHERE LOWER(casa)=LOWER(%s)", (nome_novo, nome_antigo))
                atualizadas += cur.rowcount
            # Normaliza variantes de múltipla
            cur.execute(
                "UPDATE apostas SET esporte='🎯 Múltipla' WHERE LOWER(esporte) LIKE '%ltipla%' AND esporte != '🎯 Múltipla'"
            )
            atualizadas += cur.rowcount
            # Rei Do Pitaco → Rei do Pitaco
            cur.execute(
                "UPDATE apostas SET casa='Rei do Pitaco' WHERE LOWER(casa)='rei do pitaco' AND casa != 'Rei do Pitaco'"
            )
            atualizadas += cur.rowcount
        conn.commit()
    return atualizadas

async def cmd_migrar_casas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = migrar_casas_gemeas()
    await update.message.reply_text(
        f"✅ *Migração concluída!*\n{n} aposta(s) atualizadas com os nomes novos das casas gêmeas.",
        parse_mode="Markdown", reply_markup=teclado_menu()
    )


def set_unidade_atual(valor):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO configuracoes (chave, valor) VALUES ('unidade_atual', %s)
                ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
            """, (str(valor),))
        conn.commit()

def inserir_gasto(data, descricao, valor):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gastos (data, descricao, valor) VALUES (%s,%s,%s) RETURNING id
            """, (data, descricao, valor))
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id

def listar_gastos():
    with conectar() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM gastos ORDER BY data ASC, id ASC")
            return [dict(r) for r in cur.fetchall()]

def total_gastos():
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(valor),0) FROM gastos")
            row = cur.fetchone()
    return float(row[0]) if row else 0.0

# ── HELPERS ───────────────────────────────────────────────────────────────────
def normalizar_casa(s):
    s = (s or "").strip()
    if not s: return "Sem casa"
    # Tenta match exato primeiro, depois case-insensitive
    if s in MAPA_CASAS_GEMEAS:
        return MAPA_CASAS_GEMEAS[s]
    s_lower = s.lower()
    for k, v in MAPA_CASAS_GEMEAS.items():
        if k.lower() == s_lower:
            return v
    return s if s in CASAS else s.title()

MAPA_ESPORTE = {
    # Futebol
    "futebol":              "⚽ Futebol",
    "soccer":               "⚽ Futebol",
    "football":             "⚽ Futebol",
    "⚽ futebol":           "⚽ Futebol",
    # Basquete
    "basquete":             "🏀 Basquete",
    "basketball":           "🏀 Basquete",
    "🏀 basquete":          "🏀 Basquete",
    # Tênis
    "tenis":                "🎾 Tênis",
    "tênis":                "🎾 Tênis",
    "tennis":               "🎾 Tênis",
    "🎾 tênis":             "🎾 Tênis",
    # Hóquei
    "hoquei":               "🏒 Hóquei",
    "hóquei":               "🏒 Hóquei",
    "ice hockey":           "🏒 Hóquei",
    "hockey":               "🏒 Hóquei",
    "🏒 hóquei":            "🏒 Hóquei",
    # Futebol Americano
    "futebol americano":    "🏈 Futebol Americano",
    "american football":    "🏈 Futebol Americano",
    "nfl":                  "🏈 Futebol Americano",
    "🏈 futebol americano": "🏈 Futebol Americano",
    # Beisebol
    "beisebol":             "⚾ Beisebol",
    "baseball":             "⚾ Beisebol",
    "⚾ beisebol":          "⚾ Beisebol",
    # MMA/Boxe
    "mma/boxe":             "🥊 MMA/Boxe",
    "mma":                  "🥊 MMA/Boxe",
    "boxe":                 "🥊 MMA/Boxe",
    "boxing":               "🥊 MMA/Boxe",
    "🥊 mma/boxe":          "🥊 MMA/Boxe",
    # Vôlei
    "volei":                "🏐 Vôlei",
    "vôlei":                "🏐 Vôlei",
    "volleyball":           "🏐 Vôlei",
    "🏐 vôlei":             "🏐 Vôlei",
    # F1
    "f1":                   "🏎️ F1",
    "formula 1":            "🏎️ F1",
    "fórmula 1":            "🏎️ F1",
    "formula1":             "🏎️ F1",
    "🏎️ f1":               "🏎️ F1",
    # Esports
    "esports":              "🎮 Esports",
    "esport":               "🎮 Esports",
    "e-sports":             "🎮 Esports",
    "e-sport":              "🎮 Esports",
    "🎮 esports":           "🎮 Esports",
    # Cricket
    "cricket":              "🏏 Cricket",
    "🏏 cricket":           "🏏 Cricket",
}

def normalizar_esporte(s):
    if not s: return ""
    s = s.strip()
    sl = s.lower()
    if "ltipla" in sl and ("esporte" in sl or "sport" in sl or sl in ("multipla","múltipla")):
        return "🎯 Múltipla"
    return MAPA_ESPORTE.get(sl, s)

# ── IA: extrair aposta de print/mensagem encaminhada ────────────────────────────
def agora_brasilia():
    """Horário de Brasília (UTC-3), calculado a partir do UTC pra não depender
    do fuso do servidor onde o bot está rodando (Render costuma rodar em UTC)."""
    return datetime.now(timezone.utc) - timedelta(hours=3)

def extrair_apostas_via_ia(legenda: str, foto_bytes):
    """Manda o print (e/ou a legenda) pra Claude e pede de volta uma LISTA de
    apostas detectadas (pode ser 1 ou várias, se o print trouxer mais de uma
    aposta separada). Se a data/horário do evento não estiverem visíveis no
    print, deixa a IA pesquisar na web pra achar o horário real do jogo/luta/
    partida (custa mais tokens + $0,01 por pesquisa, mas evita salvar apostas
    com data errada). Levanta exceção se não conseguir extrair nada."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    hoje = agora_brasilia().strftime("%Y-%m-%d")
    unidade_atual = get_unidade_atual()
    esportes_nomes = ", ".join(e.split(" ", 1)[-1] for e in ESPORTES if e != "Outro")
    prompt = (
        "Você recebe o print de uma aposta esportiva (bilhete/bet slip) de uma casa de apostas, "
        "às vezes junto com uma legenda escrita pelo apostador. Extraia os dados da(s) aposta(s).\n\n"
        "Sobre QUANTIDADE: o print pode conter MAIS DE UMA aposta separada e independente — por "
        "exemplo, várias entradas empilhadas na tela, cada uma com sua própria odd/stake, ou vários "
        "tips distintos mandados de uma vez. Nesse caso, retorne um item da lista pra CADA aposta "
        "detectada. Isso é diferente de uma aposta MÚLTIPLA (várias seleções combinadas numa odd só, "
        "tipo um bilhete 'combo') — isso é UMA aposta só: retorne um único item, com as seleções "
        "resumidas na descrição e esporte '🎯 Múltipla' se fizer sentido. Na dúvida entre as duas "
        "leituras, olhe se cada linha do print tem odd/stake PRÓPRIOS (várias apostas) ou se é um "
        "odd/stake ÚNICO cobrindo todas as seleções junto (uma múltipla só).\n\n"
        "Sobre ODD: em apostas de 'aumento' (odd boost), o número que aparece em destaque no "
        "bilhete pode ser a odd ORIGINAL, e a odd de verdade (já aumentada) aparece escrita na "
        "descrição/legenda do texto (ex: 'aumentada para 2.50', 'boost 3.00'). Quando isso "
        "acontecer, priorize e use a odd citada na descrição/legenda, não a odd em destaque no "
        "print.\n\n"
        "Sobre DATA e HORÁRIO: se não estiverem visíveis claramente no print (algumas casas não "
        "mostram, ou o print está cortado), use a ferramenta de busca na web para descobrir a data "
        "e o horário reais do jogo/partida/luta envolvido, com base nos times/atletas/competição "
        "identificados na imagem ou na legenda. É CRÍTICO acertar o horário de Brasília (UTC-3): "
        "quando pesquisar, confirme em qual fuso horário a fonte está informando o horário do jogo "
        "(a maioria dos sites internacionais usa UTC, horário local do país-sede, ou ET dos EUA) e "
        "só então converta pra Brasília subtraindo/somando as horas corretas — não assuma que o "
        "horário encontrado já está em Brasília. Se tiver qualquer dúvida sobre o fuso da fonte, "
        "prefira uma fonte que declare o fuso explicitamente. Só use a data de hoje como último "
        "recurso, se a busca não encontrar nada. Se forem várias apostas com jogos diferentes, "
        "pesquise a data/horário de cada uma separadamente.\n\n"
        "Sobre STAKE (valor apostado): PRIORIDADE — se a descrição/legenda trouxer uma indicação "
        "de tamanho de stake em '%' ou 'u' (unidades), incluindo um LIMITE (ex: '2%', '1u', "
        "'1.5u', 'até 2u', 'limite 1%'), use SEMPRE esse valor como base do cálculo — mesmo que a "
        "imagem do bilhete mostre um valor em R$ diferente. Ignore o valor em R$ do print nesse "
        "caso; ele pode ser o valor default da casa, não o que o apostador realmente pretende "
        "apostar. Só use o valor em R$ mostrado na imagem quando a descrição/legenda NÃO trouxer "
        "nenhuma indicação de %/unidades.\n"
        "Cálculo: número seguido de '%' ou 'u' NÃO é porcentagem de banca, é uma notação comum de "
        "tipster pra 'unidades de stake'. O cálculo é simples e direto, não complique: "
        "stake_em_reais = número_de_unidades × valor_da_unidade. "
        f"O valor da unidade agora é R$ {unidade_atual:.2f}. Preencha o campo 'stake' já com o "
        "valor calculado em R$ (não deixe o número de unidades bruto, nem o símbolo % ou u).\n"
        "Se a descrição/legenda trouxer um LIMITE (ex: 'até 2u', 'limite 1%', 'máximo R$100') e a "
        "quantidade de unidades/porcentagem indicada passar desse limite, NÃO use o valor "
        "indicado — use o valor do limite. Ou seja, o limite sempre vence quando for menor que o "
        "valor calculado normalmente.\n\n"
        "Depois de pesquisar (se precisar), sua resposta final deve conter SOMENTE um JSON válido "
        "(sem texto antes/depois, sem cerca de markdown) — um ARRAY, mesmo que só tenha 1 aposta, "
        "neste formato exato:\n"
        '[{"descricao": "...", "odd": 0.0, "stake": 0.0, "casa": "...", "esporte": "...", '
        '"data": "YYYY-MM-DD", "horario": "HH:MM ou null"}]\n\n'
        f"Casas conhecidas (use uma dessas se reconhecer; senão, o nome exato que aparecer no print): "
        f"{', '.join(CASAS)}\n"
        f"Esportes conhecidos (idem): {esportes_nomes}\n"
        f"Hoje é {hoje} (use como referência pra achar jogos futuros/recentes na busca).\n"
        "'descricao' é um resumo curto e direto da seleção apostada, sem casa/odd/valor, mas "
        "SEMPRE incluindo o jogo/confronto (times, atletas ou lutadores envolvidos) — ex: "
        "'menos de 5.5 cartões Vasco da Gama x Santos', '1.5 bases Colt Emerson'. Se o mercado for "
        "sobre um dos lados específicos (ex: handicap, um jogador específico), cite o nome dele e, "
        "quando fizer sentido pro contexto, o adversário/confronto também.\n"
        f"Legenda enviada junto pelo apostador (pode trazer dados extras, ou estar vazia): "
        f"{legenda or '(vazia)'}"
    )

    content = [{"type": "text", "text": prompt}]
    if foto_bytes:
        content.insert(0, {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(foto_bytes).decode("utf-8"),
            },
        })

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
        messages=[{"role": "user", "content": content}],
    )
    blocos_texto = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    texto = blocos_texto[-1].strip() if blocos_texto else ""
    texto = texto.strip("`").strip()
    if texto.lower().startswith("json"):
        texto = texto[4:].strip()
    m = re.search(r"\[.*\]", texto, re.DOTALL)
    if m:
        texto = m.group(0)
    lista = json.loads(texto)
    if isinstance(lista, dict):
        lista = [lista]  # segurança, caso a IA devolva um objeto solto em vez de array
    if not lista:
        raise ValueError("não detectei nenhuma aposta nesse print.")

    apostas = []
    for i, dados in enumerate(lista, start=1):
        dados["casa"]    = normalizar_casa(str(dados.get("casa") or "Outra"))
        dados["esporte"] = normalizar_esporte(str(dados.get("esporte") or "Outro"))
        try:
            dados["odd"] = float(dados["odd"])
        except (TypeError, ValueError):
            raise ValueError(f"não consegui identificar a *odd* da aposta {i} do print.")
        try:
            dados["stake"] = float(dados["stake"])
        except (TypeError, ValueError):
            raise ValueError(
                f"não consegui identificar o *valor apostado* da aposta {i} do print. Se o print "
                "usa uma notação tipo \"2%\" ou \"2u\" (unidades) sem valor em R$ visível, confere "
                "se a legenda ou a imagem deixam isso claro — senão lança essa aposta manualmente."
            )
        dados["descricao"] = str(dados.get("descricao") or "").strip() or "(sem descrição)"
        if not dados.get("data"):
            dados["data"] = hoje
        if not dados.get("horario"):
            dados["horario"] = ""
        apostas.append(dados)
    return apostas

async def receber_encaminhada(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler de nível superior (fora de qualquer conversa): quando chega uma
    foto (print de aposta encaminhado), manda pra IA extrair os dados e pede
    confirmação por botão antes de salvar de verdade."""
    msg = update.message
    if not msg or not msg.photo:
        return
    if not get_modo_ia():
        return  # Modo IA desligado: ignora fotos silenciosamente
    legenda = msg.caption or ""

    tg_file = await msg.photo[-1].get_file()
    foto_bytes = bytes(await tg_file.download_as_bytearray())

    aviso = await msg.reply_text("Lendo a aposta...")
    try:
        apostas = extrair_apostas_via_ia(legenda, foto_bytes)
    except Exception as e:
        logging.exception("Falha ao extrair aposta via IA")
        texto_erro = f"❌ Não consegui ler essa aposta automaticamente: {e}"
        try:
            await aviso.edit_text(texto_erro, parse_mode="Markdown")
        except Exception:
            await aviso.edit_text(texto_erro.replace("*", ""))
        return

    ctx.user_data["ia_pendente"] = apostas

    def _bloco(dados, numero=None):
        data_fmt = datetime.strptime(dados["data"], "%Y-%m-%d").strftime("%d/%m/%Y")
        hora_txt = f" às {dados['horario']}" if dados.get("horario") else ""
        prefixo = f"*Aposta {numero}*\n" if numero else ""
        return (
            f"{prefixo}📅 {data_fmt}{hora_txt}\n🏅 {esc_md(dados['esporte'])}\n"
            f"🏷 {esc_md(dados['descricao'])}\n🔢 Odd: {dados['odd']:g}\n"
            f"💰 Stake: R$ {dados['stake']:.2f}\n🏦 Casa: {esc_md(dados['casa'])}"
        )

    if len(apostas) == 1:
        titulo = "*Aposta detectada:*\n\n"
        corpo = _bloco(apostas[0])
        botao_salvar = "✅ Salvar"
    else:
        titulo = f"*{len(apostas)} apostas detectadas nesse print:*\n\n"
        corpo = "\n\n".join(_bloco(a, i) for i, a in enumerate(apostas, start=1))
        botao_salvar = f"✅ Salvar todas ({len(apostas)})"

    preview = f"{titulo}{corpo}\n\nConfirma?"
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton(botao_salvar, callback_data="ia_salvar"),
        InlineKeyboardButton("❌ Descartar", callback_data="ia_descartar"),
    ]])
    try:
        await aviso.edit_text(preview, parse_mode="Markdown", reply_markup=teclado)
    except Exception:
        await aviso.edit_text(preview.replace("*", ""), reply_markup=teclado)

async def ia_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Processa o clique em Salvar/Descartar do preview gerado pela IA.
    Salva uma ou várias apostas de uma vez, dependendo do que foi detectado."""
    query = update.callback_query
    await query.answer()
    apostas = ctx.user_data.pop("ia_pendente", None)
    if query.data == "ia_descartar" or not apostas:
        await query.edit_message_text("❌ Descartado.")
        return

    salvas, erros = [], []
    for dados in apostas:
        dados["resultado"] = "pendente"
        dados["freebet"] = 0
        try:
            salvas.append(inserir(dados))
        except Exception as e:
            logging.exception("Falha ao salvar aposta extraída via IA")
            erros.append(str(e))

    if len(apostas) == 1:
        if salvas:
            await query.edit_message_text(f"✅ Aposta #{salvas[0]} salva a partir do print!")
        else:
            await query.edit_message_text(f"❌ Erro ao salvar: {erros[0]}")
        return

    partes = []
    if salvas:
        partes.append("✅ Salvas: " + ", ".join(f"#{i}" for i in salvas))
    if erros:
        partes.append(f"❌ {len(erros)} falharam: " + "; ".join(erros))
    await query.edit_message_text("\n".join(partes))

def stake_real(a):
    """Retorna o stake efetivamente em risco (dinheiro próprio), excluindo o
    valor coberto por freebet. Usado para 'stake em curso' e 'capital atual',
    pra freebet não contar como capital próprio parado numa aposta."""
    stake = float(a["stake"])
    try:
        freebet = float(a.get("freebet") or 0)
    except Exception:
        freebet = stake if str(a.get("freebet","")).strip().lower() == "sim" else 0.0
    freebet = min(freebet, stake)
    return stake - freebet

def lucro_aposta(a):
    stake   = float(a["stake"])
    odd     = float(a["odd"])
    try:
        freebet = float(a.get("freebet") or 0)
    except:
        freebet = stake if str(a.get("freebet","")).strip().lower() == "sim" else 0.0
    freebet = min(freebet, stake)
    if a["resultado"] == "ganhou":
        return stake * (odd - 1)
    if a["resultado"] == "perdeu":
        return -(stake - freebet)
    if a["resultado"] == "void":
        cashout_valor = float(a.get("cashout_valor") or 0)
        if cashout_valor > 0:
            return cashout_valor - stake  # lucro real do cashout
    return 0.0

def eh_cashout(a):
    """Retorna True se a aposta void foi um cashout (tem cashout_valor > 0)."""
    return a["resultado"] == "void" and float(a.get("cashout_valor") or 0) > 0

def pico_e_fundo(apostas):
    """Percorre as apostas em ordem cronológica somando o lucro acumulado e
    retorna (pico, fundo): o máximo e o mínimo que esse acumulado chegou a
    atingir em algum momento (começando de 0). Ex: se o saldo do período
    nunca ficou negativo, fundo = 0; se nunca ficou positivo, pico = 0.
    Isso é diferente da aposta de maior/menor lucro isolada."""
    ordenadas = sorted(apostas, key=lambda a: (str(a["data"]), a.get("horario") or "", int(a["id"])))
    acumulado = 0.0
    pico = 0.0
    fundo = 0.0
    for a in ordenadas:
        acumulado += lucro_aposta(a)
        pico  = max(pico, acumulado)
        fundo = min(fundo, acumulado)
    return pico, fundo

LIMITE_TELEGRAM = 3500  # margem de segurança abaixo do limite real (4096)

def paginar_linhas(linhas, limite=LIMITE_TELEGRAM):
    """Agrupa uma lista de linhas (strings) em blocos que respeitam o limite
    de caracteres do Telegram, sem cortar uma linha ao meio."""
    paginas = []
    atual = []
    tamanho = 0
    for linha in linhas:
        tam_linha = len(linha) + 1  # +1 do \n
        if atual and tamanho + tam_linha > limite:
            paginas.append("\n".join(atual))
            atual = []
            tamanho = 0
        atual.append(linha)
        tamanho += tam_linha
    if atual:
        paginas.append("\n".join(atual))
    return paginas

async def enviar_paginado(update, linhas, reply_markup=None, parse_mode="Markdown"):
    """Envia uma lista de linhas como uma ou mais mensagens, respeitando o
    limite de caracteres do Telegram. O teclado só vai na última mensagem."""
    paginas = paginar_linhas(linhas)
    total = len(paginas)
    for i, pagina in enumerate(paginas):
        prefixo = f"📄 _Página {i+1}/{total}_\n\n" if total > 1 else ""
        await update.message.reply_text(
            prefixo + pagina,
            reply_markup=reply_markup if i == total - 1 else None,
            parse_mode=parse_mode
        )

async def voltar_menu(update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("O que mais?", reply_markup=teclado_menu())
    return ConversationHandler.END

async def cancelar(update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelado.", reply_markup=teclado_menu())
    return ConversationHandler.END

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 *Gestor de Apostas*\n\nEscolha uma opção:",
        reply_markup=teclado_menu(), parse_mode="Markdown"
    )

# ── MENU PRINCIPAL ────────────────────────────────────────────────────────────
async def menu_botao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text

    if txt == "⏳ Ver pendentes":    return await ver_pendentes(update, ctx)
    if txt.startswith("Modo IA"):
        novo_estado = not get_modo_ia()
        set_modo_ia(novo_estado)
        aviso = (
            "*Modo IA ativado.*\nAgora é só encaminhar o print da aposta que eu leio e te "
            "mostro pra confirmar antes de salvar."
            if novo_estado else
            "*Modo IA desativado.*\nFotos encaminhadas vão ser ignoradas até você ligar de novo."
        )
        await update.message.reply_text(aviso, reply_markup=teclado_menu(), parse_mode="Markdown")
        return

# ── NOVA APOSTA ───────────────────────────────────────────────────────────────
async def nova_aposta_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hoje = datetime.now().strftime("%d/%m/%Y")
    await update.message.reply_text(
        f"📅 *Data do jogo*\nHoje é *{hoje}* — mande *0* para confirmar ou digite outra:",
        reply_markup=teclado_cancelar(), parse_mode="Markdown"
    )
    return DATA

async def receber_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    if raw == VOLTAR_BTN: return await nova_aposta_inicio(update, ctx)
    if raw == "0":
        ctx.user_data["data"] = datetime.now().strftime("%Y-%m-%d")
    else:
        data = normalizar_data(raw)
        if not data:
            await update.message.reply_text("❌ Data inválida:")
            return DATA
        ctx.user_data["data"] = data
    agora = datetime.now().strftime("%H:%M")
    await update.message.reply_text(
        f"⏰ *Horário do jogo*\nAgora são *{agora}* — mande *0* para usar ou digite outro (HH:MM):",
        reply_markup=teclado_cancelar_voltar(), parse_mode="Markdown"
    )
    return HORARIO

def normalizar_data(raw: str):
    """Converte entrada flexível de data para YYYY-MM-DD.
    Aceita: 20 → dia 20 do mês atual | 20/05 → 20/05 do ano atual | 20/05/2026 ou 20/05/26
    """
    agora = datetime.now()
    raw = raw.strip()
    # Só dia: 20 → dia 20 do mês e ano atuais
    try:
        dia = int(raw)
        if 1 <= dia <= 31:
            try:
                return datetime(agora.year, agora.month, dia).strftime("%Y-%m-%d")
            except ValueError:
                return None
    except ValueError:
        pass
    # DD/MM → ano atual
    try:
        d = datetime.strptime(raw, "%d/%m")
        return datetime(agora.year, d.month, d.day).strftime("%Y-%m-%d")
    except ValueError:
        pass
    # DD/MM/AAAA ou DD/MM/AA
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None

async def receber_horario(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    if raw == VOLTAR_BTN:
        hoje = datetime.now().strftime("%d/%m/%Y")
        await update.message.reply_text(
            f"📅 *Data do jogo*\nHoje é *{hoje}* — mande *0* para confirmar ou digite outra:",
            reply_markup=teclado_cancelar(), parse_mode="Markdown"
        )
        return DATA
    if raw == "0":
        ctx.user_data["horario"] = datetime.now().strftime("%H:%M")
    else:
        horario = normalizar_horario(raw)
        if not horario:
            await update.message.reply_text("❌ Horário inválido:")
            return HORARIO
        ctx.user_data["horario"] = horario
    await update.message.reply_text("🏷 *Descrição:*", reply_markup=teclado_cancelar_voltar(), parse_mode="Markdown")
    return DESCRICAO

def normalizar_horario(raw: str):
    """Converte entrada flexível de horário para HH:MM.
    Aceita: 15 → 15:00 | 15:3 → 15:30 | 153 → 15:30 | 1530 → 15:30 | 15:30 → 15:30
    """
    import re
    raw = raw.strip().replace(".", ":").replace(",", ":")
    # Formato HH:MM ou H:MM
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", raw)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        # 15:3 → 15:30
        if m.group(2) and len(m.group(2)) == 1:
            mi = int(m.group(2)) * 10
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None
    # Só hora: 15 → 15:00
    m = re.fullmatch(r"(\d{1,2})", raw)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return f"{h:02d}:00"
        return None
    # Formato compacto: 1530 → 15:30, 153 → 15:30, 900 → 09:00
    m = re.fullmatch(r"(\d{3,4})", raw)
    if m:
        s = m.group(1).zfill(4)
        h, mi = int(s[:2]), int(s[2:])
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None
    return None

async def receber_descricao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    if raw == VOLTAR_BTN:
        agora = datetime.now().strftime("%H:%M")
        await update.message.reply_text(
            f"⏰ *Horário do jogo*\nAgora são *{agora}* — mande *0* para usar ou digite outro (HH:MM):",
            reply_markup=teclado_cancelar_voltar(), parse_mode="Markdown"
        )
        return HORARIO
    ctx.user_data["descricao"] = raw
    await update.message.reply_text("🔢 *Odd:*", reply_markup=teclado_cancelar_voltar(), parse_mode="Markdown")
    return ODD

def teclado_stake(unidade):
    """Gera teclado com sugestões de stake baseadas na unidade atual."""
    u = float(unidade)
    sugestoes = [
        f"1u = R${u:.2f}",
        f"0.75u = R${u*0.75:.2f}",
        f"0.5u = R${u*0.5:.2f}",
        f"0.25u = R${u*0.25:.2f}",
    ]
    linhas = [[s] for s in sugestoes] + [[VOLTAR_BTN, CANCELAR_BTN]]
    return ReplyKeyboardMarkup(linhas, resize_keyboard=True, one_time_keyboard=True)

async def receber_odd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    if raw == VOLTAR_BTN:
        await update.message.reply_text("🏷 *Descrição:*", reply_markup=teclado_cancelar_voltar(), parse_mode="Markdown")
        return DESCRICAO
    try:
        ctx.user_data["odd"] = float(raw.replace(",", "."))
        unidade = get_unidade_atual()
        await update.message.reply_text(
            f"💰 *Stake (R$):*\nUnidade atual: R$ {unidade:.2f}",
            reply_markup=teclado_stake(unidade), parse_mode="Markdown"
        )
        return STAKE
    except ValueError:
        await update.message.reply_text("❌ Odd inválida:")
        return ODD

async def receber_stake(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    if raw == VOLTAR_BTN:
        unidade = get_unidade_atual()
        await update.message.reply_text(
            f"🔢 *Odd:*", reply_markup=teclado_cancelar_voltar(), parse_mode="Markdown"
        )
        return ODD
    # Parse sugestão tipo "1u = R$80.00" ou "0.75u = R$60.00"
    import re as _re
    m_sug = _re.match(r"[\d.]+u\s*=\s*R\$([\d.,]+)", raw)
    if m_sug:
        raw = m_sug.group(1)
    try:
        ctx.user_data["stake"] = float(raw.replace(",", "."))
        teclado_esp = [[e] for e in get_esportes_ordenados()] + [[VOLTAR_BTN, CANCELAR_BTN]]
        await update.message.reply_text("🏅 *Esporte:*",
            reply_markup=ReplyKeyboardMarkup(teclado_esp, resize_keyboard=True, one_time_keyboard=True),
            parse_mode="Markdown")
        return ESPORTE
    except ValueError:
        await update.message.reply_text("❌ Stake inválido:")
        return STAKE

async def receber_esporte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    if raw == VOLTAR_BTN:
        unidade = get_unidade_atual()
        await update.message.reply_text(
            f"💰 *Stake (R$):*\nUnidade atual: R$ {unidade:.2f}",
            reply_markup=teclado_stake(unidade), parse_mode="Markdown"
        )
        return STAKE
    if ctx.user_data.pop("aguardando_esporte_custom", False):
        ctx.user_data["esporte"] = normalizar_esporte(raw)
    elif raw == "Outro":
        await update.message.reply_text("Digite o nome do esporte:", reply_markup=teclado_cancelar())
        ctx.user_data["aguardando_esporte_custom"] = True
        return ESPORTE
    else:
        ctx.user_data["esporte"] = normalizar_esporte(raw)
    teclado_casa = [[c] for c in get_casas_ordenadas()] + [[VOLTAR_BTN, CANCELAR_BTN]]
    await update.message.reply_text("🏦 *Casa de aposta:*",
        reply_markup=ReplyKeyboardMarkup(teclado_casa, resize_keyboard=True, one_time_keyboard=True),
        parse_mode="Markdown")
    return CASA

async def receber_casa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    casa = update.message.text.strip()
    if casa == CANCELAR_BTN: return await cancelar(update, ctx)
    if casa == VOLTAR_BTN:
        teclado_esp = [[e] for e in get_esportes_ordenados()] + [[VOLTAR_BTN, CANCELAR_BTN]]
        await update.message.reply_text("🏅 *Esporte:*",
            reply_markup=ReplyKeyboardMarkup(teclado_esp, resize_keyboard=True, one_time_keyboard=True),
            parse_mode="Markdown")
        return ESPORTE
    if casa == "Outra":
        await update.message.reply_text("Digite o nome da casa:", reply_markup=teclado_cancelar())
        ctx.user_data["aguardando_casa_custom"] = True
        return CASA
    ctx.user_data.pop("aguardando_casa_custom", False)
    casas_validas = get_casas_ordenadas()
    ctx.user_data["casa"] = casa.strip().title() if casa not in casas_validas else casa

    new_id = inserir({
        "data": ctx.user_data["data"], "horario": ctx.user_data.get("horario",""),
        "descricao": ctx.user_data["descricao"], "odd": ctx.user_data["odd"],
        "stake": ctx.user_data["stake"], "resultado": "pendente",
        "casa": ctx.user_data["casa"], "esporte": ctx.user_data.get("esporte",""),
    })
    data_fmt = datetime.strptime(ctx.user_data["data"], "%Y-%m-%d").strftime("%d/%m/%Y")
    hora_txt = f" às {ctx.user_data['horario']}" if ctx.user_data.get("horario") else ""
    await update.message.reply_text(
        f"✅ *Aposta #{new_id} salva!*\n\n"
        f"📅 {data_fmt}{hora_txt}\n🏅 {esc_md(ctx.user_data.get('esporte',''))}\n"
        f"🏷 {esc_md(ctx.user_data['descricao'])}\n🔢 Odd: {float(ctx.user_data['odd']):g}\n"
        f"💰 Stake: R$ {float(ctx.user_data['stake']):.2f}\n🏦 Casa: {esc_md(ctx.user_data['casa'])}",
        parse_mode="Markdown"
    )
    return await voltar_menu(update, ctx)

# ── VER PENDENTES ─────────────────────────────────────────────────────────────
async def ver_pendentes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas   = carregar()
    pendentes = sorted([a for a in apostas if a["resultado"] == "pendente"],
                       key=lambda a: (str(a["data"]), a.get("horario") or ""))
    if not pendentes:
        await update.message.reply_text("✅ Nenhuma aposta pendente!")
        return
    linhas = [f"⏳ *{len(pendentes)} apostas pendentes:*\n"]
    for a in pendentes:
        data_fmt = a["data"].strftime("%d/%m/%Y") if hasattr(a["data"],"strftime") else str(a["data"])[:10]
        hora = f" {a.get('horario','')}" if a.get("horario") else ""
        linhas.append(f"*#{a['id']}* {data_fmt}{hora} | odd {float(a['odd']):g} | R${float(a['stake']):.0f} | {esc_md(a.get('casa',''))}\n_{esc_md(a['descricao'])}_\n")
    await enviar_paginado(update, linhas, parse_mode="Markdown")
NOMES_MES_PT = {
    "janeiro":1,"jan":1,"fevereiro":2,"fev":2,"março":3,"mar":3,"abril":4,"abr":4,
    "maio":5,"mai":5,"junho":6,"jun":6,"julho":7,"jul":7,"agosto":8,"ago":8,
    "setembro":9,"set":9,"outubro":10,"out":10,"novembro":11,"nov":11,"dezembro":12,"dez":12,
}

async def resultados(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    if not res:
        await update.message.reply_text("Nenhuma aposta resolvida ainda.")
        return
    lucro_total = sum(lucro_aposta(a) for a in res)
    stake_total = sum(float(a["stake"]) for a in res)
    vitorias    = sum(1 for a in res if a["resultado"] == "ganhou")
    roi         = lucro_total / stake_total if stake_total else 0
    progressao  = lucro_total / BANCA_INICIAL
    sinal       = "+" if lucro_total >= 0 else ""
    emoji       = "📈" if lucro_total >= 0 else "📉"
    pendentes_ap = [a for a in apostas if a["resultado"] == "pendente"]
    stake_curso  = sum(stake_real(a) for a in pendentes_ap)
    possivel_ganho = sum(float(a["stake"]) * (float(a["odd"]) - 1) for a in pendentes_ap)
    unidade_atual = get_unidade_atual()
    # Lucro em unidades: usa a unidade registrada em cada aposta (histórico fiel)
    lucro_unidades = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in res)
    lucro_medio    = lucro_total / len(res) if res else 0
    capital_atual  = BANCA_INICIAL + lucro_total - stake_curso - total_gastos()

    agora = datetime.now()
    hoje_date = agora.date()

    # Mês atual
    res_mes = [a for a in res if
               (a["data"] if isinstance(a["data"], type(hoje_date)) else
                datetime.strptime(str(a["data"])[:10], "%Y-%m-%d").date()
               ).replace(day=1) == hoje_date.replace(day=1)]
    lucro_mes   = sum(lucro_aposta(a) for a in res_mes)
    lucro_mes_u = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in res_mes)
    sinal_m = "+" if lucro_mes >= 0 else ""
    emoji_m = "🟢" if lucro_mes >= 0 else "🔴"

    # Semana atual (domingo a sábado)
    dias_desde_domingo = (agora.weekday() + 1) % 7
    inicio_semana_date = (agora - timedelta(days=dias_desde_domingo)).date()
    res_semana = [a for a in res if
                  (a["data"] if isinstance(a["data"], type(hoje_date)) else
                   datetime.strptime(str(a["data"])[:10], "%Y-%m-%d").date()
                  ) >= inicio_semana_date]
    lucro_semana   = sum(lucro_aposta(a) for a in res_semana)
    lucro_semana_u = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in res_semana)
    sinal_s = "+" if lucro_semana >= 0 else ""
    emoji_s = "🟢" if lucro_semana >= 0 else "🔴"

    NOMES_MES_LABEL = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    nome_mes_atual  = NOMES_MES_LABEL[agora.month]

    await update.message.reply_text(
        f"{emoji} *Resultados Gerais*\n\n"
        f"💰 Lucro Total: *{sinal}R$ {lucro_total:.2f}* ({lucro_unidades:+.2f}u)\n"
        f"📊 ROI: *{roi:+.1%}* | Progressão: *{progressao:+.2%}*\n"
        f"🏆 {vitorias}V / {len(res)-vitorias}D\n\n"
        f"{emoji_m} {nome_mes_atual}: *{sinal_m}R$ {lucro_mes:.2f}* ({lucro_mes_u:+.2f}u) | {len(res_mes)} ap\n"
        f"{emoji_s} Semana: *{sinal_s}R$ {lucro_semana:.2f}* ({lucro_semana_u:+.2f}u) | {len(res_semana)} ap",
        reply_markup=teclado_resultados(), parse_mode="Markdown"
    )
    return RESULTADOS_MENU

async def estatisticas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mostra métricas complementares que não aparecem em 'Resultados Gerais':
    stake em curso, possível ganho, média/aposta, capital atual, aposta de
    maior ganho/perda, melhor/pior dia e odd média."""
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    if not res:
        await update.message.reply_text("Nenhuma aposta resolvida ainda.", reply_markup=teclado_resultados())
        return RESULTADOS_MENU

    lucro_total = sum(lucro_aposta(a) for a in res)
    lucro_medio = lucro_total / len(res) if res else 0
    pendentes_ap   = [a for a in apostas if a["resultado"] == "pendente"]
    stake_curso    = sum(stake_real(a) for a in pendentes_ap)
    possivel_ganho = sum(float(a["stake"]) * (float(a["odd"]) - 1) for a in pendentes_ap)
    capital_atual  = BANCA_INICIAL + lucro_total - stake_curso - total_gastos()
    # Odd média ponderada: quanto eu ficaria pra cada R$1 apostado se tudo
    # desse green (retorno potencial total / stake total), não a média
    # simples das odds.
    stake_total_res = sum(float(a["stake"]) for a in res)
    retorno_total_res = sum(float(a["stake"]) * float(a["odd"]) for a in res)
    odd_media      = retorno_total_res / stake_total_res if stake_total_res else 0

    melhor_aposta = max(res, key=lambda a: lucro_aposta(a))
    pior_aposta   = min(res, key=lambda a: lucro_aposta(a))
    ganhos        = [a for a in res if a["resultado"] == "ganhou"]
    maior_odd_ok  = max(ganhos, key=lambda a: float(a["odd"])) if ganhos else None

    def _data_fmt(a):
        return a["data"].strftime("%d/%m/%Y") if hasattr(a["data"], "strftime") else str(a["data"])[:10]

    # Agrupa lucro por dia pra achar melhor/pior dia
    lucro_por_dia = {}
    for a in res:
        d = _data_fmt(a)
        lucro_por_dia[d] = lucro_por_dia.get(d, 0) + lucro_aposta(a)
    melhor_dia, lucro_melhor_dia = max(lucro_por_dia.items(), key=lambda kv: kv[1])
    pior_dia, lucro_pior_dia     = min(lucro_por_dia.items(), key=lambda kv: kv[1])

    linhas = (
        f"📊 *Estatísticas*\n\n"
        f"📐 Média/aposta: *R$ {lucro_medio:.2f}*\n"
        f"🏦 Capital Atual: *R$ {capital_atual:.2f}*\n"
        f"⏳ Stake em curso: *R$ {stake_curso:.2f}* ({len(pendentes_ap)} ap) | Possível ganho: *+R$ {possivel_ganho:.2f}*\n"
        f"🔢 Odd média: *{odd_media:.2f}*\n\n"
        f"🔝 Maior ganho: *#{melhor_aposta['id']}* {_data_fmt(melhor_aposta)} → +R$ {lucro_aposta(melhor_aposta):.2f}\n"
        f"_{esc_md(melhor_aposta['descricao'])}_\n\n"
        f"🔻 Maior perda: *#{pior_aposta['id']}* {_data_fmt(pior_aposta)} → R$ {lucro_aposta(pior_aposta):.2f}\n"
        f"_{esc_md(pior_aposta['descricao'])}_\n\n"
        f"🟢 Melhor dia: *{melhor_dia}* → +R$ {lucro_melhor_dia:.2f}\n"
        f"🔴 Pior dia: *{pior_dia}* → R$ {lucro_pior_dia:.2f}"
    )
    if maior_odd_ok:
        linhas += (
            f"\n\n🎯 Maior odd acertada: *#{maior_odd_ok['id']}* {_data_fmt(maior_odd_ok)} → "
            f"odd {float(maior_odd_ok['odd']):g}\n_{esc_md(maior_odd_ok['descricao'])}_"
        )
    await update.message.reply_text(linhas, reply_markup=teclado_resultados(), parse_mode="Markdown")
    return RESULTADOS_MENU

async def resultados_por_casa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    casas   = {}
    for a in res:
        casa = normalizar_casa(a.get("casa"))
        if casa not in casas:
            casas[casa] = {"ap":0,"g":0,"stake":0.0,"lucro":0.0}
        c = casas[casa]; c["ap"] += 1; c["stake"] += float(a["stake"])
        c["lucro"] += lucro_aposta(a)
        if a["resultado"] == "ganhou": c["g"] += 1
    ordenadas = sorted(casas.items(), key=lambda x: x[1]["lucro"], reverse=True)
    linhas    = ["🏦 *Por Casa:*\n"]
    for nome, c in ordenadas:
        roi   = c["lucro"]/c["stake"] if c["stake"] else 0
        emoji = "🟢" if c["lucro"] >= 0 else "🔴"
        sinal = "+" if c["lucro"] >= 0 else ""
        linhas.append(f"{emoji} *{nome}*\n  {c['ap']} ap | {c['g']}V/{c['ap']-c['g']}D | {sinal}R$ {c['lucro']:.2f} | ROI: {roi:+.1%}\n")
    linhas.append("\nDigite uma data (DD/MM) ou 🔙 Voltar:")
    await enviar_paginado(update, linhas, reply_markup=teclado_resultados(), parse_mode="Markdown")

async def resultados_por_mes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    if not res:
        await update.message.reply_text("Nenhuma aposta resolvida ainda.", reply_markup=teclado_resultados())
        return

    from collections import defaultdict
    por_mes = defaultdict(list)
    for a in res:
        data_obj = a["data"] if hasattr(a["data"], "strftime") else datetime.strptime(str(a["data"])[:10], "%Y-%m-%d")
        chave = data_obj.strftime("%Y-%m")
        por_mes[chave].append(a)

    NOMES_MES = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    linhas = ["📅 *Resultados por Mês:*\n"]
    acum = 0
    for chave in sorted(por_mes.keys()):
        ap  = por_mes[chave]
        ano, mes = int(chave[:4]), int(chave[5:])
        lucro_m = sum(lucro_aposta(a) for a in ap)
        stake_m = sum(float(a["stake"]) for a in ap)
        g_m     = sum(1 for a in ap if a["resultado"] == "ganhou")
        roi_m   = lucro_m / stake_m if stake_m else 0
        acum   += lucro_m
        emoji_m = "🟢" if lucro_m >= 0 else "🔴"
        sinal_m = "+" if lucro_m >= 0 else ""
        # Lucro em unidades usando a unidade de cada aposta individualmente
        lucro_u = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in ap)
        linhas.append(
            f"{emoji_m} *{NOMES_MES[mes]}/{ano}*\n"
            f"  {len(ap)} ap | {g_m}V/{len(ap)-g_m}D | ROI {roi_m:+.1%}\n"
            f"  Lucro: *{sinal_m}R$ {lucro_m:.2f}* ({lucro_u:+.2f}u)\n"
        )
    sinal_ac = "+" if acum >= 0 else ""
    linhas.append(f"📊 *Acumulado: {sinal_ac}R$ {acum:.2f}*")
    linhas.append("\nDigite uma data (DD/MM), 🏦 *Por Casa* ou 🔙 *Voltar*:")
    await enviar_paginado(update, linhas, reply_markup=teclado_resultados(), parse_mode="Markdown")

async def resultados_por_mes_especifico(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mes: int, ano: int = None):
    """Mostra resultado de um mês específico (ex: 'Janeiro', 'Abr/2025')."""
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    agora   = datetime.now()
    NOMES_MES = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                 "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
    # Se não especificou ano: tenta ano atual; se vazio, tenta ano anterior
    anos_busca = [agora.year] if ano else [agora.year, agora.year - 1]
    if ano:
        anos_busca = [ano]
    for ano_b in anos_busca:
        do_mes = [a for a in res if
                  str(a["data"])[:7] == f"{ano_b}-{mes:02d}"]
        if do_mes:
            unidade_atual = get_unidade_atual()
            lucro_m   = sum(lucro_aposta(a) for a in do_mes)
            stake_m   = sum(float(a["stake"]) for a in do_mes)
            g_m       = sum(1 for a in do_mes if a["resultado"] == "ganhou")
            roi_m     = lucro_m / stake_m if stake_m else 0
            lucro_u_m = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in do_mes)
            emoji_m   = "🟢" if lucro_m >= 0 else "🔴"
            sinal_m   = "+" if lucro_m >= 0 else ""
            await update.message.reply_text(
                f"{emoji_m} *{NOMES_MES[mes]}/{ano_b}*\n\n"
                f"💰 Lucro: *{sinal_m}R$ {lucro_m:.2f}* ({lucro_u_m:+.2f}u)\n"
                f"📊 ROI: *{roi_m:+.1%}*\n"
                f"🏆 {g_m}V / {len(do_mes)-g_m}D | {len(do_mes)} apostas",
                reply_markup=teclado_resultados(), parse_mode="Markdown"
            )
            return
    label_ano = f"/{ano}" if ano else ""
    await update.message.reply_text(
        f"Nenhuma aposta resolvida em {NOMES_MES[mes]}{label_ano}.",
        reply_markup=teclado_resultados()
    )

# ── SEMANA (Resultados > Semana > escolhe mês > escolhe semana) ──────────────
NOMES_MES_ABREV = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]

async def resultados_semana_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    if not res:
        await update.message.reply_text("Nenhuma aposta resolvida ainda.", reply_markup=teclado_resultados())
        return RESULTADOS_MENU

    chaves = sorted({str(a["data"])[:7] for a in res}, reverse=True)  # "YYYY-MM", mais recente primeiro
    ctx.user_data["semana_meses"] = {}
    linhas_botoes = []
    for chave in chaves:
        ano, mes = int(chave[:4]), int(chave[5:])
        label = f"{NOMES_MES_ABREV[mes]}/{ano}"
        ctx.user_data["semana_meses"][label] = (ano, mes)
        linhas_botoes.append(label)

    teclado = [linhas_botoes[i:i+2] for i in range(0, len(linhas_botoes), 2)] + [["🔙 Voltar"]]
    await update.message.reply_text(
        "📆 *Semana* — escolha o mês:",
        reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return SEMANA_MES

async def resultados_semana_mes_escolhido(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == "🔙 Voltar":
        await update.message.reply_text("Voltando…", reply_markup=teclado_resultados())
        return RESULTADOS_MENU

    mes_info = ctx.user_data.get("semana_meses", {}).get(txt)
    if not mes_info:
        await update.message.reply_text("❓ Escolha um dos meses na lista.")
        return SEMANA_MES
    ano, mes = mes_info

    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    do_mes  = [a for a in res if str(a["data"])[:7] == f"{ano}-{mes:02d}"]

    from collections import defaultdict
    por_semana = defaultdict(list)
    for a in do_mes:
        d = a["data"] if hasattr(a["data"], "strftime") else datetime.strptime(str(a["data"])[:10], "%Y-%m-%d")
        chave_sem = d.isocalendar()[:2]  # (ano_iso, semana_iso)
        por_semana[chave_sem].append(a)

    ctx.user_data["semana_mes_atual"] = (ano, mes)
    ctx.user_data["semana_opcoes"] = {}
    linhas_botoes = []
    for chave_sem in sorted(por_semana.keys()):
        ano_iso, semana_iso = chave_sem
        seg = datetime.fromisocalendar(ano_iso, semana_iso, 1)
        dom = datetime.fromisocalendar(ano_iso, semana_iso, 7)
        label = f"Semana {semana_iso} ({seg.strftime('%d/%m')}–{dom.strftime('%d/%m')})"
        ctx.user_data["semana_opcoes"][label] = por_semana[chave_sem]
        linhas_botoes.append([label])

    teclado = linhas_botoes + [["🔙 Voltar"]]
    await update.message.reply_text(
        f"📆 *{NOMES_MES_ABREV[mes]}/{ano}* — escolha a semana:",
        reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return SEMANA_SEMANA

async def resultados_semana_escolhida(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == "🔙 Voltar":
        return await resultados_semana_inicio(update, ctx)

    ap_semana = ctx.user_data.get("semana_opcoes", {}).get(txt)
    if ap_semana is None:
        await update.message.reply_text("❓ Escolha uma das semanas na lista.")
        return SEMANA_SEMANA

    lucro_s   = sum(lucro_aposta(a) for a in ap_semana)
    stake_s   = sum(float(a["stake"]) for a in ap_semana)
    g_s       = sum(1 for a in ap_semana if a["resultado"] == "ganhou")
    roi_s     = lucro_s / stake_s if stake_s else 0
    lucro_u_s = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in ap_semana)
    media_s   = lucro_s / len(ap_semana) if ap_semana else 0
    pico_s, fundo_s = pico_e_fundo(ap_semana)
    emoji_s   = "🟢" if lucro_s >= 0 else "🔴"
    sinal_s   = "+" if lucro_s >= 0 else ""

    await update.message.reply_text(
        f"{emoji_s} *{txt}*\n\n"
        f"💰 Lucro: *{sinal_s}R$ {lucro_s:.2f}* ({lucro_u_s:+.2f}u)\n"
        f"📊 ROI: *{roi_s:+.1%}* | Média/aposta: R$ {media_s:.2f}\n"
        f"🏆 {g_s}V / {len(ap_semana)-g_s}D | {len(ap_semana)} apostas\n"
        f"🔝 Máxima da semana: *+R$ {pico_s:.2f}*\n🔻 Mínima da semana: *R$ {fundo_s:.2f}*",
        reply_markup=teclado_resultados(), parse_mode="Markdown"
    )
    return RESULTADOS_MENU

async def resultados_por_esporte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas = carregar()
    res     = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]
    if not res:
        await update.message.reply_text("Nenhuma aposta resolvida ainda.", reply_markup=teclado_resultados())
        return
    esportes = {}
    for a in res:
        esp = normalizar_esporte(a.get("esporte")) or "Sem esporte"
        if esp not in esportes:
            esportes[esp] = {"ap":0,"g":0,"stake":0.0,"lucro":0.0}
        e = esportes[esp]; e["ap"] += 1; e["stake"] += float(a["stake"])
        e["lucro"] += lucro_aposta(a)
        if a["resultado"] == "ganhou": e["g"] += 1
    ordenados = sorted(esportes.items(), key=lambda x: x[1]["lucro"], reverse=True)
    linhas = ["🏅 *Por Esporte:*\n"]
    for nome, e in ordenados:
        roi   = e["lucro"]/e["stake"] if e["stake"] else 0
        emoji = "🟢" if e["lucro"] >= 0 else "🔴"
        sinal = "+" if e["lucro"] >= 0 else ""
        linhas.append(f"{emoji} *{nome}*\n  {e['ap']} ap | {e['g']}V/{e['ap']-e['g']}D | {sinal}R$ {e['lucro']:.2f} | ROI: {roi:+.1%}\n")
    linhas.append("\nDigite uma data (DD/MM) ou 🔙 Voltar:")
    await enviar_paginado(update, linhas, reply_markup=teclado_resultados(), parse_mode="Markdown")

async def resultados_dia(update: Update, ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> bool:
    agora = datetime.now()
    raw   = raw.strip()

    # Busca por ano: 4 dígitos ex: 2026
    if raw.isdigit() and len(raw) == 4:
        ano = int(raw)
        if 2000 <= ano <= 2099:
            apostas  = carregar()
            do_ano   = [a for a in apostas if str(a["data"])[:4] == str(ano) and (a["resultado"] in ("ganhou","perdeu") or eh_cashout(a))]
            if not do_ano:
                await update.message.reply_text(f"Nenhuma aposta resolvida em {ano}.", reply_markup=teclado_resultados())
                return True
            lucro_a  = sum(lucro_aposta(a) for a in do_ano)
            stake_a  = sum(float(a["stake"]) for a in do_ano)
            g = sum(1 for a in do_ano if a["resultado"] == "ganhou")
            roi = lucro_a / stake_a if stake_a else 0
            emoji = "🟢" if lucro_a >= 0 else "🔴"
            sinal = "+" if lucro_a >= 0 else ""
            await update.message.reply_text(
                f"{emoji} *{ano}* — {len(do_ano)} apostas | {g}V/{len(do_ano)-g}D\n"
                f"💰 {sinal}R$ {lucro_a:.2f} | ROI: {roi:+.1%}",
                reply_markup=teclado_resultados(), parse_mode="Markdown"
            )
            return True

    # Resolve data usando a mesma lógica do cadastro
    data_obj = None
    # 0 = hoje
    if raw == "0":
        data_obj = agora
    else:
        data_str_norm = normalizar_data(raw)
        if data_str_norm:
            data_obj = datetime.strptime(data_str_norm, "%Y-%m-%d")

    if not data_obj:
        return False

    data_str = data_obj.strftime("%Y-%m-%d")
    apostas  = carregar()
    do_dia   = [a for a in apostas if str(a["data"])[:10] == data_str
                and (a["resultado"] in ("ganhou","perdeu") or eh_cashout(a))]
    if not do_dia:
        await update.message.reply_text(
            f"Nenhuma aposta resolvida em {data_obj.strftime('%d/%m/%Y')}.",
            reply_markup=teclado_resultados())
        return True
    lucro_dia   = sum(lucro_aposta(a) for a in do_dia)
    lucro_dia_u = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in do_dia)
    stake_dia   = sum(float(a["stake"]) for a in do_dia)
    media_dia   = lucro_dia / len(do_dia) if do_dia else 0
    roi_dia     = lucro_dia / stake_dia if stake_dia else 0
    g  = sum(1 for a in do_dia if a["resultado"] == "ganhou")
    p  = sum(1 for a in do_dia if a["resultado"] == "perdeu")
    co = sum(1 for a in do_dia if eh_cashout(a))
    vo = sum(1 for a in do_dia if a["resultado"] == "void" and not eh_cashout(a))
    emoji = "🟢" if lucro_dia >= 0 else "🔴"
    sinal = "+" if lucro_dia >= 0 else ""
    resumo_str = f"{g}V {p}D"
    if co: resumo_str += f" {co}CO"
    if vo: resumo_str += f" {vo}Void"
    pico_dia, fundo_dia = pico_e_fundo(do_dia)
    linhas = [
        f"{emoji} *{data_obj.strftime('%d/%m/%Y')}* — {resumo_str} — {sinal}R$ {lucro_dia:.2f} ({lucro_dia_u:+.2f}u)\n"
        f"📊 Média/aposta: R$ {media_dia:.2f} | ROI do dia: {roi_dia:+.1%}\n"
        f"🔝 Máxima do dia: *+R$ {pico_dia:.2f}*\n🔻 Mínima do dia: *R$ {fundo_dia:.2f}*\n"
    ]
    emojis_res = {"ganhou":"✅","perdeu":"❌"}
    for a in do_dia:
        l   = lucro_aposta(a)
        u_a = float(a.get("unidade") or 50)
        l_u = l / u_a
        s   = "+" if l >= 0 else ""
        if eh_cashout(a):
            cv = float(a.get("cashout_valor") or 0)
            linhas.append(f"💸 *#{a['id']}* odd {float(a['odd']):g} | R${float(a['stake']):.2f} → CO R${cv:.2f} ({s}R$ {l:.2f} / {l_u:+.2f}u)\n_{esc_md(a['descricao'])}_\n")
        elif a["resultado"] == "void":
            linhas.append(f"↩️ *#{a['id']}* odd {float(a['odd']):g} | R${float(a['stake']):.2f} → Void (R$ 0.00 / 0.00u)\n_{esc_md(a['descricao'])}_\n")
        else:
            linhas.append(f"{emojis_res.get(a['resultado'],'')} *#{a['id']}* odd {float(a['odd']):g} | R${float(a['stake']):.2f} → {s}R$ {l:.2f} ({l_u:+.2f}u)\n_{esc_md(a['descricao'])}_\n")
    linhas.append("Digite outra data, 🏦 *Por Casa* ou 🔙 *Voltar*:")
    await enviar_paginado(update, linhas, reply_markup=teclado_resultados(), parse_mode="Markdown")
    return True

async def resultados_por_casa_detalhe(update: Update, ctx: ContextTypes.DEFAULT_TYPE, nome_casa: str):
    """Mostra todas as apostas de uma casa específica."""
    apostas = carregar()
    res = [a for a in apostas if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]

    # Match flexível: normaliza e compara case-insensitive, ou substring
    nome_lower = nome_casa.strip().lower()
    do_casa = [a for a in res if
               nome_lower in (a.get("casa") or "").lower() or
               nome_lower in normalizar_casa(a.get("casa") or "").lower()]

    if not do_casa:
        await update.message.reply_text(
            f"Nenhuma aposta resolvida encontrada para *{nome_casa}*.",
            reply_markup=teclado_resultados(), parse_mode="Markdown"
        )
        return

    lucro_c   = sum(lucro_aposta(a) for a in do_casa)
    stake_c   = sum(float(a["stake"]) for a in do_casa)
    g_c       = sum(1 for a in do_casa if a["resultado"] == "ganhou")
    roi_c     = lucro_c / stake_c if stake_c else 0
    lucro_u_c = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in do_casa)
    emoji_c   = "🟢" if lucro_c >= 0 else "🔴"
    sinal_c   = "+" if lucro_c >= 0 else ""

    nome_exib = normalizar_casa(do_casa[0].get("casa") or nome_casa)
    pico_c, fundo_c = pico_e_fundo(do_casa)
    linhas = [
        f"🏦 *{nome_exib}* — {len(do_casa)} apostas",
        f"{emoji_c} Lucro: *{sinal_c}R$ {lucro_c:.2f}* ({lucro_u_c:+.2f}u) | ROI: {roi_c:+.1%} | {g_c}V/{len(do_casa)-g_c}D",
        f"🔝 Máximo histórico: *+R$ {pico_c:.2f}*\n🔻 Mínimo histórico: *R$ {fundo_c:.2f}*\n"
    ]

    emojis_res = {"ganhou":"✅","perdeu":"❌","void":"↩️","pendente":"⏳"}
    for a in sorted(do_casa, key=lambda x: (str(x["data"]), x.get("horario") or "")):
        l   = lucro_aposta(a)
        u_a = float(a.get("unidade") or 50)
        l_u = l / u_a
        s   = "+" if l >= 0 else ""
        data_fmt = a["data"].strftime("%d/%m/%Y") if hasattr(a["data"],"strftime") else str(a["data"])[:10]
        res_emoji = "💸" if eh_cashout(a) else emojis_res.get(a["resultado"], "")
        linhas.append(
            f"{res_emoji} *#{a['id']}* {data_fmt} | odd {float(a['odd']):g} | R${float(a['stake']):.2f} \u2192 {s}R$ {l:.2f} ({l_u:+.2f}u)\n_{esc_md(a['descricao'])}_\n"
        )

    linhas.append("Digite outra casa, data ou 🔙 *Voltar*:")
    await enviar_paginado(update, linhas, reply_markup=teclado_resultados(), parse_mode="Markdown")

async def resultados_resposta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler das respostas dentro do ConversationHandler de resultados."""
    txt = update.message.text
    if txt == "🔙 Voltar":
        return await voltar_menu(update, ctx)
    if txt == "🏦 Por Casa":
        await resultados_por_casa(update, ctx)
        return RESULTADOS_MENU
    if txt == "📅 Por Mês":
        await resultados_por_mes(update, ctx)
        return RESULTADOS_MENU
    if txt == "🏅 Por Esporte":
        await resultados_por_esporte(update, ctx)
        return RESULTADOS_MENU
    if txt == "📊 Estatísticas":
        return await estatisticas(update, ctx)
    if txt == "📆 Semana":
        return await resultados_semana_inicio(update, ctx)
    # Filtro por nome de mês: "Janeiro", "jan", "Abril/2026", etc.
    txt_lower = txt.strip().lower()
    # Detecta "mes/ano" ou "mes ano" ou só "mes"
    ano_filtro = None
    nome_teste = txt_lower
    for sep in ("/", " ", "-"):
        if sep in txt_lower:
            partes = txt_lower.split(sep, 1)
            if partes[1].isdigit() and len(partes[1]) == 4:
                nome_teste = partes[0].strip()
                ano_filtro = int(partes[1])
                break
    mes_num = NOMES_MES_PT.get(nome_teste)
    if mes_num:
        await resultados_por_mes_especifico(update, ctx, mes_num, ano_filtro)
        return RESULTADOS_MENU
    if await resultados_dia(update, ctx, txt):
        return RESULTADOS_MENU
    # Tenta match de casa de apostas
    apostas_todas = carregar()
    casas_banco = set(normalizar_casa(a.get("casa") or "").lower() for a in apostas_todas)
    if txt_lower in casas_banco or any(txt_lower in c for c in casas_banco):
        await resultados_por_casa_detalhe(update, ctx, txt)
        return RESULTADOS_MENU
    await update.message.reply_text(
        "❓ Não reconheci. Use os botões, digite uma data (20/05), ano (2026), mês (Janeiro) ou nome de uma casa (Betano, Bet365…).",
        reply_markup=teclado_resultados()
    )
    return RESULTADOS_MENU
CAMPOS_EDITAVEIS = ["data","horario","descricao","odd","stake","esporte","casa","resultado","cashout","freebet","deletar"]
CAMPOS_LABEL     = {
    "data":"📅 Data","horario":"⏰ Horário","descricao":"🏷 Descrição",
    "odd":"🔢 Odd","stake":"💰 Stake","esporte":"🏅 Esporte",
    "casa":"🏦 Casa","resultado":"📊 Resultado","cashout":"💸 Cashout","freebet":"🎁 Freebet (R$)","deletar":"🗑 Deletar aposta"
}

def _montar_linhas_listagem(apostas_todas):
    """Monta as linhas de texto da listagem de apostas para editar."""
    linhas = [f"✏️ *Editar aposta* ({len(apostas_todas)} pendentes)\n"]
    for a in apostas_todas:
        data_fmt = a["data"].strftime("%d/%m") if hasattr(a["data"], "strftime") else str(a["data"])[:10]
        hora     = f" {a.get('horario','')}" if a.get("horario") else ""
        status   = "⏳" if a["resultado"] == "pendente" else ("✅" if a["resultado"] == "ganhou" else ("❌" if a["resultado"] == "perdeu" else "↩️"))
        linhas.append(f"{status} *#{a['id']}* {data_fmt}{hora} | odd {float(a['odd']):g} | _{esc_md(str(a['descricao'])[:28])}_")
    linhas.append("\nDigite o *ID* da aposta:")
    return linhas

async def editar_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas   = carregar()
    pendentes = [a for a in apostas if a["resultado"] == "pendente"]
    todas     = sorted(pendentes, key=lambda a: (str(a["data"]), a.get("horario") or "", int(a["id"])))
    ctx.user_data["editar_apostas"] = todas

    hoje_str    = datetime.now().strftime("%Y-%m-%d")
    res_hoje    = [a for a in apostas if str(a["data"])[:10] == hoje_str
                   and (a["resultado"] in ("ganhou","perdeu") or eh_cashout(a))]
    pendentes_hoje = [a for a in pendentes if str(a["data"])[:10] == hoje_str]
    linha_hoje  = ""
    if res_hoje or pendentes_hoje:
        lucro_resolvido_hoje = sum(lucro_aposta(a) for a in res_hoje)

        def _potencial_win(a):
            return float(a["stake"]) * (float(a["odd"]) - 1)

        def _potencial_loss(a):
            stake   = float(a["stake"])
            try:
                freebet = float(a.get("freebet") or 0)
            except Exception:
                freebet = 0.0
            freebet = min(freebet, stake)
            return -(stake - freebet)

        maxima_hoje = lucro_resolvido_hoje + sum(_potencial_win(a) for a in pendentes_hoje)
        minima_hoje = lucro_resolvido_hoje + sum(_potencial_loss(a) for a in pendentes_hoje)
        linha_hoje = (
            f"\n🔝 Máxima possível hoje (se todas baterem): *+R$ {maxima_hoje:.2f}*"
            f"\n🔻 Mínima possível hoje (se todas perderem): *R$ {minima_hoje:.2f}*"
            f"\n📂 Em aberto hoje: *{len(pendentes_hoje)} ap*"
        )

    if not todas:
        texto = "✅ Nenhuma aposta pendente!"
        if linha_hoje:
            texto += linha_hoje
        texto += "\n\nDigite o *ID* de uma aposta já resolvida para editá-la:"
        await update.message.reply_text(
            texto,
            reply_markup=ReplyKeyboardMarkup([[CANCELAR_BTN]], resize_keyboard=True),
            parse_mode="Markdown"
        )
        return EDITAR_PAGINA

    stake_curso    = sum(stake_real(a) for a in todas)
    possivel_ganho = sum(float(a["stake"]) * (float(a["odd"]) - 1) for a in todas)
    await update.message.reply_text(
        f"⏳ Stake em curso: *R$ {stake_curso:.2f}* ({len(todas)} ap) | "
        f"Possível lucro (juntando todas): *+R$ {possivel_ganho:.2f}*"
        f"{linha_hoje}",
        parse_mode="Markdown"
    )

    linhas = _montar_linhas_listagem(todas)
    await enviar_paginado(
        update, linhas,
        reply_markup=ReplyKeyboardMarkup([[CANCELAR_BTN]], resize_keyboard=True),
        parse_mode="Markdown"
    )
    return EDITAR_PAGINA

async def editar_navegar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler do estado EDITAR_PAGINA — recebe o ID da aposta a editar."""
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN:
        return await cancelar(update, ctx)

    # Tentou digitar um ID
    try:
        id_alvo = int(raw)
    except ValueError:
        await update.message.reply_text("❌ Digite o número do ID da aposta:")
        return EDITAR_PAGINA

    return await _abrir_edicao_aposta(update, ctx, id_alvo)

async def editar_direto_comando(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point de 'editar <id>': pula a listagem e já abre a aposta pra
    escolher o campo, de qualquer ponto da conversa."""
    m = re.match(r"(?i)^editar\s+(\d+)$", update.message.text.strip())
    id_alvo = int(m.group(1))
    return await _abrir_edicao_aposta(update, ctx, id_alvo)

async def _abrir_edicao_aposta(update: Update, ctx: ContextTypes.DEFAULT_TYPE, id_alvo: int):
    aposta = buscar_por_id(id_alvo)
    if not aposta:
        await update.message.reply_text("❌ ID não encontrado. Tente outro:")
        return EDITAR_PAGINA

    ctx.user_data["editar_id"] = id_alvo
    data_fmt = aposta["data"].strftime("%d/%m/%Y") if hasattr(aposta["data"], "strftime") else str(aposta["data"])[:10]
    hora     = f" {aposta.get('horario','')}" if aposta.get("horario") else ""
    _label_res = {"ganhou": "Green ✅", "perdeu": "Red ❌", "void": "Void ↩️", "pendente": "Pendente ⏳"}
    info = (
        f"✏️ *Aposta #{id_alvo}*\n\n📅 {data_fmt}{hora}\n🏷 {esc_md(aposta['descricao'])}\n"
        f"🔢 Odd: {float(aposta['odd']):g}\n💰 Stake: R$ {float(aposta['stake']):.2f}\n"
        f"🏦 Casa: {esc_md(aposta.get('casa',''))}\n🏅 Esporte: {esc_md(aposta.get('esporte',''))}\n"
        f"📊 Resultado: {_label_res.get(aposta['resultado'], aposta['resultado'])}\n"
        f"🎁 Freebet: R$ {float(aposta['freebet'] or 0):.2f}\n\n*Qual campo editar?*"
    )
    teclado = [[CAMPOS_LABEL[c]] for c in CAMPOS_EDITAVEIS] + [[CANCELAR_BTN]]
    await update.message.reply_text(
        info,
        reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True, one_time_keyboard=True),
        parse_mode="Markdown"
    )
    return EDITAR_CAMPO



async def editar_receber_campo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    campo = next((k for k,v in CAMPOS_LABEL.items() if v == raw), None)
    if not campo:
        await update.message.reply_text("❌ Escolha um dos campos:")
        return EDITAR_CAMPO
    ctx.user_data["editar_campo"] = campo
    if campo == "casa":
        teclado = [[c] for c in get_casas_ordenadas()] + [[CANCELAR_BTN]]
        await update.message.reply_text("🏦 *Nova casa:*",
            reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True, one_time_keyboard=True),
            parse_mode="Markdown")
        return EDITAR_CASA
    if campo == "esporte":
        teclado = [[e] for e in ESPORTES] + [[CANCELAR_BTN]]
        await update.message.reply_text("🏅 *Novo esporte:*",
            reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True, one_time_keyboard=True),
            parse_mode="Markdown")
        return EDITAR_CASA
    if campo == "resultado":
        teclado = [["✅ Green","❌ Red"],["↩️ Void","⏳ Pendente"],[CANCELAR_BTN]]
        await update.message.reply_text("📊 *Qual o resultado?*",
            reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True, one_time_keyboard=True),
            parse_mode="Markdown")
        return EDITAR_CASA
    if campo == "freebet":
        aposta_atual = buscar_por_id(ctx.user_data["editar_id"])
        stake_atual  = float(aposta_atual["stake"])
        await update.message.reply_text(
            f"🎁 *Freebet*\nDigite o valor em R$ que foi freebet nessa aposta.\n"
            f"Stake total: R$ {stake_atual:.2f}\n"
            f"Digite *0* para remover freebet:",
            reply_markup=teclado_cancelar(), parse_mode="Markdown")
        return EDITAR_VALOR
    if campo == "deletar":
        id_alvo  = ctx.user_data["editar_id"]
        aposta   = buscar_por_id(id_alvo)
        descr    = str(aposta["descricao"])[:30] if aposta else "?"
        await update.message.reply_text(
            f"🗑 *Deletar Aposta #{id_alvo}*\n_{descr}_\n\n"
            f"⚠️ Esta ação é *irreversível*. Digite *SIM* para confirmar:",
            reply_markup=teclado_cancelar(), parse_mode="Markdown")
        return EDITAR_VALOR
    if campo == "cashout":
        await update.message.reply_text(
            "💸 *Cashout*\nDigite o valor que voce *recebeu* de volta (R$):\n"
            "Ex: apostou R$50, fez cashout por R$30 -> digite *30*\n"
            "Se perdeu tudo -> digite *0*",
            reply_markup=teclado_cancelar(), parse_mode="Markdown")
        return EDITAR_VALOR
    dicas = {"data":"Nova data ou 0 para hoje:","horario":"Novo horário ou 0 para agora:",
             "descricao":"Nova descrição:","odd":"Nova odd:","stake":"Novo stake (R$):"}
    await update.message.reply_text(dicas[campo], reply_markup=teclado_cancelar())
    return EDITAR_VALOR

async def editar_receber_valor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw   = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    campo   = ctx.user_data["editar_campo"]
    id_alvo = ctx.user_data["editar_id"]
    novo_valor = None
    if campo == "deletar":
        if raw.upper() == "SIM":
            deletar_aposta(id_alvo)
            await update.message.reply_text(f"🗑 *Aposta #{id_alvo} deletada com sucesso!*", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Cancelado. Aposta não deletada.")
        return await voltar_menu(update, ctx)
    if campo == "data":
        if raw == "0": novo_valor = datetime.now().strftime("%Y-%m-%d")
        else:
            novo_valor = normalizar_data(raw)
            if not novo_valor:
                await update.message.reply_text("❌ Data inválida:")
                return EDITAR_VALOR
    elif campo == "horario":
        if raw == "0":
            novo_valor = datetime.now().strftime("%H:%M")
        else:
            novo_valor = normalizar_horario(raw)
            if not novo_valor:
                await update.message.reply_text("❌ Horário inválido:")
                return EDITAR_VALOR
    elif campo in ("odd","stake","cashout","freebet"):
        try: novo_valor = float(raw.replace(",","."))
        except ValueError:
            await update.message.reply_text("❌ Digite um número:")
            return EDITAR_VALOR
        if campo == "freebet":
            aposta_atual = buscar_por_id(id_alvo)
            stake_atual  = float(aposta_atual["stake"])
            if novo_valor > stake_atual:
                await update.message.reply_text(
                    f"❌ Freebet não pode ser maior que o stake (R$ {stake_atual:.2f}). Digite novamente:")
                return EDITAR_VALOR
        if campo == "cashout":
            aposta   = buscar_por_id(id_alvo)
            stake    = float(aposta["stake"])
            recebido = novo_valor
            lucro_co = recebido - stake
            agora    = datetime.now()
            atualizar_campo(id_alvo, "resultado",     "void")
            atualizar_campo(id_alvo, "cashout_valor", recebido)
            atualizar_campo(id_alvo, "data",          agora.strftime("%Y-%m-%d"))
            atualizar_campo(id_alvo, "horario",       agora.strftime("%H:%M"))
            sinal    = "+" if lucro_co >= 0 else ""
            emoji_co = "📈" if lucro_co >= 0 else "📉"
            await update.message.reply_text(
                f"💸 *Cashout registrado!*\n\n"
                f"💰 Apostado: R$ {stake:.2f}\n"
                f"💸 Recebido: R$ {recebido:.2f}\n"
                f"{emoji_co} Resultado: *{sinal}R$ {lucro_co:.2f}*\n"
                f"📅 Planilhado em: *{agora.strftime('%d/%m/%Y às %H:%M')}*",
                parse_mode="Markdown")
            return await voltar_menu(update, ctx)
    else:
        novo_valor = raw
    return await aplicar_edicao(update, ctx, id_alvo, campo, novo_valor)

async def editar_receber_casa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw   = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    campo   = ctx.user_data["editar_campo"]
    id_alvo = ctx.user_data["editar_id"]
    if campo == "resultado":
        mapa = {"✅ Green":"ganhou","❌ Red":"perdeu","✅ Ganhou":"ganhou","❌ Perdeu":"perdeu","↩️ Void":"void","⏳ Pendente":"pendente"}
        valor = mapa.get(raw, raw)
        return await aplicar_edicao(update, ctx, id_alvo, "resultado", valor)

    if raw == "Outra" or raw == "Outro":
        await update.message.reply_text("Digite o nome:", reply_markup=teclado_cancelar())
        ctx.user_data["editar_campo"] = campo + "_custom"
        return EDITAR_VALOR
    valor_final = normalizar_esporte(raw) if campo == "esporte" else raw
    return await aplicar_edicao(update, ctx, id_alvo, campo, valor_final)

async def aplicar_edicao(update, ctx, id_alvo, campo, novo_valor):
    campo_real = campo.replace("_custom","")
    if campo_real == "esporte" and isinstance(novo_valor, str):
        novo_valor = normalizar_esporte(novo_valor)
    if campo_real == "casa" and isinstance(novo_valor, str) and novo_valor not in get_casas_ordenadas():
        novo_valor = novo_valor.strip().title()
    atualizar_campo(id_alvo, campo_real, novo_valor)
    label = CAMPOS_LABEL.get(campo_real, campo_real)
    exibe = novo_valor
    if campo_real == "data" and isinstance(novo_valor,str) and len(novo_valor)==10:
        exibe = datetime.strptime(novo_valor,"%Y-%m-%d").strftime("%d/%m/%Y")
    elif campo_real in ("odd","stake"):
        exibe = f"{float(novo_valor):.2f}"
    # Escolher emoji baseado no resultado se campo for resultado
    emoji_conf = "✅"
    if campo_real == "resultado":
        if novo_valor == "perdeu": emoji_conf = "❌"
        elif novo_valor == "void": emoji_conf = "↩️"
        elif novo_valor == "pendente": emoji_conf = "⏳"
        exibe = {"ganhou":"Green ✅","perdeu":"Red ❌","void":"Void ↩️","pendente":"Pendente ⏳"}.get(novo_valor, novo_valor)
    await update.message.reply_text(f"{emoji_conf} *Aposta #{id_alvo} atualizada!*\n{label} → *{exibe}*", parse_mode="Markdown")
    return await voltar_menu(update, ctx)


# ── MUDAR UNIDADE ─────────────────────────────────────────────────────────────
async def mudar_unidade_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    unidade_atual = get_unidade_atual()
    await update.message.reply_text(
        f"⚙️ *Mudar Unidade*\n\n"
        f"Unidade atual: *R$ {unidade_atual:.2f}*\n\n"
        f"A nova unidade será salva em todas as apostas *a partir de agora*.\n"
        f"Apostas anteriores mantêm a unidade original no histórico.\n\n"
        f"Digite o novo valor da unidade em R$:",
        reply_markup=teclado_cancelar(), parse_mode="Markdown"
    )
    return MUDAR_UNIDADE_VALOR

async def mudar_unidade_receber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    try:
        novo_valor = float(raw.replace(",", "."))
        if novo_valor <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Digite um valor válido maior que zero:")
        return MUDAR_UNIDADE_VALOR
    unidade_anterior = get_unidade_atual()
    set_unidade_atual(novo_valor)
    await update.message.reply_text(
        f"✅ *Unidade atualizada!*\n\n"
        f"Anterior: R$ {unidade_anterior:.2f}\n"
        f"Nova: *R$ {novo_valor:.2f}*\n\n"
        f"Todas as apostas daqui pra frente usarão essa unidade.",
        parse_mode="Markdown"
    )
    return await voltar_menu(update, ctx)

# ── CONFIGURAÇÕES ─────────────────────────────────────────────────────────────
async def config_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ *Configurações*\n\nO que deseja ajustar?",
        reply_markup=teclado_config(), parse_mode="Markdown"
    )
    return CONFIG_MENU

async def config_resposta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Voltar":
        return await voltar_menu(update, ctx)
    if txt == "💲 Mudar Unidade":
        return await mudar_unidade_inicio(update, ctx)
    if txt == "💸 Gastos":
        return await gastos_menu(update, ctx)
    if txt == "🚫 Limitações":
        return await limitacoes_inicio(update, ctx)
    await update.message.reply_text("❓ Use os botões abaixo.", reply_markup=teclado_config())
    return CONFIG_MENU

async def gastos_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total = total_gastos()
    await update.message.reply_text(
        f"💸 *Gastos*\n\nTotal registrado: *R$ {total:.2f}*\n\n"
        f"Isso entra no cálculo de *Lucro Real* na planilha "
        f"(Lucro das apostas − Gastos). Não afeta ROI, Win Rate nem Lucro Units.",
        reply_markup=teclado_gastos(), parse_mode="Markdown"
    )
    return GASTOS_MENU

async def gastos_resposta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Voltar":
        return await config_inicio(update, ctx)
    if txt == "➕ Novo Gasto":
        await update.message.reply_text(
            "📝 Descrição do gasto (ex: Assinatura Grupo VIP):",
            reply_markup=teclado_cancelar()
        )
        return GASTO_DESCRICAO
    if txt == "📋 Ver Gastos":
        gastos = listar_gastos()
        if not gastos:
            await update.message.reply_text("Nenhum gasto registrado ainda.", reply_markup=teclado_gastos())
        else:
            linhas = [
                f"#{g['id']} | {g['data'].strftime('%d/%m/%Y')} | {esc_md(g['descricao'])} | R$ {float(g['valor']):.2f}"
                for g in gastos
            ]
            await enviar_paginado(update, linhas, reply_markup=teclado_gastos())
        return GASTOS_MENU
    await update.message.reply_text("❓ Use os botões abaixo.", reply_markup=teclado_gastos())
    return GASTOS_MENU

async def receber_gasto_descricao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == CANCELAR_BTN: return await cancelar(update, ctx)
    ctx.user_data["gasto_descricao"] = txt
    await update.message.reply_text("💰 Valor do gasto em R$:", reply_markup=teclado_cancelar())
    return GASTO_VALOR

async def receber_gasto_valor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == CANCELAR_BTN: return await cancelar(update, ctx)
    try:
        valor = float(raw.replace(",", "."))
        if valor <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Digite um valor válido maior que zero:")
        return GASTO_VALOR
    desc = ctx.user_data.pop("gasto_descricao", "")
    inserir_gasto(datetime.now().date(), desc, valor)
    await update.message.reply_text(
        f"✅ *Gasto registrado!*\n\n{desc} — R$ {valor:.2f}",
        parse_mode="Markdown"
    )
    return await gastos_menu(update, ctx)

# ── LIMITAÇÕES ────────────────────────────────────────────────────────────────
async def limitacoes_inicio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    casas = get_casas_limitadas()
    if casas:
        texto = (
            "🚫 *Limitações*\n\nCasas em que você está limitado:\n\n"
            + "\n".join(f"• {esc_md(c)}" for c in casas)
            + "\n\nToque numa casa para remover da lista, ou adicione uma nova."
        )
    else:
        texto = (
            "🚫 *Limitações*\n\n"
            "Você não tem nenhuma casa limitada no momento.\n\n"
            "Casas limitadas somem das opções ao registrar uma nova aposta."
        )
    await update.message.reply_text(
        texto, reply_markup=teclado_limitacoes(casas), parse_mode="Markdown"
    )
    return LIMITACOES_MENU

async def limitacoes_resposta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == "🔙 Voltar":
        return await config_inicio(update, ctx)
    if txt == "➕ Adicionar Casa":
        casas_limitadas = get_casas_limitadas()
        limitadas_lower = set(c.lower() for c in casas_limitadas)
        # Junta a lista fixa com as casas que já apareceram em apostas reais,
        # pra casas como "Rei do Pitaco" (que não está em CASAS) também aparecerem prontas.
        casas_usadas = set()
        try:
            with conectar() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT casa FROM apostas WHERE casa IS NOT NULL AND casa != ''")
                    casas_usadas = {r[0] for r in cur.fetchall()}
        except Exception:
            pass
        todas_casas = sorted(set(c for c in CASAS if c != "Outra") | casas_usadas)
        teclado = [[c] for c in todas_casas if c.lower() not in limitadas_lower]
        teclado.append([CANCELAR_BTN])
        await update.message.reply_text(
            "🏦 *Qual casa você está limitado?*\nEscolha da lista ou digite o nome:",
            reply_markup=ReplyKeyboardMarkup(teclado, resize_keyboard=True, one_time_keyboard=True),
            parse_mode="Markdown"
        )
        return LIMITACOES_ADD
    casas_limitadas = get_casas_limitadas()
    if txt in casas_limitadas:
        remover_casa_limitada(txt)
        await update.message.reply_text(
            f"✅ *{esc_md(txt)}* removida das limitações. Ela volta a aparecer nas opções de nova aposta.",
            parse_mode="Markdown"
        )
        return await limitacoes_inicio(update, ctx)
    await update.message.reply_text("❓ Use os botões abaixo.", reply_markup=teclado_limitacoes(casas_limitadas))
    return LIMITACOES_MENU

async def limitacoes_add_receber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == CANCELAR_BTN:
        return await limitacoes_inicio(update, ctx)
    # Se a casa digitada já existe (em CASAS ou já usada em alguma aposta, mesmo
    # com grafia diferente), usa a grafia já existente pra manter consistência.
    casa = txt if txt in CASAS else txt.title()
    try:
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT casa FROM apostas WHERE casa IS NOT NULL AND lower(casa)=lower(%s) LIMIT 1",
                    (txt,)
                )
                row = cur.fetchone()
                if row:
                    casa = row[0]
    except Exception:
        pass
    adicionar_casa_limitada(casa)
    await update.message.reply_text(
        f"🚫 *{esc_md(casa)}* adicionada às limitações.\nEla não vai mais aparecer nas opções de nova aposta.",
        parse_mode="Markdown"
    )
    return await limitacoes_inicio(update, ctx)

# ── GERAR MENU ────────────────────────────────────────────────────────────────
async def gerar_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Exportar*\n\n"
        "• *Gerar Dashboard* → xlsx com estatísticas e gráficos\n"
        "• *Dashboard HTML* → página única offline, abre no navegador\n"
        "• *Gerar Dados* → xlsx com dados brutos para migração de banco",
        reply_markup=teclado_gerar(), parse_mode="Markdown"
    )
    return GERAR_MENU

async def gerar_resposta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler das respostas dentro do ConversationHandler de gerar."""
    txt = update.message.text
    if txt == "🔙 Voltar":
        return await voltar_menu(update, ctx)
    if txt == "📊 Gerar Dashboard":
        await gerar_dashboard(update, ctx)
        await update.message.reply_text("O que mais?", reply_markup=teclado_gerar())
        return GERAR_MENU
    if txt == "🌐 Dashboard HTML":
        await gerar_dashboard_html(update, ctx)
        await update.message.reply_text("O que mais?", reply_markup=teclado_gerar())
        return GERAR_MENU
    if txt == "📂 Gerar Dados":
        await gerar_xlsx_dados(update, ctx)
        return GERAR_MENU
    await update.message.reply_text("❓ Use os botões abaixo.", reply_markup=teclado_gerar())
    return GERAR_MENU

async def gerar_xlsx_dados(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gera migrar_db.py com todos os dados embutidos — funciona mesmo se o banco antigo sumir."""
    await update.message.reply_text("⏳ Gerando script de migração, aguarde...")
    try:
        apostas       = carregar()
        gastos        = listar_gastos()
        casas_lim     = get_casas_limitadas()
        unidade_atual = get_unidade_atual()
        max_id        = max((a["id"] for a in apostas), default=0)
        max_id_gasto  = max((g["id"] for g in gastos), default=0)
        total         = len(apostas)
        from datetime import datetime as dt2

        # Serializa cada aposta como tupla Python embutida no script
        linhas = []
        for a in apostas:
            data_str = a["data"].strftime("%Y-%m-%d") if hasattr(a["data"], "strftime") else str(a["data"])[:10]
            horario  = a.get("horario") or ""
            desc     = str(a["descricao"]).replace("\n", " ").replace("\r", " ").replace("'", "\\'")
            casa     = str(a.get("casa") or "").replace("'", "\\'")
            esporte  = str(a.get("esporte") or "").replace("'", "\\'")
            resultado = a.get("resultado") or "pendente"
            freebet  = float(a.get("freebet") or 0)
            unidade  = float(a.get("unidade") or 50)
            cashout  = float(a.get("cashout_valor") or 0)
            odd      = float(a["odd"])
            stake    = float(a["stake"])
            linhas.append(
                f"    ({a['id']}, '{data_str}', '{horario}', '{desc}', "
                f"{repr(odd)}, {repr(stake)}, '{resultado}', '{casa}', '{esporte}', "
                f"{repr(freebet)}, {repr(unidade)}, {repr(cashout)}),"
            )

        dados_str = "\n".join(linhas)

        # Serializa gastos
        linhas_gastos = []
        for g in gastos:
            data_g = g["data"].strftime("%Y-%m-%d") if hasattr(g["data"], "strftime") else str(g["data"])[:10]
            desc_g = str(g["descricao"]).replace("\n", " ").replace("\r", " ").replace("'", "\\'")
            valor_g = float(g["valor"])
            linhas_gastos.append(f"    ({g['id']}, '{data_g}', '{desc_g}', {repr(valor_g)}),")
        gastos_str = "\n".join(linhas_gastos)

        # Serializa casas limitadas
        linhas_casas_lim = [f"    '{c}'," for c in casas_lim]
        casas_lim_str = "\n".join(linhas_casas_lim)

        script = f'''"""
Migração de banco de dados — Bot Apostas
Gerado em {dt2.now().strftime("%d/%m/%Y %H:%M")} | {total} apostas | unidade: R$ {unidade_atual:.0f}

⚠️  Os dados já estão embutidos neste script.
    O banco antigo NÃO precisa estar online para rodar.

Como usar:
  1. Troque NEW_URL pela External URL do banco novo
     (Render → banco novo → Connections → External Database URL)
  2. No CMD:
       cd C:\\\\Users\\\\enzop\\\\Downloads
       python migrar_db.py
  3. Render → seu serviço → Environment → DATABASE_URL
     Coloque a Internal URL do novo banco no Environment → Save Changes → Manual Deploy
"""
import psycopg2

NEW_URL = "postgresql://db_julho_user:YP9AtSQzw3gwSle8CZZ3x9Rha7GcxpK0@dpg-d94mklu7r5hc73fb972g-a.oregon-postgres.render.com/db_julho"  # <-- troque pela External URL do banco novo quando migrar

APOSTAS = [
# (id, data, horario, descricao, odd, stake, resultado, casa, esporte, freebet, unidade, cashout_valor)
{dados_str}
]

GASTOS = [
# (id, data, descricao, valor)
{gastos_str}
]

CASAS_LIMITADAS = [
{casas_lim_str}
]

UNIDADE_ATUAL = "{unidade_atual:.0f}"
MAX_ID        = {max_id}
MAX_ID_GASTO  = {max_id_gasto}

if __name__ == "__main__":
    if not NEW_URL:
        print("❌ NEW_URL está vazio. Coloque a External URL do banco novo.")
        exit(1)

    conn = psycopg2.connect(NEW_URL)
    cur  = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS apostas (
        id SERIAL PRIMARY KEY, data DATE NOT NULL, horario VARCHAR(5),
        descricao TEXT NOT NULL, odd NUMERIC(12,6) NOT NULL, stake NUMERIC(10,2) NOT NULL,
        resultado VARCHAR(10) DEFAULT \'pendente\', casa VARCHAR(50), esporte VARCHAR(50),
        freebet NUMERIC(10,2) DEFAULT 0, unidade NUMERIC(10,2) DEFAULT 50,
        cashout_valor NUMERIC(10,2) DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS configuracoes (
        chave VARCHAR(50) PRIMARY KEY, valor TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS gastos (
        id SERIAL PRIMARY KEY, data DATE NOT NULL, descricao TEXT NOT NULL,
        valor NUMERIC(10,2) NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS casas_limitadas (
        casa VARCHAR(50) PRIMARY KEY)""")
    conn.commit()

    import time as _time
    total_ap = len(APOSTAS)
    print(f"Inserindo {{total_ap}} apostas (pode cair e rodar de novo — retoma sozinho)...")
    bar_width = 40
    t_inicio = _time.time()
    COMMIT_A_CADA = 100
    i = 0
    while i < total_ap:
        a = APOSTAS[i]
        try:
            cur.execute("""INSERT INTO apostas
                (id,data,horario,descricao,odd,stake,resultado,casa,esporte,freebet,unidade,cashout_valor)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    data=EXCLUDED.data, horario=EXCLUDED.horario, descricao=EXCLUDED.descricao,
                    odd=EXCLUDED.odd, stake=EXCLUDED.stake, resultado=EXCLUDED.resultado,
                    casa=EXCLUDED.casa, esporte=EXCLUDED.esporte, freebet=EXCLUDED.freebet,
                    unidade=EXCLUDED.unidade, cashout_valor=EXCLUDED.cashout_valor""", a)
            i += 1
        except psycopg2.OperationalError as e:
            print(f"\\n⚠️  Conexão caiu em {{i}}/{{total_ap}}: {{e}}. Reconectando em 3s...")
            _time.sleep(3)
            try: conn.rollback()
            except Exception: pass
            conn = psycopg2.connect(NEW_URL)
            cur = conn.cursor()
            continue
        if i % COMMIT_A_CADA == 0 or i == total_ap:
            conn.commit()
            pct = i / total_ap
            feito = int(bar_width * pct)
            barra = "█" * feito + "░" * (bar_width - feito)
            decorrido = _time.time() - t_inicio
            estimado = (decorrido / i) * (total_ap - i) if i > 0 else 0
            print(f"\\r[{{barra}}] {{i}}/{{total_ap}} ({{pct:.0%}}) | ~{{estimado:.0f}}s restantes   ", end="", flush=True)
    print()  # newline after progress bar

    print(f"Inserindo {{len(GASTOS)}} gastos...")
    for g in GASTOS:
        cur.execute("""INSERT INTO gastos (id,data,descricao,valor) VALUES (%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                data=EXCLUDED.data, descricao=EXCLUDED.descricao, valor=EXCLUDED.valor""", g)
    if GASTOS:
        cur.execute("SELECT setval(pg_get_serial_sequence('gastos','id'), %s)", (MAX_ID_GASTO,))
    conn.commit()

    print(f"Inserindo {{len(CASAS_LIMITADAS)}} casa(s) limitada(s)...")
    for c in CASAS_LIMITADAS:
        cur.execute("INSERT INTO casas_limitadas (casa) VALUES (%s) ON CONFLICT (casa) DO NOTHING", (c,))
    conn.commit()

    cur.execute("""INSERT INTO configuracoes (chave,valor) VALUES (\'unidade_atual\',%s)
        ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""", (UNIDADE_ATUAL,))
    cur.execute("SELECT setval(\'apostas_id_seq\', %s)", (MAX_ID,))

    conn.commit(); conn.close()
    cashouts = sum(1 for a in APOSTAS if a[6] == "void" and float(a[11]) > 0)
    print(f"✅ {{len(APOSTAS)}} apostas migradas | {{len(GASTOS)}} gastos | {{len(CASAS_LIMITADAS)}} casa(s) limitada(s) | {{cashouts}} cashouts | unidade: R$ {{UNIDADE_ATUAL}}")
    print("Agora faça o Manual Deploy no Render com a nova DATABASE_URL.")
'''

        script_bytes = script.encode("utf-8")
        instrucoes = (
            "📂 *Script de migração gerado!*\n\n"
            f"_{total} apostas | {len(gastos)} gastos | {len(casas_lim)} casa(s) limitada(s) | unidade atual: R$ {unidade_atual:.0f}_\n\n"
            "⚠️ *Os dados já estão embutidos no script.*\n"
            "Funciona mesmo se o banco antigo sumir.\n\n"
            "*Como usar:*\n\n"
            "1️⃣ Salve o `migrar_db.py` na pasta Downloads\n"
            "2️⃣ Atualize `NEW_URL` no arquivo com a *External URL* do banco novo\n"
            "3️⃣ No `bot_apostas.py`, atualize `DATABASE_URL` com a *External URL* também\n"
            "4️⃣ No CMD:\n"
            "   `cd C:\\\\Users\\\\enzop\\\\Downloads`\n"
            "   `python migrar_db.py`\n"
            "5️⃣ Render → Environment → `DATABASE_URL` = *Internal URL* → *Manual Deploy*"
        )
        await update.message.reply_document(
            document=InputFile(io.BytesIO(script_bytes), filename="migrar_db.py"),
            caption=instrucoes,
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro:\n`{str(e)}`", parse_mode="Markdown")



# ── GERAR DASHBOARD ───────────────────────────────────────────────────────────
async def gerar_dashboard_html(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Gerando dashboard em HTML, aguarde...")
    try:
        html = _construir_dashboard_html()
        buf = io.BytesIO(html.encode("utf-8"))
        buf.seek(0)
        await update.message.reply_document(
            document=InputFile(buf, filename="dashboard.html"),
            caption="🌐 Abre o arquivo no navegador — funciona offline, sem instalar nada."
        )
    except Exception as _e:
        import traceback as _tb
        _trace = _tb.format_exc()[-700:]
        try:
            await update.message.reply_text(f"❌ Erro ao gerar dashboard HTML:\n{str(_e)}\n\n{_trace}")
        except Exception:
            await update.message.reply_text("❌ Erro ao gerar dashboard HTML. Verifique os logs.")

def _construir_dashboard_html():
    """Monta um dashboard em um único arquivo .html autocontido (CSS e JS
    embutidos, sem chamadas externas) com os mesmos dados/métricas do
    dashboard em Excel, mas navegável e filtrável direto no navegador."""
    import json

    apostas = carregar()
    apostas_ord = sorted(apostas, key=lambda a: (str(a["data"]), a.get("horario") or "99:99", int(a["id"])))
    df_res = [a for a in apostas_ord if a["resultado"] in ("ganhou", "perdeu") or eh_cashout(a)]

    BANCA = 5000
    lucro_total = sum(lucro_aposta(a) for a in df_res)
    stake_total = sum(float(a["stake"]) for a in df_res)
    vitorias    = sum(1 for a in df_res if a["resultado"] == "ganhou")
    win_rate    = vitorias / len(df_res) if df_res else 0
    roi         = lucro_total / stake_total if stake_total else 0
    progressao  = lucro_total / BANCA
    total       = len(apostas)
    resolvidas  = len(df_res)
    pendentes   = total - resolvidas
    stake_curso = sum(stake_real(a) for a in apostas if a["resultado"] == "pendente")
    lucro_units = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in df_res)

    # série de evolução da banca (lucro acumulado, só apostas resolvidas)
    banca_serie = []
    acum = 0.0
    for a in df_res:
        acum += lucro_aposta(a)
        banca_serie.append(acum)

    # linhas completas pra tabela navegável
    linhas = []
    acum2 = 0.0
    for a in apostas_ord:
        res = a["resultado"]
        lucro_a = lucro_u_a = banca_a = None
        if res in ("ganhou", "perdeu") or eh_cashout(a):
            lucro_a = round(lucro_aposta(a), 2)
            unid_a  = float(a.get("unidade") or 50)
            lucro_u_a = round(lucro_a / unid_a, 2) if unid_a else None
            acum2 += lucro_a
            banca_a = round(acum2, 2)
        res_d = {"ganhou": "Green", "perdeu": "Red", "void": "Void", "pendente": "Pendente"}.get(res, res)
        if eh_cashout(a):
            res_d = "Cashout"
        data_fmt = a["data"].strftime("%d/%m/%Y") if hasattr(a["data"], "strftime") else str(a["data"])[:10]
        linhas.append({
            "id": a["id"], "data": data_fmt, "hora": a.get("horario") or "",
            "descricao": a["descricao"], "odd": float(a["odd"]), "stake": float(a["stake"]),
            "esporte": a.get("esporte") or "", "casa": a.get("casa") or "",
            "resultado": res_d, "lucro": lucro_a, "lucro_u": lucro_u_a, "banca": banca_a,
        })

    def agrega(chave):
        grupos = {}
        for a in df_res:
            k = a.get(chave) or "—"
            g = grupos.setdefault(k, {"n": 0, "green": 0, "lucro": 0.0, "stake": 0.0})
            g["n"] += 1
            g["lucro"] += lucro_aposta(a)
            g["stake"] += float(a["stake"])
            if a["resultado"] == "ganhou":
                g["green"] += 1
        out = [{
            "nome": k, "n": g["n"],
            "win_rate": g["green"] / g["n"] if g["n"] else 0,
            "lucro": round(g["lucro"], 2),
            "roi": g["lucro"] / g["stake"] if g["stake"] else 0,
        } for k, g in grupos.items()]
        out.sort(key=lambda x: x["lucro"], reverse=True)
        return out

    por_casa = agrega("casa")
    por_esporte = agrega("esporte")

    def svg_evolucao(serie):
        if not serie:
            return "<p class='vazio'>Sem apostas resolvidas ainda.</p>"
        w, h = 900, 260
        pad_l, pad_r, pad_t, pad_b = 40, 20, 20, 30
        n = len(serie)
        minv = min(0, min(serie))
        maxv = max(0, max(serie))
        span = (maxv - minv) or 1
        def x_of(i): return pad_l + ((i / (n - 1)) if n > 1 else 0) * (w - pad_l - pad_r)
        def y_of(v): return pad_t + (1 - (v - minv) / span) * (h - pad_t - pad_b)
        pts = [(x_of(i), y_of(v)) for i, v in enumerate(serie)]
        path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        area = path + f" L {pts[-1][0]:.1f},{y_of(0):.1f} L {pts[0][0]:.1f},{y_of(0):.1f} Z"
        zero_y = y_of(0)
        cor_linha = "var(--green)" if serie[-1] >= 0 else "var(--red)"
        return (
            f'<svg viewBox="0 0 {w} {h}" class="chart-svg" preserveAspectRatio="none">'
            f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{w - pad_r}" y2="{zero_y:.1f}" class="zero-line"/>'
            f'<path d="{area}" fill="{cor_linha}" opacity="0.15"/>'
            f'<path d="{path}" class="chart-line" stroke="{cor_linha}"/>'
            f'</svg>'
        )

    chart_html = svg_evolucao(banca_serie)
    linhas_json = json.dumps(linhas, ensure_ascii=False)
    esportes_opts = "".join(f'<option value="{e}">{e}</option>' for e in sorted({l["esporte"] for l in linhas if l["esporte"]}))
    casas_opts = "".join(f'<option value="{c}">{c}</option>' for c in sorted({l["casa"] for l in linhas if l["casa"]}))

    def barra_grupo(titulo, dados):
        maxlucro = max((abs(d["lucro"]) for d in dados), default=1) or 1
        corpo = ""
        for d in dados:
            pct = min(100, abs(d["lucro"]) / maxlucro * 100)
            cls = "pos" if d["lucro"] >= 0 else "neg"
            corpo += (
                f'<div class="bar-row"><div class="bar-label">{d["nome"]}</div>'
                f'<div class="bar-track"><div class="bar-fill {cls}" style="width:{pct:.1f}%"></div></div>'
                f'<div class="bar-val {cls}">R$ {d["lucro"]:+.2f}</div>'
                f'<div class="bar-meta">{d["n"]} apostas · {d["win_rate"]:.0%} green · ROI {d["roi"]:+.1%}</div></div>'
            )
        return f'<div class="card"><h3>{titulo}</h3>{corpo}</div>'

    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard de Apostas</title>
<style>
  :root {{
    --bg:#0f172a; --card:#172136; --border:#2b3a55; --text:#e2e8f0; --muted:#94a3b8;
    --green:#22c55e; --red:#ef4444; --amber:#f59e0b;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:'Segoe UI',Roboto,Arial,sans-serif;
    background:linear-gradient(180deg,var(--bg),#0b1220); color:var(--text); padding:32px 24px 80px; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); margin-bottom:28px; font-size:14px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:14px; margin-bottom:28px; }}
  .stat {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px; }}
  .stat .lbl {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }}
  .stat .val {{ font-size:22px; font-weight:700; margin-top:6px; }}
  .pos {{ color:var(--green); }} .neg {{ color:var(--red); }} .amber {{ color:var(--amber); }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; margin-bottom:24px; }}
  .card h3 {{ margin-top:0; font-size:16px; }}
  .chart-svg {{ width:100%; height:260px; }}
  .chart-line {{ fill:none; stroke-width:2.5; }}
  .zero-line {{ stroke:var(--border); stroke-dasharray:4 4; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
  @media (max-width:900px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
  .bar-row {{ margin-bottom:14px; }}
  .bar-label {{ font-size:13px; margin-bottom:4px; font-weight:600; }}
  .bar-track {{ background:#0f172a; border-radius:8px; height:10px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:8px; }}
  .bar-fill.pos {{ background:var(--green); }} .bar-fill.neg {{ background:var(--red); }}
  .bar-val {{ font-size:12px; margin-top:3px; font-weight:600; }}
  .bar-meta {{ font-size:11px; color:var(--muted); }}
  .filters {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }}
  .filters input, .filters select {{ background:#0f172a; border:1px solid var(--border); color:var(--text);
    padding:8px 10px; border-radius:8px; font-size:13px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:8px 10px; text-align:center; border-bottom:1px solid var(--border); white-space:nowrap; }}
  th {{ position:sticky; top:0; background:#111a2e; cursor:pointer; user-select:none; font-size:12px;
    text-transform:uppercase; color:var(--muted); }}
  th:hover {{ color:var(--text); }}
  td.desc {{ text-align:left; white-space:normal; max-width:320px; }}
  tbody tr:hover {{ background:rgba(255,255,255,0.03); }}
  .table-wrap {{ max-height:640px; overflow:auto; border-radius:10px; }}
  .badge {{ padding:2px 8px; border-radius:20px; font-size:11px; font-weight:700; }}
  .badge.Green {{ background:rgba(34,197,94,.15); color:var(--green); }}
  .badge.Red {{ background:rgba(239,68,68,.15); color:var(--red); }}
  .badge.Void, .badge.Cashout {{ background:rgba(245,158,11,.15); color:var(--amber); }}
  .badge.Pendente {{ background:rgba(148,163,184,.15); color:var(--muted); }}
  footer {{ color:var(--muted); font-size:12px; text-align:center; margin-top:40px; }}
</style>
</head>
<body>
  <h1>📊 Dashboard de Apostas</h1>
  <div class="sub">Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')} · {total} apostas registradas</div>

  <div class="cards">
    <div class="stat"><div class="lbl">Total</div><div class="val">{total}</div></div>
    <div class="stat"><div class="lbl">Resolvidas</div><div class="val">{resolvidas}</div></div>
    <div class="stat"><div class="lbl">Pendentes</div><div class="val {'amber' if pendentes else ''}">{pendentes}</div></div>
    <div class="stat"><div class="lbl">Win Rate</div><div class="val {'pos' if win_rate >= 0.5 else 'neg'}">{win_rate:.1%}</div></div>
    <div class="stat"><div class="lbl">Lucro R$</div><div class="val {'pos' if lucro_total >= 0 else 'neg'}">R$ {lucro_total:.2f}</div></div>
    <div class="stat"><div class="lbl">Lucro Units</div><div class="val {'pos' if lucro_units >= 0 else 'neg'}">{lucro_units:+.1f}u</div></div>
    <div class="stat"><div class="lbl">ROI</div><div class="val {'pos' if roi >= 0 else 'neg'}">{roi:+.1%}</div></div>
    <div class="stat"><div class="lbl">Progressão</div><div class="val {'pos' if progressao >= 0 else 'neg'}">{progressao:+.1%}</div></div>
    <div class="stat"><div class="lbl">Em Curso</div><div class="val {'amber' if stake_curso > 0 else ''}">R$ {stake_curso:.0f}</div></div>
  </div>

  <div class="card">
    <h3>Evolução da Banca (lucro acumulado)</h3>
    {chart_html}
  </div>

  <div class="grid2">
    {barra_grupo("Por Casa de Aposta", por_casa)}
    {barra_grupo("Por Esporte", por_esporte)}
  </div>

  <div class="card">
    <h3>Todas as Apostas</h3>
    <div class="filters">
      <input id="busca" type="text" placeholder="Buscar na descrição...">
      <select id="fEsporte"><option value="">Todos esportes</option>{esportes_opts}</select>
      <select id="fCasa"><option value="">Todas casas</option>{casas_opts}</select>
      <select id="fResultado">
        <option value="">Todos resultados</option>
        <option value="Green">Green</option><option value="Red">Red</option>
        <option value="Void">Void</option><option value="Cashout">Cashout</option>
        <option value="Pendente">Pendente</option>
      </select>
    </div>
    <div class="table-wrap">
      <table id="tabela">
        <thead>
          <tr>
            <th data-k="id">#</th><th data-k="data">Data</th><th data-k="hora">Hora</th>
            <th data-k="descricao" style="text-align:left">Descrição</th>
            <th data-k="odd">Odd</th><th data-k="stake">Stake</th>
            <th data-k="esporte">Esporte</th><th data-k="casa">Casa</th>
            <th data-k="resultado">Resultado</th><th data-k="lucro">Lucro</th>
            <th data-k="lucro_u">Lucro U</th><th data-k="banca">Banca</th>
          </tr>
        </thead>
        <tbody id="corpoTabela"></tbody>
      </table>
    </div>
  </div>

  <footer>Dashboard gerado localmente pelo bot · nenhum dado sai do seu computador</footer>

<script>
const DADOS = {linhas_json};
let ordemAtual = {{ campo: null, asc: true }};

function fmt(v, tipo) {{
  if (v === null || v === undefined) return '—';
  if (tipo === 'money') return 'R$ ' + Number(v).toFixed(2);
  if (tipo === 'u') return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + 'u';
  return v;
}}

function renderizar() {{
  const busca = document.getElementById('busca').value.toLowerCase();
  const fEsp = document.getElementById('fEsporte').value;
  const fCasa = document.getElementById('fCasa').value;
  const fRes = document.getElementById('fResultado').value;

  let linhas = DADOS.filter(l =>
    (!busca || l.descricao.toLowerCase().includes(busca)) &&
    (!fEsp || l.esporte === fEsp) &&
    (!fCasa || l.casa === fCasa) &&
    (!fRes || l.resultado === fRes)
  );

  if (ordemAtual.campo) {{
    linhas.sort((a, b) => {{
      let x = a[ordemAtual.campo], y = b[ordemAtual.campo];
      if (x === null) x = -Infinity;
      if (y === null) y = -Infinity;
      if (typeof x === 'string') return ordemAtual.asc ? x.localeCompare(y) : y.localeCompare(x);
      return ordemAtual.asc ? x - y : y - x;
    }});
  }}

  document.getElementById('corpoTabela').innerHTML = linhas.map(l => `
    <tr>
      <td>#${{l.id}}</td><td>${{l.data}}</td><td>${{l.hora || ''}}</td>
      <td class="desc">${{l.descricao}}</td>
      <td>${{Number(l.odd).toFixed(2)}}</td><td>${{fmt(l.stake, 'money')}}</td>
      <td>${{l.esporte}}</td><td>${{l.casa}}</td>
      <td><span class="badge ${{l.resultado}}">${{l.resultado}}</span></td>
      <td class="${{l.lucro == null ? '' : (l.lucro >= 0 ? 'pos' : 'neg')}}">${{fmt(l.lucro, 'money')}}</td>
      <td class="${{l.lucro_u == null ? '' : (l.lucro_u >= 0 ? 'pos' : 'neg')}}">${{fmt(l.lucro_u, 'u')}}</td>
      <td class="${{l.banca == null ? '' : (l.banca >= 0 ? 'pos' : 'neg')}}">${{fmt(l.banca, 'money')}}</td>
    </tr>`).join('');
}}

document.querySelectorAll('th[data-k]').forEach(th => {{
  th.addEventListener('click', () => {{
    const k = th.dataset.k;
    ordemAtual = {{ campo: k, asc: ordemAtual.campo === k ? !ordemAtual.asc : true }};
    renderizar();
  }});
}});

['busca', 'fEsporte', 'fCasa', 'fResultado'].forEach(id =>
  document.getElementById(id).addEventListener('input', renderizar)
);

renderizar();
</script>
</body>
</html>"""

async def gerar_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Gerando dashboard, aguarde...")
    try:
        await _gerar_dashboard_interno(update, ctx)
    except Exception as _e:
        import traceback as _tb
        _trace = _tb.format_exc()[-700:]
        try:
            await update.message.reply_text(
                f"❌ Erro ao gerar dashboard:\n{str(_e)}\n\n{_trace}",
                reply_markup=teclado_gerar()
            )
        except Exception:
            await update.message.reply_text("❌ Erro ao gerar dashboard. Verifique os logs.")

async def _gerar_dashboard_interno(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.chart import LineChart, Reference
    from openpyxl.utils import get_column_letter
    from datetime import timedelta

    apostas = carregar()
    apostas_ord = sorted(apostas, key=lambda a: (str(a["data"]), a.get("horario") or "99:99", int(a["id"])))
    df_res = [a for a in apostas_ord if a["resultado"] in ("ganhou","perdeu") or eh_cashout(a)]

    BANCA = 5000
    DARK  = "1E293B"; GREEN = "16A34A"; RED = "DC2626"; AMBER = "D97706"
    WHITE = "FFFFFF"; ALT   = "EFF6FF"; BORDER_C = "CBD5E1"

    def est(cell, bold=False, fc="FFFFFF", bg=None, size=11, align="center"):
        cell.font = Font(name="Arial", bold=bold, color=fc, size=size)
        cell.alignment = Alignment(horizontal=align, vertical="center")
        if bg: cell.fill = PatternFill("solid", start_color=bg)

    def brd():
        s = Side(style="thin", color=BORDER_C)
        return Border(left=s, right=s, top=s, bottom=s)

    def cor(val):
        try: return GREEN if float(val) >= 0 else RED
        except: return "000000"

    # Métricas
    lucro_total = sum(
        lucro_aposta(a) for a in df_res
    )
    stake_total = sum(float(a["stake"]) for a in df_res)
    vitorias    = sum(1 for a in df_res if a["resultado"]=="ganhou")
    win_rate    = vitorias/len(df_res) if df_res else 0
    roi         = lucro_total/stake_total if stake_total else 0
    progressao  = lucro_total/BANCA
    total       = len(apostas)
    resolvidas  = len(df_res)
    pendentes   = total - resolvidas

    # Banca acumulada
    banca_acum = []
    acum = 0
    for a in df_res:
        acum += lucro_aposta(a)
        banca_acum.append(acum)

    wb = openpyxl.Workbook()

    # ── ABA 1: DASHBOARD ──
    ws = wb.active; ws.title = "Dashboard"
    ws.row_dimensions[2].height = 8

    unidade_dash = get_unidade_atual()
    lucro_units_dash = sum(lucro_aposta(a) / float(a.get("unidade") or 50) for a in df_res)
    stake_curso = sum(stake_real(a) for a in apostas if a["resultado"]=="pendente")
    cards = [
        ("Total",         str(total),                    DARK),
        ("Resolvidas",    str(resolvidas),               DARK),
        ("Pendentes",     str(pendentes),                AMBER if pendentes else DARK),
        ("Win Rate",      f"{win_rate:.1%}",             GREEN if win_rate>=0.5 else RED),
        ("Lucro R$",      f"R${lucro_total:.2f}",        GREEN if lucro_total>=0 else RED),
        ("Lucro Units",   f"{lucro_units_dash:+.1f}u",   GREEN if lucro_units_dash>=0 else RED),
        ("ROI",           f"{roi:+.1%}",                 GREEN if roi>=0 else RED),
        ("Progressao",    f"{progressao:+.1%}",          GREEN if progressao>=0 else RED),
        ("Em Curso",      f"R${stake_curso:.0f}",        AMBER if stake_curso>0 else DARK),
    ]
    # 9 cards em 9 colunas com largura uniforme
    for i,(label,val,bg) in enumerate(cards,1):
        c=ws.cell(row=3,column=i,value=label); est(c,bg=bg,size=9,fc="DBEAFE"); ws.row_dimensions[3].height=20
        v=ws.cell(row=4,column=i,value=val);   est(v,bold=True,bg=bg,size=12);  ws.row_dimensions[4].height=32
        d=ws.cell(row=5,column=i);              est(d,bg="0F172A");              ws.row_dimensions[5].height=4
        ws.column_dimensions[get_column_letter(i)].width=15
    ws.row_dimensions[6].height=10

    HDR=7; DAT=8
    headers=["#","Data","Hora","Descricao","Odd","Stake","Esporte","Casa","Resultado","Lucro","Lucro U","Banca","Progressao"]
    ws.row_dimensions[HDR].height=22
    for c,h in enumerate(headers,1):
        cell=ws.cell(row=HDR,column=c,value=h); est(cell,bold=True,bg=DARK,size=10); cell.border=brd()

    acum2=0
    id_to_row={}
    for i,a in enumerate(apostas_ord):
        er=DAT+i; ws.row_dimensions[er].height=18
        id_to_row[a["id"]]=er
        rb=WHITE if i%2==0 else ALT
        res=a["resultado"]
        lucro_a=lucro_u_a=banca_a=prog_a=""
        if res in ("ganhou","perdeu") or eh_cashout(a):
            lucro_a=lucro_aposta(a)
            unid_a=float(a.get("unidade") or 50)
            lucro_u_a=round(lucro_a/unid_a,2) if unid_a else ""
            acum2+=lucro_a; banca_a=round(acum2,2); prog_a=round(acum2/BANCA,4)
        res_d={"ganhou":"Green","perdeu":"Red","void":"Void","pendente":"Pendente"}.get(res,res)
        if eh_cashout(a): res_d="Cashout"
        data_fmt=a["data"].strftime("%d/%m/%Y") if hasattr(a["data"],"strftime") else str(a["data"])[:10]
        vals=[a["id"],data_fmt,a.get("horario",""),a["descricao"],a["odd"],a["stake"],
              a.get("esporte",""),a.get("casa",""),res_d,lucro_a,lucro_u_a,banca_a,prog_a]
        for c,val in enumerate(vals,1):
            cell=ws.cell(row=er,column=c,value=val)
            fc="000000"
            if c in (10,11,12) and val!="": fc=cor(val)
            if c==13 and val!="": fc=cor(val)
            cell.font=Font(name="Arial",size=10,color=fc)
            cell.alignment=Alignment(horizontal="left" if c==4 else "center",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if c in (5,6,10,12) and val!="": cell.number_format="#,##0.00"
            if c==11 and val!="": cell.number_format="+0.00;-0.00;0.00"
            if c==13 and val!="": cell.number_format="+0.00%;-0.00%;0.00%"

    tr=DAT+len(apostas_ord); ws.row_dimensions[tr].height=20
    for c in range(1,14):
        cell=ws.cell(row=tr,column=c)
        if c==4: cell.value="TOTAL"
        elif c==6: cell.value=f"=SUM(F{DAT}:F{tr-1})"; cell.number_format="#,##0.00"
        elif c==10: cell.value=f"=SUM(J{DAT}:J{tr-1})"; cell.number_format="#,##0.00"
        elif c==11: cell.value=f"=SUM(K{DAT}:K{tr-1})"; cell.number_format="+0.00;-0.00;0.00"
        est(cell,bold=True,bg=DARK,size=10); cell.border=brd()

    widths=[8,12,11,55,14,12,14,20,13,12,10,13,12]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.merge_cells("A1:M1"); ws["A1"] = "DASHBOARD DE APOSTAS"
    est(ws["A1"], bold=True, bg=DARK, size=16); ws.row_dimensions[1].height=40

    # ── ABA 2: ESTATÍSTICAS ──
    from openpyxl.chart import BarChart
    wst = wb.create_sheet("Estatísticas", 1)

    # Cálculos
    apostas_ganhas   = [a for a in df_res if a["resultado"] == "ganhou"]
    apostas_perdidas = [a for a in df_res if a["resultado"] == "perdeu"]
    capital_em_jogo  = sum(float(a["stake"]) for a in apostas if a["resultado"] == "pendente")
    total_gastos_e   = total_gastos()
    lucro_real_e     = lucro_total - total_gastos_e
    capital_atual    = BANCA + lucro_total - capital_em_jogo - total_gastos_e
    maior_odd_acert  = max((float(a["odd"]) for a in apostas_ganhas), default=0)
    maior_stake_val  = max((float(a["stake"]) for a in df_res), default=0)
    stake_total_e    = sum(float(a["stake"]) for a in df_res)
    stake_media_e    = stake_total_e / len(df_res) if df_res else 0
    # Odd média ponderada por stake (retorno potencial total / stake total),
    # nao a media simples das odds — reflete quanto renderia por real apostado
    # se tudo desse green.
    odd_media_e      = (sum(float(a["stake"])*float(a["odd"]) for a in df_res) / stake_total_e) if stake_total_e else 0
    maior_lucro_e    = max((lucro_aposta(a) for a in apostas_ganhas), default=0)
    maior_perda_e    = min((lucro_aposta(a) for a in apostas_perdidas), default=0)
    lucro_medio_e    = lucro_total / len(df_res) if df_res else 0

    seq_verde_max = seq_verm_max = seq_verde_cur = seq_verm_cur = 0
    seq_verde_ini_id = seq_verde_fim_id = seq_verm_ini_id = seq_verm_fim_id = None
    _cur_verde_ini = _cur_verm_ini = None
    for a in df_res:
        if a["resultado"] == "ganhou":
            if seq_verde_cur == 0: _cur_verde_ini = a["id"]
            seq_verde_cur += 1; seq_verm_cur = 0
            if seq_verde_cur > seq_verde_max:
                seq_verde_max = seq_verde_cur
                seq_verde_ini_id, seq_verde_fim_id = _cur_verde_ini, a["id"]
        elif a["resultado"] == "perdeu":
            if seq_verm_cur == 0: _cur_verm_ini = a["id"]
            seq_verm_cur += 1; seq_verde_cur = 0
            if seq_verm_cur > seq_verm_max:
                seq_verm_max = seq_verm_cur
                seq_verm_ini_id, seq_verm_fim_id = _cur_verm_ini, a["id"]
        else:
            seq_verde_cur = seq_verm_cur = 0

    # Por Unidade
    por_unidade_e = {}
    for a in df_res:
        u = float(a.get("unidade") or 50)
        if u not in por_unidade_e:
            por_unidade_e[u] = {"ap":0,"g":0,"lucro":0.0,"stake":0.0}
        por_unidade_e[u]["ap"] += 1
        por_unidade_e[u]["lucro"] += lucro_aposta(a)
        por_unidade_e[u]["stake"] += float(a["stake"])
        if a["resultado"] == "ganhou": por_unidade_e[u]["g"] += 1

    # Layout
    wst.merge_cells("A1:C1"); wst["A1"] = "ESTATÍSTICAS"
    est(wst["A1"], bold=True, bg=DARK, size=14)
    wst.row_dimensions[1].height = 34; wst.row_dimensions[2].height = 6

    def stat_row(ws, row, label, valor, fmt=None, cor_val=False):
        cl = ws.cell(row=row, column=1, value=label)
        cl.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cl.fill = PatternFill("solid", start_color="1E3A5F")
        cl.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
        cl.border = brd()
        cv = ws.cell(row=row, column=2, value=valor)
        fc = "000000"
        if cor_val:
            try: fc = GREEN if float(valor) >= 0 else RED
            except: pass
        cv.font = Font(name="Arial", size=10, bold=True, color=fc)
        cv.alignment = Alignment(horizontal="center", vertical="center")
        cv.fill = PatternFill("solid", start_color="EFF6FF")
        cv.border = brd()
        if fmt: cv.number_format = fmt
        ws.row_dimensions[row].height = 20

    metricas = [
        ("Capital Atual (R$)",         round(capital_atual, 2),      "#,##0.00", True),
        ("Despesas Totais (R$)",       round(total_gastos_e, 2),     "#,##0.00", False),
        ("Lucro Real (R$)",            round(lucro_real_e, 2),       "#,##0.00", True),
        ("Stake Investida Total (R$)",  round(stake_total_e, 2),      "#,##0.00", False),
        ("Stake Média (R$)",            round(stake_media_e, 2),      "#,##0.00", False),
        ("Odd Média",                   round(odd_media_e, 3),        "0.000",    False),
        ("Lucro Médio por Aposta (R$)", round(lucro_medio_e, 2),      "#,##0.00", True),
        ("Maior Odd Acertada",          round(maior_odd_acert, 3),    "0.000",    False),
        ("Maior Stake (R$)",            round(maior_stake_val, 2),    "#,##0.00", False),
        ("Maior Lucro em uma aposta (R$)", round(maior_lucro_e, 2),  "#,##0.00", True),
        ("Maior Perda em uma aposta (R$)", round(maior_perda_e, 2),  "#,##0.00", True),
        ("Maior Sequência de Greens",   seq_verde_max,                "0",        False),
        ("Maior Sequência de Reds",     seq_verm_max,                 "0",        False),
    ]
    for i,(label,valor,fmt,cor_v) in enumerate(metricas):
        stat_row(wst, 3+i, label, valor, fmt, cor_v)

    def _seq_id_box(row, ini_id, fim_id):
        wst.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        cell = wst.cell(row=row, column=3)
        cell.value = f"IDs: #{ini_id} → #{fim_id}" if ini_id is not None else "—"
        cell.font = Font(name="Arial", size=9, italic=True, color="1E3A5F")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.fill = PatternFill("solid", start_color="EFF6FF")
        cell.border = brd()
        if ini_id in id_to_row:
            cell.hyperlink = f"#Dashboard!A{id_to_row[ini_id]}"

    _row_greens = 3 + metricas.index(next(m for m in metricas if m[0]=="Maior Sequência de Greens"))
    _row_reds   = 3 + metricas.index(next(m for m in metricas if m[0]=="Maior Sequência de Reds"))
    _seq_id_box(_row_greens, seq_verde_ini_id, seq_verde_fim_id)
    _seq_id_box(_row_reds,   seq_verm_ini_id,  seq_verm_fim_id)

    # ── LEADERBOARD (colunas D em diante) ──
    LB_COL = 5
    LB_HEADERS = ["#","Data","Hora","Descrição","Odd","Stake","Casa","Esporte","Lucro R$","Lucro U"]
    LB_WIDTHS  = [8, 12, 8, 70, 8, 10, 20, 14, 12, 10]
    N_LB = len(LB_HEADERS)

    def lb_title(row, titulo, bg_color):
        wst.merge_cells(start_row=row, start_column=LB_COL, end_row=row, end_column=LB_COL+N_LB-1)
        cell = wst.cell(row=row, column=LB_COL, value=titulo)
        est(cell, bold=True, bg=bg_color, size=11)
        wst.row_dimensions[row].height = 22

    def lb_headers_row(row):
        for j, h in enumerate(LB_HEADERS):
            cell = wst.cell(row=row, column=LB_COL+j, value=h)
            est(cell, bold=True, bg="1E3A5F", size=9); cell.border = brd()
        wst.row_dimensions[row].height = 18

    def lb_data_row(row, aposta, idx):
        l = lucro_aposta(aposta)
        unid = float(aposta.get("unidade") or 50)
        lucro_u = round(l / unid, 2) if unid else 0
        data_fmt = aposta["data"].strftime("%d/%m/%Y") if hasattr(aposta["data"],"strftime") else str(aposta["data"])[:10]
        vals = [aposta["id"], data_fmt, aposta.get("horario",""), aposta["descricao"],
                float(aposta["odd"]), float(aposta["stake"]),
                aposta.get("casa",""), aposta.get("esporte",""), round(l,2), lucro_u]
        rb = WHITE if idx%2==0 else ALT
        for j, val in enumerate(vals):
            cell = wst.cell(row=row, column=LB_COL+j, value=val)
            fc = cor(val) if j in (8,9) else "000000"
            cell.font = Font(name="Arial", size=9, color=fc)
            cell.alignment = Alignment(horizontal="left" if j==3 else "center", vertical="center", wrap_text=False)
            cell.fill = PatternFill("solid", start_color=rb); cell.border = brd()
            if j in (4,5,8,9): cell.number_format = "#,##0.00"
        wst.row_dimensions[row].height = 18

    lb_r = 3

    # Top 5 Greens
    top5_greens = sorted(apostas_ganhas, key=lambda a: lucro_aposta(a), reverse=True)[:5]
    lb_title(lb_r, "🏆 TOP 5 MAIORES GREENS", GREEN); lb_r += 1
    lb_headers_row(lb_r); lb_r += 1
    for i, a in enumerate(top5_greens):
        lb_data_row(lb_r, a, i); lb_r += 1

    lb_r += 1  # gap

    # Top 5 Reds
    top5_reds = sorted(apostas_perdidas, key=lambda a: lucro_aposta(a))[:5]
    lb_title(lb_r, "💀 TOP 5 MAIORES REDS", RED); lb_r += 1
    lb_headers_row(lb_r); lb_r += 1
    for i, a in enumerate(top5_reds):
        lb_data_row(lb_r, a, i); lb_r += 1

    lb_r += 1  # gap

    # Recordes: Melhor Dia / Semana / Mês
    from collections import defaultdict as _dd_lb
    import datetime as _dt_lb

    por_dia_lb = _dd_lb(list)
    for a in df_res:
        por_dia_lb[str(a["data"])[:10]].append(a)
    melhor_dia_k = max(por_dia_lb, key=lambda k: sum(lucro_aposta(x) for x in por_dia_lb[k])) if por_dia_lb else None
    melhor_dia_lucro = round(sum(lucro_aposta(x) for x in por_dia_lb[melhor_dia_k]),2) if melhor_dia_k else 0
    try: melhor_dia_fmt = _dt_lb.datetime.strptime(melhor_dia_k,"%Y-%m-%d").strftime("%d/%m/%Y")
    except: melhor_dia_fmt = melhor_dia_k or "—"

    pior_dia_k = min(por_dia_lb, key=lambda k: sum(lucro_aposta(x) for x in por_dia_lb[k])) if por_dia_lb else None
    pior_dia_lucro = round(sum(lucro_aposta(x) for x in por_dia_lb[pior_dia_k]),2) if pior_dia_k else 0
    try: pior_dia_fmt = _dt_lb.datetime.strptime(pior_dia_k,"%Y-%m-%d").strftime("%d/%m/%Y")
    except: pior_dia_fmt = pior_dia_k or "—"

    melhor_media_dia_k = max(
        por_dia_lb, key=lambda k: sum(lucro_aposta(x) for x in por_dia_lb[k]) / len(por_dia_lb[k])
    ) if por_dia_lb else None
    melhor_media_dia_val = round(
        sum(lucro_aposta(x) for x in por_dia_lb[melhor_media_dia_k]) / len(por_dia_lb[melhor_media_dia_k]), 2
    ) if melhor_media_dia_k else 0
    try: melhor_media_dia_fmt = _dt_lb.datetime.strptime(melhor_media_dia_k,"%Y-%m-%d").strftime("%d/%m/%Y")
    except: melhor_media_dia_fmt = melhor_media_dia_k or "—"

    def _dom_lb(dt):
        if not isinstance(dt, _dt_lb.datetime):
            dt = _dt_lb.datetime(dt.year, dt.month, dt.day) if isinstance(dt, _dt_lb.date) else _dt_lb.datetime.strptime(str(dt)[:10],"%Y-%m-%d")
        return (dt - timedelta(days=(dt.weekday()+1)%7)).replace(hour=0,minute=0,second=0,microsecond=0)

    por_sem_lb = _dd_lb(list)
    for a in df_res:
        por_sem_lb[_dom_lb(a["data"])].append(a)
    melhor_sem_k = max(por_sem_lb, key=lambda k: sum(lucro_aposta(x) for x in por_sem_lb[k])) if por_sem_lb else None
    melhor_sem_lucro = round(sum(lucro_aposta(x) for x in por_sem_lb[melhor_sem_k]),2) if melhor_sem_k else 0
    if melhor_sem_k:
        sab_lb = melhor_sem_k + timedelta(days=6)
        melhor_sem_fmt = f"{melhor_sem_k.strftime('%d/%m')} - {sab_lb.strftime('%d/%m/%Y')}"
    else:
        melhor_sem_fmt = "—"

    por_mes_lb = _dd_lb(list)
    for a in df_res:
        d_lb = a["data"] if hasattr(a["data"],"strftime") else _dt_lb.datetime.strptime(str(a["data"])[:10],"%Y-%m-%d")
        por_mes_lb[d_lb.strftime("%Y-%m")].append(a)
    melhor_mes_k = max(por_mes_lb, key=lambda k: sum(lucro_aposta(x) for x in por_mes_lb[k])) if por_mes_lb else None
    melhor_mes_lucro = round(sum(lucro_aposta(x) for x in por_mes_lb[melhor_mes_k]),2) if melhor_mes_k else 0
    _NOMES_M = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    if melhor_mes_k:
        _ano_lb,_mes_lb = int(melhor_mes_k[:4]),int(melhor_mes_k[5:])
        melhor_mes_fmt = f"{_NOMES_M[_mes_lb]}/{_ano_lb}"
    else:
        melhor_mes_fmt = "—"

    wst.merge_cells(start_row=lb_r, start_column=LB_COL, end_row=lb_r, end_column=LB_COL+N_LB-1)
    cell_rec = wst.cell(row=lb_r, column=LB_COL, value="📅 RECORDES DE LUCRO")
    est(cell_rec, bold=True, bg=DARK, size=11); wst.row_dimensions[lb_r].height = 22; lb_r += 1

    records = [
        ("Melhor Dia",    melhor_dia_fmt, melhor_dia_lucro),
        ("Pior Dia",      pior_dia_fmt,   pior_dia_lucro),
        ("Melhor Média/Aposta (Dia)", melhor_media_dia_fmt, melhor_media_dia_val),
        ("Melhor Semana", melhor_sem_fmt, melhor_sem_lucro),
        ("Melhor Mês",    melhor_mes_fmt, melhor_mes_lucro),
    ]
    _dia_key_por_label = {
        "Melhor Dia": melhor_dia_k,
        "Pior Dia": pior_dia_k,
        "Melhor Média/Aposta (Dia)": melhor_media_dia_k,
    }
    for i,(label_r, periodo_r, lucro_r) in enumerate(records):
        rb = WHITE if i%2==0 else ALT
        for j, val in enumerate([label_r, periodo_r, round(lucro_r, 2)]):
            cell = wst.cell(row=lb_r, column=LB_COL+j, value=val)
            fc = cor(lucro_r) if j==2 else ("1E3A5F" if j==0 else "000000")
            cell.font = Font(name="Arial", size=10, bold=(j!=1), color=fc)
            cell.fill = PatternFill("solid", start_color=rb); cell.border = brd()
            cell.alignment = Alignment(horizontal="left" if j<2 else "center", vertical="center")
            if j==2: cell.number_format = "#,##0.00"
            if j==0 and label_r in _dia_key_por_label:
                dia_k = _dia_key_por_label[label_r]
                bets_do_dia = por_dia_lb.get(dia_k) if dia_k else None
                if bets_do_dia:
                    primeiro_id = bets_do_dia[0]["id"]
                    if primeiro_id in id_to_row:
                        cell.hyperlink = f"#Dashboard!A{id_to_row[primeiro_id]}"
                        cell.font = Font(name="Arial", size=10, bold=True, color="0563C1", underline="single")
        for j in range(3, N_LB):
            cell = wst.cell(row=lb_r, column=LB_COL+j)
            cell.fill = PatternFill("solid", start_color=rb); cell.border = brd()
        wst.row_dimensions[lb_r].height=20; lb_r+=1

    for j,w in enumerate(LB_WIDTHS):
        col_l = get_column_letter(LB_COL+j)
        cur_w = wst.column_dimensions[col_l].width or 0
        if cur_w < w:
            wst.column_dimensions[col_l].width = w

    # Por Unidade: começa DEPOIS que o leaderboard terminar (evita colisão de merged cells nas cols 5-9)
    pu_start = max(3 + len(metricas) + 2, lb_r + 1)
    wst.row_dimensions[pu_start-1].height = 8
    wst.merge_cells(f"A{pu_start}:C{pu_start}"); cell_pu = wst[f"A{pu_start}"]
    cell_pu.value = "POR UNIDADE"
    est(cell_pu, bold=True, bg=DARK, size=11)
    wst.row_dimensions[pu_start].height = 22
    for col,h in enumerate(["Unidade","Apostas","Ganhou","Perdeu","Stake","Lucro R$","Lucro Units","ROI","Win Rate"],1):
        cell = wst.cell(row=pu_start+1, column=col, value=h)
        est(cell, bold=True, bg="1E3A5F", size=9); cell.border = brd()
        wst.row_dimensions[pu_start+1].height = 18
    for i,u_val in enumerate(sorted(por_unidade_e.keys())):
        d = por_unidade_e[u_val]
        er = pu_start+2+i; rb = WHITE if i%2==0 else ALT
        roi_u = d["lucro"]/d["stake"] if d["stake"] else 0
        wr_u  = d["g"]/d["ap"] if d["ap"] else 0
        lu_u  = d["lucro"]/u_val if u_val else 0
        for col,val in enumerate([f"R$ {u_val:.0f}/ap",d["ap"],d["g"],d["ap"]-d["g"],round(d["stake"],2),round(d["lucro"],2),round(lu_u,2),roi_u,wr_u],1):
            cell = wst.cell(row=er,column=col,value=val); fc="000000"
            if col in (6,7,8): fc=cor(val)
            cell.font=Font(name="Arial",size=9,color=fc)
            cell.alignment=Alignment(horizontal="center" if col>1 else "left",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if col in (5,6): cell.number_format="#,##0.00"
            if col==7: cell.number_format="+0.00;-0.00;0.00"
            if col in (8,9): cell.number_format="+0.0%;-0.0%;0.0%"
        wst.row_dimensions[er].height=18

    # Larguras — só aplica se menor que o valor já definido (LB pode ter setado maior)
    wst.column_dimensions["A"].width = 32
    wst.column_dimensions["B"].width = 16
    for i in range(3,10):
        col_l = get_column_letter(i)
        if (wst.column_dimensions[col_l].width or 0) < 12:
            wst.column_dimensions[col_l].width = 12

    # ── Gráficos por faixa de odds (0.5 em 0.5, sem faixas vazias) ──
    odd_max = max((float(a["odd"]) for a in df_res), default=3.5)
    lo = 1.00
    FAIXAS = []
    while lo < odd_max + 0.5:
        hi = round(lo + 0.49, 2)
        FAIXAS.append((round(lo,2), hi))
        lo = round(lo + 0.5, 2)

    def lbl_f(lo,hi): return f"{lo:.2f}-{hi:.2f}"

    dados_faixa = []
    for lo,hi in FAIXAS:
        ap_f = [a for a in df_res if lo <= float(a["odd"]) <= hi]
        if not ap_f: continue
        g_f = sum(1 for a in ap_f if a["resultado"]=="ganhou")
        p_f = sum(1 for a in ap_f if a["resultado"]=="perdeu")
        lucro_f = round(sum(lucro_aposta(a) for a in ap_f),2)
        dados_faixa.append((lbl_f(lo,hi), g_f, p_f, lucro_f))

    # Linha dinâmica: começa após Por Unidade (pu_start + 2 linhas header + nº unidades + 2 de gap)
    n_unidades = len(por_unidade_e)
    data_row = pu_start + 2 + n_unidades + 2  # header PU + col header PU + linhas PU + gap
    hdr_col = 1  # coluna A

    # Título da seção
    wst.merge_cells(f"A{data_row}:D{data_row}")
    cell_fo = wst[f"A{data_row}"]
    cell_fo.value = "POR FAIXA DE ODDS"
    est(cell_fo, bold=True, bg=DARK, size=11)
    wst.row_dimensions[data_row].height = 22
    data_row += 1

    headers_f = ["Faixa de Odds","Ganhou","Perdeu","Lucro R$"]
    for c,h in enumerate(headers_f, hdr_col):
        cell = wst.cell(row=data_row, column=c, value=h)
        est(cell, bold=True, bg="1E3A5F", size=9); cell.border=brd()
        wst.row_dimensions[data_row].height=18
    tbl_hdr_row = data_row
    data_row += 1

    for i,(faixa,g,p,lf) in enumerate(dados_faixa):
        er = data_row+i; rb = WHITE if i%2==0 else ALT
        for c,val in enumerate([faixa,g,p,lf], hdr_col):
            cell=wst.cell(row=er,column=c,value=val)
            fc="000000"
            if c==hdr_col+3: fc=cor(val)
            cell.font=Font(name="Arial",size=9,color=fc)
            cell.alignment=Alignment(horizontal="center" if c>hdr_col else "left",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if c==hdr_col+3: cell.number_format="#,##0.00"
        wst.row_dimensions[er].height=15

    n = len(dados_faixa)
    cats = Reference(wst, min_col=hdr_col,   min_row=tbl_hdr_row+1, max_row=tbl_hdr_row+n)
    d_g  = Reference(wst, min_col=hdr_col+1, min_row=tbl_hdr_row,   max_row=tbl_hdr_row+n)
    d_p  = Reference(wst, min_col=hdr_col+2, min_row=tbl_hdr_row,   max_row=tbl_hdr_row+n)
    d_l  = Reference(wst, min_col=hdr_col+3, min_row=tbl_hdr_row,   max_row=tbl_hdr_row+n)

    # Gráfico 1 — apostas por faixa (coluna F, mesma linha da tabela)
    bar1 = BarChart(); bar1.type="col"; bar1.grouping="clustered"; bar1.overlap=0
    bar1.title="Apostas por Faixa de Odds"; bar1.style=10
    bar1.y_axis.title="Apostas"; bar1.width=24; bar1.height=14
    bar1.add_data(d_g, titles_from_data=True); bar1.add_data(d_p, titles_from_data=True)
    bar1.set_categories(cats)
    bar1.series[0].graphicalProperties.solidFill="16A34A"
    bar1.series[1].graphicalProperties.solidFill="DC2626"
    wst.add_chart(bar1, f"F{tbl_hdr_row}")

    # Gráfico 2 — lucro por faixa (coluna R, mesma linha — lado a lado com o gráfico 1)
    bar2 = BarChart(); bar2.type="col"; bar2.grouping="clustered"
    bar2.title="Lucro R$ por Faixa de Odds"; bar2.style=10
    bar2.y_axis.title="Lucro R$"; bar2.width=24; bar2.height=14
    bar2.add_data(d_l, titles_from_data=True); bar2.set_categories(cats)
    bar2.series[0].graphicalProperties.solidFill="2563EB"
    wst.add_chart(bar2, f"R{tbl_hdr_row}")

    # ── ABA 3: LUCRO POR DIA ──
    from collections import defaultdict
    import calendar as _cal
    import datetime as _dtcal
    from openpyxl.comments import Comment

    por_dia=defaultdict(list)
    for a in df_res:
        por_dia[str(a["data"])[:10]].append(a)

    # Descobre range de meses a gerar
    datas_todas = sorted(por_dia.keys())
    if datas_todas:
        dt_min = _dtcal.datetime.strptime(datas_todas[0], "%Y-%m-%d")
        dt_max = _dtcal.datetime.strptime(datas_todas[-1], "%Y-%m-%d")
    else:
        dt_min = dt_max = _dtcal.datetime.now()

    # lucro por dia (dict date_str → float)
    lucro_por_dia = {k: round(sum(lucro_aposta(a) for a in v), 2) for k,v in por_dia.items()}
    stake_por_dia = {k: sum(float(a["stake"]) for a in v) for k,v in por_dia.items()}
    n_por_dia     = {k: len(v) for k,v in por_dia.items()}

    # Paleta
    BG_GREEN_STRONG = "0D5C2E"; BG_GREEN_MID = "1A8A3E"; BG_GREEN_LIGHT = "2DB55D"
    BG_RED_STRONG   = "7B0D0D"; BG_RED_MID   = "B52020"; BG_RED_LIGHT   = "D94040"
    BG_EMPTY        = "1E293B"; BG_TODAY      = "2D3A5C"
    DIAS_SEMANA = ["Dom","Seg","Ter","Qua","Qui","Sex","Sáb"]

    wd = wb.create_sheet("Calendário")
    wd.sheet_view.showGridLines = False

    row_cursor = 1
    mes_atual = dt_min.replace(day=1)
    while mes_atual <= dt_max.replace(day=1):
        ano, mes = mes_atual.year, mes_atual.month
        NOMES_M_CAL = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                        "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

        # Título do mês
        wd.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=7)
        cel_titulo = wd.cell(row=row_cursor, column=1, value=f"{NOMES_M_CAL[mes].upper()}  {ano}")
        cel_titulo.font = Font(name="Arial", bold=True, size=14, color="FFFFFF")
        cel_titulo.fill = PatternFill("solid", start_color=DARK)
        cel_titulo.alignment = Alignment(horizontal="center", vertical="center")
        wd.row_dimensions[row_cursor].height = 30
        row_cursor += 1

        # Cabeçalho dias da semana
        for c_idx, dia_nome in enumerate(DIAS_SEMANA, 1):
            cel = wd.cell(row=row_cursor, column=c_idx, value=dia_nome)
            cel.font = Font(name="Arial", bold=True, size=10, color="A0AEC0")
            cel.fill = PatternFill("solid", start_color="0F172A")
            cel.alignment = Alignment(horizontal="center", vertical="center")
        wd.row_dimensions[row_cursor].height = 22
        row_cursor += 1

        # Gera semanas do mês (calendar.monthcalendar, semanas dom-sab)
        cal_obj = _cal.Calendar(firstweekday=6)  # domingo=0
        semanas = cal_obj.monthdayscalendar(ano, mes)

        for semana in semanas:
            for c_idx, dia_num in enumerate(semana, 1):
                cel = wd.cell(row=row_cursor, column=c_idx)
                if dia_num == 0:
                    cel.fill = PatternFill("solid", start_color="0A1020")
                else:
                    data_str = f"{ano:04d}-{mes:02d}-{dia_num:02d}"
                    lucro_d = lucro_por_dia.get(data_str)
                    if lucro_d is None:
                        bg = BG_EMPTY
                        txt = str(dia_num)
                        fc_txt = "4A5568"
                    else:
                        positivo = lucro_d >= 0
                        abs_l = abs(lucro_d)
                        if positivo:
                            bg = BG_GREEN_STRONG if abs_l >= 500 else BG_GREEN_MID if abs_l >= 100 else BG_GREEN_LIGHT
                        else:
                            bg = BG_RED_STRONG if abs_l >= 500 else BG_RED_MID if abs_l >= 100 else BG_RED_LIGHT
                        fc_txt = "FFFFFF"
                        # Formata valor
                        if abs_l >= 1000:
                            val_str = f"${abs_l/1000:.1f}K" if abs_l < 10000 else f"${abs_l/1000:.0f}K"
                        else:
                            val_str = f"${abs_l:.0f}" if abs_l == int(abs_l) else f"${abs_l:.1f}"
                        if not positivo: val_str = "-" + val_str
                        txt = f"{dia_num}\n{val_str}"
                    cel.value = txt
                    cel.font = Font(name="Arial", bold=True, size=9, color=fc_txt)
                    cel.fill = PatternFill("solid", start_color=bg)
                    cel.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    if lucro_d is not None:
                        n_d = n_por_dia.get(data_str, 0)
                        stake_d = stake_por_dia.get(data_str, 0)
                        media_d = lucro_d / n_d if n_d else 0
                        roi_d = lucro_d / stake_d if stake_d else 0
                        cel.comment = Comment(
                            f"{n_d} aposta(s)\nMédia/aposta: R$ {media_d:.2f}\nROI do dia: {roi_d:+.1%}",
                            "Dashboard"
                        )
            wd.row_dimensions[row_cursor].height = 42
            row_cursor += 1

        # Linha de totais do mês
        lucro_mes_cal = sum(v for k,v in lucro_por_dia.items() if k[:7]==f"{ano:04d}-{mes:02d}")
        dias_com_acao = sum(1 for k in lucro_por_dia if k[:7]==f"{ano:04d}-{mes:02d}")
        wd.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=7)
        sinal_m = "+" if lucro_mes_cal >= 0 else ""
        cel_tot = wd.cell(row=row_cursor, column=1, value=f"Total {NOMES_M_CAL[mes]}: {sinal_m}R$ {lucro_mes_cal:.2f}  |  {dias_com_acao} dias com apostas")
        fc_tot = GREEN if lucro_mes_cal >= 0 else RED
        cel_tot.font = Font(name="Arial", bold=True, size=10, color=fc_tot)
        cel_tot.fill = PatternFill("solid", start_color="0F172A")
        cel_tot.alignment = Alignment(horizontal="center", vertical="center")
        wd.row_dimensions[row_cursor].height = 20
        row_cursor += 2  # gap entre meses

        # Próximo mês
        if mes == 12:
            mes_atual = mes_atual.replace(year=ano+1, month=1)
        else:
            mes_atual = mes_atual.replace(month=mes+1)

    # Largura das colunas (7 colunas iguais)
    for c_idx in range(1, 8):
        wd.column_dimensions[get_column_letter(c_idx)].width = 14

    # ── ABA 3: POR CASA ──
    casas={}
    for a in df_res:
        casa=normalizar_casa(a.get("casa"))
        if casa not in casas: casas[casa]={"ap":0,"g":0,"stake":0.0,"lucro":0.0,"lucro_u":0.0}
        c=casas[casa]; c["ap"]+=1; c["stake"]+=float(a["stake"])
        c["lucro"]+=lucro_aposta(a)
        c["lucro_u"]+=lucro_aposta(a)/float(a.get("unidade") or 50)
        if a["resultado"]=="ganhou": c["g"]+=1
    wc=wb.create_sheet("Por Casa")
    wc.merge_cells("A1:I1"); wc["A1"]="POR CASA"
    est(wc["A1"],bold=True,bg=DARK,size=14); wc.row_dimensions[1].height=34; wc.row_dimensions[2].height=8
    for c,h in enumerate(["Casa","Apostas","Ganhou","Perdeu","Stake","Lucro R$","Lucro Units","ROI","Win Rate"],1):
        cell=wc.cell(row=3,column=c,value=h); est(cell,bold=True,bg=DARK,size=10); cell.border=brd()
    wc.row_dimensions[3].height=22
    for i,(nome,c) in enumerate(sorted(casas.items(),key=lambda x:-x[1]["lucro"])):
        er=4+i; rb=WHITE if i%2==0 else ALT
        roi_c=c["lucro"]/c["stake"] if c["stake"] else 0
        wr_c=c["g"]/c["ap"] if c["ap"] else 0
        for col,val in enumerate([nome,c["ap"],c["g"],c["ap"]-c["g"],c["stake"],c["lucro"],round(c["lucro_u"],2),roi_c,wr_c],1):
            cell=wc.cell(row=er,column=col,value=val); fc="000000"
            if col in (6,7,8): fc=cor(val)
            cell.font=Font(name="Arial",size=10,color=fc)
            cell.alignment=Alignment(horizontal="left" if col==1 else "center",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if col in (5,6): cell.number_format="#,##0.00"
            if col==7: cell.number_format="+0.00;-0.00;0.00"
            if col in (8,9): cell.number_format="+0.0%;-0.0%;0.0%"
        wc.row_dimensions[er].height=18
    for i,w in enumerate([18,10,10,10,14,14,14,10,12],1): wc.column_dimensions[get_column_letter(i)].width=w

    # ── ABA 4: POR ESPORTE ──
    esportes={}
    for a in df_res:
        esp=normalizar_esporte(a.get("esporte")) or "Sem esporte"
        if esp not in esportes: esportes[esp]={"ap":0,"g":0,"stake":0.0,"lucro":0.0,"lucro_u":0.0}
        e=esportes[esp]; e["ap"]+=1; e["stake"]+=float(a["stake"])
        e["lucro"]+=lucro_aposta(a)
        e["lucro_u"]+=lucro_aposta(a)/float(a.get("unidade") or 50)
        if a["resultado"]=="ganhou": e["g"]+=1
    we=wb.create_sheet("Por Esporte")
    we.merge_cells("A1:I1"); we["A1"]="POR ESPORTE"
    est(we["A1"],bold=True,bg=DARK,size=14); we.row_dimensions[1].height=34; we.row_dimensions[2].height=8
    for c,h in enumerate(["Esporte","Apostas","Ganhou","Perdeu","Stake","Lucro R$","Lucro Units","ROI","Win Rate"],1):
        cell=we.cell(row=3,column=c,value=h); est(cell,bold=True,bg=DARK,size=10); cell.border=brd()
    we.row_dimensions[3].height=22
    for i,(nome,e) in enumerate(sorted(esportes.items(),key=lambda x:-x[1]["lucro"])):
        er=4+i; rb=WHITE if i%2==0 else ALT
        roi_e=e["lucro"]/e["stake"] if e["stake"] else 0
        wr_e=e["g"]/e["ap"] if e["ap"] else 0
        for col,val in enumerate([nome,e["ap"],e["g"],e["ap"]-e["g"],e["stake"],e["lucro"],round(e["lucro_u"],2),roi_e,wr_e],1):
            cell=we.cell(row=er,column=col,value=val); fc="000000"
            if col in (6,7,8): fc=cor(val)
            cell.font=Font(name="Arial",size=10,color=fc)
            cell.alignment=Alignment(horizontal="left" if col==1 else "center",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if col in (5,6): cell.number_format="#,##0.00"
            if col==7: cell.number_format="+0.00;-0.00;0.00"
            if col in (8,9): cell.number_format="+0.0%;-0.0%;0.0%"
        we.row_dimensions[er].height=18
    for i,w in enumerate([18,10,10,10,14,14,14,10,12],1): we.column_dimensions[get_column_letter(i)].width=w

    # ── ABA 5: POR SEMANA (domingo a sabado) ──
    import datetime as dt_mod
    def domingo_da_semana(dt):
        """Retorna o domingo que inicia a semana (dom-sab) de uma data."""
        # Garante que seja datetime (date.replace não aceita hour/minute/second)
        if not isinstance(dt, dt_mod.datetime):
            if isinstance(dt, dt_mod.date):
                dt = dt_mod.datetime(dt.year, dt.month, dt.day)
            else:
                dt = dt_mod.datetime.strptime(str(dt)[:10], "%Y-%m-%d")
        dias = (dt.weekday() + 1) % 7
        return (dt - timedelta(days=dias)).replace(hour=0, minute=0, second=0, microsecond=0)
    por_sem=defaultdict(list)
    for a in df_res:
        dom=domingo_da_semana(a["data"])
        por_sem[dom].append(a)
    ws2=wb.create_sheet("Por Semana")
    ws2.merge_cells("A1:I1"); ws2["A1"]="POR SEMANA"
    est(ws2["A1"],bold=True,bg=DARK,size=14); ws2.row_dimensions[1].height=34; ws2.row_dimensions[2].height=8
    for c,h in enumerate(["Semana","Periodo","Apostas","Ganhou","Perdeu","Stake","Lucro R$","Lucro Units","ROI"],1):
        cell=ws2.cell(row=3,column=c,value=h); est(cell,bold=True,bg=DARK,size=10); cell.border=brd()
    ws2.row_dimensions[3].height=22
    for i,(dom,ap) in enumerate(sorted(por_sem.items())):
        er=4+i; rb=WHITE if i%2==0 else ALT
        lucro_s=sum(lucro_aposta(a) for a in ap)
        stake_s=sum(float(a["stake"]) for a in ap)
        g_s=sum(1 for a in ap if a["resultado"]=="ganhou")
        roi_s=lucro_s/stake_s if stake_s else 0
        lucro_u_s=sum(lucro_aposta(a)/float(a.get("unidade") or 50) for a in ap)
        sab=dom+timedelta(days=6)
        num_semana=dom.isocalendar()[1]
        periodo=f"{dom.strftime('%d/%m')} - {sab.strftime('%d/%m/%Y')}"
        for col,val in enumerate([f"Semana {num_semana}",periodo,len(ap),g_s,len(ap)-g_s,stake_s,lucro_s,round(lucro_u_s,2),roi_s],1):
            cell=ws2.cell(row=er,column=col,value=val); fc="000000"
            if col in (7,8,9): fc=cor(val)
            cell.font=Font(name="Arial",size=10,color=fc)
            cell.alignment=Alignment(horizontal="left" if col==2 else "center",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if col in (6,7): cell.number_format="#,##0.00"
            if col==8: cell.number_format="+0.00;-0.00;0.00"
            if col==9: cell.number_format="+0.0%;-0.0%;0.0%"
        ws2.row_dimensions[er].height=18
    for i,w in enumerate([12,22,10,10,10,14,14,14,10],1): ws2.column_dimensions[get_column_letter(i)].width=w

    # ── ABA 6: POR MÊS ──
    from collections import defaultdict as _dd2
    NOMES_MES_XL = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    por_mes_xl = _dd2(list)
    for a in df_res:
        d = a["data"] if hasattr(a["data"],"strftime") else __import__("datetime").datetime.strptime(str(a["data"])[:10],"%Y-%m-%d")
        por_mes_xl[d.strftime("%Y-%m")].append(a)

    wm = wb.create_sheet("Por Mes")
    wm.merge_cells("A1:I1"); wm["A1"] = "POR MÊS"
    est(wm["A1"],bold=True,bg=DARK,size=14); wm.row_dimensions[1].height=34; wm.row_dimensions[2].height=8
    for c,h in enumerate(["Mês","Apostas","Ganhou","Perdeu","Stake","Lucro R$","Lucro Units","ROI","Win Rate"],1):
        cell=wm.cell(row=3,column=c,value=h); est(cell,bold=True,bg=DARK,size=10); cell.border=brd()
    wm.row_dimensions[3].height=22

    acum_m = 0
    for i,chave in enumerate(sorted(por_mes_xl.keys())):
        ap_m = por_mes_xl[chave]
        ano_m,mes_m = int(chave[:4]),int(chave[5:])
        lucro_m  = sum(lucro_aposta(a) for a in ap_m)
        stake_m  = sum(float(a["stake"]) for a in ap_m)
        g_m      = sum(1 for a in ap_m if a["resultado"]=="ganhou")
        roi_m    = lucro_m/stake_m if stake_m else 0
        wr_m     = g_m/len(ap_m) if ap_m else 0
        # Lucro em unidades: cada aposta usa a unidade registrada nela
        lucro_u_m = sum(lucro_aposta(a)/float(a.get("unidade") or 50) for a in ap_m)
        acum_m  += lucro_m
        er=4+i; rb=WHITE if i%2==0 else ALT
        for col,val in enumerate([f"{NOMES_MES_XL[mes_m]}/{ano_m}",len(ap_m),g_m,len(ap_m)-g_m,stake_m,lucro_m,round(lucro_u_m,2),roi_m,wr_m],1):
            cell=wm.cell(row=er,column=col,value=val); fc="000000"
            if col in (6,7,8): fc=cor(val)
            cell.font=Font(name="Arial",size=10,color=fc)
            cell.alignment=Alignment(horizontal="left" if col==1 else "center",vertical="center")
            cell.fill=PatternFill("solid",start_color=rb); cell.border=brd()
            if col in (5,6): cell.number_format="#,##0.00"
            if col==7: cell.number_format="+0.00;-0.00;0.00"
            if col in (8,9): cell.number_format="+0.0%;-0.0%;0.0%"
        wm.row_dimensions[er].height=18

    # Linha de total
    tr_m = 4+len(por_mes_xl)
    for col in range(1,10):
        cell=wm.cell(row=tr_m,column=col)
        if col==1: cell.value="TOTAL"
        elif col==2: cell.value=len(df_res)
        elif col==3: cell.value=sum(1 for a in df_res if a["resultado"]=="ganhou")
        elif col==4: cell.value=sum(1 for a in df_res if a["resultado"]=="perdeu")
        elif col==5: cell.value=round(sum(float(a["stake"]) for a in df_res),2); cell.number_format="#,##0.00"
        elif col==6: cell.value=round(sum(lucro_aposta(a) for a in df_res),2); cell.number_format="#,##0.00"; fc=cor(cell.value); cell.font=Font(name="Arial",bold=True,size=10,color=fc)
        elif col==7:
            total_u=sum(lucro_aposta(a)/float(a.get("unidade") or 50) for a in df_res)
            cell.value=round(total_u,2); cell.number_format="+0.00;-0.00;0.00"; fc=cor(cell.value); cell.font=Font(name="Arial",bold=True,size=10,color=fc)
        if col not in (6,7): cell.font=Font(name="Arial",bold=True,size=10,color="FFFFFF")
        est(cell,bold=True,bg=DARK,size=10); cell.border=brd()
    wm.row_dimensions[tr_m].height=22
    for i,w in enumerate([12,10,10,10,14,14,14,10,12],1): wm.column_dimensions[get_column_letter(i)].width=w

    # ── ABA 7: GRAFICO ──
    wg=wb.create_sheet("Evolucao da Banca")
    wg["A1"]="Aposta #"; wg["B1"]="Banca Acumulada"
    for i,(val) in enumerate(banca_acum,2):
        wg.cell(row=i,column=1,value=i-1); wg.cell(row=i,column=2,value=round(val,2))
    if len(banca_acum)>=2:
        chart=LineChart(); chart.title="Evolucao da Banca"; chart.style=10
        chart.y_axis.title="R$"; chart.x_axis.title="Aposta #"
        chart.y_axis.numFmt="#,##0.00"; chart.width=26; chart.height=14
        dr=Reference(wg,min_col=2,min_row=1,max_row=len(banca_acum)+1)
        cr=Reference(wg,min_col=1,min_row=2,max_row=len(banca_acum)+1)
        chart.add_data(dr,titles_from_data=True); chart.set_categories(cr)
        chart.series[0].graphicalProperties.line.solidFill="2563EB"
        chart.series[0].graphicalProperties.line.width=22000
        wg.add_chart(chart,"D2")

    # ── ABA: GASTOS ──
    gastos_lista = listar_gastos()
    wgas = wb.create_sheet("Gastos")
    wgas.merge_cells("A1:C1"); wgas["A1"] = "GASTOS / INVESTIMENTOS"
    est(wgas["A1"], bold=True, bg=DARK, size=14)
    wgas.row_dimensions[1].height = 30; wgas.row_dimensions[2].height = 6
    for c, h in enumerate(["Data", "Descrição", "Valor (R$)"], 1):
        cell = wgas.cell(row=3, column=c, value=h)
        est(cell, bold=True, bg=DARK, size=10); cell.border = brd()
    for i, g in enumerate(gastos_lista):
        r = 4 + i
        rb = WHITE if i % 2 == 0 else ALT
        data_g = g["data"].strftime("%d/%m/%Y") if hasattr(g["data"], "strftime") else str(g["data"])[:10]
        for c, val in enumerate([data_g, g["descricao"], float(g["valor"])], 1):
            cell = wgas.cell(row=r, column=c, value=val)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(horizontal="left" if c == 2 else "center", vertical="center")
            cell.fill = PatternFill("solid", start_color=rb); cell.border = brd()
            if c == 3: cell.number_format = "#,##0.00"
    tr_g = 4 + len(gastos_lista)
    tcell = wgas.cell(row=tr_g, column=2, value="TOTAL")
    vcell = wgas.cell(row=tr_g, column=3, value=round(total_gastos_e, 2)); vcell.number_format = "#,##0.00"
    for cell in (tcell, vcell):
        est(cell, bold=True, bg=DARK, size=10); cell.border = brd()
    wgas.column_dimensions["A"].width = 14
    wgas.column_dimensions["B"].width = 45
    wgas.column_dimensions["C"].width = 16

    # Salvar na memória e enviar
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    from datetime import datetime as dt2
    nome_arq=f"dashboard_{dt2.now().strftime('%d%m%Y_%H%M')}.xlsx"
    await update.message.reply_document(
        document=InputFile(buf, filename=nome_arq),
        caption=f"Dashboard gerado! {total} apostas | R$ {lucro_total:.2f} lucro | ROI {roi:+.1%}"
    )

# ── EXPORTAR CSV ──────────────────────────────────────────────────────────────
async def exportar_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apostas   = carregar()
    total     = len(apostas)
    pendentes = sum(1 for a in apostas if a["resultado"] == "pendente")
    import io
    output = io.StringIO()
    import csv as csv_mod
    writer = csv_mod.writer(output, quoting=csv_mod.QUOTE_ALL)
    writer.writerow(["id","data","horario","descricao","odd","stake","resultado","casa","esporte"])
    for a in apostas:
        data = a["data"].strftime("%Y-%m-%d") if hasattr(a["data"],"strftime") else str(a["data"])
        writer.writerow([a["id"],data,a.get("horario",""),a["descricao"],a["odd"],a["stake"],a["resultado"],a.get("casa",""),a.get("esporte","")])
    csv_bytes = output.getvalue().encode("utf-8")
    caption   = f"\U0001f4ca apostas.csv\n{total} apostas | {pendentes} pendentes"
    await update.message.reply_document(document=InputFile(csv_bytes, filename="apostas.csv"), caption=caption)

# ── SERVIDOR KEEP-ALIVE ───────────────────────────────────────────────────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *args): pass

def iniciar_servidor():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    inicializar_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    BOTOES_MENU = ["📝 Nova aposta","⏳ Ver pendentes","📈 Resultados","✏️ Editar aposta","📊 Gerar Resultados","⚙️ Configurações"]
    fallbacks_padrao = [
        CommandHandler("cancelar", cancelar),
        MessageHandler(filters.Regex(f"^{CANCELAR_BTN}$"), cancelar),
        MessageHandler(filters.Regex("^(" + "|".join(re.escape(b) for b in BOTOES_MENU) + ")$"), cancelar),
    ]

    conv_nova = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📝 Nova aposta$"), nova_aposta_inicio)],
        states={
            DATA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_data)],
            HORARIO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_horario)],
            DESCRICAO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_descricao)],
            ODD:       [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_odd)],
            STAKE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_stake)],
            ESPORTE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_esporte)],
            CASA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_casa)],
        },
        fallbacks=fallbacks_padrao,
    )

    conv_editar = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^✏️ Editar aposta$"), editar_inicio),
            MessageHandler(filters.Regex(r"(?i)^editar\s+\d+$"), editar_direto_comando),
        ],
        states={
            EDITAR_PAGINA: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_navegar)],
            EDITAR_CAMPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_receber_campo)],
            EDITAR_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_receber_valor)],
            EDITAR_CASA:  [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_receber_casa)],
        },
        fallbacks=fallbacks_padrao,
    )

    conv_config = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚙️ Configurações$"), config_inicio)],
        states={
            CONFIG_MENU:          [MessageHandler(filters.TEXT & ~filters.COMMAND, config_resposta)],
            MUDAR_UNIDADE_VALOR:  [MessageHandler(filters.TEXT & ~filters.COMMAND, mudar_unidade_receber)],
            GASTOS_MENU:          [MessageHandler(filters.TEXT & ~filters.COMMAND, gastos_resposta)],
            GASTO_DESCRICAO:      [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_gasto_descricao)],
            GASTO_VALOR:          [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_gasto_valor)],
            LIMITACOES_MENU:      [MessageHandler(filters.TEXT & ~filters.COMMAND, limitacoes_resposta)],
            LIMITACOES_ADD:       [MessageHandler(filters.TEXT & ~filters.COMMAND, limitacoes_add_receber)],
        },
        fallbacks=fallbacks_padrao,
    )

    conv_resultados = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📈 Resultados$"), resultados)],
        states={
            RESULTADOS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, resultados_resposta)],
            SEMANA_MES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, resultados_semana_mes_escolhido)],
            SEMANA_SEMANA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, resultados_semana_escolhida)],
        },
        fallbacks=fallbacks_padrao,
    )

    conv_gerar = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📊 Gerar Resultados$"), gerar_menu)],
        states={
            GERAR_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, gerar_resposta)],
        },
        fallbacks=fallbacks_padrao,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exportar", exportar_csv))
    app.add_handler(CommandHandler("migrar_casas", cmd_migrar_casas))
    conv_duplicar_desc = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^duplicar\s+\d+\s+descricao$"), duplicar_descricao_inicio)],
        states={
            DUPLICAR_DESC_AGUARDA: [MessageHandler(filters.TEXT & ~filters.COMMAND, duplicar_descricao_receber)],
        },
        fallbacks=fallbacks_padrao,
    )

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^duplicar\s+\d+\s+casa\s+.+$"), duplicar_comando))
    app.add_handler(conv_duplicar_desc)
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^duplicar\s+\d+$"), duplicar_comando))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^triplicar\s+\d+$"), triplicar_comando))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(green|red|void)\s+[\d\s,e]+$"), marcar_multiplo_comando))
    app.add_handler(conv_nova)
    app.add_handler(conv_editar)
    app.add_handler(conv_config)
    app.add_handler(conv_resultados)
    app.add_handler(conv_gerar)
    app.add_handler(CallbackQueryHandler(ia_callback, pattern="^ia_"))
    app.add_handler(MessageHandler(filters.PHOTO, receber_encaminhada))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_botao))

    threading.Thread(target=iniciar_servidor, daemon=True).start()
    print("🤖 Bot rodando! Abra o Telegram e mande /start")
    app.run_polling()

if __name__ == "__main__":
    main()