import io
import os
import re
import json
import sqlite3
import threading
import requests
import telebot
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN não definido no .env")
bot = telebot.TeleBot(BOT_TOKEN)

DB_FILE = os.path.join(os.path.dirname(__file__), "claro.db")

# chats aguardando cookie manual
aguardando_cookie = set()

PARCEIROS = ["APPLE", "AMAZON", "DISNEY", "MAX", "NETFLIX", "UNIVERSAL"]
NOMES = {
    "APPLE":     "🍎 AppleTV+",
    "AMAZON":    "📦 Amazon Prime",
    "DISNEY":    "🏰 Disney+",
    "MAX":       "🎬 HBO Max",
    "NETFLIX":   "🔴 Netflix",
    "UNIVERSAL": "🎡 Universal+"
}
API_URL = "https://www.clarotvmais.com.br/avsclient/products/user/ott/activation"

# 
#  BANCO DE DADOS
# 
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS contas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            credencial TEXT    NOT NULL,
            cookie     TEXT    NOT NULL,
            data       TEXT    NOT NULL,
            hora       TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            conta_id   INTEGER NOT NULL REFERENCES contas(id),
            plataforma TEXT    NOT NULL,
            link       TEXT    NOT NULL
        );
    """)
    con.commit()
    con.close()

def salvar_conta(credencial, links_list):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    now  = datetime.now()
    data = now.strftime("%Y-%m-%d")
    hora = now.strftime("%H:%M:%S")
    cur.execute("INSERT INTO contas (credencial, cookie, data, hora) VALUES (?,?,?,?)",
                (credencial, "", data, hora))
    conta_id = cur.lastrowid
    for item in links_list:
        cur.execute("INSERT INTO links (conta_id, plataforma, link) VALUES (?,?,?)",
                    (conta_id, item["plataforma"], item["link"]))
    con.commit()
    con.close()
    return conta_id

def buscar_link(conta_id, plataforma):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT link FROM links WHERE conta_id=? AND plataforma=?", (conta_id, plataforma))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def buscar_datas_disponiveis():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        SELECT data, COUNT(DISTINCT id) FROM contas
        GROUP BY data ORDER BY data DESC LIMIT 10
    """)
    rows = cur.fetchall()
    con.close()
    return rows

def buscar_plataformas_da_data(data_str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        SELECT l.plataforma, COUNT(DISTINCT c.id)
        FROM contas c JOIN links l ON l.conta_id = c.id
        WHERE c.data = ? GROUP BY l.plataforma ORDER BY l.plataforma
    """, (data_str,))
    rows = cur.fetchall()
    con.close()
    return rows

def buscar_contas_por_data_plat(data_str, plataforma):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        SELECT c.id, c.credencial, c.hora
        FROM contas c JOIN links l ON l.conta_id = c.id
        WHERE c.data = ? AND l.plataforma = ?
        ORDER BY c.id DESC
    """, (data_str, plataforma))
    rows = cur.fetchall()
    con.close()
    return rows

def buscar_links_conta(conta_id):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT plataforma, link FROM links WHERE conta_id=? ORDER BY plataforma", (conta_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def limpar_dia(data_str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM links WHERE conta_id IN (SELECT id FROM contas WHERE data=?)", (data_str,))
    cur.execute("DELETE FROM contas WHERE data=?", (data_str,))
    con.commit()
    con.close()

def buscar_todos_links(data_str=None):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    if data_str:
        cur.execute("""
            SELECT c.credencial, l.plataforma, l.link
            FROM contas c JOIN links l ON l.conta_id = c.id
            WHERE c.data = ? ORDER BY c.id DESC, l.plataforma
        """, (data_str,))
    else:
        cur.execute("""
            SELECT c.credencial, l.plataforma, l.link
            FROM contas c JOIN links l ON l.conta_id = c.id
            ORDER BY c.id DESC, l.plataforma
        """)
    rows = cur.fetchall()
    con.close()
    return rows

# 
#  API CLARO TV
# 
def puxar_links(cookie):
    headers = {
        "User-Agent":      "okhttp/4.12.0",
        "Pragma":          "no-cache",
        "Accept":          "*/*",
        "Cache-Control":   "no-cache",
        "x-querystring":   "",
        "Content-Type":    "application/json; charset=UTF-8",
        "Host":            "www.clarotvmais.com.br",
        "Connection":      "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Cookie":          cookie.strip()
    }

    def _buscar(parceiro):
        try:
            r = requests.post(API_URL, headers=headers,
                              json={"channel": "ANDROIDMOBILEH", "partnerId": parceiro},
                              timeout=10)
            if r.ok:
                link = r.json().get("response", {}).get("activationLink")
                if link:
                    return {"plataforma": parceiro, "link": link}
        except Exception as e:
            print(f"Erro {parceiro}: {e}")
        return None

    resultados = []
    with ThreadPoolExecutor(max_workers=len(PARCEIROS)) as ex:
        futuros = {ex.submit(_buscar, p): p for p in PARCEIROS}
        for fut in as_completed(futuros):
            res = fut.result()
            if res:
                resultados.append(res)
    # manter ordem original
    ordem = {p: i for i, p in enumerate(PARCEIROS)}
    resultados.sort(key=lambda x: ordem.get(x["plataforma"], 99))
    return resultados

# 
#  TECLADOS
# 
def _emoji_curto(p):
    nome   = NOMES.get(p, p)
    partes = nome.split()
    return partes[0], (partes[1] if len(partes) > 1 else p)

def teclado_menu_principal():
    mu = types.InlineKeyboardMarkup(row_width=1)
    mu.add(
        types.InlineKeyboardButton("🔍 Buscar links (cookie)", callback_data="menu_buscar"),
        types.InlineKeyboardButton("📋 Ver links salvos",       callback_data="menu_salvos"),
        types.InlineKeyboardButton("📄 Exportar tudo do dia",   callback_data="menu_exportar"),
        types.InlineKeyboardButton("🗑️ Limpar links do dia",    callback_data="menu_limpar"),
    )
    return mu

def teclado_datas_menu():
    datas = buscar_datas_disponiveis()
    mu    = types.InlineKeyboardMarkup(row_width=2)
    bts   = []
    for (ds, total) in datas:
        try:
            label = datetime.strptime(ds, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            label = ds
        bts.append(types.InlineKeyboardButton(f" {label} ({total})", callback_data=f"data:{ds}"))
    if bts:
        mu.add(*bts)
    mu.add(types.InlineKeyboardButton("◀️ Início", callback_data="menu_inicio"))
    return mu

def teclado_plataformas_data(data_str):
    plats = buscar_plataformas_da_data(data_str)
    mu    = types.InlineKeyboardMarkup(row_width=3)
    bts   = []
    for (p, total) in plats:
        e, c = _emoji_curto(p)
        bts.append(types.InlineKeyboardButton(f"{e} {c} ({total})", callback_data=f"dp:{data_str}:{p}"))
    if bts:
        mu.add(*bts)
    mu.add(types.InlineKeyboardButton("◀️ Datas", callback_data="menu_salvos"))
    return mu

def teclado_contas(contas, plataforma, data_str):
    mu  = types.InlineKeyboardMarkup(row_width=2)
    bts = []
    for (cid, cred, hora) in contas:
        bts.append(types.InlineKeyboardButton(
            f"👤 {cred[:18]} {hora[:5]}", callback_data=f"ac:{cid}:{data_str}"))
    mu.add(*bts)
    mu.add(
        types.InlineKeyboardButton("◀️ Plataformas", callback_data=f"data:{data_str}"),
        types.InlineKeyboardButton("◀️ Início",       callback_data="menu_inicio")
    )
    return mu

def teclado_links_conta(conta_id, plataformas):
    mu  = types.InlineKeyboardMarkup(row_width=3)
    bts = []
    for p in plataformas:
        e, c = _emoji_curto(p)
        bts.append(types.InlineKeyboardButton(f"{e} {c}", callback_data=f"gl:{conta_id}:{p}"))
    mu.add(*bts)
    return mu

# 
#  HELPERS
# 
def _enviar_bloco_db(chat_id, rows, prefixo):
    grupos = {}
    for (cred, plat, link) in rows:
        grupos.setdefault(plat, []).append(f"{cred} > {link}")

    for plat in PARCEIROS:
        if plat not in grupos:
            continue
        e, nome_plat = _emoji_curto(plat)
        conteudo = "\n".join(grupos[plat])
        buf = io.BytesIO(conteudo.encode("utf-8"))
        nome_plat_limpo = nome_plat.replace(" ", "_")
        buf.name = f"{nome_plat_limpo}_{prefixo}.txt"
        bot.send_document(chat_id, buf, caption=f"{e} {nome_plat}")

def _texto_menu():
    hoje  = datetime.now().strftime("%Y-%m-%d")

    plats = buscar_plataformas_da_data(hoje)
    if plats:
        linhas_plat = ""
        for (p, total) in plats:
            e, c = _emoji_curto(p)
            linhas_plat += f"  {e} {c}: *{total}*\n"
    else:
        linhas_plat = "  Nenhum ainda\n"

    hoje_fmt = datetime.now().strftime("%d/%m/%Y")
    return (
        "🤖 *Claro TV+ Bot*\n\n"
        f"📅 *{hoje_fmt}*\n"
        f"{linhas_plat}\n"
        "O que deseja fazer?"
    )

# 
#  COMANDOS
# 
@bot.message_handler(commands=["start", "menu"])
def cmd_start(msg):
    bot.send_message(msg.chat.id, _texto_menu(),
                     parse_mode="Markdown", reply_markup=teclado_menu_principal())

@bot.message_handler(commands=["db"])
def cmd_db(msg):
    hoje = datetime.now().strftime("%Y-%m-%d")
    rows = buscar_todos_links(hoje)
    if not rows:
        bot.send_message(msg.chat.id, "Nenhum link salvo hoje.")
        return
    _enviar_bloco_db(msg.chat.id, rows, hoje)

@bot.message_handler(commands=["db_tudo"])
def cmd_db_tudo(msg):
    rows = buscar_todos_links()
    if not rows:
        bot.send_message(msg.chat.id, "Banco vazio.")
        return
    _enviar_bloco_db(msg.chat.id, rows, "tudo")

# 
#  CALLBACKS
# 
@bot.callback_query_handler(func=lambda c: c.data == "menu_inicio")
def cb_inicio(call):
    bot.edit_message_text(_texto_menu(), chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          parse_mode="Markdown", reply_markup=teclado_menu_principal())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_buscar")
def cb_buscar(call):
    aguardando_cookie.add(call.message.chat.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Cancelar", callback_data="menu_inicio"))
    bot.edit_message_text(
        "🍪 *Mande o cookie agora*\n\n"
        "Cole o cookie completo do site Claro TV+.\n"
        "Para múltiplos, separe com uma linha em branco.",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
        parse_mode="Markdown", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_salvos")
def cb_salvos(call):
    datas = buscar_datas_disponiveis()
    if not datas:
        bot.answer_callback_query(call.id, " Nenhum link salvo ainda.", show_alert=True)
        return
    bot.edit_message_text(" *Links salvos*\n\nEscolha a data:",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          parse_mode="Markdown", reply_markup=teclado_datas_menu())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_exportar")
def cb_exportar(call):
    hoje = datetime.now().strftime("%Y-%m-%d")
    rows = buscar_todos_links(hoje)
    bot.answer_callback_query(call.id)
    if not rows:
        bot.send_message(call.message.chat.id, "Nenhum link salvo hoje.")
        return
    _enviar_bloco_db(call.message.chat.id, rows, hoje)

@bot.callback_query_handler(func=lambda c: c.data.startswith("data:"))
def cb_data(call):
    ds = call.data.split(":", 1)[1]
    try:
        label = datetime.strptime(ds, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        label = ds
    plats = buscar_plataformas_da_data(ds)
    if not plats:
        bot.answer_callback_query(call.id, " Sem links nesta data.", show_alert=True)
        return
    total = sum(t for (_, t) in plats)
    bot.edit_message_text(f" *{label}*  {total} conta(s)\n\nEscolha a plataforma:",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          parse_mode="Markdown", reply_markup=teclado_plataformas_data(ds))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dp:"))
def cb_data_plat(call):
    _, ds, plat = call.data.split(":", 2)
    nome = NOMES.get(plat, plat)
    try:
        label = datetime.strptime(ds, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        label = ds

    # Busca todos os links dessa plataforma na data
    rows = buscar_contas_por_data_plat(ds, plat)  # retorna (conta_id, credencial, hora)
    if not rows:
        bot.answer_callback_query(call.id, "Nenhum link encontrado.", show_alert=True)
        return

    # Monta .txt com todos os links
    linhas = []
    for (cid, cred, hora) in rows:
        link = buscar_link(cid, plat)   # só o link desta plataforma
        if link:
            linhas.append(f"{cred} > {link}")

    conteudo = "\n".join(linhas)
    buf = io.BytesIO(conteudo.encode("utf-8"))
    buf.name = f"{plat.lower()}_{label.replace('/','_')}.txt"
    e, c = _emoji_curto(plat)
    bot.send_document(call.message.chat.id, buf,
        caption=f"{e} {c} — {label} — {len(linhas)} link(s)")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_limpar")
def cb_menu_limpar(call):
    hoje = datetime.now().strftime("%d/%m/%Y")
    mu = types.InlineKeyboardMarkup(row_width=2)
    mu.add(
        types.InlineKeyboardButton("✅ Sim, limpar", callback_data="confirmar_limpar"),
        types.InlineKeyboardButton("❌ Cancelar",    callback_data="menu_inicio"),
    )
    bot.edit_message_text(f"🗑️ Apagar *todos* os links do dia *{hoje}*?\n\nIsso não tem volta.",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          parse_mode="Markdown", reply_markup=mu)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "confirmar_limpar")
def cb_confirmar_limpar(call):
    hoje = datetime.now().strftime("%Y-%m-%d")
    limpar_dia(hoje)
    bot.edit_message_text("✅ Links do dia apagados.",
                          chat_id=call.message.chat.id, message_id=call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ac:"))
def cb_get_conta(call):
    partes = call.data.split(":", 2)
    cid_str = partes[1]
    links = buscar_links_conta(int(cid_str))
    bot.answer_callback_query(call.id)
    if not links:
        bot.send_message(call.message.chat.id, "Nenhum link encontrado.")
        return
    linhas = []
    for (plat, link) in links:
        e, c = _emoji_curto(plat)
        linhas.append(f"{e} {c}")
        linhas.append(link)
        linhas.append("")
    bot.send_message(call.message.chat.id, "\n".join(linhas).strip(), disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("gl:"))
def cb_get_link(call):
    _, cid_str, plat = call.data.split(":", 2)
    link = buscar_link(int(cid_str), plat)
    nome = NOMES.get(plat, plat)
    if link:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"{nome}\n{link}", disable_web_page_preview=False)
    else:
        bot.answer_callback_query(call.id, " Link não encontrado.", show_alert=True)

# 
#  RECEBER COOKIE / EXTENSÃO
# 
@bot.message_handler(content_types=["text"],
                     func=lambda m: not m.text.startswith("/"))
def receber_conta(msg):
    texto   = msg.text.strip()
    chat_id = msg.chat.id
    if not texto:
        return

    eh_cookie   = len(texto) > 100 and (";" in texto or "=" in texto)
    eh_extensao = any(f"{p}=" in texto for p in PARCEIROS)
    if chat_id not in aguardando_cookie and not eh_cookie and not eh_extensao:
        bot.send_message(chat_id, _texto_menu(),
                         parse_mode="Markdown", reply_markup=teclado_menu_principal())
        return

    aguardando_cookie.discard(chat_id)

    # Apaga a mensagem do cookie do chat
    try:
        bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass

    blocos = [b.strip() for b in re.split(r"\n\s*\n", texto) if b.strip()]

    if len(blocos) > 1:
        ag = bot.send_message(chat_id, f" Processando {len(blocos)} cookie(s)...")
        resultados = []
        for i, bloco in enumerate(blocos, 1):
            bot.edit_message_text(f" Processando {i}/{len(blocos)}...",
                                  chat_id=chat_id, message_id=ag.message_id)
            resultados.append(_processar_bloco(chat_id, bloco, silencioso=True))

        ok  = [r for r in resultados if r["ok"]]
        err = [r for r in resultados if not r["ok"]]
        resumo = f" *{len(ok)} ok*   *{len(err)} erro(s)*\n\n"
        for r in ok:
            resumo += f" `{r['credencial']}`  {r['count']} link(s)\n"
        for r in err:
            resumo += f" `{r['credencial']}`  {r['motivo']}\n"
        bot.edit_message_text(resumo, chat_id=chat_id, message_id=ag.message_id, parse_mode="Markdown")
        for r in ok:
            now = datetime.now().strftime("%d/%m %H:%M")
            links_db = buscar_links_conta(r["conta_id"])
            mu = types.InlineKeyboardMarkup(row_width=2)
            for (plat, link) in links_db:
                e, c = _emoji_curto(plat)
                mu.add(types.InlineKeyboardButton(f"{e} {c}", url=link))
            bot.send_message(chat_id,
                f"✅ *{r['count']} link(s)*\n`{r['credencial']}`  {now}",
                parse_mode="Markdown", reply_markup=mu)
        return

    _processar_bloco(chat_id, blocos[0], silencioso=False)


def _processar_bloco(chat_id, bloco, silencioso=False):
    linhas        = bloco.splitlines()
    credencial    = "manual"
    cookie_linhas = linhas
    if linhas[0].lower().startswith("credencial:"):
        credencial    = linhas[0].split(":", 1)[1].strip()
        cookie_linhas = linhas[1:]

    links_list = []
    for linha in cookie_linhas:
        linha = linha.strip()
        if "=" in linha:
            partes = linha.split("=", 1)
            plat   = partes[0].strip().upper()
            link   = partes[1].strip()
            if plat in PARCEIROS and link.startswith("http"):
                links_list.append({"plataforma": plat, "link": link})

    if links_list:
        conta_id   = salvar_conta(credencial, links_list)
        plataformas = [i["plataforma"] for i in links_list]
        if not silencioso:
            now = datetime.now().strftime("%d/%m %H:%M")
            mu = types.InlineKeyboardMarkup(row_width=2)
            for i in links_list:
                e, c = _emoji_curto(i["plataforma"])
                mu.add(types.InlineKeyboardButton(f"{e} {c}", url=i["link"]))
            bot.send_message(chat_id,
                f"✅ *{len(links_list)} link(s)*\n`{credencial}`  {now}",
                parse_mode="Markdown", reply_markup=mu)
        return {"ok": True, "credencial": credencial, "conta_id": conta_id,
                "plataformas": plataformas, "count": len(links_list)}

    cookie = " ".join(cookie_linhas).strip()
    if not cookie:
        if not silencioso:
            bot.send_message(chat_id, " Cookie vazio.")
        return {"ok": False, "credencial": credencial, "motivo": "cookie vazio"}

    ag = None
    if not silencioso:
        ag = bot.send_message(chat_id, " Procurando links... aguarde")

    links = puxar_links(cookie)

    if not links:
        if not silencioso and ag:
            bot.edit_message_text(f" `{credencial}`\n Cookie inválido ou expirado.",
                                  chat_id=chat_id, message_id=ag.message_id, parse_mode="Markdown")
        return {"ok": False, "credencial": credencial, "motivo": "cookie inválido ou expirado"}

    conta_id    = salvar_conta(credencial, links)
    plataformas = [i["plataforma"] for i in links]
    if not silencioso and ag:
        now = datetime.now().strftime("%d/%m %H:%M")
        mu = types.InlineKeyboardMarkup(row_width=2)
        for i in links:
            e, c = _emoji_curto(i["plataforma"])
            mu.add(types.InlineKeyboardButton(f"{e} {c}", url=i["link"]))
        bot.edit_message_text(
            f"✅ *{len(links)} link(s)*\n`{credencial}`  {now}",
            chat_id=chat_id, message_id=ag.message_id,
            parse_mode="Markdown", reply_markup=mu)
    return {"ok": True, "credencial": credencial, "conta_id": conta_id,
            "plataformas": plataformas, "count": len(links)}

# 
#  SERVIDOR HTTP LOCAL (para a extensao Chrome)
# 
CHAT_ID_PADRAO = os.getenv("CHAT_ID", "7497611482")

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silencia logs
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(size)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            data      = json.loads(body)
            credencial = data.get("credencial", "manual")
            cookie    = data.get("cookie", "")
            chat_id   = int(data.get("chat_id", CHAT_ID_PADRAO))
            bloco     = f"credencial: {credencial}\n{cookie}"
            threading.Thread(target=_processar_bloco,
                             args=(chat_id, bloco, False), daemon=True).start()
            self.wfile.write(json.dumps({"ok": True}).encode())
        except Exception as e:
            self.wfile.write(json.dumps({"ok": False, "erro": str(e)}).encode())

def _iniciar_servidor():
    port = int(os.getenv("PORT", 8080))
    srv = HTTPServer(("0.0.0.0", port), _Handler)
    print(f" Servidor HTTP rodando na porta {port}")
    srv.serve_forever()

# 
init_db()
threading.Thread(target=_iniciar_servidor, daemon=True).start()
print(" Bot rodando... Ctrl+C para parar.")
bot.delete_webhook(drop_pending_updates=True)
bot.infinity_polling()