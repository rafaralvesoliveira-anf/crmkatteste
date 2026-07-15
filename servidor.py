# -*- coding: utf-8 -*-
"""
Painel KAT — servidor para HOSPEDAGEM (Render, Railway, etc.), com IA nativa.

Experiência de quem visualiza: abre o link e usa. NÃO digita senha nenhuma.

Como a chave fica protegida sem pedir senha:
  - A chave da Anthropic fica SOMENTE na variável de ambiente do host
    (ANTHROPIC_API_KEY) — nunca vai para o navegador nem para o repositório.
  - Acesso por TOKEN no link (opcional, recomendado): você compartilha
    https://SEU-APP/?t=SEU_TOKEN — a pessoa só clica; o servidor grava um cookie
    e ela nunca mais vê token nem senha. Sem o token, mostra "acesso restrito".
    Se você não definir ACCESS_TOKEN, o painel fica totalmente aberto.
  - Limite de requisições por IP (RATE_MAX/min) para evitar abuso.
  - (Fora daqui) coloque um LIMITE DE GASTO na chave, no console da Anthropic.

Variáveis de ambiente no host:
  ANTHROPIC_API_KEY  -> sua chave (nova, rotacionada)      [obrigatória p/ IA]
  ACCESS_TOKEN       -> um segredo p/ o link (ex.: kat-demo-9x2)  [opcional]
  MODELO             -> modelo da IA (padrão claude-haiku-4-5, mais barato;
                        pode trocar p/ claude-sonnet-5 ou claude-opus-4-8)  [opcional]
  RATE_MAX           -> requisições de IA por minuto por IP (padrão 30)
  PORT               -> definido automaticamente pelo host
"""
import base64, json, os, time, threading, collections
from urllib.parse import urlparse, parse_qs
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
CANDIDATOS_BASE = [
    os.path.join(AQUI, "crm_kat_base_ficticia.xlsx"),
    os.path.join(os.path.dirname(AQUI), "dados", "crm_kat_base_ficticia.xlsx"),
]
BASE = next((p for p in CANDIDATOS_BASE if os.path.exists(p)), CANDIDATOS_BASE[0])
PORT = int(os.environ.get("PORT", os.environ.get("PORTA", "7799")))
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
RATE_MAX = int(os.environ.get("RATE_MAX", "30"))
# Modelo padrão: Haiku (mais barato). Trocável pela variável de ambiente MODELO
# (ex.: claude-sonnet-5 ou claude-opus-4-8) sem mexer no código.
MODELO = os.environ.get("MODELO", "claude-haiku-4-5")

try:
    import anthropic
except Exception:
    anthropic = None

# ---------------------------------------------------------------------------
# Contexto por visão (a partir da base fictícia)
# ---------------------------------------------------------------------------
xl = pd.read_excel(BASE, sheet_name=None)
cli = xl["clientes"]; ass = xl["assessores"]; reun = xl["reunioes"]

sp = ass[(ass["unidade"] == "São Paulo") & (ass["cargo"].str.contains("Assessor"))]
cont = cli.groupby("assessor_id").size()
sp = sp.assign(n=sp["assessor_id"].map(cont).fillna(0)).sort_values("n")
ASSESSOR_ID = sp.iloc[len(sp)//2]["assessor_id"]
ASSESSOR_NOME = sp.iloc[len(sp)//2]["assessor"]
UNIDADE = "São Paulo"
ult_reuniao = reun.groupby("cliente_id")["data"].max()

def brl(v):
    v = float(v)
    if v >= 1e6: return f"R$ {v/1e6:.1f} mi"
    if v >= 1e3: return f"R$ {v/1e3:.0f} mil"
    return f"R$ {v:.0f}"

def contexto_carteira(ids, limite=40):
    c = cli[cli["cliente_id"].isin(ids)].sort_values("patrimonio_investimentos_safra", ascending=False)
    out = []
    for _, r in c.head(limite).iterrows():
        ur = ult_reuniao.get(r["cliente_id"])
        ur = pd.to_datetime(ur).strftime("%d/%m/%Y") if pd.notna(ur) else "sem registro"
        out.append(f'- {r["nome"]} ({r["tipo"]}, {r["perfil_investidor"]}) | AUC {brl(r["patrimonio_investimentos_safra"])} | '
                   f'ecossistema {r["pct_adesao_ecossistema"]}% ({"apto" if r["ecossistema_apto"]=="Sim" else "não apto"}) | '
                   f'{r["num_classes_investimentos"]}/6 classes | AFI {r["tem_afi"]} | última reunião {ur} | '
                   f'receita 12m {brl(r["receita_total_12m"])}')
    return "\n".join(out)

def resumo(ids, titulo):
    c = cli[cli["cliente_id"].isin(ids)]
    return (f"{titulo}\nClientes: {len(c)} | PL/AUC total: {brl(c['patrimonio_investimentos_safra'].sum())} | "
        f"ecossistema médio: {c['pct_adesao_ecossistema'].mean():.0f}% | aptos: {(c['ecossistema_apto']=='Sim').sum()}\n"
        f"Receita 12m por segmento — Investimentos {brl(c['receita_investimentos_12m'].sum())}, "
        f"Seguros {brl(c['receita_seguros_12m'].sum())}, Consórcio {brl(c['receita_consorcio_12m'].sum())}, "
        f"Câmbio {brl(c['receita_cambio_12m'].sum())}, Crédito {brl(c['receita_credito_12m'].sum())}\n")

ids_ass = set(cli[cli["assessor_id"] == ASSESSOR_ID]["cliente_id"])
ids_uni = set(cli[cli["unidade"] == UNIDADE]["cliente_id"])
ids_all = set(cli["cliente_id"])

def por_assessor(ids):
    g = cli[cli["cliente_id"].isin(ids)].groupby("assessor").agg(
        clientes=("cliente_id","count"), AUC=("patrimonio_investimentos_safra","sum"),
        receita=("receita_total_12m","sum"), aptos=("ecossistema_apto", lambda s:(s=="Sim").sum()))
    return "\n".join(f'- {a}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}, aptos {int(r.aptos)}'
                     for a, r in g.sort_values("receita", ascending=False).iterrows())

def por_unidade():
    g = cli.groupby("unidade").agg(clientes=("cliente_id","count"), AUC=("patrimonio_investimentos_safra","sum"),
        receita=("receita_total_12m","sum"), eco=("pct_adesao_ecossistema","mean"),
        aptos=("ecossistema_apto", lambda s:(s=="Sim").sum()))
    return "\n".join(f'- {u}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}, '
                     f'ecossistema {r.eco:.0f}%, aptos {int(r.aptos)}'
                     for u, r in g.sort_values("receita", ascending=False).iterrows())

CONTEXTO = {
    "assessor": resumo(ids_ass, f"VISÃO ASSESSOR — {ASSESSOR_NOME} (Unidade {UNIDADE})") + "\nCLIENTES DA CARTEIRA:\n" + contexto_carteira(ids_ass, 40),
    "lider": resumo(ids_uni, f"VISÃO TEAM LEADER — Unidade {UNIDADE}") + "\nPOR ASSESSOR:\n" + por_assessor(ids_uni) + "\n\nMAIORES CLIENTES:\n" + contexto_carteira(ids_uni, 20),
    "gestao": resumo(ids_all, "VISÃO GESTÃO — KAT (5 unidades, 20 assessores)") + "\nPOR UNIDADE:\n" + por_unidade(),
}
SYSTEM = ("Você é o assistente de IA do CRM da KAT Investimentos (escritório de assessoria de investimentos). "
    "Responda em português (BR), objetivo e útil. Use SOMENTE os dados fornecidos (base fictícia de teste); "
    "se algo não estiver nos dados, diga que não há na base. Pode fazer contas, rankings e recomendações táticas. "
    "Seja conciso.\n\n=== DADOS DA VISÃO ATUAL ===\n")

def responder(scope, question, history):
    if anthropic is None:
        return {"error": "Biblioteca 'anthropic' não instalada no servidor."}
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return {"error": "Servidor sem ANTHROPIC_API_KEY configurada."}
    if not str(question).strip():
        return {"error": "Pergunta vazia."}
    ctx = CONTEXTO.get(scope, CONTEXTO["assessor"])
    msgs = [{"role": h["role"], "content": str(h["content"])[:4000]}
            for h in (history or [])[-8:] if h.get("role") in ("user","assistant") and h.get("content")]
    msgs.append({"role": "user", "content": str(question)[:4000]})
    try:
        resp = anthropic.Anthropic().messages.create(model=MODELO, max_tokens=1200, system=SYSTEM + ctx, messages=msgs)
        txt = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return {"answer": txt.strip() or "(sem resposta)"}
    except Exception as e:
        return {"error": f"Erro ao chamar a IA: {e}"}

# --- limite de requisições por IP (proteção de custo, invisível p/ o usuário) ---
_hits = collections.defaultdict(list)
_lock = threading.Lock()
def rate_ok(ip):
    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.pop(0)
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True

PAGINA_NEGADO = ("<!doctype html><meta charset=utf-8><title>Acesso restrito</title>"
    "<div style='font-family:system-ui;max-width:460px;margin:16vh auto;text-align:center;color:#1f4a43'>"
    "<div style='font-size:26px;font-weight:800;letter-spacing:3px'>KAT <span style='color:#c4a461'>INVESTIMENTOS</span></div>"
    "<p style='margin-top:18px;color:#5b6b67'>Acesso restrito. Use o link de convite que você recebeu "
    "(ele contém o código de acesso).</p></div>")

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=AQUI, **k)

    def _ip(self):
        return (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def _acesso(self):
        """'ok' = liberado; 'set' = token válido no link (gravar cookie); 'no' = negado."""
        if not ACCESS_TOKEN:
            return "ok"
        if f"kat_sess={ACCESS_TOKEN}" in self.headers.get("Cookie", ""):
            return "ok"
        if parse_qs(urlparse(self.path).query).get("t", [""])[0] == ACCESS_TOKEN:
            return "set"
        return "no"

    def _negar_html(self):
        body = PAGINA_NEGADO.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        acc = self._acesso()
        if acc == "no":
            return self._negar_html()
        if acc == "set":
            # grava cookie e redireciona para a URL limpa (some o token da barra)
            self.send_response(302)
            self.send_header("Set-Cookie", f"kat_sess={ACCESS_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path in ("/", ""):
            self.path = "/painel_kat.html"
        return super().do_GET()

    def do_POST(self):
        if self._acesso() == "no":
            return self._json({"error": "Acesso não autorizado."}, 403)
        if self.path.split("?")[0] == "/api/chat":
            if not rate_ok(self._ip()):
                return self._json({"error": "Muitas perguntas em pouco tempo. Aguarde alguns segundos."}, 429)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            return self._json(responder(data.get("scope", "assessor"), data.get("question", ""), data.get("history")))
        self.send_error(404, "Not found")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    tem_chave = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    print(f"Painel KAT em http://{HOST}:{PORT} | acesso: {'token no link' if ACCESS_TOKEN else 'ABERTO'} "
          f"| limite IA: {RATE_MAX}/min por IP | IA: {'ativa' if tem_chave else 'sem chave'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
