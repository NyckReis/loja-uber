import os
import sys
import json
import time
import base64
import sqlite3
import secrets
import threading
import urllib.request
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "loja_database.db")
TEMPLATES_DIR = os.path.join(BASE_DIR, "loja_templates")
CODIGOS_TXT = os.path.join(BASE_DIR, "codigos_unicos.txt")
FALLBACK_TXT = os.path.join(BASE_DIR, "CODIGOS_UBER.txt")

# Cache de sessões do Admin
admin_sessions = set()

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS codigos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        codigo TEXT UNIQUE,
        status TEXT DEFAULT 'disponivel',
        data_adicionado TEXT,
        data_venda TEXT,
        order_id TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedidos (
        order_id TEXT PRIMARY KEY,
        cliente_nome TEXT,
        cliente_contato TEXT,
        cliente_cpf TEXT,
        valor REAL,
        status TEXT DEFAULT 'PENDING',
        bspay_txid TEXT,
        pix_copia_cola TEXT,
        pix_qrcode_base64 TEXT,
        codigo_entregue TEXT,
        criado_em TEXT,
        pago_em TEXT
    )
    """)

    # Configurações padrão com as credenciais da BsPay
    defaults = {
        "bspay_client_id": "nyckzao_ec5991cb9666c504",
        "bspay_client_secret": "a46971c0ff24346523010898d3326771da6da936345e5c97fc933d159c856e70",
        "bspay_mode": "producao", # 'teste' ou 'producao'
        "admin_password": "Nyck@1345",
        "preco_cupom": "20.00",
        "titulo_oferta": "Ganhe R$ 500 a mais pelas primeiras 30 viagens de Uber Moto",
        "subtitulo_oferta": "Exclusivo para a categoria Moto e contas novas. Complete 30 viagens e receba o bônus de R$ 500 direto na sua conta bancária.",
        "whatsapp_suporte": "92999999999",
        "bspay_webhook_url": ""
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    conn.commit()

    # Se a tabela de códigos estiver vazia, importa automaticamente do codigos_unicos.txt
    cur.execute("SELECT COUNT(*) FROM codigos")
    count = cur.fetchone()[0]
    if count == 0:
        import_count = sync_codes_from_file(conn)
        print(f"[*] Banco inicializado com {import_count} códigos do arquivo de extração.")

    conn.close()

def sync_codes_from_file(conn=None):
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True

    source_file = CODIGOS_TXT if os.path.exists(CODIGOS_TXT) else FALLBACK_TXT
    if not os.path.exists(source_file):
        if close_conn:
            conn.close()
        return 0

    imported = 0
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()

    try:
        with open(source_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                code = line.strip().split()[0] if line.strip() else ""
                code = code.strip("(),;\"'")
                if len(code) >= 5 and len(code) <= 30 and code.isalnum():
                    try:
                        cur.execute(
                            "INSERT OR IGNORE INTO codigos (codigo, status, data_adicionado) VALUES (?, 'disponivel', ?)",
                            (code, now_str)
                        )
                        if cur.rowcount > 0:
                            imported += 1
                    except Exception:
                        pass
        conn.commit()
    except Exception as e:
        print(f"Erro ao sincronizar códigos: {e}")

    if close_conn:
        conn.close()
    return imported

def get_config(key, default=""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_config(key, value):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# Integração BsPay (v2 OAuth2 e Cash-in PIX)
def bspay_get_oauth_token(client_id, client_secret):
    url = "https://api.bspay.co/v2/oauth/token"
    auth_str = f"{client_id}:{client_secret}"
    b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    payload = json.dumps({"grant_type": "client_credentials"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("access_token"), None
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return None, f"HTTP {e.code}: {err_msg}"
    except Exception as e:
        return None, str(e)

def bspay_create_pix_charge(client_id, client_secret, order_id, amount, payer_name, payer_cpf, postback_url=""):
    token, err = bspay_get_oauth_token(client_id, client_secret)
    if not token:
        return None, f"Falha na autenticação BsPay: {err}"

    url = "https://api.bspay.co/v2/pix/qrcode"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # Tratamento de CPF
    clean_cpf = "".join(filter(str.isdigit, payer_cpf or ""))
    if len(clean_cpf) != 11:
        clean_cpf = "00000000000"

    payload = {
        "amount": round(float(amount), 2),
        "currency": "BRL",
        "external_id": str(order_id),
        "postback_url": postback_url or "",
        "payer": {
            "name": payer_name or "Cliente Uber Moto",
            "document": clean_cpf
        }
    }

    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data, None
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return None, f"Erro BsPay {e.code}: {err_msg}"
    except Exception as e:
        return None, str(e)

def bspay_check_charge_status(client_id, client_secret, txid):
    token, err = bspay_get_oauth_token(client_id, client_secret)
    if not token:
        return None, f"Erro autenticação BsPay: {err}"

    url = "https://api.bspay.co/v2/consult-transaction"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    payload = json.dumps({"pix_id": str(txid)}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data, None
    except Exception as e:
        return None, str(e)

def remove_code_from_files(code, order_id=None):
    """Remove o código vendido dos arquivos .txt do estoque e salva histórico"""
    vendidos_txt = os.path.join(BASE_DIR, "codigos_vendidos.txt")
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Registra no arquivo de códigos vendidos
    try:
        with open(vendidos_txt, "a", encoding="utf-8") as f:
            f.write(f"{code} | Pedido: {order_id or 'N/A'} | Vendido em: {now_str}\n")
    except Exception as e:
        print(f"[!] Erro ao registrar em codigos_vendidos.txt: {e}")

    # 2. Apaga dos arquivos de estoque (.txt)
    for filepath in [CODIGOS_TXT, FALLBACK_TXT]:
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            code_clean = code.lower().strip()
            new_lines = []
            removed = False
            for line in lines:
                parts = line.strip().split()
                line_code = parts[0].strip("(),;\"'").lower() if parts else ""
                if line_code == code_clean or code_clean in line.lower():
                    removed = True
                    continue
                new_lines.append(line)

            if removed:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                print(f"[*] Código {code} apagado com sucesso de {os.path.basename(filepath)}")
        except Exception as e:
            print(f"[!] Erro ao remover código de {filepath}: {e}")

def fulfill_order(order_id):
    """Atribui um código de desconto ao pedido, marca como PAID e remove do estoque"""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT status, codigo_entregue FROM pedidos WHERE order_id = ?", (order_id,))
    ped = cur.fetchone()
    if not ped:
        conn.close()
        return None, "Pedido não encontrado"

    if ped["status"] == "PAID" and ped["codigo_entregue"]:
        conn.close()
        return ped["codigo_entregue"], None

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # Busca um código disponível
    cur.execute("SELECT id, codigo FROM codigos WHERE status = 'disponivel' LIMIT 1")
    code_row = cur.fetchone()

    if not code_row:
        # Se esgotou, gera um aviso ou cria fallback
        conn.close()
        return None, "Estoque temporariamente esgotado! Contate o suporte."

    cod_id = code_row["id"]
    assigned_code = code_row["codigo"]

    # Marca código como vendido no banco de dados
    cur.execute(
        "UPDATE codigos SET status = 'vendido', data_venda = ?, order_id = ? WHERE id = ?",
        (now_str, order_id, cod_id)
    )

    # Remove do arquivo físico .txt na hora
    remove_code_from_files(assigned_code, order_id)

    # Atualiza o pedido
    cur.execute(
        "UPDATE pedidos SET status = 'PAID', codigo_entregue = ?, pago_em = ? WHERE order_id = ?",
        (assigned_code, now_str, order_id)
    )

    conn.commit()
    conn.close()
    return assigned_code, None

class LojaHandler(BaseHTTPRequestHandler):
    def send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def send_html(self, status_code, content):
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def is_admin_authenticated(self):
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1].strip()
            return token in admin_sessions
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Arquivos estáticos (imagens, svg, etc.)
        if path.startswith("/assets/"):
            filename = os.path.basename(path)
            asset_path = os.path.join(TEMPLATES_DIR, "assets", filename)
            if os.path.exists(asset_path):
                ext = os.path.splitext(filename)[1].lower()
                mime = "image/png"
                if ext == ".svg":
                    mime = "image/svg+xml"
                elif ext in [".jpg", ".jpeg"]:
                    mime = "image/jpeg"
                elif ext == ".webp":
                    mime = "image/webp"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                with open(asset_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_json(404, {"error": "Arquivo não encontrado"})
                return

        # Página Principal (Loja / Checkout)
        if path == "/" or path == "/index.html":
            index_file = os.path.join(TEMPLATES_DIR, "index.html")
            if os.path.exists(index_file):
                with open(index_file, "r", encoding="utf-8") as f:
                    self.send_html(200, f.read())
            else:
                self.send_html(404, "Página não encontrada.")

        # Painel Administrativo
        elif path == "/admin" or path == "/admin/":
            admin_file = os.path.join(TEMPLATES_DIR, "admin.html")
            if os.path.exists(admin_file):
                with open(admin_file, "r", encoding="utf-8") as f:
                    self.send_html(200, f.read())
            else:
                self.send_html(404, "Painel não encontrado.")

        # Consulta pública de status do pedido
        elif path.startswith("/api/pedido/"):
            parts = path.split("/")
            if len(parts) >= 4:
                order_id = parts[3]
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT * FROM pedidos WHERE order_id = ?", (order_id,))
                row = cur.fetchone()

                # Se estiver PENDING e em modo BsPay Produção, consulta status na BsPay
                if row and row["status"] == "PENDING" and row["bspay_txid"]:
                    mode = get_config("bspay_mode", "teste")
                    c_id = get_config("bspay_client_id")
                    c_sec = get_config("bspay_client_secret")
                    if mode == "producao" and c_id and c_sec:
                        res, _ = bspay_check_charge_status(c_id, c_sec, row["bspay_txid"])
                        status_val = ""
                        if res:
                            status_val = str(res.get("requestBody", {}).get("status") or res.get("status") or "").upper()
                        if status_val in ["PAID", "COMPLETED", "SETTLED", "CONFIRMED", "APPROVED"]:
                            fulfill_order(order_id)
                            cur.execute("SELECT * FROM pedidos WHERE order_id = ?", (order_id,))
                            row = cur.fetchone()

                conn.close()

                if row:
                    self.send_json(200, {
                        "order_id": row["order_id"],
                        "status": row["status"],
                        "valor": row["valor"],
                        "cliente_nome": row["cliente_nome"],
                        "pix_copia_cola": row["pix_copia_cola"],
                        "pix_qrcode_base64": row["pix_qrcode_base64"],
                        "codigo_entregue": row["codigo_entregue"] if row["status"] == "PAID" else None,
                        "pago_em": row["pago_em"]
                    })
                else:
                    self.send_json(404, {"error": "Pedido não encontrado"})
            else:
                self.send_json(400, {"error": "ID de pedido inválido"})

        # Dados públicos da oferta (Preço, Título, Estoque restante)
        elif path == "/api/oferta":
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM codigos WHERE status = 'disponivel'")
            estoque = cur.fetchone()[0]
            conn.close()

            self.send_json(200, {
                "titulo": get_config("titulo_oferta"),
                "subtitulo": get_config("subtitulo_oferta"),
                "preco": float(get_config("preco_cupom", "20.00")),
                "whatsapp_suporte": get_config("whatsapp_suporte"),
                "estoque_disponivel": estoque,
                "modo_teste": get_config("bspay_mode", "teste") == "teste"
            })

        # --- ENDPOINTS PROTEGIDOS DO ADMIN ---
        elif path == "/api/admin/stats":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            conn = get_db()
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM codigos WHERE status = 'disponivel'")
            estoque_disponivel = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM codigos WHERE status = 'vendido'")
            total_vendidos = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*), COALESCE(SUM(valor), 0) FROM pedidos WHERE status = 'PAID'")
            pedidos_pagos, total_faturado = cur.fetchone()

            cur.execute("SELECT COUNT(*) FROM pedidos WHERE status = 'PENDING'")
            pedidos_pendentes = cur.fetchone()[0]

            # Vendas recentes
            cur.execute("SELECT * FROM pedidos ORDER BY criado_em DESC LIMIT 15")
            pedidos_recentes = [dict(r) for r in cur.fetchall()]

            conn.close()

            self.send_json(200, {
                "estoque_disponivel": estoque_disponivel,
                "total_vendidos": total_vendidos,
                "pedidos_pagos": pedidos_pagos,
                "total_faturado": total_faturado,
                "pedidos_pendentes": pedidos_pendentes,
                "pedidos_recentes": pedidos_recentes,
                "bspay_mode": get_config("bspay_mode", "teste")
            })

        elif path == "/api/admin/config":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            host = self.headers.get("Host", "localhost:5000")
            default_webhook = f"http://{host}/api/webhook/bspay"

            self.send_json(200, {
                "bspay_client_id": get_config("bspay_client_id"),
                "bspay_client_secret": get_config("bspay_client_secret"),
                "bspay_mode": get_config("bspay_mode", "teste"),
                "preco_cupom": get_config("preco_cupom", "20.00"),
                "titulo_oferta": get_config("titulo_oferta"),
                "whatsapp_suporte": get_config("whatsapp_suporte"),
                "webhook_url": get_config("bspay_webhook_url") or default_webhook
            })

        elif path == "/api/admin/codigos":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM codigos ORDER BY id DESC LIMIT 50")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            self.send_json(200, {"codigos": rows})

        else:
            self.send_json(404, {"error": "Endpoint não encontrado"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Leitura do body JSON
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(post_body)
        except Exception:
            body = {}

        # 1. CHECKOUT PÚBLICO: Criar Cobrança PIX
        if path == "/api/checkout":
            nome = body.get("nome", "").strip() or "Cliente Uber Moto"
            contato = body.get("contato", "").strip()
            cpf = body.get("cpf", "").strip()
            valor = float(get_config("preco_cupom", "20.00"))

            # Verifica se há estoque
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM codigos WHERE status = 'disponivel'")
            estoque = cur.fetchone()[0]

            if estoque == 0:
                conn.close()
                self.send_json(400, {"error": "Estoque esgotado no momento. Tente novamente em alguns minutos."})
                return

            order_id = f"UBR-{int(time.time())}-{secrets.token_hex(2).upper()}"
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")

            mode = get_config("bspay_mode", "teste")
            client_id = get_config("bspay_client_id")
            client_secret = get_config("bspay_client_secret")
            webhook_url = get_config("bspay_webhook_url")

            pix_copia_cola = ""
            pix_qrcode_base64 = ""
            bspay_txid = ""

            # MODO PRODUÇÃO BSPAY
            if mode == "producao" and client_id and client_secret:
                res, err = bspay_create_pix_charge(
                    client_id, client_secret, order_id, valor, nome, cpf, webhook_url
                )
                if res:
                    bspay_txid = str(res.get("transactionId") or res.get("transaction_id") or res.get("id") or "")
                    qr_raw = res.get("qrcode") or res.get("emv") or res.get("qrcode_text") or ""
                    if qr_raw.startswith("000201"):
                        pix_copia_cola = qr_raw
                        pix_qrcode_base64 = ""
                    else:
                        pix_qrcode_base64 = qr_raw
                        pix_copia_cola = res.get("emv") or res.get("qrcode_text") or qr_raw
                else:
                    conn.close()
                    self.send_json(500, {"error": f"Erro ao gerar PIX na BsPay: {err}"})
                    return
            else:
                # MODO SIMULAÇÃO / TESTE (Gera payload de teste)
                bspay_txid = f"TEST_TX_{secrets.token_hex(6)}"
                pix_copia_cola = f"00020126580014br.gov.bcb.pix0136test-bspay-{order_id}5204000053039865405{valor:.2f}5802BR5913UBER CUPOM6009SAO PAULO62070503***6304ABCD"
                pix_qrcode_base64 = ""

            cur.execute("""
            INSERT INTO pedidos (
                order_id, cliente_nome, cliente_contato, cliente_cpf, valor,
                status, bspay_txid, pix_copia_cola, pix_qrcode_base64, criado_em
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """, (order_id, nome, contato, cpf, valor, bspay_txid, pix_copia_cola, pix_qrcode_base64, now_str))

            conn.commit()
            conn.close()

            self.send_json(200, {
                "success": True,
                "order_id": order_id,
                "valor": valor,
                "pix_copia_cola": pix_copia_cola,
                "pix_qrcode_base64": pix_qrcode_base64,
                "is_teste": mode == "teste"
            })

        # 2. SIMULAR PAGAMENTO (Ambiente de Teste para o usuário testar a entrega na hora)
        elif path.startswith("/api/pedido/") and path.endswith("/simular_pagamento"):
            order_id = path.split("/")[3]
            code, err = fulfill_order(order_id)
            if code:
                self.send_json(200, {"success": True, "codigo": code, "order_id": order_id})
            else:
                self.send_json(400, {"error": err or "Erro ao aprovar pedido"})

        # 3. WEBHOOK DA BSPAY (Postback de Confirmação Automática)
        elif path == "/api/webhook/bspay":
            req_body = body.get("requestBody", {}) if isinstance(body.get("requestBody"), dict) else body
            external_id = req_body.get("external_id") or req_body.get("transaction_id") or body.get("external_id")
            tx_id = str(req_body.get("transactionId") or req_body.get("id") or body.get("transactionId") or "")
            status = str(req_body.get("status") or body.get("status") or "").upper()

            if status in ["PAID", "COMPLETED", "SETTLED", "APPROVED", "CONFIRMED"]:
                conn = get_db()
                cur = conn.cursor()
                row = None
                if external_id:
                    cur.execute("SELECT order_id FROM pedidos WHERE order_id = ?", (external_id,))
                    row = cur.fetchone()
                if not row and tx_id:
                    cur.execute("SELECT order_id FROM pedidos WHERE bspay_txid = ?", (tx_id,))
                    row = cur.fetchone()
                conn.close()

                if row:
                    fulfill_order(row["order_id"])
                    self.send_json(200, {"status": "ok", "delivered": True})
                    return

            self.send_json(200, {"status": "ignored"})

        # 4. LOGIN ADMIN
        elif path == "/api/admin/login":
            senha = body.get("password", "")
            admin_pwd = get_config("admin_password", "Nyck@1345")
            if senha == admin_pwd:
                token = secrets.token_hex(24)
                admin_sessions.add(token)
                self.send_json(200, {"success": True, "token": token})
            else:
                self.send_json(401, {"error": "Senha incorreta"})

        # 5. SALVAR CONFIGURAÇÕES ADMIN
        elif path == "/api/admin/config":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            for key in ["bspay_client_id", "bspay_client_secret", "bspay_mode", "preco_cupom", "titulo_oferta", "whatsapp_suporte", "bspay_webhook_url"]:
                if key in body:
                    set_config(key, body[key])

            if "admin_password" in body and body["admin_password"].strip():
                set_config("admin_password", body["admin_password"].strip())

            self.send_json(200, {"success": True, "message": "Configurações atualizadas!"})

        # 6. TESTAR CONEXÃO BSPAY
        elif path == "/api/admin/test_bspay":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            c_id = body.get("client_id") or get_config("bspay_client_id")
            c_sec = body.get("client_secret") or get_config("bspay_client_secret")

            if not c_id or not c_sec:
                self.send_json(400, {"error": "Preencha o Client ID e Client Secret da BsPay."})
                return

            token, err = bspay_get_oauth_token(c_id, c_sec)
            if token:
                self.send_json(200, {"success": True, "message": "✅ Conexão com BsPay bem-sucedida! Token OAuth 2.0 gerado com sucesso."})
            else:
                self.send_json(400, {"error": f"❌ Falha ao conectar na BsPay: {err}"})

        # 7. IMPORTAR CÓDIGOS DO EXTRATOR
        elif path == "/api/admin/codigos/importar":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            imported = sync_codes_from_file()
            self.send_json(200, {"success": True, "imported": imported})

        # 8. ADICIONAR CÓDIGOS MANUALMENTE EM LOTE
        elif path == "/api/admin/codigos/adicionar":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            texto = body.get("codigos_texto", "")
            lines = [l.strip() for l in texto.splitlines() if l.strip()]
            conn = get_db()
            cur = conn.cursor()
            added = 0
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")

            for c in lines:
                code_clean = c.split()[0].strip("(),;\"'")
                if len(code_clean) >= 4 and code_clean.replace("-", "").replace("_", "").isalnum():
                    try:
                        cur.execute(
                            "INSERT OR IGNORE INTO codigos (codigo, status, data_adicionado) VALUES (?, 'disponivel', ?)",
                            (code_clean, now_str)
                        )
                        if cur.rowcount > 0:
                            added += 1
                    except Exception:
                        pass
            conn.commit()
            conn.close()

            self.send_json(200, {"success": True, "added": added})

        # 9. APROVAR PEDIDO MANUALMENTE PELO ADMIN
        elif path.startswith("/api/admin/pedidos/") and path.endswith("/aprovar"):
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            order_id = path.split("/")[4]
            code, err = fulfill_order(order_id)
            if code:
                self.send_json(200, {"success": True, "codigo": code})
            else:
                self.send_json(400, {"error": err or "Erro ao aprovar pedido"})

        # 10. LIMPAR ESTOQUE DISPONÍVEL MANUALMENTE
        elif path == "/api/admin/codigos/limpar":
            if not self.is_admin_authenticated():
                self.send_json(401, {"error": "Não autorizado"})
                return

            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM codigos WHERE status = 'disponivel'")
            deleted = cur.rowcount
            conn.commit()
            conn.close()

            self.send_json(200, {"success": True, "deleted": deleted})

        else:
            self.send_json(404, {"error": "Endpoint não encontrado"})

def is_port_in_use(port):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False

def run_loja_server():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    init_db()
    
    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
    else:
        port = 5000
        if is_port_in_use(port):
            # Se a porta 5000 estiver ocupada (ex: por outro projeto), usa 5050
            port = 5050

    server_address = ("0.0.0.0", port)
    try:
        httpd = ThreadingHTTPServer(server_address, LojaHandler)
    except OSError:
        port = 5050
        server_address = ("0.0.0.0", port)
        httpd = ThreadingHTTPServer(server_address, LojaHandler)

    print("=================================================================")
    print("   🛒 LOJA DE CUPONS UBER MOTO - BSPAY & ADMIN INTEGRADO")
    print("=================================================================")
    print(f"[*] Site da Loja:   http://localhost:{port}")
    print(f"[*] Painel Admin:   http://localhost:{port}/admin (Senha: Nyck@1345)")
    print("=================================================================")

    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass

    while True:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nLoja encerrada.")
            break
        except Exception as e:
            print(f"Aviso no servidor: {e}. Continuando...")
            time.sleep(0.5)

if __name__ == "__main__":
    run_loja_server()
